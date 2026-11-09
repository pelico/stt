"""应用配置，全部通过环境变量注入。"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # RTSP 流地址
    rtsp_url: str = "rtsp://127.0.0.1:8554/live"

    # ASR 引擎：目前只支持 aliyun
    asr_engine: str = "aliyun"

    # 阿里云百炼 API Key
    dashscope_api_key: str = ""

    # 阿里云百炼实时语音识别模型
    asr_model: str = "paraformer-realtime-v2"

    # 音频采样率（固定 16kHz，ASR 要求）
    sample_rate: int = 16000

    # 声道数（固定单声道）
    channels: int = 1

    # VAD 模式 0-3，越大越激进（越容易判定为静音）
    vad_mode: int = 3

    # VAD 帧长（毫秒），webrtcvad 只支持 10/20/30
    vad_frame_ms: int = 30

    # 连续静音多少帧后认为一句话结束
    vad_silence_frames: int = 10

    # 连续语音多少帧后才认为是有效语音（过滤短促噪声）
    vad_min_speech_frames: int = 3

    # HTTP 服务端口
    http_port: int = 8000

    # 数据库路径（容器内）
    db_path: str = "/data/asr.db"

    # 是否保存音频片段
    save_audio: bool = False
    audio_dir: str = "/data/audio"

    # ffmpeg 路径
    ffmpeg_path: str = "ffmpeg"

    # ffmpeg 拉流超时（秒）
    ffmpeg_timeout: int = 30

    # 日志级别
    log_level: str = "INFO"


settings = Settings()
