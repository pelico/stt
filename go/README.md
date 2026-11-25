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

## 镜像

推送到 GitHub 后，Actions（`.github/workflows/ghcr-image.yml`）自动构建多架构镜像并推送到 ghcr.io：

- 平台：`linux/amd64`、`linux/arm/v7`（armv7l 设备直接 pull，无需本地构建）
- 触发：push 到 `main` / `go-rewrite`（改 `go/**` 时）、打 `v*` 标签、手动触发
- 镜像名：`ghcr.io/<用户名>/<仓库名>`（小写），tag = 分支名 / 语义版本 / 短 SHA

armv7l 设备使用：

```bash
docker pull ghcr.io/<用户名>/<仓库名>:go-rewrite
```

## 运行

1. 复制 `.env.example` 为 `.env`，填入音频流地址与 ASR 密钥（`.env` 不会提交到 Git）；
2. `docker-compose.yml` 里把 `ghcr.io/OWNER/REPO` 改成实际镜像名；
3. 启动：

```bash
docker compose up -d
# 浏览器打开 http://<设备IP>:8000
```

音频流地址与 ASR 接口地址**不预置**在仓库/镜像中，也可以启动后在网页「设置」里填写，保存即生效。

VAD/断句参数已预置实测调优值（`VAD_MODE=2`、门限 `-27 dBFS`、句首补帧 300ms、句尾保留 600ms、单段最长 20s、合并间隔 2s），开箱即可用；不同音频源可在网页设置里微调。

本地开发（需本机有 ffmpeg）：

```bash
go build -o stt ./cmd/stt
HTTP_PORT=8000 DB_PATH=./data/asr.db SETTINGS_PATH=./data/settings.json ./stt
```

## 环境变量

与 Python 版完全相同（见上一级目录的 README.md）。Go 版额外支持：
- `FFMPEG_PATH`（默认 `ffmpeg`）
- `AUDIO_DIR`（默认 `/data/audio`）
- `ACCESS_PASSWORD`（页面访问口令，与 `ACCESS_TOKEN` 等效，也可运行时在设置里修改）

## 前端快捷入口

- `#settings` — 直接打开设置抽屉
- `#logs` — 直接打开日志面板
