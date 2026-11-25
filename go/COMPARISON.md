# Go 版 vs Python 版：实测对照（更新于 2026-11-25）

> 分支 `go-rewrite`。Python 数据来自 GHCR 镜像 manifest（体积）与**本机实跑**（运行内存/CPU，同机同 ffmpeg 9.1）；Go 版为本机构建 + 实跑同机对比。两版均为 Windows 本机进程实测，非容器。

## 镜像体积

| 架构 | Python 版（实测压缩层） | Go 版（实测二进制 + alpine 推算） | 缩小 |
|---|---|---|---|
| amd64 | 247.5 MiB | ~26.9 MiB（二进制 13.8 MiB 实测） | **9.2×** |
| armv7 | 214.4 MiB | ~25.2 MiB（二进制 11.99 MB 实测） | **8.5×** |

Go 镜像构成：strip 二进制 + alpine 3.21 基础 ~2.7 MiB + ffmpeg/tzdata/ca-certs ~11 MiB。
Python 镜像大头：pip 安装层 162 MiB（amd64）/ 140 MiB（armv7），占 65%。

## 运行内存（同机实测，Windows Private Bytes）

| 组件 | Python 版（实跑） | Go 版（实跑） | 差距 |
|---|---|---|---|
| 主进程 | 89.1 MB（WS 105 MB） | 69.5 MB（WS 56–58 MB） | **主进程省 ~22%** |
| ffmpeg 子进程 | ~17 MB（相同二进制） | 17.4–17.8 MB | 相同 |
| 合计 | ~106 MB | ~87 MB | **省 ~18%** |

- Python 主进程含 CPython + FastAPI + pydantic v2 + SQLAlchemy + dashscope + uvicorn；Go 版单二进制 + 内嵌前端 + wasm VAD。
- Go 版 ~70 MB 中相当一部分是 Go runtime 预留堆，随并发/队列增长缓慢；Python 的 89 MB 是导入全部依赖后的基线，几乎不随负载下降。
- 对 1G 内存 armv7 设备：两版都能跑，Go 版余量更大；若叠加浏览器端多客户端 WS 音频推送，Python 更容易触顶。

> 注：Python 版实测为"拉流失败重试循环"稳态（无真实流可拉），未含音频帧处理路径的峰值；Go 版为真实拉流 + 40 次 ASR 调用后的稳态。此口径对 Python 略有利，实际差距应更大。

## CPU 开销（同机实测）

| 场景 | Python 版 | Go 版 |
|---|---|---|
| 主进程平均 CPU（拉流失败重试态） | ~2.5% 单核 | ~0.7% 单核（真实拉流 24 分钟 10.6s CPU） |
| VAD 每帧判定 | C 扩展，微秒级 | wasm 执行，略慢于 C 但远小于 30ms 帧间隔 |
| 拉流/解码 | ffmpeg 主导 | 完全相同（同一个 ffmpeg 二进制） |
| 识别等待 | 网络 IO 主导 | 完全相同 |

## 端到端延迟

**基本无变化**。瓶颈在云端 ASR（实测 0.6–6.9s/请求），语言层面省下的是毫秒级。Go 版真正改善的是启动时间（<100ms vs ~2s）与内存压力导致的 swap 抖动。

## 功能对齐（本次回归后更新）

- ✅ 核心链路：ffmpeg 拉流 → VAD 切句 → 双模式 SQLite 队列 → 串行 worker → 三引擎 ASR → WS 字幕 → SQLite 历史
- ✅ VAD 全参数（preroll / tail pad / RMS / 静音帧 / 有效占比 / **可配置单段最长（新增）**）
- ✅ 稳定性：看门狗可被 context 打断（修复）、切流即时重启（修复，不再杀生命周期）、退避、降级、熔断、**生产端段合并缓冲（新增，快速 ASR 下合并生效）**
- ✅ 40+ 运行时配置 + 校验 + 热更 + 打码 + 持久化；**访问口令可运行时修改/清除（新增）**
- ✅ 全部 REST API + WS + 登录限速；rtsp 历史路径修正（不再写盘根）
- ✅ 诊断日志、metrics、被过滤音频留证；前端完整重做

## 回归测试记录（2026-11-25）

```
go vet ./...:            clean
go build amd64/armv7:    13,803,008 B / 11,993,272 B
node --check app.js:     clean
REST 冒烟:               health/settings/transcripts/rtsp-history/logs/login/静态资源 全 200；未知静态 404；非法配置 400
鉴权回归:                设口令→无凭证 401/错误口令 401/登录 200；改口令→旧 cookie 失效 401；
                          X-Access-Token 头 200；清口令（clear_access_password）→恢复开放；WS 三态 307/101/101 全对
WS 鉴权:                 有口令时无凭证 307→登录页、带 token 101；无口令 101
真实链路:                ogg HTTP 流→ffmpeg→VAD→openai 引擎 ASR 成功出字幕（43+ 次调用 0 失败）
```

## 未验证 / 待实机确认

1. Docker 镜像实际体积（本机无 Docker，数值为二进制实测 + alpine 层推算）。
2. 阿里云/腾讯 ASR 引擎连通性（无 key，未打真实 API）。
3. 24h+ 烤机与 armv7 实机内存余量。
