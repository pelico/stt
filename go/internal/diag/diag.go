package diag

import (
	"fmt"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"
)

// LogEntry is one log line shown in the web log viewer.
type LogEntry struct {
	ID      int    `json:"id"`
	Time    string `json:"time"`
	Level   string `json:"level"`
	Message string `json:"message"`
	Summary string `json:"summary,omitempty"`
}

// LogBuffer is a bounded in-memory ring of log lines for the web UI.
type LogBuffer struct {
	mu       sync.RWMutex
	capacity int
	entries  []LogEntry
	nextID   int
	file     *os.File
	fileSize int64
	maxBytes int64
	backups  int
	path     string
}

func NewLogBuffer(capacity int, filePath string, maxMB, backups int) *LogBuffer {
	b := &LogBuffer{
		capacity: capacity,
		entries:  make([]LogEntry, 0, capacity),
		path:     filePath,
		maxBytes: int64(maxMB) * 1024 * 1024,
		backups:  backups,
	}
	if filePath != "" {
		_ = os.MkdirAll(filepath.Dir(filePath), 0o755)
		if f, err := os.OpenFile(filePath, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644); err == nil {
			b.file = f
			if st, err := f.Stat(); err == nil {
				b.fileSize = st.Size()
			}
		}
	}
	return b
}

func (b *LogBuffer) Add(level, format string, args ...any) {
	msg := fmt.Sprintf(format, args...)
	entry := LogEntry{
		Time:    time.Now().Format("15:04:05"),
		Level:   level,
		Message: msg,
	}
	b.mu.Lock()
	b.nextID++
	entry.ID = b.nextID
	b.entries = append(b.entries, entry)
	if len(b.entries) > b.capacity {
		copy(b.entries, b.entries[len(b.entries)-b.capacity:])
		b.entries = b.entries[:b.capacity]
	}
	if b.file != nil {
		line := fmt.Sprintf("%s [%s] %s\n", entry.Time, level, msg)
		n, err := io.WriteString(b.file, line)
		if err == nil {
			b.fileSize += int64(n)
			if b.fileSize > b.maxBytes {
				b.rotate()
			}
		}
	}
	b.mu.Unlock()
	slog.Debug(msg)
}

func (b *LogBuffer) rotate() {
	if b.file == nil {
		return
	}
	_ = b.file.Close()
	for i := b.backups - 1; i >= 1; i-- {
		src := fmt.Sprintf("%s.%d", b.path, i)
		dst := fmt.Sprintf("%s.%d", b.path, i+1)
		_ = os.Rename(src, dst)
	}
	_ = os.Rename(b.path, b.path+".1")
	f, err := os.OpenFile(b.path, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o644)
	if err == nil {
		b.file = f
		b.fileSize = 0
	}
}

func (b *LogBuffer) Since(after int, limit int) ([]LogEntry, int) {
	b.mu.RLock()
	defer b.mu.RUnlock()
	out := make([]LogEntry, 0, limit)
	for _, e := range b.entries {
		if e.ID > after {
			out = append(out, e)
			if len(out) >= limit {
				break
			}
		}
	}
	return out, b.nextID
}

func (b *LogBuffer) Clear() int {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.entries = b.entries[:0]
	return b.nextID
}

func (b *LogBuffer) Close() {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.file != nil {
		_ = b.file.Close()
		b.file = nil
	}
}

// ASRMetrics tracks ASR success/failure and latency.
type ASRMetrics struct {
	mu        sync.Mutex
	window    int
	next      int
	calls     int
	success   int
	failed    int
	latencies []float64
	last      float64
	p95       float64
	queueCtx  string
}

func NewASRMetrics(window int) *ASRMetrics {
	return &ASRMetrics{window: window, latencies: make([]float64, 0, window)}
}

func (m *ASRMetrics) NextID() int {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.next++
	return m.next
}

func (m *ASRMetrics) Record(ok bool, latency float64, slow bool) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.calls++
	if ok {
		m.success++
	} else {
		m.failed++
	}
	m.last = latency
	m.latencies = append(m.latencies, latency)
	if len(m.latencies) > m.window {
		m.latencies = m.latencies[len(m.latencies)-m.window:]
	}
	sorted := append([]float64{}, m.latencies...)
	sort.Float64s(sorted)
	if len(sorted) > 0 {
		idx := int(float64(len(sorted)-1) * 0.95)
		m.p95 = sorted[idx]
	}
}

func (m *ASRMetrics) SetQueueContext(s string) {
	m.mu.Lock()
	m.queueCtx = s
	m.mu.Unlock()
}

func (m *ASRMetrics) Snapshot() map[string]any {
	m.mu.Lock()
	defer m.mu.Unlock()
	return map[string]any{
		"calls":     m.calls,
		"success":   m.success,
		"failed":    m.failed,
		"last_sec":  fmt.Sprintf("%.2f", m.last),
		"p95_sec":   fmt.Sprintf("%.2f", m.p95),
		"queue_ctx": m.queueCtx,
	}
}

// DescribeException maps Go errors to actionable diagnostics (mirrors Python describe_exception).
func DescribeException(err error, statusCode int) (code, detail, hint string) {
	msg := err.Error()
	lower := strings.ToLower(msg)
	switch {
	case strings.Contains(lower, "timeout") || strings.Contains(lower, "deadline exceeded"):
		if strings.Contains(lower, "connect") || strings.Contains(lower, "dial") {
			return "connect_timeout", msg, "连接阶段未建立：查网络与百炼服务状态"
		}
		return "read_timeout", msg, "结果等待超时：检查服务端推理耗时、内存与 swap"
	case statusCode >= 500:
		return fmt.Sprintf("http_%d", statusCode), msg, "反代/上游不可用：检查网关与模型服务"
	case statusCode == 401 || statusCode == 403:
		return fmt.Sprintf("http_%d", statusCode), msg, "鉴权失败：检查 API Key 与 Authorization 透传"
	case statusCode == 404:
		return "http_404", msg, "路径不存在：检查 Base URL 与前缀"
	case strings.Contains(lower, "eof"):
		return "bad_response", msg, "返回了不完整响应：检查反代是否中途断开"
	}
	return "unknown", msg, "查看详细日志排查"
}
