"""FFmpeg 拉流 + VAD 静音检测，把连续音频切成一句话一句话的片段。

VAD 参数从运行时配置读取，Web 上修改后立即生效。
判定条件：webrtcvad 判语音 且 音量高于能量门限。
"""
import asyncio
import logging
import math
from array import array
from typing import Callable, Optional

import webrtcvad

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
    ):
        self.cfg = cfg
        self.on_speech_segment = on_speech_segment
        self.on_vad_state = on_vad_state
        self.on_audio_frame = on_audio_frame
        self.sample_rate = 16000
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._running = False
        self._restart = asyncio.Event()
        self._vad: Optional[webrtcvad.Vad] = None
        self._vad_mode: Optional[int] = None

        # 识别任务队列：避免识别阻塞音频读取
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=20)
        self._worker: Optional[asyncio.Task] = None

        # 调试统计
        self._segments = 0
        self._dropped = 0
        self._last_duration_ms = 0

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
        }

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
        """后台串行识别，不阻塞音频读取。"""
        while True:
            segment = await self._queue.get()
            try:
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
        self._queue.put_nowait(segment)

    async def start(self):
        """启动拉流循环。"""
        self._running = True
        self._worker = asyncio.create_task(self._consume())
        while self._running:
            try:
                await self._run_once()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("拉流异常: %s", e)

            if not self._running:
                break

            try:
                await asyncio.wait_for(self._restart.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
            self._restart.clear()

    _FFMPEG_ERROR_HINTS = (
        "error", "failed", "invalid", "unauthorized", "401", "403", "404",
        "refused", "timed out", "no route", "could not", "denied",
    )

    async def _drain_stderr(self, proc):
        """把 ffmpeg 的关键错误转进应用日志，方便在网页日志页排查拉流问题。"""
        if proc.stderr is None:
            return
        try:
            while True:
                raw = await proc.stderr.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", "replace").strip()
                if line and any(h in line.lower() for h in self._FFMPEG_ERROR_HINTS):
                    logger.warning("ffmpeg: %s", line)
        except asyncio.CancelledError:
            pass

    async def _run_once(self):
        url = self.cfg.get("rtsp_url")
        cmd = [
            "ffmpeg",
            "-rtsp_transport", "tcp",
            "-i", url,
            "-vn",
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

        loop = asyncio.get_event_loop()
        last_emit = 0.0
        last_speech = None

        while self._running:
            frame_ms = int(self.cfg.get("vad_frame_ms", 30))
            frame_bytes = self.sample_rate * frame_ms // 1000 * 2
            silence_limit = int(self.cfg.get("vad_silence_frames", 10))
            min_speech = int(self.cfg.get("vad_min_speech_frames", 3))
            rms_limit = float(self.cfg.get("vad_rms_dbfs", -45))

            data = await proc.stdout.read(frame_bytes)
            if not data:
                logger.warning("ffmpeg 输出结束")
                break

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
                        speech_buffer = b""
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
                        duration_ms = int(
                            len(speech_buffer) / (self.sample_rate * 2) * 1000
                        )
                        if speech_frames >= min_speech:
                            self._segments += 1
                            self._last_duration_ms = duration_ms
                            logger.info("语音段结束: %.1f 秒", duration_ms / 1000)
                            self._enqueue(speech_buffer)
                        else:
                            self._dropped += 1
                            logger.debug("丢弃过短语音段: %d ms", duration_ms)
                        in_speech = False
                        speech_buffer = b""
                        silence_count = 0
                        speech_frames = 0

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
