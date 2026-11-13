# STT 服务端（SenseVoice/Zipformer）Docker 部署

GCP 裸机那套（venv + systemd）的容器化版本，多架构镜像：**amd64 / arm64 / armv7**，1GB 内存设备可用。

## 快速部署（推荐，走 CI 镜像）

```bash
mkdir -p stt-server/models && cd stt-server
# 下载 docker-compose.yml 到本目录（或整个 clone 后进 server/docker/）
docker compose pull
docker compose up -d
```

首次启动会自动从 sherpa-onnx release 下载模型到 `./models/`（zipformer ~90MB / sensevoice ~1GB），
之后重建容器不再下载。验证：

```bash
curl http://127.0.0.1:8010/health
# {"status":"ok","workers":1,"asr_type":"zipformer",...}
```

## 离线/弱网设备

先把模型包放到挂载目录手动解压，跳过在线下载：

```bash
cd models
wget https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-streaming-zipformer-small-bilingual-zh-en-2023-02-16.tar.bz2
tar -xjf sherpa-onnx-streaming-zipformer-small-bilingual-zh-en-2023-02-16.tar.bz2 && rm *.tar.bz2
```

## 配置项（compose environment）

| 变量 | 默认 | 说明 |
|---|---|---|
| `SV_ASR_TYPE` | `zipformer` | `zipformer`（~90MB，1GB 设备首选，输出无标点）或 `sensevoice`（~500MB，更准，≥2GB 内存再用） |
| `SV_WORKERS` | `1` | 并发推理线程数；≥2GB 多核设备可加到 2 |
| `SV_MAX_PENDING` | `3` | 排队上限，满则 503 快速拒绝 |
| `SV_LANGUAGE` | `zh` | 识别语种 |
| `SV_PORT` | `8010` | 容器内监听端口 |
| `SV_MLOCK` | `0` | 保持 0，容器内禁用 mlockall（小内存机已验证会锁死） |

## 客户端接入

把调用方（rtsp-stt 的 `OPENAI_BASE_URL`）指向本机 8010 端口即可，协议与 OpenAI
`/v1/audio/transcriptions` 兼容；前面加 Caddy/Nginx 反代同理（GCP 上是
`handle_path /stt/* { reverse_proxy 172.18.0.1:8010 }` 的写法）。

## 日志

```bash
docker compose logs -f          # 实时
docker compose logs stt-server | grep 推理   # 每段推理耗时/实时率
```
