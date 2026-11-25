package config

import (
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"
)

var timeRE = regexp.MustCompile(`^([01]\d|2[0-3]):[0-5]\d$`)

// Settings holds environment-only settings (mirror of Python config.Settings).
type Settings struct {
	RTSPURL            string
	ASREngine          string
	ASRModel           string
	ASRLanguage        string
	DashScopeAPIKey    string
	TencentSecretID    string
	TencentSecretKey   string
	TencentRegion      string
	OpenAIBaseURL      string
	OpenAIAPIKey       string
	OpenAIModel        string
	ASRTimeout         int
	ASRConnectTimeout  int
	ASRSlowMS          int
	ASRSlowRTFactor    float64
	ASRSlowOverheadSec float64
	ASRSlowMargin      float64
	LogFileEnabled     bool
	LogFilePath        string
	LogFileMaxMB       int
	LogFileBackups     int
	QueueMode          string
	QueueMaxItems      int
	QueueMaxMB         int
	QueueMaxAttempts   int
	ASRMergeGapSec     float64
	ASRMergeMaxSec     int
	ASRMaxQueueAge     int
	SampleRate         int
	VADEngine          string
	VADMode            int
	VADFrameMS         int
	VADSilenceFrames   int
	VADMinSpeechFrames int
	VADMinSpeechRatio  float64
	VADPrerollMS       int
	VADTailPadMS       int
	VADMaxSegmentSec   int
	VADRMSDBFS         float64
	FFmpegHighpass     int
	FFmpegDenoise      bool
	FilterNoiseText    bool
	FilterIgnoreWords  string
	SaveRejectedAudio  bool
	RejectedAudioMax   int
	ASREnabled         bool
	StreamEnabled      bool
	ASRScheduleEnabled bool
	ASRStartTime       string
	ASREndTime         string
	HTTPPort           int
	DBPath             string
	SettingsPath       string
	SaveAudio          bool
	AudioDir           string
	AccessToken        string
	AccessPassword     string
	FFmpegPath         string
	FFmpegTimeout      int
	LogLevel           string
}

func env(key, def string) string {
	if v, ok := os.LookupEnv(key); ok {
		return v
	}
	return def
}
func envInt(key string, def int) int {
	v, err := strconv.Atoi(env(key, ""))
	if err != nil {
		return def
	}
	return v
}
func envFloat(key string, def float64) float64 {
	v, err := strconv.ParseFloat(env(key, ""), 64)
	if err != nil {
		return def
	}
	return v
}
func envBool(key string, def bool) bool {
	v, ok := os.LookupEnv(key)
	if !ok {
		return def
	}
	return parseBool(v)
}
func parseBool(v string) bool {
	switch strings.ToLower(strings.TrimSpace(v)) {
	case "1", "true", "yes", "on":
		return true
	}
	return false
}

const defaultIgnoreWords = "嗯,啊,哦,呃,唉,哎,呵,哈,嘿,唔,噢,喂,呀,呐,呗,嘛,额," +
	"嗯嗯,啊啊,呵呵,哈哈,呵呵呵,不不不,四,三,二,一," +
	"here,shoes,foot,eye,i love you,thank you,thanks,bye,okay"

func LoadSettings() *Settings {
	return &Settings{
		RTSPURL:            env("RTSP_URL", ""),
		ASREngine:          env("ASR_ENGINE", "aliyun"),
		ASRModel:           env("ASR_MODEL", "paraformer-realtime-v2"),
		ASRLanguage:        env("ASR_LANGUAGE", "zh"),
		DashScopeAPIKey:    env("DASHSCOPE_API_KEY", ""),
		TencentSecretID:    env("TENCENT_SECRET_ID", ""),
		TencentSecretKey:   env("TENCENT_SECRET_KEY", ""),
		TencentRegion:      env("TENCENT_REGION", "ap-shanghai"),
		OpenAIBaseURL:      env("OPENAI_BASE_URL", ""),
		OpenAIAPIKey:       env("OPENAI_API_KEY", ""),
		OpenAIModel:        env("OPENAI_MODEL", "whisper-1"),
		ASRTimeout:         envInt("ASR_TIMEOUT", 45),
		ASRConnectTimeout:  envInt("ASR_CONNECT_TIMEOUT", 10),
		ASRSlowMS:          envInt("ASR_SLOW_MS", 10000),
		ASRSlowRTFactor:    envFloat("ASR_SLOW_RT_FACTOR", 1.5),
		ASRSlowOverheadSec: envFloat("ASR_SLOW_OVERHEAD_SEC", 3.0),
		ASRSlowMargin:      envFloat("ASR_SLOW_MARGIN", 1.3),
		LogFileEnabled:     envBool("LOG_FILE_ENABLED", true),
		LogFilePath:        env("LOG_FILE_PATH", "/data/logs/stt.log"),
		LogFileMaxMB:       envInt("LOG_FILE_MAX_MB", 5),
		LogFileBackups:     envInt("LOG_FILE_BACKUPS", 3),
		QueueMode:          env("QUEUE_MODE", "durable"),
		// 以下为实测调优后的预设值（广播/网络音频流场景），可在页面设置中覆盖
		QueueMaxItems:      envInt("QUEUE_MAX_ITEMS", 1000),
		QueueMaxMB:         envInt("QUEUE_MAX_MB", 128),
		QueueMaxAttempts:   envInt("QUEUE_MAX_ATTEMPTS", 5),
		ASRMergeGapSec:     envFloat("ASR_MERGE_GAP_SEC", 2.0),
		ASRMergeMaxSec:     envInt("ASR_MERGE_MAX_SEC", 10),
		ASRMaxQueueAge:     envInt("ASR_MAX_QUEUE_AGE", 10),
		SampleRate:         16000,
		VADEngine:          env("VAD_ENGINE", "webrtc"),
		VADMode:            envInt("VAD_MODE", 2),
		VADFrameMS:         envInt("VAD_FRAME_MS", 30),
		VADSilenceFrames:   envInt("VAD_SILENCE_FRAMES", 20),
		VADMinSpeechFrames: envInt("VAD_MIN_SPEECH_FRAMES", 8),
		VADMinSpeechRatio:  envFloat("VAD_MIN_SPEECH_RATIO", 0.5),
		VADPrerollMS:       envInt("VAD_PREROLL_MS", 300),
		VADTailPadMS:       envInt("VAD_TAIL_PAD_MS", 600),
		VADMaxSegmentSec:   envInt("VAD_MAX_SEGMENT_SEC", 20),
		VADRMSDBFS:         envFloat("VAD_RMS_DBFS", -27),
		FFmpegHighpass:     envInt("FFMPEG_HIGHPASS", 100),
		FFmpegDenoise:      envBool("FFMPEG_DENOISE", false),
		FilterNoiseText:    envBool("FILTER_NOISE_TEXT", true),
		FilterIgnoreWords:  env("FILTER_IGNORE_WORDS", defaultIgnoreWords),
		SaveRejectedAudio:  envBool("SAVE_REJECTED_AUDIO", false),
		RejectedAudioMax:   envInt("REJECTED_AUDIO_MAX", 50),
		ASREnabled:         envBool("ASR_ENABLED", true),
		StreamEnabled:      envBool("STREAM_ENABLED", true),
		ASRScheduleEnabled: envBool("ASR_SCHEDULE_ENABLED", false),
		ASRStartTime:       env("ASR_START_TIME", "08:00"),
		ASREndTime:         env("ASR_END_TIME", "22:00"),
		HTTPPort:           envInt("HTTP_PORT", 8000),
		DBPath:             env("DB_PATH", "/data/asr.db"),
		SettingsPath:       env("SETTINGS_PATH", "/data/settings.json"),
		SaveAudio:          envBool("SAVE_AUDIO", false),
		AudioDir:           env("AUDIO_DIR", "/data/audio"),
		AccessToken:        env("ACCESS_TOKEN", ""),
		AccessPassword:     env("ACCESS_PASSWORD", env("ACCESS_TOKEN", "")),
		FFmpegPath:         env("FFMPEG_PATH", "ffmpeg"),
		FFmpegTimeout:      envInt("FFMPEG_TIMEOUT", 20),
		LogLevel:           env("LOG_LEVEL", "INFO"),
	}
}

// Field types for runtime config (mirrors FIELD_TYPES).
type FieldType int

const (
	TypeString FieldType = iota
	TypeInt
	TypeFloat
	TypeBool
)

var fieldTypes = map[string]FieldType{
	"rtsp_url": TypeString, "asr_engine": TypeString, "asr_model": TypeString,
	"asr_language": TypeString, "dashscope_api_key": TypeString,
	"access_password": TypeString,
	"tencent_secret_id": TypeString, "tencent_secret_key": TypeString,
	"tencent_region": TypeString, "openai_base_url": TypeString,
	"openai_api_key": TypeString, "openai_model": TypeString,
	"vad_mode": TypeInt, "vad_frame_ms": TypeInt, "vad_silence_frames": TypeInt,
	"vad_min_speech_frames": TypeInt, "vad_min_speech_ratio": TypeFloat,
	"vad_preroll_ms": TypeInt, "vad_tail_pad_ms": TypeInt, "vad_max_segment_sec": TypeInt,
	"vad_rms_dbfs": TypeFloat, "vad_engine": TypeString,
	"asr_slow_ms": TypeInt, "asr_slow_rt_factor": TypeFloat,
	"asr_slow_overhead_sec": TypeFloat, "asr_slow_margin": TypeFloat,
	"ffmpeg_path":     TypeString,
	"ffmpeg_highpass": TypeInt, "ffmpeg_timeout": TypeInt,
	"asr_max_queue_age": TypeInt, "queue_mode": TypeString,
	"queue_max_items": TypeInt, "queue_max_mb": TypeInt, "queue_max_attempts": TypeInt,
	"asr_merge_gap_sec": TypeFloat, "asr_merge_max_sec": TypeInt,
	"filter_noise_text": TypeBool, "filter_ignore_words": TypeString,
	"save_rejected_audio": TypeBool, "asr_enabled": TypeBool,
	"asr_schedule_enabled": TypeBool, "asr_start_time": TypeString, "asr_end_time": TypeString,
	"stream_enabled": TypeBool,
	"ffmpeg_denoise": TypeBool,
}

var secretFields = map[string]bool{
	"dashscope_api_key": true, "tencent_secret_id": true,
	"tencent_secret_key": true, "openai_api_key": true,
	"access_password": true,
}

var Engines = []string{"aliyun", "tencent", "openai"}

func defaults(s *Settings) map[string]any {
	return map[string]any{
		"rtsp_url": s.RTSPURL, "asr_engine": s.ASREngine, "asr_model": s.ASRModel,
		"asr_language": s.ASRLanguage, "dashscope_api_key": s.DashScopeAPIKey,
		"access_password": s.AccessPassword,
		"tencent_secret_id": s.TencentSecretID, "tencent_secret_key": s.TencentSecretKey,
		"tencent_region": s.TencentRegion, "openai_base_url": s.OpenAIBaseURL,
		"openai_api_key": s.OpenAIAPIKey, "openai_model": s.OpenAIModel,
		"vad_mode": s.VADMode, "vad_frame_ms": s.VADFrameMS,
		"vad_silence_frames": s.VADSilenceFrames, "vad_min_speech_frames": s.VADMinSpeechFrames,
		"vad_min_speech_ratio": s.VADMinSpeechRatio, "vad_preroll_ms": s.VADPrerollMS,
		"vad_tail_pad_ms": s.VADTailPadMS, "vad_max_segment_sec": s.VADMaxSegmentSec,
		"vad_rms_dbfs": s.VADRMSDBFS, "vad_engine": s.VADEngine,
		"asr_slow_ms": s.ASRSlowMS, "asr_slow_rt_factor": s.ASRSlowRTFactor,
		"asr_slow_overhead_sec": s.ASRSlowOverheadSec, "asr_slow_margin": s.ASRSlowMargin,
		"ffmpeg_path":     s.FFmpegPath,
		"ffmpeg_highpass": s.FFmpegHighpass, "ffmpeg_denoise": s.FFmpegDenoise,
		"ffmpeg_timeout": s.FFmpegTimeout, "asr_max_queue_age": s.ASRMaxQueueAge,
		"queue_mode": s.QueueMode, "queue_max_items": s.QueueMaxItems,
		"queue_max_mb": s.QueueMaxMB, "queue_max_attempts": s.QueueMaxAttempts,
		"asr_merge_gap_sec": s.ASRMergeGapSec, "asr_merge_max_sec": s.ASRMergeMaxSec,
		"filter_noise_text": s.FilterNoiseText, "filter_ignore_words": s.FilterIgnoreWords,
		"save_rejected_audio": s.SaveRejectedAudio, "asr_enabled": s.ASREnabled,
		"asr_schedule_enabled": s.ASRScheduleEnabled, "asr_start_time": s.ASRStartTime,
		"stream_enabled": s.StreamEnabled,
		"asr_end_time": s.ASREndTime,
	}
}

func validate(key string, raw any) (any, error) {
	ft, ok := fieldTypes[key]
	if !ok {
		return nil, fmt.Errorf("unknown field %s", key)
	}
	switch ft {
	case TypeString:
		if raw == nil {
			return "", nil
		}
		sv := strings.TrimSpace(fmt.Sprint(raw))
		if key == "vad_engine" && sv != "webrtc" && sv != "energy" {
			return nil, fmt.Errorf("vad_engine 只能是 webrtc 或 energy")
		}
		return sv, nil
	case TypeInt:
		v, err := toInt(raw)
		if err != nil {
			return nil, fmt.Errorf("%s 必须是整数", key)
		}
		switch key {
		case "vad_mode":
			if v < 0 || v > 3 {
				return nil, fmt.Errorf("vad_mode 必须是 0-3")
			}
		case "vad_frame_ms":
			if v != 10 && v != 20 && v != 30 {
				return nil, fmt.Errorf("vad_frame_ms 只能是 10/20/30")
			}
		case "vad_silence_frames":
			if v < 1 || v > 200 {
				return nil, fmt.Errorf("vad_silence_frames 必须在 1-200")
			}
		case "vad_min_speech_frames":
			if v < 1 || v > 100 {
				return nil, fmt.Errorf("vad_min_speech_frames 必须在 1-100")
			}
		case "vad_rms_dbfs":
			if v < -80 || v > -10 {
				return nil, fmt.Errorf("vad_rms_dbfs 必须在 -80 ~ -10")
			}
		case "vad_preroll_ms":
			if v < 0 || v > 1000 {
				return nil, fmt.Errorf("vad_preroll_ms 必须在 0 ~ 1000")
			}
		case "vad_tail_pad_ms":
			if v < 0 || v > 2000 {
				return nil, fmt.Errorf("vad_tail_pad_ms 必须在 0 ~ 2000")
			}
		case "vad_max_segment_sec":
			if v < 5 || v > 60 {
				return nil, fmt.Errorf("vad_max_segment_sec 必须在 5 ~ 60")
			}
		case "ffmpeg_highpass":
			if v < 0 || v > 8000 {
				return nil, fmt.Errorf("ffmpeg_highpass 必须在 0 ~ 8000")
			}
		case "ffmpeg_timeout":
			if v < 5 || v > 120 {
				return nil, fmt.Errorf("ffmpeg_timeout 必须在 5 ~ 120")
			}
		case "asr_max_queue_age":
			if v < 0 || v > 300 {
				return nil, fmt.Errorf("asr_max_queue_age 必须在 0 ~ 300")
			}
		case "queue_max_items":
			if v < 10 || v > 10000 {
				return nil, fmt.Errorf("queue_max_items 必须在 10 ~ 10000")
			}
		case "queue_max_mb":
			if v < 1 || v > 1024 {
				return nil, fmt.Errorf("queue_max_mb 必须在 1 ~ 1024")
			}
		case "queue_max_attempts":
			if v < 0 || v > 100 {
				return nil, fmt.Errorf("queue_max_attempts 必须在 0 ~ 100")
			}
		case "asr_merge_max_sec":
			if v < 10 || v > 120 {
				return nil, fmt.Errorf("asr_merge_max_sec 必须在 10 ~ 120")
			}
		case "asr_slow_ms":
			if v < 1000 || v > 60000 {
				return nil, fmt.Errorf("asr_slow_ms 必须在 1000 ~ 60000")
			}
		}
		return v, nil
	case TypeFloat:
		v, err := toFloat(raw)
		if err != nil {
			return nil, fmt.Errorf("%s 必须是数字", key)
		}
		switch key {
		case "vad_min_speech_ratio":
			if v < 0 || v > 1 {
				return nil, fmt.Errorf("vad_min_speech_ratio 必须在 0 ~ 1")
			}
		case "asr_merge_gap_sec":
			if v < 0 || v > 10 {
				return nil, fmt.Errorf("asr_merge_gap_sec 必须在 0 ~ 10")
			}
		case "asr_slow_rt_factor":
			if v < 0.1 || v > 5 {
				return nil, fmt.Errorf("asr_slow_rt_factor 必须在 0.1 ~ 5")
			}
		case "asr_slow_overhead_sec":
			if v < 0 || v > 30 {
				return nil, fmt.Errorf("asr_slow_overhead_sec 必须在 0 ~ 30")
			}
		case "asr_slow_margin":
			if v < 1.0 || v > 3.0 {
				return nil, fmt.Errorf("asr_slow_margin 必须在 1.0 ~ 3.0")
			}
		}
		return v, nil
	case TypeBool:
		if b, ok := raw.(bool); ok {
			return b, nil
		}
		return parseBool(fmt.Sprint(raw)), nil
	}
	return raw, nil
}

func toInt(v any) (int, error) {
	switch x := v.(type) {
	case int:
		return x, nil
	case float64:
		return int(x), nil
	case json.Number:
		return strconv.Atoi(string(x))
	case string:
		return strconv.Atoi(strings.TrimSpace(x))
	default:
		return strconv.Atoi(fmt.Sprint(v))
	}
}
func toFloat(v any) (float64, error) {
	switch x := v.(type) {
	case float64:
		return x, nil
	case int:
		return float64(x), nil
	case json.Number:
		return strconv.ParseFloat(string(x), 64)
	case string:
		return strconv.ParseFloat(strings.TrimSpace(x), 64)
	default:
		return strconv.ParseFloat(fmt.Sprint(v), 64)
	}
}

// RuntimeConfig is a thread-safe runtime config with JSON persistence and hot-update listeners.
type RuntimeConfig struct {
	mu        sync.RWMutex
	path      string
	values    map[string]any
	listeners []func([]string)
}

func NewRuntimeConfig(s *Settings) *RuntimeConfig {
	c := &RuntimeConfig{path: s.SettingsPath, values: defaults(s)}
	c.load()
	return c
}

func (c *RuntimeConfig) load() {
	b, err := os.ReadFile(c.path)
	if err != nil {
		return
	}
	var data map[string]any
	dec := json.NewDecoder(strings.NewReader(string(b)))
	dec.UseNumber()
	if err := dec.Decode(&data); err != nil {
		slog.Warn("读取配置文件失败", "path", c.path, "err", err)
		return
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	for k, v := range data {
		if _, ok := fieldTypes[k]; !ok {
			continue
		}
		if nv, err := validate(k, v); err == nil {
			c.values[k] = nv
		} else {
			slog.Warn("忽略非法配置", "key", k, "err", err)
		}
	}
	slog.Info("已加载运行时配置", "path", c.path)
}

func (c *RuntimeConfig) save() {
	c.mu.RLock()
	data := make(map[string]any, len(c.values))
	for k, v := range c.values {
		data[k] = v
	}
	c.mu.RUnlock()
	b, err := json.MarshalIndent(data, "", "  ")
	if err != nil {
		return
	}
	_ = os.MkdirAll(filepath.Dir(c.path), 0o755)
	tmp := c.path + ".tmp"
	if err := os.WriteFile(tmp, b, 0o644); err == nil {
		_ = os.Rename(tmp, c.path)
	}
}

func (c *RuntimeConfig) Get(key string) any {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.values[key]
}
func (c *RuntimeConfig) Str(key string) string {
	v, _ := c.Get(key).(string)
	return v
}
func (c *RuntimeConfig) Int(key string) int {
	v, _ := toInt(c.Get(key))
	return v
}
func (c *RuntimeConfig) Float(key string) float64 {
	v, _ := toFloat(c.Get(key))
	return v
}
func (c *RuntimeConfig) Bool(key string) bool {
	b, _ := c.Get(key).(bool)
	return b
}

func (c *RuntimeConfig) Subscribe(fn func([]string)) {
	c.mu.Lock()
	c.listeners = append(c.listeners, fn)
	c.mu.Unlock()
}

func mask(v string) string {
	if v == "" {
		return ""
	}
	r := []rune(v)
	if len(r) <= 8 {
		return "****"
	}
	return string(r[:4]) + "****" + string(r[len(r)-4:])
}

func (c *RuntimeConfig) Snapshot(maskSecrets bool) map[string]any {
	c.mu.RLock()
	out := make(map[string]any, len(c.values)*2)
	for k, v := range c.values {
		out[k] = v
	}
	c.mu.RUnlock()
	if maskSecrets {
		for k := range secretFields {
			v, _ := out[k].(string)
			out[k] = mask(v)
			out[k+"_set"] = v != ""
		}
	}
	return out
}

func (c *RuntimeConfig) Update(patch map[string]any) ([]string, error) {
	changed := []string{}
	c.mu.Lock()
	for key, raw := range patch {
		ft, ok := fieldTypes[key]
		if !ok {
			continue
		}
		v, err := validate(key, raw)
		if err != nil {
			c.mu.Unlock()
			return nil, err
		}
		if ft == TypeString {
			s, _ := v.(string)
			if secretFields[key] && (s == "" || strings.Contains(s, "****")) {
				// 密钥字段留空/掩码=不修改；但访问口令是用户主动输入的鉴权值，
				// 允许显式清空（clear_access_password=true）以关闭口令。
				if key != "access_password" || patch["clear_access_password"] != true {
					continue
				}
			}
		}
		if !equalValue(c.values[key], v) {
			c.values[key] = v
			changed = append(changed, key)
		}
	}
	c.mu.Unlock()
	if len(changed) > 0 {
		c.save()
		slog.Info("配置已更新", "fields", strings.Join(changed, ", "))
	}
	c.mu.RLock()
	listeners := append([]func([]string){}, c.listeners...)
	c.mu.RUnlock()
	for _, fn := range listeners {
		fn(changed)
	}
	return changed, nil
}

func equalValue(a, b any) bool {
	return fmt.Sprintf("%v", a) == fmt.Sprintf("%v", b)
}

// InSchedule reports whether current local time falls in configured window.
func (c *RuntimeConfig) InSchedule() bool {
	if !c.Bool("asr_schedule_enabled") {
		return true
	}
	start, end := c.Str("asr_start_time"), c.Str("asr_end_time")
	if !timeRE.MatchString(start) || !timeRE.MatchString(end) {
		return true
	}
	toMin := func(t string) int {
		var h, m int
		fmt.Sscanf(t, "%d:%d", &h, &m)
		return h*60 + m
	}
	s, e := toMin(start), toMin(end)
	now := currentMinutes()
	if s == e {
		return true
	}
	if s < e {
		return now >= s && now < e
	}
	return now >= s || now < e
}

func (c *RuntimeConfig) ASRActive() bool {
	return c.Bool("asr_enabled") && c.InSchedule()
}

func (c *RuntimeConfig) EngineReady() bool {
	switch c.Str("asr_engine") {
	case "aliyun":
		return c.Str("dashscope_api_key") != ""
	case "tencent":
		return c.Str("tencent_secret_id") != "" && c.Str("tencent_secret_key") != ""
	case "openai":
		return c.Str("openai_base_url") != "" && c.Str("openai_api_key") != ""
	}
	return false
}

func currentMinutes() int {
	t := time.Now()
	return t.Hour()*60 + t.Minute()
}
