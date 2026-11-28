package asr

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"mime/multipart"
	"net/http"
	"strings"
	"time"
)

// OpenAI implements /audio/transcriptions (Whisper-compatible).
type OpenAI struct {
	cfg      Config
	timeouts Timeouts
	client   *http.Client
	pred     *LatencyPredictor
}

func NewOpenAI(cfg Config, t Timeouts) *OpenAI {
	return &OpenAI{
		cfg: cfg, timeouts: t,
		// 不用 client 级固定超时：读超时按预测值逐请求动态计算，防活锁
		client: &http.Client{},
		pred:   NewLatencyPredictor(cfg),
	}
}
func (o *OpenAI) Name() string { return "openai" }

func (o *OpenAI) Recognize(pcm []byte) (string, error) {
	baseURL := strings.TrimRight(o.cfg.Str("openai_base_url"), "/")
	apiKey := o.cfg.Str("openai_api_key")
	if baseURL == "" || apiKey == "" {
		return "", fmt.Errorf("未配置 OPENAI_BASE_URL / OPENAI_API_KEY")
	}
	model := o.cfg.Str("openai_model")
	if model == "" {
		model = "whisper-1"
	}
	lang := o.cfg.Str("asr_language")
	if lang == "auto" {
		lang = ""
	}

	var body bytes.Buffer
	mp := multipart.NewWriter(&body)
	_ = mp.WriteField("model", model)
	if lang != "" {
		_ = mp.WriteField("language", lang)
	}
	_ = mp.WriteField("response_format", "json")
	part, err := mp.CreateFormFile("file", "audio.wav")
	if err != nil {
		return "", err
	}
	if _, err := part.Write(PCMToWAV(pcm, 16000)); err != nil {
		return "", err
	}
	_ = mp.Close()

	req, err := http.NewRequest("POST", baseURL+"/audio/transcriptions", &body)
	if err != nil {
		return "", err
	}
	req.Header.Set("Authorization", "Bearer "+apiKey)
	req.Header.Set("Content-Type", mp.FormDataContentType())

	// 动态读超时 = max(配置读超时, 预测耗时×容差 + 10s 余量)；
	// 固定超时会误杀长合并块（超时→重试→服务端排队→更慢→活锁）
	readSec := o.pred.ReadTimeout(o.timeouts.ReadSec, float64(len(pcm))/32000)
	ctx, cancel := context.WithTimeout(context.Background(),
		time.Duration(o.timeouts.ConnectSec+readSec)*time.Second)
	defer cancel()
	req = req.WithContext(ctx)

	resp, err := o.client.Do(req)
	if err != nil {
		if strings.Contains(strings.ToLower(err.Error()), "timeout") {
			return "", fmt.Errorf("read_timeout: %w", err)
		}
		return "", fmt.Errorf("connect_timeout: %w", err)
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(resp.Body)
	if err != nil {
		return "", err
	}
	if resp.StatusCode != 200 {
		return "", fmt.Errorf("http_%d: %s", resp.StatusCode, string(data))
	}
	var parsed struct {
		Text string `json:"text"`
	}
	if err := json.Unmarshal(data, &parsed); err != nil {
		return "", fmt.Errorf("bad_response: %w", err)
	}
	return strings.TrimSpace(parsed.Text), nil
}

// SlowPredictor 接口实现：供 Server 记录预测日志与慢请求告警。
func (o *OpenAI) Predict(d float64) float64        { return o.pred.Predict(d) }
func (o *OpenAI) SlowThreshold(d float64) float64  { return o.pred.SlowThreshold(d) }
func (o *OpenAI) LearnedRT() float64               { return o.pred.LearnedRT() }
func (o *OpenAI) LearnedOverhead() float64         { return o.pred.LearnedOverhead() }
func (o *OpenAI) Observe(lat, d float64)           { o.pred.Observe(lat, d) }
