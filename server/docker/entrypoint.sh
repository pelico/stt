#!/bin/sh
# 启动前置：按 SV_ASR_TYPE 确保模型就绪（缺了自动从 sherpa-onnx release 下载），
# 然后单进程拉起 uvicorn（并发由 app 内线程池 SV_WORKERS 控制，--workers 必须为 1，
# 否则每个 fork 进程都会各自加载一份模型）。
set -e

MODELS_ROOT=/data/models
ASR_TYPE=$(echo "${SV_ASR_TYPE:-sensevoice}" | tr '[:upper:]' '[:lower:]')

case "$ASR_TYPE" in
    zipformer)
        NAME=sherpa-onnx-streaming-zipformer-small-bilingual-zh-en-2023-02-16
        ;;
    sensevoice|*)
        NAME=sherpa-onnx-sense-voice-zh-en-ja-yue-2024-07-17
        ;;
esac

URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/${NAME}.tar.bz2"
DIR="$MODELS_ROOT/$NAME"

if ls "$DIR"/*.onnx >/dev/null 2>&1; then
    echo "模型已就绪: $DIR"
else
    echo "首次启动：下载模型 $NAME 到 $MODELS_ROOT"
    echo "（国内网络慢或离线设备：可提前把 ${NAME}.tar.bz2 放到挂载目录里手动解压，跳过下载）"
    mkdir -p "$MODELS_ROOT"
    curl -fL --retry 3 --progress-bar -o /tmp/model.tar.bz2 "$URL"
    tar -xjf /tmp/model.tar.bz2 -C "$MODELS_ROOT"
    rm -f /tmp/model.tar.bz2
    echo "模型下载完成"
fi

cd /app
exec python -m uvicorn sv_app:app --host 0.0.0.0 --port "${SV_PORT:-8010}" --workers 1
