# RTSP STT - 摄像头语音转文字

从 RTSP 流中提取音频，通过阿里云百炼实时语音识别转为文字，并在 Web 页面展示实时字幕和历史记录。

专为玩客云等 **armv7l** 低性能设备设计：设备端只做音频抓取和转发，识别在云端完成。

## 功能

- 🎥 接入 go2rtc 等输出的 RTSP 流
- 🔇 VAD 静音检测，只在有人说话时识别（省免费额度）
- ☁️ 阿里云百炼实时语音识别（每月 10 小时免费额度）
- 💾 SQLite 历史记录持久化
- 🌐 Web 页面展示实时字幕 + 历史记录搜索
- 🐳 支持 armv7 / amd64 多架构 Docker 镜像

## 快速开始

### 1. 获取阿里云百炼 API Key

前往 [阿里云百炼控制台](https://bailian.console.aliyun.com/) 开通「实时语音识别」服务，获取 API Key。

### 2. Docker 运行

```bash
docker run -d \
  --name rtsp-stt \
  -p 8000:8000 \
  -e RTSP_URL=rtsp://192.168.1.100:8554/live \
  -e DASHSCOPE_API_KEY=sk-你的key \
  -v $(pwd)/data:/data \
  ghcr.io/pelico/stt:latest
```

### 3. 访问页面

打开 `http://<设备IP>:8000`

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
      - RTSP_URL=rt://192.168.1.100:8554/live
      - DASHSCOPE_API_KEY=sk-你的key
    volumes:
      - ./data:/data
```

## 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `RTSP_URL` | `rtsp://127.0.0.1:8554/live` | RTSP 流地址 |
| `DASHSCOPE_API_KEY` | 空 | 阿里云百炼 API Key |
| `ASR_MODEL` | `paraformer-realtime-v2` | 识别模型 |
| `VAD_MODE` | `3` | VAD 模式 0-3，越大越激进 |
| `HTTP_PORT` | `8000` | 服务端口 |
| `DB_PATH` | `/data/asr.db` | 数据库路径 |
| `LOG_LEVEL` | `INFO` | 日志级别 |

## 架构

```
RTSP (go2rtc)
    │
    ▼
FFmpeg 抽 AAC → 转 PCM(16kHz) → webrtcvad 静音检测
    │
    ▼ (有人说话时)
阿里云百炼实时语音识别
    │
    ▼
FastAPI → SQLite (历史) + WebSocket → 浏览器
```

## 自行构建

```bash
docker buildx build --platform linux/amd64,linux/arm/v7 -t stt .
```
