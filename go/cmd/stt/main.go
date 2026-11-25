package main

import (
	"context"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/pelico/stt-go/internal/config"
	"github.com/pelico/stt-go/internal/web"
)

func main() {
	settings := config.LoadSettings()
	level := slog.LevelInfo
	switch settings.LogLevel {
	case "DEBUG":
		level = slog.LevelDebug
	case "WARN":
		level = slog.LevelWarn
	case "ERROR":
		level = slog.LevelError
	}
	slog.SetDefault(slog.New(slog.NewTextHandler(os.Stdout, &slog.HandlerOptions{Level: level})))

	srv, err := web.New(settings)
	if err != nil {
		slog.Error("初始化失败", "err", err)
		os.Exit(1)
	}
	srv.Start()
	defer srv.Close()

	httpServer := &http.Server{
		Addr:              fmt.Sprintf(":%d", settings.HTTPPort),
		Handler:           srv.Routes(),
		ReadHeaderTimeout: 10 * time.Second,
	}
	go func() {
		slog.Info("HTTP 服务已启动", "port", settings.HTTPPort)
		if err := httpServer.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			slog.Error("HTTP 服务异常", "err", err)
		}
	}()

	quit := make(chan os.Signal, 1)
	signal.Notify(quit, syscall.SIGINT, syscall.SIGTERM)
	<-quit
	slog.Info("正在关闭服务")
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_ = httpServer.Shutdown(ctx)
}
