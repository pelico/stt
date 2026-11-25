package web

import (
	"context"
	"embed"
	"encoding/json"
	"fmt"
	"io/fs"
	"net/http"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/gorilla/websocket"

	"github.com/pelico/stt-go/internal/asr"
	"github.com/pelico/stt-go/internal/config"
	"github.com/pelico/stt-go/internal/diag"
	"github.com/pelico/stt-go/internal/filter"
	"github.com/pelico/stt-go/internal/store"
	"github.com/pelico/stt-go/internal/stream"
)

//go:embed static/*
var staticFS embed.FS

// ConfigView adapts RuntimeConfig to the interfaces used by stream/asr packages.
type ConfigView struct {
	*config.RuntimeConfig
	Metrics *diag.ASRMetrics
}

func (c *ConfigView) SetQueueContext(s string) { c.Metrics.SetQueueContext(s) }

// Server orchestrates all components and serves HTTP/WebSocket.
type Server struct {
	settings *config.Settings
	cfg      *ConfigView
	history  *store.HistoryDB
	pending  *store.PendingStore
	rtspHist *store.RTSPHistory
	logs     *diag.LogBuffer
	metrics  *diag.ASRMetrics
	dumps    *filter.AudioDump
	auth     *AuthGuard
	streamer *stream.Streamer

	mu              sync.RWMutex
	clients         map[*WSClient]bool
	asrFailures     int
	asrBlockedUntil time.Time
	engine          asr.Engine
	engineSig       string
	engineMu        sync.Mutex

	lastSlowRT   float64
	lastRTUpdate time.Time
	ctx          context.Context
	cancel       context.CancelFunc
}

func New(s *config.Settings) (*Server, error) {
	cfg := config.NewRuntimeConfig(s)
	logs := diag.NewLogBuffer(1000, s.LogFilePath, s.LogFileMaxMB, s.LogFileBackups)
	metrics := diag.NewASRMetrics(50)

	history, err := store.OpenHistory(s.DBPath)
	if err != nil {
		return nil, fmt.Errorf("open history db: %w", err)
	}
	pendingDB := strings.TrimSuffix(s.DBPath, ".db") + "-pending.db"
	pending, err := store.OpenPending(pendingDB)
	if err != nil {
		return nil, fmt.Errorf("open pending db: %w", err)
	}
	rtspHist := store.NewRTSPHistory("/data/rtsp_history.json")

	srv := &Server{
		settings:   s,
		cfg:        &ConfigView{RuntimeConfig: cfg, Metrics: metrics},
		history:    history,
		pending:    pending,
		rtspHist:   rtspHist,
		logs:       logs,
		metrics:    metrics,
		dumps:      filter.NewAudioDump(s.AudioDir+"/rejected", s.RejectedAudioMax),
		auth:       NewAuthGuard(s.AccessToken),
		clients:    make(map[*WSClient]bool),
		lastSlowRT: s.ASRSlowRTFactor,
	}
	srv.ctx, srv.cancel = context.WithCancel(context.Background())
	return srv, nil
}

func (s *Server) log(level, format string, args ...any) {
	s.logs.Add(level, format, args...)
}

func (s *Server) Start() {
	s.rtspHist.Add(s.cfg.Str("rtsp_url"))
	s.cfg.Subscribe(func(changed []string) {
		for _, k := range changed {
			if k == "rtsp_url" {
				s.log("INFO", "RTSP 地址已变更，重启拉流")
				if s.streamer != nil {
					s.streamer.Restart()
				}
			}
			if k == "queue_mode" {
				s.log("WARN", "识别队列模式已变更，需重启容器后生效")
			}
		}
	})
	s.streamer = stream.New(stream.Callbacks{
		OnSpeechSegment:   s.onSpeechSegment,
		OnVADState:        func(st stream.State) { s.broadcast(st) },
		OnAudioFrame:      func(frame []byte) { s.broadcastAudio(frame) },
		ShouldStream:      func() bool { return s.cfg.InSchedule() },
		OnStreamState:     func(paused bool) { s.broadcast(map[string]any{"type": "stream", "paused": paused}) },
		OnRejectedSegment: func(pcm []byte, reason string) { s.dumpRejected(pcm, "vad-"+reason) },
		Log:               s.log,
		Config:            s.cfg,
		Queue:             s.pending,
	})
	go s.streamer.Start(s.ctx)
	s.log("INFO", "音频拉流已启动，RTSP: %s，引擎: %s", s.cfg.Str("rtsp_url"), s.cfg.Str("asr_engine"))
}

func (s *Server) Close() {
	s.cancel()
	if s.streamer != nil {
		s.streamer.Stop()
	}
	s.logs.Close()
	_ = s.history.Close()
	_ = s.pending.Close()
}

func (s *Server) broadcast(msg any) {
	s.mu.RLock()
	clients := make([]*WSClient, 0, len(s.clients))
	for c := range s.clients {
		clients = append(clients, c)
	}
	s.mu.RUnlock()
	for _, c := range clients {
		c.Offer(msg)
	}
}

func (s *Server) broadcastAudio(frame []byte) {
	s.mu.RLock()
	clients := make([]*WSClient, 0, len(s.clients))
	for c := range s.clients {
		clients = append(clients, c)
	}
	s.mu.RUnlock()
	for _, c := range clients {
		if c.IsListening() {
			c.Offer(append([]byte{}, frame...))
		}
	}
}

func (s *Server) dumpRejected(pcm []byte, reason string) {
	if !s.cfg.Bool("save_rejected_audio") {
		return
	}
	if name := s.dumps.Dump(pcm, reason); name != "" {
		s.log("INFO", "已留存被过滤音频: %s（%s）", name, reason)
	}
}

// getEngine returns a cached engine, rebuilt when its signature changes.
func (s *Server) getEngine() asr.Engine {
	s.engineMu.Lock()
	defer s.engineMu.Unlock()
	sig := strings.Join([]string{
		s.cfg.Str("asr_engine"), s.cfg.Str("dashscope_api_key"), s.cfg.Str("asr_model"),
		s.cfg.Str("asr_language"), s.cfg.Str("tencent_secret_id"), s.cfg.Str("tencent_secret_key"),
		s.cfg.Str("tencent_region"), s.cfg.Str("openai_base_url"), s.cfg.Str("openai_api_key"),
		s.cfg.Str("openai_model"),
	}, "|")
	if s.engine != nil && s.engineSig == sig {
		return s.engine
	}
	t := asr.Timeouts{ConnectSec: s.settings.ASRConnectTimeout, ReadSec: s.settings.ASRTimeout}
	e, err := asr.NewEngine(s.cfg, t)
	if err != nil {
		s.log("ERROR", "创建 ASR 引擎失败: %v", err)
		return nil
	}
	s.engine, s.engineSig = e, sig
	return e
}

func (s *Server) onSpeechSegment(pcm []byte, wallISO string) bool {
	started := time.Now()
	cfg := s.cfg
	durationSec := len(pcm) / 32000
	backlog := 0
	if s.streamer != nil {
		backlog = s.streamer.Backlog()
	}
	if !cfg.Bool("asr_enabled") {
		return true
	}
	s.mu.RLock()
	blocked := time.Now().Before(s.asrBlockedUntil)
	s.mu.RUnlock()
	if blocked {
		s.log("DEBUG", "识别服务熔断中，跳过本段")
		return false
	}

	engine := s.getEngine()
	if engine == nil {
		return false
	}
	text, err := engine.Recognize(pcm)
	latency := time.Since(started).Seconds()
	reqID := s.metrics.NextID()
	if err != nil {
		s.mu.Lock()
		s.asrFailures++
		fails := s.asrFailures
		s.mu.Unlock()
		code, detail, hint := diag.DescribeException(err, 0)
		s.metrics.Record(false, latency, false)
		s.log("ERROR", "[ASR] req#%d ✗ %s after %.2fs audio=%.1fs/%dKB | %s | 建议: %s",
			reqID, code, latency, float64(len(pcm))/32000, len(pcm)/1024, detail, hint)
		if fails >= 3 {
			backoffSec := min(60, 5*(1<<min(fails-3, 4)))
			s.mu.Lock()
			s.asrBlockedUntil = time.Now().Add(time.Duration(backoffSec) * time.Second)
			s.mu.Unlock()
			s.log("WARN", "识别服务连续失败，熔断 %d 秒后探活", backoffSec)
			s.broadcast(map[string]any{"type": "asr_state", "blocked": true, "retry_in": backoffSec})
		}
		return false
	}

	s.mu.Lock()
	if s.asrFailures > 0 {
		s.asrFailures = 0
		s.asrBlockedUntil = time.Time{}
		s.mu.Unlock()
		s.log("INFO", "识别服务已恢复")
		s.broadcast(map[string]any{"type": "asr_state", "blocked": false})
	} else {
		s.mu.Unlock()
	}

	slow := s.isSlow(latency, float64(len(pcm))/32000)
	s.metrics.Record(true, latency, slow)
	if slow {
		s.log("WARN", "[ASR] req#%d 慢请求 %.2fs（预测见诊断）", reqID, latency)
	}
	s.log("INFO", "[ASR] req#%d ✓ %.2fs audio=%.1fs/%dKB | %d字: %s",
		reqID, latency, float64(len(pcm))/32000, len(pcm)/1024, len([]rune(text)), truncate(text, 80))

	if text == "" {
		return true
	}
	if cfg.Bool("filter_noise_text") {
		if reason := filter.NoiseReason(text, cfg.Str("filter_ignore_words")); reason != "" {
			s.log("INFO", "丢弃噪音识别结果 (%s): %s", reason, text)
			s.dumpRejected(pcm, "text-"+reason)
			return true
		}
	}

	capturedAt := time.Now().UTC()
	if wallISO != "" {
		if t, err := time.Parse("2006-01-02T15:04:05", wallISO); err == nil {
			capturedAt = t
		}
	}
	rec, err := s.history.Save(text, durationSec, cfg.Str("asr_engine"), capturedAt)
	var timestamp string
	var id any
	if err != nil {
		s.log("ERROR", "保存数据库失败: %v", err)
		timestamp = capturedAt.Format("2006-01-02T15:04:05")
	} else {
		timestamp, id = rec.Timestamp, rec.ID
	}
	_ = backlog
	s.broadcast(map[string]any{
		"type": "transcript", "text": text, "timestamp": timestamp,
		"duration_sec": durationSec, "id": id,
	})
	return true
}

func (s *Server) isSlow(latency, audioSec float64) bool {
	if latency*1000 <= float64(s.settings.ASRSlowMS) {
		return false
	}
	s.mu.Lock()
	rt := s.lastSlowRT
	s.mu.Unlock()
	predicted := audioSec*rt + s.settings.ASRSlowOverheadSec
	return predicted > 0 && latency > predicted*s.settings.ASRSlowMargin
}

func truncate(s string, n int) string {
	r := []rune(strings.ReplaceAll(s, "\n", " "))
	if len(r) > n {
		return string(r[:n])
	}
	return string(r)
}

func min(a, b int) int {
	if a < b {
		return a
	}
	return b
}

func (s *Server) Routes() http.Handler {
	mux := http.NewServeMux()
	assets, _ := fs.Sub(staticFS, "static")
	fileServer := http.FileServer(http.FS(assets))

	mux.HandleFunc("/api/login", s.handleLogin)
	mux.HandleFunc("/api/logout", s.handleLogout)
	mux.HandleFunc("/api/transcripts", s.handleTranscripts)
	mux.HandleFunc("/api/settings", s.handleSettings)
	mux.HandleFunc("/api/rtsp-history", s.handleRTSPHistory)
	mux.HandleFunc("/api/logs", s.handleLogs)
	mux.HandleFunc("/api/health", s.handleHealth)
	mux.HandleFunc("/ws", s.handleWS)
	mux.HandleFunc("/login", s.handleLoginPage)
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/" {
			fileServer.ServeHTTP(w, r)
			return
		}
		w.Header().Set("Cache-Control", "no-cache")
		b, _ := staticFS.ReadFile("static/index.html")
		_, _ = w.Write(b)
	})
	return s.authMiddleware(mux)
}

func (s *Server) authMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		path := r.URL.Path
		if !s.auth.Enabled() || path == "/login" || path == "/api/login" || path == "/api/logout" || path == "/api/health" || path == "/favicon.ico" {
			next.ServeHTTP(w, r)
			return
		}
		if s.auth.authorized(r) {
			next.ServeHTTP(w, r)
			return
		}
		if strings.HasPrefix(path, "/api/") {
			writeJSON(w, 401, map[string]any{"detail": "unauthorized"})
			return
		}
		http.Redirect(w, r, "/login?next="+urlQueryEscape(path), http.StatusTemporaryRedirect)
	})
}

var upgrader = websocket.Upgrader{
	ReadBufferSize: 4096, WriteBufferSize: 4096,
	CheckOrigin: func(r *http.Request) bool { return true },
}

func (s *Server) handleWS(w http.ResponseWriter, r *http.Request) {
	if !s.auth.WSAuthorized(r) {
		w.WriteHeader(http.StatusUnauthorized)
		return
	}
	conn, err := upgrader.Upgrade(w, r, nil)
	if err != nil {
		return
	}
	client := NewWSClient(conn)
	s.mu.Lock()
	s.clients[client] = true
	count := len(s.clients)
	s.mu.Unlock()
	s.log("INFO", "WebSocket 客户端连接，当前 %d 个", count)
	if s.streamer != nil && s.streamer.Paused() {
		client.Offer(map[string]any{"type": "stream", "paused": true})
	}
	defer func() {
		s.mu.Lock()
		delete(s.clients, client)
		n := len(s.clients)
		s.mu.Unlock()
		client.Close()
		s.log("INFO", "WebSocket 客户端断开，剩余 %d 个", n)
	}()
	for {
		_, data, err := conn.ReadMessage()
		if err != nil {
			return
		}
		var msg struct {
			Type string `json:"type"`
			On   bool   `json:"on"`
		}
		if json.Unmarshal(data, &msg) == nil && msg.Type == "listen" {
			client.SetListening(msg.On)
		}
	}
}

func (s *Server) handleLogin(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeJSON(w, 405, map[string]any{"detail": "method not allowed"})
		return
	}
	var body struct {
		Password string `json:"password"`
	}
	_ = json.NewDecoder(r.Body).Decode(&body)
	ok, locked, retry := s.auth.CheckLogin(body.Password)
	if locked {
		w.Header().Set("Retry-After", strconv.Itoa(retry))
		writeJSON(w, 429, map[string]any{"detail": "尝试次数过多，请稍后再试"})
		return
	}
	if !ok {
		writeJSON(w, 401, map[string]any{"detail": "口令错误"})
		return
	}
	s.auth.setSessionCookie(w, r)
	writeJSON(w, 200, map[string]any{"ok": true})
}

func (s *Server) handleLogout(w http.ResponseWriter, r *http.Request) {
	http.SetCookie(w, &http.Cookie{Name: cookieName, Value: "", Path: "/", MaxAge: -1})
	writeJSON(w, 200, map[string]any{"ok": true})
}

func (s *Server) handleLoginPage(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Cache-Control", "no-cache")
	b, _ := staticFS.ReadFile("static/login.html")
	_, _ = w.Write(b)
}

func (s *Server) handleTranscripts(w http.ResponseWriter, r *http.Request) {
	switch r.Method {
	case http.MethodDelete:
		if err := s.history.Clear(); err != nil {
			writeJSON(w, 500, map[string]any{"detail": err.Error()})
			return
		}
		s.log("INFO", "已清空识别历史")
		writeJSON(w, 200, map[string]any{"ok": true})
		return
	case http.MethodGet:
		// handled below
	default:
		writeJSON(w, 405, map[string]any{"detail": "method not allowed"})
		return
	}
	q := r.URL.Query()
	limit, _ := strconv.Atoi(q.Get("limit"))
	offset, _ := strconv.Atoi(q.Get("offset"))
	items, total, err := s.history.List(limit, offset, q.Get("keyword"))
	if err != nil {
		writeJSON(w, 500, map[string]any{"detail": err.Error()})
		return
	}
	writeJSON(w, 200, map[string]any{"items": items, "total": total})
}

func (s *Server) handleSettings(w http.ResponseWriter, r *http.Request) {
	switch r.Method {
	case http.MethodGet:
		writeJSON(w, 200, map[string]any{
			"values":           s.cfg.Snapshot(true),
			"engines":          config.Engines,
			"engine_ready":     s.cfg.EngineReady(),
			"asr_active":       s.cfg.ASRActive(),
			"access_token_set": s.settings.AccessToken != "",
		})
	case http.MethodPost:
		var patch map[string]any
		dec := json.NewDecoder(r.Body)
		dec.UseNumber()
		if err := dec.Decode(&patch); err != nil {
			writeJSON(w, 400, map[string]any{"detail": "invalid JSON"})
			return
		}
		changed, err := s.cfg.Update(patch)
		if err != nil {
			writeJSON(w, 400, map[string]any{"detail": err.Error()})
			return
		}
		for k := range patch {
			if k == "rtsp_url" {
				s.rtspHist.Add(s.cfg.Str("rtsp_url"))
			}
		}
		writeJSON(w, 200, map[string]any{
			"changed": changed, "values": s.cfg.Snapshot(true),
			"engine_ready": s.cfg.EngineReady(), "asr_active": s.cfg.ASRActive(),
		})
	default:
		writeJSON(w, 405, map[string]any{"detail": "method not allowed"})
	}
}

func (s *Server) handleRTSPHistory(w http.ResponseWriter, r *http.Request) {
	switch r.Method {
	case http.MethodGet:
		writeJSON(w, 200, map[string]any{"items": s.rtspHist.List()})
	case http.MethodDelete:
		writeJSON(w, 200, map[string]any{"items": s.rtspHist.Remove(r.URL.Query().Get("url"))})
	default:
		writeJSON(w, 405, map[string]any{"detail": "method not allowed"})
	}
}

func (s *Server) handleLogs(w http.ResponseWriter, r *http.Request) {
	switch r.Method {
	case http.MethodGet:
		after, _ := strconv.Atoi(r.URL.Query().Get("after"))
		limit, _ := strconv.Atoi(r.URL.Query().Get("limit"))
		if limit < 1 || limit > 1000 {
			limit = 300
		}
		items, lastID := s.logs.Since(after, limit)
		writeJSON(w, 200, map[string]any{"items": items, "last_id": lastID})
	case http.MethodDelete:
		writeJSON(w, 200, map[string]any{"cleared": true, "last_id": s.logs.Clear()})
	default:
		writeJSON(w, 405, map[string]any{"detail": "method not allowed"})
	}
}

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	backlog, backlogBytes, lag := 0, int64(0), 0.0
	var queueMode string
	var totals map[string]int
	var streamRunning, streamPaused bool
	if s.streamer != nil {
		backlog, backlogBytes, lag = s.streamer.Backlog(), s.streamer.BacklogBytes(), s.streamer.LagSec()
		queueMode, totals = s.streamer.Mode(), s.streamer.QueueTotals()
		streamRunning, streamPaused = s.streamer.Running(), s.streamer.Paused()
	}
	s.mu.RLock()
	blocked := time.Now().Before(s.asrBlockedUntil)
	s.mu.RUnlock()
	logFile := ""
	if s.settings.LogFileEnabled {
		logFile = s.settings.LogFilePath
	}
	writeJSON(w, 200, map[string]any{
		"status": "ok", "rtsp_url": s.cfg.Str("rtsp_url"), "asr_engine": s.cfg.Str("asr_engine"),
		"engine_ready": s.cfg.EngineReady(), "asr_enabled": s.cfg.Bool("asr_enabled"),
		"asr_schedule_enabled": s.cfg.Bool("asr_schedule_enabled"),
		"asr_start_time":       s.cfg.Str("asr_start_time"), "asr_end_time": s.cfg.Str("asr_end_time"),
		"asr_active": s.cfg.ASRActive(), "asr_blocked": blocked,
		"stream_paused": streamPaused, "stream_running": streamRunning,
		"queue_mode": queueMode, "queue_backlog": backlog,
		"queue_backlog_bytes": backlogBytes, "queue_lag_sec": lag,
		"queue_totals": totals, "diagnostics": s.metrics.Snapshot(),
		"asr_timeout": s.settings.ASRTimeout, "asr_connect_timeout": s.settings.ASRConnectTimeout,
		"asr_slow_ms": s.settings.ASRSlowMS, "log_file": logFile,
	})
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func urlQueryEscape(s string) string {
	return strings.ReplaceAll(strings.ReplaceAll(s, "/", "%2F"), "?", "%3F")
}

var _ = os.Getenv
