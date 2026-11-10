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

        # 调试统计
        self._segments = 0
        self._dropped = 0
        self._last_duration_ms = 0
        self._fail_streak = 0
        self._stale_dropped = 0
        self._last_stale_log = 0.0

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
            "stale": self._stale_dropped,
        }

    @property
    def paused(self) -> bool:
        """时段外挂起中（未拉流）。"""
        return self._paused

    def restart(self):
        """立即中断当前拉流，用新配置重连。"""
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
        """后台串行识别，不阻塞音频读取。

        队列项带入队时刻：识别服务慢时，排队超过阈值的片段直接丢弃。
        否则单 worker 会把几十秒前的语音一段段慢慢补识别，队列越堆越深，
        前端拿到的全是过期文字，且每段都要白等一次超时。
        """
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
                if max_age > 0 and age > max_age:
                    self._stale_dropped += 1
                    now = time.monotonic()
                    if now - self._last_stale_log >= 10:
                        self._last_stale_log = now
                        logger.warning(
                            "识别服务跟不上：丢弃 %.0f 秒前的语音段（累计 %d 段，"
                            "队列仍有 %d 段）。请检查 ASR 服务端耗时，"
                            "或调大「队列最长等待」",
                            age, self._stale_dropped, self._queue.qsize(),
                        )
                    continue
                await self.on_speech_segment(segment)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("识别任务异常: %s", e)
            finally:
                self._queue.task_done()

    def _enqueue(self, segment: bytes):
        """入队；队列满时丢弃最旧的任务，保证实时性。"""
        if self._queue.full():
            try:
                self._queue.get_nowait()
                self._queue.task_done()
                logger.warning("识别队列已满，丢弃最旧的一段")
            except asyncio.QueueEmpty:
                pass
        self._queue.put_nowait((time.monotonic(), segment))

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
        timeout_sec = max(5, int(self.cfg.get("ffmpeg_timeout", 20)))
        cmd = [
            settings.ffmpeg_path,
            "-hide_banner", "-nostats", "-loglevel", "warning",
            "-rtsp_transport", "tcp",
            # 网络半开时不要无限等；配合下面的无数据看门狗双保险
            "-rw_timeout", str(timeout_sec * 1_000_000),
            # 部分摄像头 RTP 时间戳不前进/跳变，用墙钟时间戳 + genpts 保证单调，
            # 否则 s16le muxer 每包刷 "non monotonically increasing dts" 告警
            "-use_wallclock_as_timestamps", "1",
            "-fflags", "+genpts",
            "-i", url,
            "-vn",
        ]
        if highpass > 0:
            # 风声/风扇/电源嗡鸣等低频噪声常把 VAD 误触发，高通滤波成本极低
            cmd += ["-af", f"highpass=f={highpass}"]
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
                    speech_buffer += frame
                    speech_frames += 1
                    silence_count = 0
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
                            logger.info("语音段结束: %.1f 秒", duration_ms / 1000)
                            self._enqueue(speech_buffer)
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
