# Go 版 RTSP STT —— 纯 Go 静态二进制，无 cgo、无 gcc
# 构建：docker buildx build --platform linux/amd64,linux/arm/v7 -t stt-go .
FROM golang:1.24-alpine AS builder
WORKDIR /build
COPY go.mod go.sum ./
RUN go mod download
COPY . .
ARG TARGETOS TARGETARCH TARGETVARIANT
RUN CGO_ENABLED=0 GOOS=${TARGETOS:-linux} GOARCH=${TARGETARCH:-amd64} \
    GOARM=${TARGETVARIANT#v} \
    go build -trimpath -ldflags="-s -w" -o /stt ./cmd/stt

FROM alpine:3.21
RUN apk add --no-cache ca-certificates tzdata ffmpeg \
    && mkdir -p /data/audio
COPY --from=builder /stt /usr/local/bin/stt
ENV TZ=UTC
EXPOSE 8000
VOLUME /data
ENTRYPOINT ["/usr/local/bin/stt"]
