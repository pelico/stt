"""应用配置，全部通过环境变量注入。"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # RTSP 流地址
    rtsp_url: str = "rtsp://127.0.0.1:8554/live"

    # ASR 引擎：aliyun / tencent / openai
    asr_engine: str = "aliyun"

    # 阿里云百炼 API Key
    dashscope_api_key: str = ""

    # 阿里云百炼实时语音识别模型
    asr_model: str = "paraformer-realtime-v2"

    # 识别语言：zh / en / auto（各引擎支持程度不同）
    asr_language: str = "zh"

    # 腾讯云一句话识别
    tencent_secret_id: str = ""
    tencent_secret_key: str = ""
    tencent_region: str = "ap-shanghai"

    # OpenAI 兼容语音识别接口（可指向中转服务）
    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: str = ""
    openai_model: str = "whisper-1"

    # OpenAI 兼容接口单次识别超时（秒）
    asr_timeout: int = 45

    # 语音段在识别队列里允许等待的最长秒数。ASR 服务跟不上时，排队过久的
    # 片段直接丢弃：几十秒后才转写出来的文字没有实时意义，却会持续占住
    # 串行 worker，让积压越滚越大。0 = 不限制。Web 设置页可热更新。
    asr_max_queue_age: int = 10

    # 采样率（固定 16kHz，ASR 要求）
    sample_rate: int = 16000

    # VAD 模式 0-3，越大越激进（越容易判定为静音）
    vad_mode: int = 3

    # VAD 帧长（毫秒），webrtcvad 只支持 10/20/30
    vad_frame_ms: int = 30

    # 连续静音多少帧后认为一句话结束（默认 20 帧=600ms，避免把一句话切碎）
    vad_silence_frames: int = 20

    # 连续语音多少帧后才认为是有效语音（默认 10 帧=300ms，过滤短促噪声）
    vad_min_speech_frames: int = 10

    # 一段语音里"真语音帧"的最低占比：噪声往往只有零星几帧被 VAD 判为语音
    vad_min_speech_ratio: float = 0.5

    # 切段时向前补的音频（毫秒）：VAD 判定滞后 1~2 帧，不补会吞掉字头
    vad_preroll_ms: int = 200

    # 切段时尾部保留的静音（毫秒）：整段 600ms 静音尾送进模型是幻觉高发区
    vad_tail_pad_ms: int = 150

    # 能量门限（dBFS）：低于该音量的帧不判定为语音，-80 ~ -10
    vad_rms_dbfs: int = -45

    # ffmpeg 高通滤波截止频率（Hz），滤掉风声/风扇等低频噪声；0 表示关闭
    ffmpeg_highpass: int = 100

    # 识别结果噪音过滤：语气词、单字、幻觉短词直接丢弃
    filter_noise_text: bool = True
    filter_ignore_words: str = (
        "嗯,啊,哦,呃,唉,哎,呵,哈,嘿,唔,噢,喂,呀,呐,呗,嘛,额,"
        "嗯嗯,啊啊,呵呵,哈哈,呵呵呵,不不不,四,三,二,一,"
        "here,shoes,foot,eye,i love you,thank you,thanks,bye,okay"
    )

    # 识别总开关
    asr_enabled: bool = True

    # 只在指定时段识别（按容器本地时间）
    asr_schedule_enabled: bool = False
    asr_start_time: str = "08:00"
    asr_end_time: str = "22:00"

    # HTTP 服务端口
    http_port: int = 8000

    # 数据库路径（容器内）
    db_path: str = "/data/asr.db"

    # Web 设置持久化路径（优先级高于环境变量）
    settings_path: str = "/data/settings.json"

    # 是否保存音频片段
    save_audio: bool = False
    audio_dir: str = "/data/audio"

    # 是否把"被过滤掉"的音频片段存成 WAV，便于回听判断是否误杀（调试用）
    save_rejected_audio: bool = False
    rejected_audio_max: int = 50

    # 可选访问口令（空=不启用）。启用后 /api/* 与 /ws 需带该口令
    access_token: str = ""

    # ffmpeg 可执行文件名
    ffmpeg_path: str = "ffmpeg"

    # 拉流超时（秒）：同时用作 -rw_timeout 和"无音频数据"看门狗
    ffmpeg_timeout: int = 20

    # 日志级别
    log_level: str = "INFO"


settings = Settings()
