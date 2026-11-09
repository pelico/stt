# RTSP STT - 摄像头语音转文字

从 RTSP 流中提取音频，经 VAD 静音检测切句后调用语音识别服务，在 Web 页面展示实时字幕和历史记录。

专为玩客云等 **armv7l** 低性能设备设计：设备端只做音频抓取和转发，识别在云端完成。

## 功能

- 接入 go2rtc 等输出的 RTSP 流
- VAD 静音检测，只在有人说话时识别（节省额度）
- 多识别引擎可切换：阿里云百炼 / 腾讯云一句话识别 / OpenAI 兼容接口
- **Web 设置页**：RTSP 地址、引擎密钥、VAD 灵敏度在线调整，保存后立即生效
- SQLite 历史记录持久化，支持搜索
- WebSocket 实时字幕
- **实时监听**：点页面上的「🔊 监听」可直接收听摄像头声音，无需等识别
- **运行日志页**：在网页查看容器日志（最近 1000 行），包含 ffmpeg 拉流错误，支持暂停和清空，不用再 SSH
- **识别开关 / 时间段**：顶部一键暂停识别，或只在指定时段识别（支持跨午夜）；时段外自动断开拉流，摄像头不再被拉取，进入时段后自动重连
- **RTSP 地址历史**：自动记录最近 10 个地址，设置页下拉切换，可单独删除
- 识别异步队列，慢接口不会阻塞音频采集
- 多架构 Docker 镜像（linux/amd64、linux/arm/v7）

## 快速开始

### 1. 准备识别服务

任选其一：

| 引擎 | 免费额度 | 需要准备 |
|---|---|---|
| 阿里云百炼 | 实时识别 10 小时/月 | [百炼控制台](https://bailian.console.aliyun.com/) 的 API Key |
| 腾讯云 | 一句话识别 5000 次/月 | 访问管理里的 SecretId / SecretKey，并开通「一句话识别」 |
| OpenAI 兼容 | 取决于服务 | 任意兼容 `/audio/transcriptions` 的地址和 Key |

### 2. Docker 运行

```bash
docker run -d \
  --name rtsp-stt \
  -p 8000:8000 \
  -e RTSP_URL=rtsp://192.168.1.100:8554/live \
  -e ASR_ENGINE=aliyun \
  -e DASHSCOPE_API_KEY=sk-你的key \
  -v $(pwd)/data:/data \
  ghcr.io/pelico/stt:latest
```

### 3. 访问页面

打开 `http://<设备IP>:8000`，点右上角「⚙ 设置」可以随时修改 RTSP 地址、切换引擎、调整 VAD 参数。

## Docker Compose

```yaml
services:
  stt:
    image: ghcr.io/pelico/stt:latest
    container_name: rtsp-stt
    restart: unless-stopped
    ports:
      - "8000:8000"
    environment:
      - RTSP_URL=rtsp://192.168.1.100:8554/live
      - ASR_ENGINE=aliyun
      - DASHSCOPE_API_KEY=sk-你的key
    volumes:
      - ./data:/data
```

## 配置优先级

**Web 设置页 > 环境变量 > 内置默认值**

Web 设置保存在 `/data/settings.json`（容器内），因此 `-v ./data:/data` 挂载是必要的，否则重启后设置丢失。

## 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `RTSP_URL` | `rtsp://127.0.0.1:8554/live` | RTSP 流地址 |
| `ASR_ENGINE` | `aliyun` | `aliyun` / `tencent` / `openai` |
| `DASHSCOPE_API_KEY` | 空 | 阿里云百炼 API Key |
| `ASR_MODEL` | `paraformer-realtime-v2` | 阿里云识别模型 |
| `TENCENT_SECRET_ID` | 空 | 腾讯云 SecretId |
| `TENCENT_SECRET_KEY` | 空 | 腾讯云 SecretKey |
| `TENCENT_REGION` | `ap-shanghai` | 腾讯云地域 |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | OpenAI 兼容接口地址 |
| `OPENAI_API_KEY` | 空 | OpenAI 兼容接口 Key |
| `OPENAI_MODEL` | `whisper-1` | 转写模型 |
| `VAD_MODE` | `3` | 灵敏度 0-3，越大越严格 |
| `VAD_FRAME_MS` | `30` | 分析帧长 10/20/30 |
| `VAD_SILENCE_FRAMES` | `10` | 静音多少帧判定一句结束 |
| `VAD_MIN_SPEECH_FRAMES` | `3` | 有效语音最少帧数 |
| `VAD_RMS_DBFS` | `-45` | 能量门限（dBFS），低于该音量不算语音 |
| `ASR_ENABLED` | `true` | 识别总开关 |
| `ASR_SCHEDULE_ENABLED` | `false` | 是否只在指定时段识别 |
| `ASR_START_TIME` | `08:00` | 识别时段开始（容器本地时间） |
| `ASR_END_TIME` | `22:00` | 识别时段结束，支持跨午夜 |
| `HTTP_PORT` | `8000` | 服务端口 |
| `DB_PATH` | `/data/asr.db` | 数据库路径 |
| `SETTINGS_PATH` | `/data/settings.json` | Web 设置持久化路径 |
| `LOG_LEVEL` | `INFO` | 日志级别 |

## VAD 调参建议

页面顶部有实时 VAD 状态条：音量电平、当前是否判定为语音、切段/丢弃统计。调参时对照观察最直观。

- 环境安静、说话小声：`VAD_MODE` 调低到 1，能量门限降到 `-55` 左右
- 环境嘈杂、误触发多：`VAD_MODE` 用 3，能量门限升到 `-35` 左右，`VAD_MIN_SPEECH_FRAMES` 调到 5-8
- 一句话被切得太碎：调大 `VAD_SILENCE_FRAMES`（例如 20 → 600ms）
- 出字太慢、想要快速断句：调小 `VAD_SILENCE_FRAMES`（例如 6 → 180ms）
- 「丢弃」计数一直涨：说明切出来的段太短，调小 `VAD_MIN_SPEECH_FRAMES` 或降低能量门限

## 架构

```
RTSP (go2rtc)
    │
    ▼
FFmpeg 抽音频 → PCM(16kHz/单声道) → webrtcvad 静音检测
    │
    ▼ (检测到一句话)
识别引擎（阿里云 / 腾讯云 / OpenAI 兼容）
    │
    ▼
FastAPI → SQLite（历史）+ WebSocket → 浏览器
```

## 自行构建

```bash
docker buildx build --platform linux/amd64,linux/arm/v7 -t stt .
```
