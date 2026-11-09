# ---- 构建阶段：编译 webrtcvad 等需要 C 扩展的依赖 ----
FROM python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt .
# 把所有依赖预先编译成 wheel，运行阶段无需 gcc
RUN pip wheel --no-cache-dir -r requirements.txt -w /wheels

# ---- 运行阶段：只保留 ffmpeg 和编译好的 wheel ----
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt \
    && rm -rf /wheels

COPY app/ ./app/

RUN mkdir -p /data/audio

EXPOSE 8000

CMD ["python", "-m", "app.main"]
