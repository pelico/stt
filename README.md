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
- **实时播放**：点页面上的「▶ 播放」可直接收听摄像头声音，再点「⏹ 停止播放」关闭，无需等识别
- **噪音过滤**：VAD 有效语音占比门限 + ffmpeg 高通滤波 + 语气词/单字/幻觉短词结果丢弃，设置页可调开关与黑名单
- **运行日志页**：在网页查看容器日志（最近 1000 行），包含 ffmpeg 拉流错误，支持暂停和清空，不用再 SSH
- **识别开关 / 时间段**：顶部一键暂停识别，或只在指定时段识别（支持跨午夜）；时段外自动断开拉流，摄像头不再被拉取，进入时段后自动重连；**时段外的识别积压会继续后台转完**（只有手动暂停才全停）
- **RTSP 地址历史**：自动记录最近 10 个地址，设置页下拉切换，可单独删除
- 识别异步队列，慢接口不会阻塞音频采集
- 多架构 Docker 镜像（linux/amd64、linux/arm/v7）
- **抗断流**：关掉 ffmpeg 进度输出、网络 I/O 超时 + 无数据看门狗、失败按 5→10→20→40→60 秒退避重连，摄像头 TCP 半开不会静默卡死
- **识别服务熔断**：连续失败 3 次自动退避探活，云端模型重载期间不再把识别队列占满
- **双模式识别队列**：默认「完整优先」——语音段落盘 `./data/pending.db`，ASR 慢时按采集顺序在后台慢慢补识别、容器重启不丢，入库时间仍是说话时刻；需要低延迟可切「实时优先」按等待时间丢弃。VAD 状态栏与 `/api/health` 都能看积压数
- **段合并**：出队时把采集间隔 <1 秒的相邻语音段合并成一次识别请求（上限 30 秒），每请求约 2 秒的固定开销摊薄到多段里，吞吐翻 2~3 倍——连续对话的滞后从小时级压到分钟级，上下文变长断句也更准
- **结构化诊断日志**：每次 ASR 调用带 `req#`、目标 URL、双超时配置、音频大小、耗时/首字节时间、错误码与排查建议；日志页可按级别/关键字过滤并显示诊断摘要，同时轮转落盘到 `/data/logs/stt.log`
- **首页实时看板**：字幕面板下方常驻诊断摘要（调用/成功/失败/P95/积压/累计入队/出队/队列丢弃），VAD 状态栏显示切段、入队、出队累计与最近片段时长，不用点开日志页就能对照音频判断 VAD 参数是否合适
- **句首补帧 / 句尾裁静音**：补 200ms 前置音频避免 VAD 滞后吞字头，裁掉多余静音尾减少幻觉文本和推理耗时
- **广播解耦**：每个 WebSocket 独立发送队列，慢客户端不会拖住音频采集与 VAD
- **被过滤音频留证**（可选）：把 VAD / 文本层丢弃的片段存成 WAV 回听，判断是噪声还是误杀
- **可选访问口令**：保护设置接口、历史地址与实时音频流

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
      # 识别时段与日志时间戳按容器本地时间，不设会用 UTC（差 8 小时）
      - TZ=Asia/Shanghai
      - RTSP_URL=rtsp://192.168.1.100:8554/live
      - ASR_ENGINE=aliyun
      - DASHSCOPE_API_KEY=sk-你的key
      # 可选访问口令
      # - ACCESS_TOKEN=change-me
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
| `ASR_LANGUAGE` | `zh` | 识别语言：`zh` / `en` / `auto` |
| `TENCENT_SECRET_ID` | 空 | 腾讯云 SecretId |
| `TENCENT_SECRET_KEY` | 空 | 腾讯云 SecretKey |
| `TENCENT_REGION` | `ap-shanghai` | 腾讯云地域 |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | OpenAI 兼容接口地址 |
| `OPENAI_API_KEY` | 空 | OpenAI 兼容接口 Key |
| `OPENAI_MODEL` | `whisper-1` | 转写模型 |
| `ASR_TIMEOUT` | `45` | 读取超时（秒）：服务端算得慢时的等待上限 |
| `ASR_CONNECT_TIMEOUT` | `10` | 连接超时（秒）：与读取超时分开，异常能区分「连不上」和「算得慢」 |
| `ASR_SLOW_MS` | `10000` | 超过该毫秒数打一条「慢请求」WARNING |
| `LOG_FILE_ENABLED` | `true` | 运行日志轮转落盘（页面内存缓冲重启即清空，事后排查靠文件） |
| `LOG_FILE_PATH` | `/data/logs/stt.log` | 日志文件路径，位于持久化卷内 |
| `LOG_FILE_MAX_MB` / `LOG_FILE_BACKUPS` | `5` / `3` | 单文件大小与保留份数 |
| `QUEUE_MODE` | `durable` | `durable`=完整优先（落盘 FIFO，不丢）/ `realtime`=实时优先（过期丢弃）。**切换需重启容器** |
| `QUEUE_MAX_ITEMS` | `2000` | 完整优先模式队列条数上限，超出丢最旧并打日志；Web 设置页可热更新（1~10000） |
| `QUEUE_MAX_MB` | `256` | 完整优先模式磁盘硬上限（1~1024），防止写满 `./data` 卷；积压清空后自动 VACUUM 回收空间 |
| `QUEUE_MAX_ATTEMPTS` | `5` | 单条片段最多尝试识别几次，防坏音频卡死 FIFO；`0` = 无限重试 |
| `ASR_MAX_QUEUE_AGE` | `10` | 仅 `realtime` 模式：排队最长等待秒数，超时直接丢弃；`0` = 不限制 |
| `ASR_MERGE_GAP_SEC` | `2.0` | **段合并**：采集间隔小于该秒数的相邻语音段合并成一次识别请求，摊薄每请求约 2 秒的固定开销（CF 两跳 + WAV 封装 + 服务端排队），吞吐翻 2~3 倍，连续对话滞后明显下降；`0` = 关闭。Web 设置页可热更新（0~10s） |
| `ASR_MERGE_MAX_SEC` | `30` | 合并块最大音频时长（秒），防止单请求过大拖慢单次推理；Web 设置页可热更新（10~120s） |
| `TZ` | `UTC` | **容器时区**，务必设成 `Asia/Shanghai`，否则识别时段与日志时间差 8 小时 |
| `VAD_MODE` | `3` | 灵敏度 0-3，越大越严格 |
| `VAD_FRAME_MS` | `30` | 分析帧长 10/20/30 |
| `VAD_SILENCE_FRAMES` | `20` | 静音多少帧判定一句结束（20×30ms = 600ms） |
| `VAD_MIN_SPEECH_FRAMES` | `10` | 有效语音最少帧数（10×30ms = 300ms） |
| `VAD_MIN_SPEECH_RATIO` | `0.5` | 一段里真语音帧的最低占比，0 关闭 |
| `VAD_PREROLL_MS` | `200` | 句首补帧，避免 VAD 滞后吞掉字头 |
| `VAD_TAIL_PAD_MS` | `150` | 句尾保留静音，裁掉多余静音尾 |
| `VAD_RMS_DBFS` | `-45` | 能量门限（dBFS），低于该音量不算语音 |
| `FFMPEG_HIGHPASS` | `100` | 高通滤波截止频率（Hz），滤风声/风扇；0 关闭 |
| `FFMPEG_DENOISE` | `true` | 轻度自适应降噪（afftdn，压稳态底噪）；老 ffmpeg 无该滤镜时自动关闭 |
| `FFMPEG_TIMEOUT` | `20` | 拉流超时（秒），同时作网络 I/O 超时和无数据看门狗 |
| `FILTER_NOISE_TEXT` | `true` | 丢弃语气词、单字、幻觉短词 |
| `FILTER_IGNORE_WORDS` | 内置列表 | 噪音词黑名单（逗号分隔） |
| `SAVE_REJECTED_AUDIO` | `false` | 把被过滤片段存成 WAV 供回听（调试用） |
| `REJECTED_AUDIO_MAX` | `50` | 留证文件最多保留个数 |
| `ACCESS_TOKEN` | 空 | 访问口令，空=不启用 |
| `ASR_ENABLED` | `true` | 识别总开关 |
| `ASR_SCHEDULE_ENABLED` | `false` | 是否只在指定时段识别 |
| `ASR_START_TIME` | `08:00` | 识别时段开始（容器本地时间，见 `TZ`） |
| `ASR_END_TIME` | `22:00` | 识别时段结束，支持跨午夜 |
| `HTTP_PORT` | `8000` | 服务端口 |
| `DB_PATH` | `/data/asr.db` | 数据库路径 |
| `SETTINGS_PATH` | `/data/settings.json` | Web 设置持久化路径 |
| `LOG_LEVEL` | `INFO` | 日志级别 |

## VAD 调参建议

页面顶部有实时 VAD 状态条：音量电平、当前是否判定为语音、切段/丢弃统计。调参时对照观察最直观。

- 环境安静、说话小声：`VAD_MODE` 调低到 1，能量门限降到 `-55` 左右
- 环境嘈杂、误触发多：`VAD_MODE` 用 3，能量门限升到 `-35` 左右，`VAD_MIN_SPEECH_FRAMES` 调到 15-20，`VAD_MIN_SPEECH_RATIO` 提到 0.6
- 一句话被切得太碎：调大 `VAD_SILENCE_FRAMES`（例如 30 → 900ms）
- 出字太慢、想要快速断句：调小 `VAD_SILENCE_FRAMES`（例如 10 → 300ms），代价是长句容易被切碎
- 「丢弃」计数一直涨：调小 `VAD_MIN_SPEECH_FRAMES` 或降低能量门限；**拿不准是噪声还是误杀时，打开设置页「保存被过滤的音频片段」，回听 `./data/audio/rejected/` 里的 WAV 再调**
- 字头被吞：调大「句首补帧」；句尾总带奇怪的语气词：调小「句尾静音」

## 架构

```
RTSP (go2rtc)
    │
    ▼
FFmpeg 抽音频 → PCM(16kHz/单声道) → webrtcvad 静音检测
    │
    ▼ (检测到一句话)
识别队列（durable：./data/pending.db 磁盘 FIFO / realtime：内存有界队列）
    │
    ▼ (单个串行 worker)
识别引擎（阿里云 / 腾讯云 / OpenAI 兼容）
    │
    ▼
FastAPI → SQLite（历史，timestamp=说话时刻）+ WebSocket → 浏览器
```

## 识别队列：为什么存在采集端，而不是 ASR 服务端

音频是采集端（跑本项目容器的设备）用 VAD 切出来的；服务端的 `/audio/transcriptions` 是**同步请求**——音频没发过去之前，服务端根本无从排队。要在服务端排队，得把 API 改成「提交任务 + 回调/轮询」的异步协议，同时把音频先上传过去，复杂度高得多，还会把几百条 WAV 堆在本就吃紧的 1G 内存机器上，让 swap 更严重。所以队列落在采集端的 `./data` 卷（本来就是持久化目录），用独立 SQLite WAL 文件，入队/取最旧/出队/裁剪都是原子操作，容器重启后未处理片段仍在。

**队列位置不解决速度差**：单段识别 55 秒、VAD 每 1~2 秒产一段时，长期平均产出速率远大于消费速率，任何有限队列都会溢出，`QUEUE_MAX_ITEMS` / `QUEUE_MAX_MB` 只是把「按等待时间丢」换成「按容量丢最旧」。实际能不能补完，取决于说话是否间歇——安静下来后后台会按采集顺序把积压补完（日志会打「延迟 N 秒补处理」）。要稳定不丢，仍需把服务端单次推理压到几秒内（查 `vmstat si/so`、`MemoryMax`、模型 mmap 页是否被回收）。

## 诊断日志：超时怎么定位

一次请求在日志里是几行，用 `req#N` 串起来：

```text
16:40:01 INFO  [段] 开始 | 音频 1.9s/60KB | 排队 0.2s | 积压 9 | 引擎 openai | 模式 durable | 连续失败 0
16:40:01 INFO  [ASR] req#18 → POST https://<域名>/stt/v1/audio/transcriptions | host=<域名> model=sensevoice-small lang=zh audio=1.9s/60KB wav=61KB timeout=(connect 10s, read 45s)
16:40:46 ERROR [ASR] req#18 ✗ read_timeout after 45.02s | host=<域名> | requests.exceptions.ReadTimeout: ... | 建议: 连接已建立但服务端在读取超时内没返回，检查 GCP 的 CPU/swap/模型加载
16:40:46 ERROR [段] 失败 | 连续 1/3 次 | 耗时 45.03s | 排队 0.2s | 积压 14 | ReadTimeout: ...
```

直接可读：音频多大、请求了谁、连接/读取超时各设多少、等了多久、积压涨到多少、连续失败几次。**超时拆成 connect/read 两个值**后，握手失败和推理慢不会再混成一句 `Read timed out`。成功时还会打 `ttfb`（响应头到达耗时）与响应体大小；超过 `ASR_SLOW_MS` 额外打一条「慢请求」WARNING。

| 错误码 | 含义 | 先查哪里 |
|---|---|---|
| `connect_timeout` | TCP/TLS 握手超时 | 防火墙、Cloudflare 回源、Caddy 是否存活、DNS |
| `read_timeout` | 连上了但服务端没按时返回 | GCP 的 `vmstat si/so`、`MemoryMax`、sensevoice 进程；本机直连 vs 经域名计时对比 |
| `http_502/503/504` | 反代/上游不可用 | Caddy 反代目标与 `journalctl -u sensevoice` |
| `http_401/403` | 鉴权失败 | API Key、反代是否透传 `Authorization` |
| `http_404` | 路径不存在 | `OPENAI_BASE_URL` 与 `handle_path` 前缀 |
| `bad_response` | 返回了错误页而非 JSON | 反代是否把请求转到了别处 |

日志面板支持按级别和关键字过滤（搜 `read_timeout`、`req#18`、`慢请求` 都行），顶部摘要实时显示成功率、最近/P95 耗时、慢请求数、连续失败数、积压与当前超时配置；`/api/health` 的 `diagnostics` 是同一份数据。完整历史在容器内轮转文件里：

```bash
docker exec rtsp-stt sh -c "grep -E 'read_timeout|慢请求|熔断' /data/logs/stt.log | tail -50"
```

## 稳定性设计（都是踩过的坑）

| 隐患 | 处理 |
|---|---|
| ffmpeg 的 `-stats` 进度用 `\r` 刷行，`readline()` 会累积到 64KB 抛异常，之后 stderr 管道写满 → ffmpeg 阻塞 → 拉流静默冻死，而 health 仍显示运行中 | 命令加 `-hide_banner -nostats -loglevel warning`；stderr 改按块读取并自行按 `\r`/`\n` 切分，兜底捕获所有异常 |
| 老版本 ffmpeg（嵌入式/精简构建）不认识 `-rw_timeout`，报 `Error opening input files: Option not found`，拉流反复失败 | 检测到 `Option not found` 自动降级：先去 `-rw_timeout`，仍报错再去墙钟时间戳选项，2 秒内快速重连；半开检测退化为仅靠无数据看门狗 |
| RTSP TCP 半开（摄像头断电/换 IP）时 `read()` 永不返回 | `-rw_timeout` + 应用层「N 秒无音频数据即断开」，按 5→10→20→40→60 秒指数退避重连 |
| 云端模型重载要几分钟，每段都等一次超时会把串行识别队列占满 | 连续失败 3 次熔断退避（5→10→…→60 秒），恢复后自动清零，页面弹提示 |
| ASR 单次推理要 30~60 秒（1G 内存小机器 + swap），VAD 每 1~2 秒切一片，队列越堆越深，最后刷一片 `Read timed out` | 默认 `durable`：片段落盘 FIFO，后台按采集顺序补完，慢只带来延迟不丢数据，结果时间戳仍是说话时刻；`realtime` 模式才按 `ASR_MAX_QUEUE_AGE` 丢弃。**长期平均产出快于识别时任何有限队列都会溢出**，上限只是延缓，治本仍是降低服务端推理耗时 |
| 某个浏览器卡住会拖慢音频采集，导致丢帧和 VAD 时间轴漂移 | 每个 WebSocket 独立有界队列 + 发送任务；采集循环只做非阻塞投递 |
| 600ms 静音尾整段送进 ASR 容易「编」出嗯/啊/英文单词 | 切段裁到 150ms 静音尾，并补 200ms 句首音频 |

## 访问口令（可选）

设置 `ACCESS_TOKEN` 后，`/api/*` 与 `/ws` 都需要口令，避免同网段的人读到含账密的 RTSP 地址、改引擎把音频转走。

1. compose 里加 `- ACCESS_TOKEN=your-secret`，重启容器
2. 首次用 `http://<设备IP>:8000/?token=your-secret` 打开页面，口令会存进浏览器 localStorage
3. 之后所有请求与 WebSocket 自动带口令，直接访问正常地址即可

**不设置该变量时行为与旧版完全一致**，不会影响现有部署。注意这只是一道轻量访问控制，若要公网暴露请再加反向代理的 HTTPS 与认证。

## 自行构建

```bash
docker buildx build --platform linux/amd64,linux/arm/v7 -t stt .
```
