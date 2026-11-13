"""FFmpeg 拉流 + VAD 静音检测，把连续音频切成一句话一句话的片段。

VAD 参数从运行时配置读取，Web 上修改后立即生效。
判定条件：webrtcvad 判语音 且 音量高于能量门限。
"""
import asyncio
import logging
import math
import re
import time
from array import array
from typing import Callable, Optional

import webrtcvad

from app.config import settings
from app.diagnostics import metrics
from app.pending_store import PendingStore, utc_iso_naive

try:  # Python <= 3.12 自带；3.13+ 已移除，用纯 Python 兜底
    import audioop
except ImportError:  # pragma: no cover
    audioop = None

logger = logging.getLogger(__name__)

# 单段语音最长时长，防止一直说话导致内存无限增长
MAX_SEGMENT_SEC = 30

# VAD 状态推送节流间隔（秒）
STATE_INTERVAL = 0.2


def frame_dbfs(frame: bytes) -> float:
    """计算一帧 PCM(s16le) 的音量，单位 dBFS。"""
    if audioop is not None:
        rms = audioop.rms(frame, 2)
    else:
        samples = array("h")
        samples.frombytes(frame)
        if not samples:
            return -100.0
        rms = int(math.sqrt(sum(s * s for s in samples) / len(samples)))
    if rms <= 0:
        return -100.0
    return 20 * math.log10(rms / 32768)


class AudioStreamer:
    """从 RTSP 拉取音频，VAD 切分后回调每段语音。"""

    def __init__(
        self,
        on_speech_segment: Callable[[bytes], object],
        cfg,
        on_vad_state: Optional[Callable[[dict], object]] = None,
        on_audio_frame: Optional[Callable[[bytes], object]] = None,
        should_stream: Optional[Callable[[], bool]] = None,
        on_stream_state: Optional[Callable[[bool], object]] = None,
        on_rejected_segment: Optional[Callable[[bytes, str], object]] = None,
    ):
        self.cfg = cfg
        self.on_speech_segment = on_speech_segment
        self.on_vad_state = on_vad_state
        self.on_audio_frame = on_audio_frame
        self.should_stream = should_stream
        self.on_stream_state = on_stream_state
        self.on_rejected_segment = on_rejected_segment
        self.sample_rate = 16000
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._running = False
        self._paused = False
        self._restart = asyncio.Event()
        self._vad: Optional[webrtcvad.Vad] = None
        self._vad_mode: Optional[int] = None

        # 识别任务队列：避免识别阻塞音频读取。元素为 (入队时刻, PCM)，
        # 入队时刻用于丢弃排队过久的片段（ASR 跟不上时不补识别历史语音）。
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=20)
        self._worker: Optional[asyncio.Task] = None

        # 「完整优先」模式：磁盘 FIFO 队列。ASR 慢时片段落盘、后台按采集
        # 顺序补识别，不按等待时间丢弃；容器重启后未处理片段仍在。
        self._mode = str(self.cfg.get("queue_mode", settings.queue_mode))
        if self._mode not in ("durable", "realtime"):
            self._mode = "durable"
        self._store: Optional[PendingStore] = (
            PendingStore() if self._mode == "durable" else None
        )
        self._wakeup = asyncio.Event()
        init_count, init_bytes, init_lag = (
            self._store.stats() if self._store else (0, 0, 0.0)
        )
        self._backlog = init_count
        self._backlog_bytes = init_bytes
        self._lag_sec = init_lag
        self._last_vacuum = 0.0

        # 调试统计
        self._segments = 0
        self._dropped = 0
        self._last_duration_ms = 0
        self._fail_streak = 0
        self._stale_dropped = 0
        self._last_stale_log = 0.0

        # 累计队列计数：入队 / 完成识别 / 队列级丢弃（重试耗尽、落盘失败）。
        # 随 VAD 状态推送与 /api/health 暴露，首页可直观看到「切了多少段、
        # 真正识别了多少、丢了多少」，便于对照音频判断 VAD 参数是否合适。
        self._enqueued_total = 0
        self._done_total = 0
        self._fail_total = 0

        # ffmpeg 参数兼容性降级：老版本 ffmpeg（嵌入式/精简构建）可能不认识
        # -rw_timeout / 墙钟时间戳选项，报 "Option not found" 连输入文件都
        # 打不开。首次报错后自动去掉对应参数并快速重连，只损失该参数的功能。
        self._rw_timeout_ok = True
        self._wallclock_ok = True
        # 轻度降噪滤镜（afftdn）：老版本 ffmpeg 没有该滤镜时自动关闭
        self._afftdn_ok = True
        self._fallback_changed = False
        # 每次拉流最多降级一个选项：同一次 stderr 里连续多行
        # "Option not found" 不应把还没试过的选项也去掉
        self._option_fix_applied = False

    def _get_vad(self) -> webrtcvad.Vad:
        mode = self.cfg.get("vad_mode", 3)
        if self._vad is None or mode != self._vad_mode:
            self._vad = webrtcvad.Vad(mode)
            self._vad_mode = mode
        return self._vad

    def _state(self, level: float, is_speech: bool, in_speech: bool) -> dict:
        return {
            "type": "vad",
            "level": round(level, 1),
            "speech": is_speech,
            "active": in_speech,
            "segments": self._segments,
            "dropped": self._dropped,
            "last_duration_ms": self._last_duration_ms,
            "queued": self._queue.qsize(),
            "backlog": self._backlog,
            "backlog_bytes": self._backlog_bytes,
            "lag_sec": round(self._lag_sec),
            "mode": self._mode,
            "stale": self._stale_dropped,
            "enq": self._enqueued_total,
            "deq": self._done_total,
            "qfail": self._fail_total,
        }

    @property
    def paused(self) -> bool:
        """时段外挂起中（未拉流）。"""
        return self._paused

    @property
    def mode(self) -> str:
        """当前识别队列模式：durable / realtime（启动时确定）。"""
        return self._mode

    @property
    def backlog(self) -> int:
        """待识别积压条数。"""
        return self._backlog

    @property
    def backlog_bytes(self) -> int:
        """待识别音频总字节数。"""
        return self._backlog_bytes

    @property
    def lag_sec(self) -> float:
        """处理滞后秒数：正在补多久之前采集的话。"""
        return self._lag_sec

    @property
    def queue_totals(self) -> dict:
        """累计队列计数：入队 / 完成识别 / 队列级丢弃。"""
        return {
            "enqueued": self._enqueued_total,
            "done": self._done_total,
            "failed": self._fail_total,
        }

    def restart(self):
        """立即中断当前拉流，用新配置重连。"""
        # 换了新地址重连：旧的连续失败与新地址无关，退避计数归零，
        # 否则换地址后第一次尝试失败就按上次的指数退避等 40~60 秒
        self._fail_streak = 0
        self._restart.set()
        self._kill_proc()

    def _kill_proc(self):
        proc = self._proc
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass

    def stop(self):
        self._running = False
        self._kill_proc()
        if self._worker is not None:
            self._worker.cancel()
            self._worker = None

    async def _consume(self):
        """后台串行识别，不阻塞音频读取。按 queue_mode 选择后端。"""
        if self._mode == "durable":
            await self._consume_durable()
        else:
            await self._consume_realtime()

    async def _consume_durable(self):
        """完整优先：从磁盘 FIFO 取最旧一批，识别成功才出队。

        ASR 慢时表现为「延迟入库」而不是丢数据：说话是间歇的，安静下来后
        后台会把积压按采集顺序补完，结果时间戳仍是说话时刻。
        相邻（间隔 <= asr_merge_gap_sec）的段会合并成一次识别请求：
        每个请求约 2 秒固定开销，合并后摊薄到几段里，吞吐翻 2~3 倍。
        """
        store = self._store
        while True:
            # 手动暂停（总开关关）：积压保留，等恢复后继续补完，不消耗重试次数。
            # 时段外不停：拉流已断、不会再有新段，但已积压的话应继续补转完，
            # 否则夜里切一段听不完的对话要等到第二天早上才有结果。
            if not bool(self.cfg.get("asr_enabled", True)):
                self._backlog, self._backlog_bytes, self._lag_sec = (
                    await asyncio.to_thread(store.stats)
                )
                await asyncio.sleep(2)
                continue
            batch = await self._peek_merge_batch()
            if not batch:
                self._backlog = 0
                self._backlog_bytes = 0
                self._wakeup.clear()
                # clear 后再查一次，避免「入队发生在 clear 之前」导致漏唤醒
                if await asyncio.to_thread(store.count) == 0:
                    # 空闲期顺手回收磁盘：积压消化完 pending.db 不会自动缩小
                    now = time.monotonic()
                    if now - self._last_vacuum > 600:
                        self._last_vacuum = now
                        if await asyncio.to_thread(store.maybe_vacuum):
                            logger.info("[队列] 已清空，回收 pending.db 磁盘空间")
                    await self._wakeup.wait()
                continue
            ids = [row[0] for row in batch]
            pcm = b"".join(row[1] for row in batch)
            wall_iso = batch[0][2]
            attempts = min(row[3] for row in batch)
            queued_sec = max(0.0, time.time() - batch[0][4])
            # 出队信息不再单独打一行，并进 ASR 请求行（两者同一时刻发生）；
            # 单消费者串行，直接放 metrics 上供引擎日志读取
            ctx_parts = [f"等待{queued_sec:.0f}s", f"积压{self._backlog}",
                         f"第{attempts + 1}次"]
            if len(batch) > 1:
                ctx_parts.insert(0, f"合并{len(batch)}段")
            metrics.queue_ctx = " | ".join(ctx_parts)
            try:
                done = await self.on_speech_segment(pcm, wall_iso)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("识别任务异常: %s", e)
                done = False

            if done:
                self._done_total += len(batch)
                await asyncio.to_thread(store.delete_many, ids)
            else:
                try:
                    max_attempts = int(
                        self.cfg.get(
                            "queue_max_attempts", settings.queue_max_attempts
                        )
                    )
                except (TypeError, ValueError):
                    max_attempts = settings.queue_max_attempts
                removed = await asyncio.to_thread(
                    store.bump_many, ids, max_attempts
                )
                self._fail_total += removed
                backoff = min(30, 2 ** min(attempts, 4))
                if removed:
                    logger.warning(
                        "[队列] 第 %d 次识别失败已达上限 %d，丢弃 %d 段以免卡住 FIFO | 积压 %d",
                        attempts + 1, max_attempts, removed, self._backlog,
                    )
                else:
                    logger.warning(
                        "[队列] 第 %d 次识别失败，保留该批（%d 段），%d 秒后重试 | 积压 %d",
                        attempts + 1, len(batch), backoff, self._backlog,
                    )
                # 先刷新积压统计再退避，前端不会看到过期数字
                self._backlog, self._backlog_bytes, self._lag_sec = (
                    await asyncio.to_thread(store.stats)
                )
                # 失败退避，配合 main.py 的熔断，服务端故障期间不空转刷请求
                await asyncio.sleep(backoff)
            self._backlog, self._backlog_bytes, self._lag_sec = (
                await asyncio.to_thread(store.stats)
            )

    async def _peek_merge_batch(self) -> list:
        """取最旧的一批可合并段：相邻间隔 <= merge_gap 且总时长 <= merge_max。

        created_at 是入队时刻（≈段结束时刻），用「下一段开始 − 上一段结束」
        算间隔，比只看两段开始时间差更准。单条超长段会整条返回不合并。
        """
        store = self._store
        try:
            gap_max = float(
                self.cfg.get("asr_merge_gap_sec", settings.asr_merge_gap_sec)
            )
            max_sec = int(
                self.cfg.get("asr_merge_max_sec", settings.asr_merge_max_sec)
            )
        except (TypeError, ValueError):
            gap_max = settings.asr_merge_gap_sec
            max_sec = settings.asr_merge_max_sec
        max_sec = max(1, max_sec)
        max_bytes = max_sec * self.sample_rate * 2
        rows = await asyncio.to_thread(store.peek_batch, 16, max_bytes)
        if not rows:
            return []
        batch = [rows[0]]
        if gap_max <= 0:
            return batch
        prev_end = float(rows[0][4])
        for row in rows[1:]:
            start = float(row[4]) - len(row[1]) / (self.sample_rate * 2)
            if start - prev_end > gap_max:
                break
            batch.append(row)
            prev_end = float(row[4])
        return batch

    async def _consume_realtime(self):
        """实时优先：内存队列，排队超过阈值的片段直接丢弃。"""
        while True:
            created_at, segment = await self._queue.get()
            try:
                try:
                    max_age = int(
                        self.cfg.get(
                            "asr_max_queue_age", settings.asr_max_queue_age
                        )
                    )
                except (TypeError, ValueError):
                    max_age = settings.asr_max_queue_age
                age = time.monotonic() - created_at
                self._lag_sec = max(0.0, age)
                if max_age > 0 and age > max_age:
                    self._stale_dropped += 1
                    self._fail_total += 1
                    now = time.monotonic()
                    if now - self._last_stale_log >= 10:
                        self._last_stale_log = now
                        logger.warning(
                            "实时模式：丢弃 %.0f 秒前的语音段（累计 %d 段，"
                            "队列仍有 %d 段）。要保留全部结果请切到「完整优先」",
                            age, self._stale_dropped, self._queue.qsize(),
                        )
                    continue
                metrics.queue_ctx = ""  # 实时模式无出队上下文
                res = await self.on_speech_segment(segment)
                if res is False:
                    self._fail_total += 1
                else:
                    self._done_total += 1
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("识别任务异常: %s", e)
                self._fail_total += 1
            finally:
                self._queue.task_done()

    async def _enqueue(self, segment: bytes):
        """VAD 切出一段语音后入队。"""
        if self._mode == "durable":
            max_items = int(self.cfg.get("queue_max_items", settings.queue_max_items))
            max_mb = int(self.cfg.get("queue_max_mb", settings.queue_max_mb))
            try:
                backlog, dropped = await asyncio.to_thread(
                    self._store.put, segment, utc_iso_naive(), max_items, max_mb,
                )
            except Exception as e:
                self._fail_total += 1
                logger.error("[队列] 落盘失败，该段已丢失: %s", e)
                return
            self._backlog, self._backlog_bytes, self._lag_sec = (
                await asyncio.to_thread(self._store.stats)
            )
            self._enqueued_total += 1
            logger.info(
                "[队列] 入队 %.1fs/%dKB | 积压 %d",
                len(segment) / (self.sample_rate * 2), len(segment) // 1024,
                backlog,
            )
            if dropped:
                logger.warning(
                    "[队列] 已达上限（%d条/%dMB），丢弃最旧 %d 段 | 当前积压 %d；"
                    "ASR 长期跟不上产出速度，请排查服务端推理耗时",
                    max_items, max_mb, dropped, backlog,
                )
            self._wakeup.set()
            return

        # 实时优先：纯内存有界队列，满则丢最旧
        if self._queue.full():
            try:
                self._queue.get_nowait()
                self._queue.task_done()
                self._fail_total += 1
                logger.warning("识别队列已满，丢弃最旧的一段")
            except asyncio.QueueEmpty:
                pass
        self._queue.put_nowait((time.monotonic(), segment))
        self._enqueued_total += 1

    async def _notify_stream_state(self, paused: bool):
        if self.on_stream_state is None:
            return
        try:
            await self.on_stream_state(paused)
        except Exception:
            pass

    async def _pause_stream(self):
        """时段外挂起：断开 ffmpeg，等待重新进入时段。"""
        if not self._paused:
            self._paused = True
            logger.info("不在拉流时段，已断开 ffmpeg")
            await self._notify_stream_state(True)
        while self._running:
            if self.should_stream is None or self.should_stream():
                break
            await asyncio.sleep(2)
        if self._running:
            self._paused = False
            logger.info("进入拉流时段，恢复拉流")
            await self._notify_stream_state(False)

    async def start(self):
        """启动拉流循环。"""
        self._running = True
        self._worker = asyncio.create_task(self._consume())
        while self._running:
            if self.should_stream is not None and not self.should_stream():
                await self._pause_stream()
                continue
            try:
                healthy = await self._run_once()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("拉流异常: %s", e)
                healthy = False

            if not self._running:
                break

            # 刚因时段结束而断开：立即进入挂起，不等待重连间隔
            if self.should_stream is not None and not self.should_stream():
                continue

            # 连续拉不到数据时指数退避，避免摄像头宕机时每 5 秒刷一次错误
            if healthy:
                self._fail_streak = 0
                delay = 5
            elif self._fallback_changed:
                # 参数兼容性降级后立即重试，别让 60 秒退避拖住恢复
                self._fallback_changed = False
                delay = 2
            else:
                self._fail_streak += 1
                delay = min(60, 5 * (2 ** min(self._fail_streak - 1, 4)))
                logger.warning(
                    "拉流连续失败 %d 次，%d 秒后重连", self._fail_streak, delay
                )
            try:
                await asyncio.wait_for(self._restart.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            self._restart.clear()

    _FFMPEG_ERROR_HINTS = (
        "error", "failed", "invalid", "unauthorized", "401", "403", "404",
        "refused", "timed out", "no route", "could not", "denied",
    )
    # 已知无害告警：摄像头 RTP 时间戳不单调，裸 PCM 输出不依赖时间戳，刷屏无意义
    _FFMPEG_IGNORE_HINTS = (
        "non monotonically increasing dts",
        "application provided invalid, non monotonically",
    )

    def _log_ffmpeg_line(self, line: str):
        line = line.strip()
        if not line:
            return
        low = line.lower()
        # 老版本 ffmpeg 不认识的输入选项会在打开文件时报
        # "Option not found"，按顺序降级：先去 -rw_timeout，再去墙钟时间戳
        if "option not found" in low and not self._option_fix_applied:
            if self._rw_timeout_ok:
                self._rw_timeout_ok = False
                self._fallback_changed = True
                self._option_fix_applied = True
                logger.warning(
                    "当前 ffmpeg 不支持 -rw_timeout（老版本无该选项），"
                    "下次重连自动去掉该参数；半开检测仅依赖无数据看门狗"
                )
            elif self._wallclock_ok:
                self._wallclock_ok = False
                self._fallback_changed = True
                self._option_fix_applied = True
                logger.warning(
                    "当前 ffmpeg 不支持墙钟时间戳选项，下次重连自动去掉；"
                    "如果摄像头时间戳不单调，可能重新出现 dts 告警刷屏"
                )
        # afftdn 滤镜缺失报 "No such filter: 'afftdn'"，直接关降噪快速重连
        if "no such filter" in low and "afftdn" in low and self._afftdn_ok:
            self._afftdn_ok = False
            self._fallback_changed = True
            logger.warning(
                "当前 ffmpeg 无 afftdn 滤镜，轻度降噪自动关闭；"
                "如需降噪请升级容器内 ffmpeg 版本"
            )
        if any(h in low for h in self._FFMPEG_ERROR_HINTS) and not any(
            h in low for h in self._FFMPEG_IGNORE_HINTS
        ):
            logger.warning("ffmpeg: %s", line)

    async def _drain_stderr(self, proc):
        """把 ffmpeg 的关键错误转进应用日志。

        不能用 readline()：ffmpeg 的 -stats 进度用 \\r 刷同一行、不含 \\n，
        readline 会一直累积到 StreamReader 的 64KB 上限并抛
        "Separator is not found, and chunk exceed the limit"，之后若无人再读
        stderr，管道写满会让 ffmpeg 整体阻塞（拉流静默冻死）。
        这里按块读取并自行按 \\r / \\n 切分，且兜底所有异常。
        """
        if proc.stderr is None:
            return
        pending = ""
        try:
            while True:
                chunk = await proc.stderr.read(4096)
                if not chunk:
                    break
                pending += chunk.decode("utf-8", "replace")
                parts = re.split(r"[\r\n]+", pending)
                pending = parts.pop() or ""
                for line in parts:
                    self._log_ffmpeg_line(line)
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.warning("ffmpeg stderr 读取中止: %s", e)
        self._log_ffmpeg_line(pending)

    async def _run_once(self) -> bool:
        """跑一次拉流，返回是否成功收到过音频数据（用于重连退避）。"""
        url = self.cfg.get("rtsp_url")
        highpass = int(self.cfg.get("ffmpeg_highpass", 100))
        denoise = bool(self.cfg.get("ffmpeg_denoise", settings.ffmpeg_denoise))
        timeout_sec = max(5, int(self.cfg.get("ffmpeg_timeout", 20)))
        # 新一次拉流允许再降级一个不支持的选项
        self._option_fix_applied = False
        cmd = [
            settings.ffmpeg_path,
            "-hide_banner", "-nostats", "-loglevel", "warning",
        ]
        # -rtsp_transport 是 RTSP demuxer 专属选项，喂给 HLS/HTTP 流会报
        # "Option not found" 连输入都打不开；只对 rtsp:// 地址传入
        low_url = url.strip().lower()
        is_rtsp = low_url.startswith("rtsp://") or low_url.startswith("rtsps://")
        if is_rtsp:
            cmd += ["-rtsp_transport", "tcp"]
        else:
            # RTSP/直播流数据按实时到达；点播类源（HTTP/HLS 影片、本地文件）
            # 会被 ffmpeg 全速解码，PCM 灌入速度是实时的好几倍——下游按
            # 采样率换算时间，表现为倍速快放、VAD 时间戳错乱。
            # -re 按 PTS 原速读取（官方模拟实时设备的标准参数）
            cmd += ["-re"]
        # 网络半开时不要无限等；配合下面的无数据看门狗双保险。
        # ffmpeg 不支持该选项时由 _log_ffmpeg_line 自动置 False 降级
        if self._rw_timeout_ok:
            cmd += ["-rw_timeout", str(timeout_sec * 1_000_000)]
        if self._wallclock_ok and is_rtsp:
            # 仅 RTSP 需要：部分摄像头 RTP 时间戳不前进/跳变，用墙钟时间戳 +
            # genpts 保证单调，否则 s16le muxer 刷 "non monotonically
            # increasing dts"。HLS/TS 自带正规 PTS，绝不能用 wallclock——
            # 它把时间戳改写成到达墙钟，-re 拿自我参照的时钟节流 = 失效
            cmd += ["-use_wallclock_as_timestamps", "1", "-fflags", "+genpts"]
        cmd += ["-i", url, "-vn"]
        filters = []
        if highpass > 0:
            # 风声/风扇/电源嗡鸣等低频噪声常把 VAD 误触发，高通滤波成本极低
            filters.append(f"highpass=f={highpass}")
        if denoise and self._afftdn_ok:
            # 轻度自适应降噪：压稳态底噪（风扇/电流声），nr=12dB 温和不过头；
            # 对强 BGM 广告帮助有限，但稳态人声的断句与字准会略好
            filters.append("afftdn=nr=12:nf=-40:tn=1")
        if filters:
            cmd += ["-af", ",".join(filters)]
        cmd += [
            "-acodec", "pcm_s16le",
            "-ar", str(self.sample_rate),
            "-ac", "1",
            "-f", "s16le",
            "-",
        ]
        logger.info("启动 ffmpeg: %s", " ".join(cmd))

        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        proc = self._proc
        stderr_task = asyncio.create_task(self._drain_stderr(proc))

        buffer = b""
        speech_buffer = b""
        silence_count = 0
        speech_frames = 0
        in_speech = False
        got_data = False

        # 开讲前的滚动缓冲：VAD 判定滞后 1~2 帧，补进句首才不吞字
        preroll = b""
        preroll_limit = 0
        tail_pad_frames = 0
        # 最近 1.5s 的帧电平：强制切段时在窗口内找最安静的一帧下刀，
        # 避免把一个字切成两半（BGM/持续噪声会让 VAD 永不断句）
        recent_levels = []

        loop = asyncio.get_event_loop()
        last_emit = 0.0
        last_speech = None

        while self._running:
            if self.should_stream is not None and not self.should_stream():
                logger.info("离开拉流时段，断开 ffmpeg")
                break
            frame_ms = int(self.cfg.get("vad_frame_ms", 30))
            frame_bytes = self.sample_rate * frame_ms // 1000 * 2
            silence_limit = int(self.cfg.get("vad_silence_frames", 20))
            min_speech = int(self.cfg.get("vad_min_speech_frames", 10))
            min_ratio = float(self.cfg.get("vad_min_speech_ratio", 0.5))
            rms_limit = float(self.cfg.get("vad_rms_dbfs", -45))
            preroll_limit = max(
                frame_bytes,
                self.sample_rate * 2 * int(self.cfg.get("vad_preroll_ms", 200)) // 1000,
            )
            tail_pad_frames = max(
                0, int(self.cfg.get("vad_tail_pad_ms", 150)) // max(1, frame_ms)
            )

            # RTSP TCP 半开时 read() 可能永不返回，用看门狗强制重连
            try:
                data = await asyncio.wait_for(
                    proc.stdout.read(frame_bytes), timeout=timeout_sec
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "ffmpeg %d 秒无音频数据，判定拉流卡死，准备重连", timeout_sec
                )
                break
            if not data:
                logger.warning("ffmpeg 输出结束")
                break
            got_data = True

            buffer += data
            while len(buffer) >= frame_bytes:
                frame = buffer[:frame_bytes]
                buffer = buffer[frame_bytes:]

                level = frame_dbfs(frame)
                try:
                    vad_speech = self._get_vad().is_speech(frame, self.sample_rate)
                except Exception:
                    vad_speech = False
                # 双重判定：VAD 认为是语音，且音量过门限
                is_speech = vad_speech and level >= rms_limit

                if is_speech:
                    if not in_speech:
                        in_speech = True
                        speech_frames = 0
                        speech_buffer = preroll[-preroll_limit:]
                        recent_levels = []
                    speech_buffer += frame
                    speech_frames += 1
                    silence_count = 0
                    recent_levels.append(level)
                    lookback = max(1, 1500 // max(1, frame_ms))
                    if len(recent_levels) > lookback:
                        recent_levels.pop(0)

                    # 硬上限只在静音分支检查时，BGM/持续噪声会让 VAD 永远
                    # 判「语音」，缓冲无限涨（实测单段 100s+，超过 CF 100s
                    # 回源超时必然 524）。语音分支同样检查，满 30s 强制切段：
                    # 在最近 1.5s 里找最安静的一帧下刀，切点接近自然停顿。
                    forced = (
                        len(speech_buffer)
                        > MAX_SEGMENT_SEC * self.sample_rate * 2
                    )
                    if forced:
                        cut_frames = 0
                        if len(recent_levels) >= 2:
                            quiet_idx = min(
                                range(len(recent_levels)),
                                key=lambda i: recent_levels[i],
                            )
                            cut_frames = len(recent_levels) - 1 - quiet_idx
                        cut_bytes = cut_frames * frame_bytes
                        head = speech_buffer[:-cut_bytes] if cut_bytes else speech_buffer
                        tail = speech_buffer[len(head):]
                        duration_ms = int(len(head) / (self.sample_rate * 2) * 1000)
                        self._segments += 1
                        self._last_duration_ms = duration_ms
                        logger.info(
                            "语音段超过 %ds，强制切段: %.1fs（回退 %.1fs 找静音帧下刀）",
                            MAX_SEGMENT_SEC, duration_ms / 1000,
                            cut_frames * frame_ms / 1000,
                        )
                        await self._enqueue(head)
                        # 剩余部分作为新段继续收音，补 preroll 防吞字头
                        speech_buffer = preroll[-preroll_limit:] + tail
                        speech_frames -= cut_frames
                        silence_count = 0
                        recent_levels = []
                elif in_speech:
                    silence_count += 1
                    speech_buffer += frame

                    too_long = (
                        len(speech_buffer) > MAX_SEGMENT_SEC * self.sample_rate * 2
                    )
                    if silence_count >= silence_limit or too_long:
                        # 整段 600ms 静音尾是 ASR 幻觉高发区，裁到只留少量 padding
                        if silence_count > tail_pad_frames:
                            trim = (silence_count - tail_pad_frames) * frame_bytes
                            if 0 < trim < len(speech_buffer):
                                speech_buffer = speech_buffer[:-trim]
                        duration_ms = int(
                            len(speech_buffer) / (self.sample_rate * 2) * 1000
                        )
                        # 噪声片段往往只有零星几帧被判为语音，其余全是静音尾。
                        # 要求"真语音帧"占比达标，比单纯看帧数更能挡住短促杂音。
                        total_frames = speech_frames + silence_count
                        ratio = speech_frames / max(1, total_frames)
                        if speech_frames >= min_speech and ratio >= min_ratio:
                            self._segments += 1
                            self._last_duration_ms = duration_ms
                            await self._enqueue(speech_buffer)
                        else:
                            self._dropped += 1
                            logger.debug(
                                "丢弃疑似噪声段: %d ms, 语音帧 %d/%d (%.0f%%)",
                                duration_ms, speech_frames, total_frames, ratio * 100,
                            )
                            if self.on_rejected_segment is not None:
                                try:
                                    await self.on_rejected_segment(speech_buffer, "vad")
                                except Exception:
                                    pass
                        in_speech = False
                        speech_buffer = b""
                        silence_count = 0
                        speech_frames = 0
                        preroll = b""

                # 静音态维护滚动前置缓冲；语音态不更新，避免把句中音频塞进下一段
                if not in_speech:
                    preroll = (preroll + frame)[-preroll_limit:]

                # 实时监听：把原始 PCM 帧推给订阅端
                if self.on_audio_frame is not None:
                    try:
                        await self.on_audio_frame(frame)
                    except Exception:
                        pass

                # 状态推送：上次推送超过 200ms 或语音状态变化时
                now = loop.time()
                if self.on_vad_state and (
                    now - last_emit >= STATE_INTERVAL or is_speech != last_speech
                ):
                    last_emit = now
                    last_speech = is_speech
                    try:
                        await self.on_vad_state(
                            self._state(level, is_speech, in_speech)
                        )
                    except Exception:
                        pass

        # 时段结束或主动停止时，循环会提前 break，需要手动结束 ffmpeg
        if proc.returncode is None:
            try:
                proc.kill()
            except Exception:
                pass

        try:
            await asyncio.wait_for(stderr_task, timeout=2)
        except Exception:
            stderr_task.cancel()
        try:
            await proc.wait()
        except Exception:
            pass
        if self._proc is proc:
            self._proc = None
        return got_data
