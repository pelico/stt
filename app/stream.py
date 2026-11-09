"""FFmpeg 拉流 + VAD 静音检测，把连续音频切成一句话一句话的片段。

VAD 参数从运行时配置读取，Web 上修改后立即生效。
"""
import asyncio
import logging
from typing import Callable, Optional

import webrtcvad

logger = logging.getLogger(__name__)

# 单段语音最长时长，防止一直说话导致内存无限增长
MAX_SEGMENT_SEC = 30


class AudioStreamer:
    """从 RTSP 拉取音频，VAD 切分后回调每段语音。"""

    def __init__(self, on_speech_segment: Callable[[bytes], object], cfg):
        self.cfg = cfg
        self.on_speech_segment = on_speech_segment
        self.sample_rate = 16000
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._running = False
        self._restart = asyncio.Event()
        self._vad: Optional[webrtcvad.Vad] = None
        self._vad_mode: Optional[int] = None

    def _get_vad(self) -> webrtcvad.Vad:
        mode = self.cfg.get("vad_mode", 3)
        if self._vad is None or mode != self._vad_mode:
            self._vad = webrtcvad.Vad(mode)
            self._vad_mode = mode
        return self._vad

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

            # 正常结束等待 5 秒重连；配置变更则立即重连
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

        while self._running:
            frame_ms = int(self.cfg.get("vad_frame_ms", 30))
            frame_bytes = self.sample_rate * frame_ms // 1000 * 2
            silence_limit = int(self.cfg.get("vad_silence_frames", 10))
            min_speech = int(self.cfg.get("vad_min_speech_frames", 3))

            data = await proc.stdout.read(frame_bytes)
            if not data:
                logger.warning("ffmpeg 输出结束")
                break

            buffer += data
            while len(buffer) >= frame_bytes:
                frame = buffer[:frame_bytes]
                buffer = buffer[frame_bytes:]

                try:
                    is_speech = self._get_vad().is_speech(frame, self.sample_rate)
                except Exception:
                    is_speech = False

                if is_speech:
                    if not in_speech:
                        in_speech = True
                        speech_frames = 0
                        speech_buffer = b""
                    speech_buffer += frame
                    speech_frames += 1
                    silence_count = 0
                elif in_speech:
                    # 静音但仍在同一句里，保留尾部
                    silence_count += 1
                    speech_buffer += frame

                    # 超过最大时长强制切段
                    too_long = (
                        len(speech_buffer)
                        > MAX_SEGMENT_SEC * self.sample_rate * 2
                    )
                    if silence_count >= silence_limit or too_long:
                        if speech_frames >= min_speech:
                            logger.info(
                                "语音段结束: %.1f 秒",
                                len(speech_buffer) / (self.sample_rate * 2),
                            )
                            await self.on_speech_segment(speech_buffer)
                        in_speech = False
                        speech_buffer = b""
                        silence_count = 0
                        speech_frames = 0

        try:
            await proc.wait()
        except Exception:
            pass
        if self._proc is proc:
            self._proc = None
