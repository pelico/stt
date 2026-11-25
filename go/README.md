# RTSP STT — Go 版

核心链路与 Python 版一致：RTSP → ffmpeg 抽 PCM → WebRTC VAD 切句 → SQLite 磁盘队列 → 云 ASR → WebSocket 字幕。

## 与 Python 版的差异

| 项 | Python 版 | Go 版 |
|---|---|---|
| 运行镜像 | 247.5 MiB (amd64) / 214.4 MiB (armv7) | 预估 ~28 MiB / ~25 MiB |
| 常驻内存 | ~100–200 MB RSS | ~25–50 MB RSS |
| VAD | webrtcvad C 扩展 | godeps/go-webrtcvad（wazero 运行 WebRTC wasm，纯 Go） |
| SQLite | SQLAlchemy + sqlite3 | modernc.org/sqlite（纯 Go） |
| 阿里云 ASR | dashscope SDK | 自研轻量 WebSocket 客户端（无 vendor SDK） |
| 前端 | 单文件 HTML + 内联 JS | 现代化卡片布局 + 独立 CSS/JS，go:embed 内嵌 |

## 运行

```bash
docker run -d --name rtsp-stt-go -p 8000:8000 \
  -e RTSP_URL=rtsp://192.168.1.100:8554/live \
  -e ASR_ENGINE=aliyun \
  -e DASHSCOPE_API_KEY=sk-xxx \
  -v $(pwd)/data:/data \
  stt-go:latest
```

本地开发（需本机有 ffmpeg）：

```bash
go build -o stt ./cmd/stt
HTTP_PORT=8000 DB_PATH=./data/asr.db SETTINGS_PATH=./data/settings.json ./stt
```

## 环境变量

与 Python 版完全相同（见上一级目录的 README.md）。Go 版额外支持：
- `FFMPEG_PATH`（默认 `ffmpeg`）
- `AUDIO_DIR`（默认 `/data/audio`）

## 前端快捷入口

- `#settings` — 直接打开设置抽屉
- `#logs` — 直接打开日志面板
