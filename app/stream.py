"""FFmpeg 拉流 + VAD 静音检测，把连续音频切成一句话一句话的片段。"""
import asyncio
import logging
from typing import Callable, Optional

import webrtcvad

from app.config import settings

logger = logging.getLogger(__name__)


class AudioStreamer:
    """从 RTSP 拉取音频，VAD 切分后回调每段语音。"""

    def __init__(self, on_speech_segment: Callable[[bytes], None]):
        self.vad = webrtcvad.Vad(settings.vad_mode)
        self.on_speech_segment = on_speech_segment
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._running = False
        # 每帧字节数：采样率 * 帧长(秒) * 2(16bit)
        self._frame_bytes = (
            settings.sample_rate * settings.vad_frame_ms // 1000 * 2
        )

    async def start(self):
        """启动拉流循环。"""
        self._running = True
        retry_delay = 5
        while self._running:
            try:
                await self._run_once()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("拉流异常: %s，%d 秒后重试", e, retry_delay)
            # 无论正常退出还是异常，都等待后再重试
            if self._running:
                await asyncio.sleep(retry_delay)

    def stop(self):
        self._running = False
        if self._proc:
            try:
                self._proc.kill()
            except Exception:
                pass

    async def _run_once(self):
        cmd = [
            settings.ffmpeg_path,
            "-rtsp_transport", "tcp",
            "-i", settings.rtsp_url,
            "-vn",
            "-acodec", "pcm_s16le",
            "-ar", str(settings.sample_rate),
            "-ac", str(settings.channels),
            "-f", "s16le",
            "-",
        ]
        logger.info("启动 ffmpeg: %s", " ".join(cmd))

        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

        buffer = b""
        speech_buffer = b""
        silence_count = 0
        speech_frame_count = 0
        in_speech = False

        while self._running:
            data = await self._proc.stdout.read(self._frame_bytes)
            if not data:
                logger.warning("ffmpeg 输出结束")
                break

            buffer += data
            # 按帧处理
            while len(buffer) >= self._frame_bytes:
                frame = buffer[: self._frame_bytes]
                buffer = buffer[self._frame_bytes :]

                try:
                    is_speech = self.vad.is_speech(frame, settings.sample_rate)
                except Exception:
                    is_speech = False

                if is_speech:
                    if not in_speech:
                        # 语音开始
                        in_speech = True
                        speech_frame_count = 0
                        speech_buffer = b""
                    speech_buffer += frame
                    speech_frame_count += 1
                    silence_count = 0
                else:
                    if in_speech:
                        # 语音中，遇到静音
                        silence_count += 1
                        speech_buffer += frame
                        if silence_count >= settings.vad_silence_frames:
                            # 一句话结束
                            if speech_frame_count >= settings.vad_min_speech_frames:
                                logger.debug(
                                    "语音段结束: %d 帧, %.1f 秒",
                                    speech_frame_count,
                                    speech_frame_count * settings.vad_frame_ms / 1000,
                                )
                                await self.on_speech_segment(speech_buffer)
                            in_speech = False
                            speech_buffer = b""
                            silence_count = 0
                            speech_frame_count = 0

        # 进程结束
        try:
            await self._proc.wait()
        except Exception:
            pass
        self._proc = None
