package stream

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"io"
	"math"
	"os/exec"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"

	webrtcvad "github.com/godeps/go-webrtcvad"
	"github.com/pelico/stt-go/internal/store"
)

const (
	maxSegmentSec = 30
	stateInterval = 200 * time.Millisecond
)

// State is pushed to the UI for VAD tuning.
type State struct {
	Type           string  `json:"type"`
	Level          float64 `json:"level"`
	Speech         bool    `json:"speech"`
	Active         bool    `json:"active"`
	Segments       int     `json:"segments"`
	Dropped        int     `json:"dropped"`
	LastDurationMS int     `json:"last_duration_ms"`
	Queued         int     `json:"queued"`
	Backlog        int     `json:"backlog"`
	BacklogBytes   int64   `json:"backlog_bytes"`
	LagSec         int     `json:"lag_sec"`
	Mode           string  `json:"mode"`
	Stale          int     `json:"stale"`
	Enq            int     `json:"enq"`
	Deq            int     `json:"deq"`
	QFail          int     `json:"qfail"`
}

// Callbacks connects the streamer to the rest of the app.
type Callbacks struct {
	OnSpeechSegment   func(pcm []byte, wallISO string) bool
	OnVADState        func(State)
	OnAudioFrame      func(frame []byte)
	ShouldStream      func() bool
	OnStreamState     func(paused bool)
	OnRejectedSegment func(pcm []byte, reason string)
	Log               func(level, format string, args ...any)
	Config            ConfigProvider
	Queue             QueueProvider
}

type ConfigProvider interface {
	Str(key string) string
	Int(key string) int
	Float(key string) float64
	Bool(key string) bool
	ASRActive() bool
}

type QueueProvider interface {
	Put(pcm []byte, wallISO string, maxItems, maxMB int) (int, int, error)
	PeekBatch(maxItems, maxBytes int) ([]store.PendingItem, error)
	DeleteMany(ids []int64) error
	BumpMany(ids []int64, maxAttempts int) (int, error)
	Stats() (int, int64, float64)
	Count() int
	MaybeVacuum(thresholdMB int) bool
}

// Streamer pulls audio from RTSP via ffmpeg, detects speech with WebRTC VAD,
// and feeds segments into the recognition queue.
type Streamer struct {
	cb        Callbacks
	cancel    context.CancelFunc
	done      chan struct{}
	mu        sync.Mutex
	running   bool
	paused    bool
	restartCh chan struct{}

	vad        webrtcvad.VadInst
	vadReady   bool
	vadMode    int
	vadMu      sync.Mutex
	failStreak int

	rwTimeoutOK      bool
	wallclockOK      bool
	afftdnOK         bool
	optionFixApplied bool
	fallbackChanged  bool

	segments       int
	dropped        int
	lastDurationMS int
	staleDropped   int
	lastStaleLog   time.Time
	enqueuedTotal  int
	doneTotal      int
	failTotal      int

	rtQueue  chan rtItem
	backlog  int
	backlogB int64
	lagSec   float64
	wakeup   chan struct{}
	lastVac  time.Time
}

type rtItem struct {
	createdAt time.Time
	pcm       []byte
}

func New(cb Callbacks) *Streamer {
	return &Streamer{
		cb:          cb,
		restartCh:   make(chan struct{}, 1),
		rtQueue:     make(chan rtItem, 20),
		wakeup:      make(chan struct{}, 1),
		rwTimeoutOK: true,
		wallclockOK: true,
		afftdnOK:    true,
	}
}

func (s *Streamer) Mode() string { return s.cb.Config.Str("queue_mode") }
func (s *Streamer) Backlog() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.backlog
}
func (s *Streamer) BacklogBytes() int64 {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.backlogB
}
func (s *Streamer) LagSec() float64 {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.lagSec
}
func (s *Streamer) Paused() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.paused
}
func (s *Streamer) Running() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.running
}
func (s *Streamer) QueueTotals() map[string]int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return map[string]int{"enqueued": s.enqueuedTotal, "done": s.doneTotal, "failed": s.failTotal}
}

func (s *Streamer) Restart() {
	s.failStreak = 0
	s.cb.Log("INFO", "配置变更，重启拉流")
	select {
	case s.restartCh <- struct{}{}:
	default:
	}
	if s.cancel != nil {
		s.cancel()
	}
}

func (s *Streamer) Start(ctx context.Context) {
	ctx, cancel := context.WithCancel(ctx)
	s.mu.Lock()
	s.running = true
	s.cancel = cancel
	s.done = make(chan struct{})
	s.mu.Unlock()
	defer close(s.done)

	go s.consume(ctx)

	for ctx.Err() == nil {
		if s.cb.ShouldStream != nil && !s.cb.ShouldStream() {
			s.setPaused(true)
			select {
			case <-time.After(2 * time.Second):
			case <-ctx.Done():
				return
			}
			continue
		}
		s.setPaused(false)

		healthy := s.runOnce(ctx)
		if ctx.Err() != nil {
			return
		}
		var delay time.Duration
		if healthy {
			s.failStreak = 0
			delay = 5 * time.Second
		} else if s.fallbackChanged {
			s.fallbackChanged = false
			delay = 2 * time.Second
		} else {
			s.failStreak++
			secs := min(60, 5*(1<<min(s.failStreak-1, 4)))
			delay = time.Duration(secs) * time.Second
			s.cb.Log("WARN", "拉流连续失败 %d 次，%d 秒后重连", s.failStreak, secs)
		}
		select {
		case <-s.restartCh:
		case <-time.After(delay):
		case <-ctx.Done():
			return
		}
	}
}

func (s *Streamer) Stop() {
	s.mu.Lock()
	s.running = false
	cancel := s.cancel
	s.mu.Unlock()
	if cancel != nil {
		cancel()
	}
}

func (s *Streamer) setPaused(paused bool) {
	s.mu.Lock()
	changed := s.paused != paused
	s.paused = paused
	s.mu.Unlock()
	if changed && s.cb.OnStreamState != nil {
		s.cb.OnStreamState(paused)
	}
}

func (s *Streamer) getVAD() (webrtcvad.VadInst, error) {
	mode := s.cb.Config.Int("vad_mode")
	s.vadMu.Lock()
	defer s.vadMu.Unlock()
	if s.vadReady && mode == s.vadMode {
		return s.vad, nil
	}
	if s.vadReady {
		webrtcvad.Free(s.vad)
		s.vadReady = false
	}
	v := webrtcvad.Create()
	if err := webrtcvad.Init(v); err != nil {
		return v, err
	}
	if err := webrtcvad.SetMode(v, mode); err != nil {
		return v, err
	}
	s.vad, s.vadMode, s.vadReady = v, mode, true
	return v, nil
}

var optionNotFound = regexp.MustCompile(`option not found`)
var noSuchFilter = regexp.MustCompile(`no such filter.*afftdn`)

func (s *Streamer) logFFmpeg(line string) {
	line = strings.TrimSpace(line)
	if line == "" {
		return
	}
	lower := strings.ToLower(line)
	if optionNotFound.MatchString(lower) && !s.optionFixApplied {
		if s.rwTimeoutOK {
			s.rwTimeoutOK, s.fallbackChanged, s.optionFixApplied = false, true, true
			s.cb.Log("WARN", "当前 ffmpeg 不支持 -rw_timeout，下次重连自动去掉该参数")
		} else if s.wallclockOK {
			s.wallclockOK, s.fallbackChanged, s.optionFixApplied = false, true, true
			s.cb.Log("WARN", "当前 ffmpeg 不支持墙钟时间戳选项，下次重连自动去掉")
		}
	}
	if noSuchFilter.MatchString(lower) && s.afftdnOK {
		s.afftdnOK, s.fallbackChanged = false, true
		s.cb.Log("WARN", "当前 ffmpeg 无 afftdn 滤镜，轻度降噪自动关闭")
	}
	hints := []string{"error", "failed", "invalid", "unauthorized", "401", "403", "404", "refused", "timed out", "no route", "could not", "denied"}
	for _, h := range hints {
		if strings.Contains(lower, h) {
			if strings.Contains(lower, "non monotonically increasing dts") || strings.Contains(lower, "application provided invalid, non monotonically") {
				return
			}
			s.cb.Log("WARN", "ffmpeg: %s", line)
			return
		}
	}
}

func (s *Streamer) buildCommand(cfg ConfigProvider) ([]string, error) {
	url := cfg.Str("rtsp_url")
	highpass := cfg.Int("ffmpeg_highpass")
	denoise := cfg.Bool("ffmpeg_denoise")
	timeoutSec := max(5, cfg.Int("ffmpeg_timeout"))
	s.optionFixApplied = false

	cmd := []string{"-hide_banner", "-nostats", "-loglevel", "warning"}
	lower := strings.ToLower(strings.TrimSpace(url))
	isRTSP := strings.HasPrefix(lower, "rtsp://") || strings.HasPrefix(lower, "rtsps://")
	if isRTSP {
		cmd = append(cmd, "-rtsp_transport", "tcp")
	} else {
		cmd = append(cmd, "-re")
	}
	if s.rwTimeoutOK {
		cmd = append(cmd, "-rw_timeout", strconv.Itoa(timeoutSec*1_000_000))
	}
	if s.wallclockOK && isRTSP {
		cmd = append(cmd, "-use_wallclock_as_timestamps", "1", "-fflags", "+genpts")
	}
	cmd = append(cmd, "-i", url, "-vn")
	filters := []string{}
	if highpass > 0 {
		filters = append(filters, fmt.Sprintf("highpass=f=%d", highpass))
	}
	if denoise && s.afftdnOK {
		filters = append(filters, "afftdn=nr=12:nf=-40:tn=1")
	}
	if len(filters) > 0 {
		cmd = append(cmd, "-af", strings.Join(filters, ","))
	}
	cmd = append(cmd, "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", "-f", "s16le", "-")
	return cmd, nil
}

func (s *Streamer) runOnce(ctx context.Context) bool {
	cfg := s.cb.Config
	args, _ := s.buildCommand(cfg)
	ffmpegPath := cfg.Str("ffmpeg_path")
	if ffmpegPath == "" {
		ffmpegPath = "ffmpeg"
	}
	s.cb.Log("INFO", "启动 ffmpeg: %s %s", ffmpegPath, strings.Join(args, " "))
	cmd := exec.CommandContext(ctx, ffmpegPath, args...)
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		s.cb.Log("ERROR", "打开 ffmpeg stdout 失败: %v", err)
		return false
	}
	stderr, err := cmd.StderrPipe()
	if err != nil {
		s.cb.Log("ERROR", "打开 ffmpeg stderr 失败: %v", err)
		return false
	}
	if err := cmd.Start(); err != nil {
		s.cb.Log("ERROR", "启动 ffmpeg 失败: %v", err)
		return false
	}
	defer func() {
		if cmd.ProcessState == nil {
			_ = cmd.Process.Kill()
		}
		_ = cmd.Wait()
	}()

	go func() {
		br := bufio.NewReader(stderr)
		buf := make([]byte, 4096)
		pending := ""
		for {
			n, err := br.Read(buf)
			if n > 0 {
				pending += string(buf[:n])
				parts := regexp.MustCompile(`[\r\n]+`).Split(pending, -1)
				pending = parts[len(parts)-1]
				for _, line := range parts[:len(parts)-1] {
					s.logFFmpeg(line)
				}
			}
			if err != nil {
				break
			}
		}
		s.logFFmpeg(pending)
	}()

	sampleRate := 16000
	timeoutSec := max(5, cfg.Int("ffmpeg_timeout"))

	reader := bufio.NewReaderSize(stdout, 64*1024)
	buffer := []byte{}
	speechBuffer := []byte{}
	silenceCount := 0
	speechFrames := 0
	inSpeech := false
	gotData := false
	preroll := []byte{}
	recentLevels := []float64{}
	lastEmit := time.Time{}
	lastSpeech := false

	for ctx.Err() == nil {
		if s.cb.ShouldStream != nil && !s.cb.ShouldStream() {
			s.cb.Log("INFO", "离开拉流时段，断开 ffmpeg")
			break
		}
		frameMS := cfg.Int("vad_frame_ms")
		frameBytes := sampleRate * frameMS / 1000 * 2
		silenceLimit := cfg.Int("vad_silence_frames")
		minSpeech := cfg.Int("vad_min_speech_frames")
		minRatio := cfg.Float("vad_min_speech_ratio")
		rmsLimit := cfg.Float("vad_rms_dbfs")
		prerollLimit := max(frameBytes, sampleRate*2*cfg.Int("vad_preroll_ms")/1000)
		tailPadFrames := max(0, cfg.Int("vad_tail_pad_ms")/max(1, frameMS))

		readCtx, cancelRead := context.WithTimeout(ctx, time.Duration(timeoutSec)*time.Second)
		frame := make([]byte, frameBytes)
		n, err := readFull(readCtx, reader, frame)
		cancelRead()
		if err != nil {
			if errors.Is(err, context.DeadlineExceeded) {
				s.cb.Log("WARN", "ffmpeg %d 秒无音频数据，判定拉流卡死，准备重连", timeoutSec)
			} else if err != io.EOF {
				s.cb.Log("WARN", "读取 ffmpeg 音频失败: %v", err)
			}
			break
		}
		gotData = true
		data := frame[:n]
		buffer = append(buffer, data...)
		for len(buffer) >= frameBytes {
			f := buffer[:frameBytes]
			buffer = buffer[frameBytes:]

			level := frameDBFS(f)
			vad, verr := s.getVAD()
			vadSpeech := false
			if verr == nil {
				vadSpeech, verr = webrtcvad.Process(vad, sampleRate, f, frameBytes)
			}
			if verr != nil {
				vadSpeech = false
			}
			isSpeech := vadSpeech && level >= rmsLimit

			if isSpeech {
				if !inSpeech {
					inSpeech = true
					speechFrames = 0
					speechBuffer = append([]byte{}, tailSlice(preroll, prerollLimit)...)
					recentLevels = recentLevels[:0]
				}
				speechBuffer = append(speechBuffer, f...)
				speechFrames++
				silenceCount = 0
				recentLevels = append(recentLevels, level)
				lookback := max(1, 1500/max(1, frameMS))
				if len(recentLevels) > lookback {
					recentLevels = recentLevels[len(recentLevels)-lookback:]
				}
				if len(speechBuffer) > maxSegmentSec*sampleRate*2 {
					cutFrames := 0
					if len(recentLevels) >= 2 {
						quietIdx := 0
						for i, lv := range recentLevels {
							if lv < recentLevels[quietIdx] {
								quietIdx = i
							}
						}
						cutFrames = len(recentLevels) - 1 - quietIdx
					}
					cutBytes := cutFrames * frameBytes
					if cutBytes > len(speechBuffer) {
						cutBytes = 0
					}
					head := speechBuffer[:len(speechBuffer)-cutBytes]
					tail := speechBuffer[len(head):]
					durationMS := int(float64(len(head)) / float64(sampleRate*2) * 1000)
					s.segments++
					s.lastDurationMS = durationMS
					s.cb.Log("INFO", "语音段超过 %ds，强制切段: %.1fs", maxSegmentSec, float64(durationMS)/1000)
					s.enqueue(head)
					speechBuffer = append(append([]byte{}, tailSlice(preroll, prerollLimit)...), tail...)
					speechFrames -= cutFrames
					if speechFrames < 0 {
						speechFrames = 0
					}
					silenceCount = 0
					recentLevels = recentLevels[:0]
				}
			} else if inSpeech {
				silenceCount++
				speechBuffer = append(speechBuffer, f...)
				tooLong := len(speechBuffer) > maxSegmentSec*sampleRate*2
				if silenceCount >= silenceLimit || tooLong {
					if silenceCount > tailPadFrames {
						trim := (silenceCount - tailPadFrames) * frameBytes
						if trim > 0 && trim < len(speechBuffer) {
							speechBuffer = speechBuffer[:len(speechBuffer)-trim]
						}
					}
					durationMS := int(float64(len(speechBuffer)) / float64(sampleRate*2) * 1000)
					totalFrames := speechFrames + silenceCount
					ratio := float64(speechFrames) / float64(max(1, totalFrames))
					if speechFrames >= minSpeech && ratio >= minRatio {
						s.segments++
						s.lastDurationMS = durationMS
						s.enqueue(speechBuffer)
					} else {
						s.dropped++
						if s.cb.OnRejectedSegment != nil {
							s.cb.OnRejectedSegment(speechBuffer, "vad")
						}
					}
					inSpeech = false
					speechBuffer = []byte{}
					silenceCount = 0
					speechFrames = 0
					preroll = []byte{}
				}
			}
			if !inSpeech {
				preroll = append(preroll, f...)
				preroll = tailSlice(preroll, prerollLimit)
			}
			if s.cb.OnAudioFrame != nil {
				s.cb.OnAudioFrame(f)
			}
			now := time.Now()
			if s.cb.OnVADState != nil && (now.Sub(lastEmit) >= stateInterval || isSpeech != lastSpeech) {
				lastEmit, lastSpeech = now, isSpeech
				s.cb.OnVADState(State{
					Type: "vad", Level: math.Round(level*10) / 10, Speech: isSpeech, Active: inSpeech,
					Segments: s.segments, Dropped: s.dropped, LastDurationMS: s.lastDurationMS,
					Queued: len(s.rtQueue), Backlog: s.Backlog(), BacklogBytes: s.BacklogBytes(),
					LagSec: int(s.LagSec()), Mode: s.Mode(), Stale: s.staleDropped,
					Enq: s.enqueuedTotal, Deq: s.doneTotal, QFail: s.failTotal,
				})
			}
		}
	}
	return gotData
}

func readFull(ctx context.Context, r *bufio.Reader, buf []byte) (int, error) {
	total := 0
	for total < len(buf) {
		if err := ctx.Err(); err != nil {
			return total, err
		}
		n, err := r.Read(buf[total:])
		total += n
		if err != nil {
			return total, err
		}
		if total >= len(buf) {
			break
		}
	}
	return total, nil
}

func tailSlice(b []byte, limit int) []byte {
	if len(b) <= limit {
		return append([]byte{}, b...)
	}
	return append([]byte{}, b[len(b)-limit:]...)
}

func frameDBFS(frame []byte) float64 {
	var sum float64
	n := 0
	for i := 0; i+1 < len(frame); i += 2 {
		v := int16(frame[i]) | int16(frame[i+1])<<8
		sum += float64(v) * float64(v)
		n++
	}
	if n == 0 || sum <= 0 {
		return -100
	}
	rms := math.Sqrt(sum / float64(n))
	if rms <= 0 {
		return -100
	}
	return 20 * math.Log10(rms/32768)
}

func (s *Streamer) enqueue(pcm []byte) {
	cfg := s.cb.Config
	if s.Mode() == "durable" {
		wallISO := time.Now().UTC().Format("2006-01-02T15:04:05")
		count, dropped, err := s.cb.Queue.Put(pcm, wallISO, cfg.Int("queue_max_items"), cfg.Int("queue_max_mb"))
		if err != nil {
			s.mu.Lock()
			s.failTotal++
			s.mu.Unlock()
			s.cb.Log("ERROR", "[队列] 落盘失败，该段已丢失: %v", err)
			return
		}
		c, b, l := s.cb.Queue.Stats()
		s.mu.Lock()
		s.backlog, s.backlogB, s.lagSec = c, b, l
		s.enqueuedTotal++
		s.mu.Unlock()
		s.cb.Log("INFO", "[队列] 入队 %.1fs/%dKB | 积压 %d", float64(len(pcm))/32000, len(pcm)/1024, count)
		if dropped > 0 {
			s.cb.Log("WARN", "[队列] 已达上限，丢弃最旧 %d 段", dropped)
		}
		select {
		case s.wakeup <- struct{}{}:
		default:
		}
		return
	}
	if len(s.rtQueue) >= cap(s.rtQueue) {
		<-s.rtQueue
		s.mu.Lock()
		s.failTotal++
		s.mu.Unlock()
		s.cb.Log("WARN", "识别队列已满，丢弃最旧的一段")
	}
	s.rtQueue <- rtItem{createdAt: time.Now(), pcm: append([]byte{}, pcm...)}
	s.mu.Lock()
	s.enqueuedTotal++
	s.mu.Unlock()
}

func (s *Streamer) consume(ctx context.Context) {
	if s.Mode() == "durable" {
		s.consumeDurable(ctx)
	} else {
		s.consumeRealtime(ctx)
	}
}

func (s *Streamer) consumeDurable(ctx context.Context) {
	cfg := s.cb.Config
	q := s.cb.Queue
	for ctx.Err() == nil {
		if !cfg.Bool("asr_enabled") {
			select {
			case <-time.After(2 * time.Second):
			case <-ctx.Done():
				return
			}
			continue
		}
		gapMax := cfg.Float("asr_merge_gap_sec")
		maxSec := max(1, cfg.Int("asr_merge_max_sec"))
		maxBytes := maxSec * 16000 * 2
		rows, err := q.PeekBatch(16, maxBytes)
		if err != nil {
			s.cb.Log("ERROR", "读取待识别队列失败: %v", err)
			select {
			case <-time.After(time.Second):
			case <-ctx.Done():
				return
			}
			continue
		}
		if len(rows) == 0 {
			s.mu.Lock()
			s.backlog, s.backlogB, s.lagSec = 0, 0, 0
			s.mu.Unlock()
			if q.Count() == 0 && time.Since(s.lastVac) > 600*time.Second {
				s.lastVac = time.Now()
				if q.MaybeVacuum(64) {
					s.cb.Log("INFO", "[队列] 已清空，回收 pending.db 磁盘空间")
				}
			}
			select {
			case <-s.wakeup:
			case <-time.After(2 * time.Second):
			case <-ctx.Done():
				return
			}
			continue
		}
		batch := []store.PendingItem{rows[0]}
		if gapMax > 0 {
			prevEnd := rows[0].CreatedAt
			for _, row := range rows[1:] {
				start := row.CreatedAt - float64(len(row.PCM))/32000
				if start-prevEnd > gapMax {
					break
				}
				batch = append(batch, row)
				prevEnd = row.CreatedAt
			}
		}
		ids := make([]int64, len(batch))
		pcm := []byte{}
		attempts := batch[0].Attempts
		wallISO := batch[0].WallISO
		queuedSec := time.Now().Unix() - int64(batch[0].CreatedAt)
		for i, row := range batch {
			ids[i] = row.ID
			pcm = append(pcm, row.PCM...)
			if row.Attempts < attempts {
				attempts = row.Attempts
			}
		}
		ctxParts := fmt.Sprintf("等待%ds | 积压%d | 第%d次", queuedSec, s.Backlog(), attempts+1)
		if len(batch) > 1 {
			ctxParts = fmt.Sprintf("合并%d段 | %s", len(batch), ctxParts)
		}
		if s.cb.Config != nil {
			if m, ok := s.cb.Config.(interface{ SetQueueContext(string) }); ok {
				m.SetQueueContext(ctxParts)
			}
		}
		done := s.cb.OnSpeechSegment(pcm, wallISO)
		if done {
			s.mu.Lock()
			s.doneTotal += len(batch)
			s.mu.Unlock()
			_ = q.DeleteMany(ids)
		} else {
			maxAttempts := cfg.Int("queue_max_attempts")
			removed, _ := q.BumpMany(ids, maxAttempts)
			s.mu.Lock()
			s.failTotal += removed
			s.mu.Unlock()
			backoff := min(30, 1<<min(attempts, 4))
			if removed > 0 {
				s.cb.Log("WARN", "[队列] 第 %d 次识别失败已达上限，丢弃 %d 段", attempts+1, removed)
			} else {
				s.cb.Log("WARN", "[队列] 第 %d 次识别失败，保留该批，%d 秒后重试", attempts+1, backoff)
			}
			select {
			case <-time.After(time.Duration(backoff) * time.Second):
			case <-ctx.Done():
				return
			}
		}
		c, b, l := q.Stats()
		s.mu.Lock()
		s.backlog, s.backlogB, s.lagSec = c, b, l
		s.mu.Unlock()
	}
}

func (s *Streamer) consumeRealtime(ctx context.Context) {
	cfg := s.cb.Config
	for ctx.Err() == nil {
		select {
		case item := <-s.rtQueue:
			age := time.Since(item.createdAt).Seconds()
			s.mu.Lock()
			s.lagSec = age
			s.mu.Unlock()
			maxAge := cfg.Int("asr_max_queue_age")
			if maxAge > 0 && age > float64(maxAge) {
				s.mu.Lock()
				s.staleDropped++
				s.failTotal++
				s.mu.Unlock()
				if time.Since(s.lastStaleLog) >= 10*time.Second {
					s.lastStaleLog = time.Now()
					s.cb.Log("WARN", "实时模式：丢弃 %.0f 秒前的语音段", age)
				}
				continue
			}
			done := s.cb.OnSpeechSegment(item.pcm, "")
			s.mu.Lock()
			if done {
				s.doneTotal++
			} else {
				s.failTotal++
			}
			s.mu.Unlock()
		case <-ctx.Done():
			return
		}
	}
}

// min/max helpers (Go 1.21+ has builtins, these avoid version coupling).
func min(a, b int) int {
	if a < b {
		return a
	}
	return b
}
func max(a, b int) int {
	if a > b {
		return a
	}
	return b
}
