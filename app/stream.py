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
    ):
        self.cfg = cfg
        self.on_speech_segment = on_speech_segment
        self.on_vad_state = on_vad_state
        self.sample_rate = 16000
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._running = False
        self._restart = asyncio.Event()
        self._vad: Optional[webrtcvad.Vad] = None
        self._vad_mode: Optional[int] = None

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

    async def start(self):
        """启动拉流循环。"""
        self._running = True
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
            stderr=asyncio.subprocess.DEVNULL,
        )
        proc = self._proc

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
                            await self.on_speech_segment(speech_buffer)
                        else:
                            self._dropped += 1
                            logger.debug("丢弃过短语音段: %d ms", duration_ms)
                        in_speech = False
                        speech_buffer = b""
                        silence_count = 0
                        speech_frames = 0

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
            await proc.wait()
        except Exception:
            pass
        if self._proc is proc:
            self._proc = None
