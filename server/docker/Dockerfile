# syntax=docker/dockerfile:1
# SenseVoice STT 服务端镜像（多架构：amd64 / arm64 / armv7）
# 构建上下文必须是 server/ 目录（sv_app.py 在其根下）：
#   docker build -f docker/Dockerfile ..
FROM python:3.11-slim

ARG TARGETARCH

# ffmpeg：非 16kHz WAV 输入的兜底转码
# bzip2/curl：entrypoint 下载并解压模型（Debian 精简镜像默认没有 bzip2）
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg curl ca-certificates bzip2 \
    && rm -rf /var/lib/apt/lists/*

# armv7 特例：PyPI 上没有 numpy 的 armv7l wheel，改用 Debian 的 python3-numpy
# （bookworm 的 numpy 同为 cp311 ABI，挂 PYTHONPATH 即可在镜像 python 里导入）
RUN if [ "$TARGETARCH" = "arm" ]; then \
        apt-get update \
        && apt-get install -y --no-install-recommends python3-numpy \
        && rm -rf /var/lib/apt/lists/* ; \
    fi
ENV PYTHONPATH=/usr/lib/python3/dist-packages

# uvicorn 不带 [standard]：uvloop/httptools 无 armv7 wheel，纯 Python 版全架构通用。
# sherpa-onnx==2.0.1 与 GCP 现役版本一致，三架构均有官方 manylinux wheel。
RUN pip install --no-cache-dir \
        "numpy>=1.26 ; platform_machine != 'armv7l'" \
        fastapi uvicorn python-multipart \
        "sherpa-onnx==2.0.1"

WORKDIR /app
COPY sv_app.py .
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8010
# start-period 放宽：首次启动要先下载解压模型（armv7 上 SenseVoice 加载也偏慢）
HEALTHCHECK --interval=30s --timeout=5s --start-period=300s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${SV_PORT:-8010}/health" || exit 1

ENTRYPOINT ["/entrypoint.sh"]
