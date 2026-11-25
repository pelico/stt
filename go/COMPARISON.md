# Go 版 vs Python 版：实测与预估对照

> 分支 `go-rewrite`。Python 版数据来自 GHCR 镜像 manifest 实测；Go 版数据来自本地构建实测（本机无 Docker，镜像体积为基于实测二进制 + alpine 层的推算）。

## 镜像体积

| 架构 | Python 版（实测压缩层） | Go 版（预估） | 缩小 |
|---|---|---|---|
| amd64 | 247.5 MiB | ~26.8 MiB | **9.2×** |
| armv7 | 214.4 MiB | ~25.1 MiB | **8.5×** |

Go 镜像构成（预估）：strip 后二进制 13.1 MiB (amd64) / 11.4 MiB (armv7，均为本地实测) + alpine 基础 ~2.7 MiB + ffmpeg/tzdata/ca-certs ~11 MiB。

Python 镜像大头：单个 pip 安装层 162 MiB（amd64）/ 140 MiB（armv7），占镜像 65%。dashscope SDK 本身只有 ~1.2 MiB，不是主因。

## 运行内存（RSS，估算）

| 组件 | Python 版 | Go 版 |
|---|---|---|
| 主进程 | ~100–200 MB（CPython + FastAPI + pydantic + SQLAlchemy + dashscope） | ~25–50 MB |
| ffmpeg 子进程 | 两边都有，~10–30 MB | 同左 |
| 合计 | ~110–230 MB | ~35–80 MB |

对 1G 内存的 armv7 设备，这是最实质的收益。Go 进程无 GIL、无解释器、GC 压力小。

## CPU 开销

| 场景 | Python 版 | Go 版 |
|---|---|---|
| 空闲 | 1–3% 单核（asyncio 循环 + GC） | <0.5% |
| VAD 每帧判定 | C 扩展，微秒级 | wasm 执行，略慢于 C 但仍远小于 30ms 帧间隔 |
| 拉流/解码 | ffmpeg 主导 | 完全相同（同一个 ffmpeg 二进制） |
| 识别等待 | 网络 IO 主导 | 完全相同 |

## 端到端延迟

**基本无变化**。瓶颈在云端 ASR（2–45s），语言层面省下的是毫秒级。Go 版真正改善的是启动时间（<100ms vs ~1–2s）与内存压力导致的 swap 抖动。

## 已完成的功能对齐

- ✅ 核心链路：ffmpeg 拉流 → VAD 切句 → 双模式 SQLite 队列 → 串行 worker → 三引擎 ASR → WebSocket 字幕 → SQLite 历史
- ✅ VAD 全部参数与状态机（preroll / tail pad / RMS 双重判定 / 30s 强制切段找静音帧 / 有效语音占比）
- ✅ 稳定性设计：无数据看门狗、5→10→20→40→60s 退避、老 ffmpeg 选项降级、连续失败 3 次熔断、段合并、慢请求动态阈值
- ✅ 全部 40 项运行时配置 + 校验 + 热更新 + 敏感字段打码 + JSON 持久化
- ✅ 全部 REST API + WebSocket + 访问口令 Cookie 会话与限速
- ✅ 诊断日志（内存环形缓冲 + 轮转落盘）、metrics、被过滤音频留证
- ✅ 前端重做：卡片布局、实时字幕、VAD 电平、指标看板、设置抽屉、日志面板、历史搜索、深浅色自动适配

## 未验证 / 待实机确认

1. **真实 RTSP 拉流与 VAD 切句准确性**：本机没有摄像头流，未做端到端的音频→字幕验证。VAD 使用 WebRTC 官方算法（wasm 移植），理论上切句边界与 C 扩展一致，但需实机对照。
2. **阿里云 ASR 连通性**：自研 WebSocket 客户端按公开协议实现（run-task → task-started → 裸 PCM 帧 → result-generated → finish-task → task-finished），未用真实 API Key 打通过。
3. **Docker 镜像实际体积**：本机无 Docker，数值为推算。构建命令见 `Dockerfile`。
4. **长期运行稳定性**：未做 24h+ 烤机。

## 本地实测记录

```
go build (amd64, -s -w):  13,776,896 bytes
go build (armv7, -s -w):  11,993,272 bytes
go vet ./...:            clean
WS 握手:                 HTTP/1.1 101 Switching Protocols
REST API:                health / settings(GET+POST) / transcripts / logs / rtsp-history 全部通过
配置校验:                vad_mode=9 → 400 "vad_mode 必须是 0-3"
敏感打码:                sk-a****mnop
前端渲染:                Edge headless 截图确认首页与设置抽屉均正常
```
