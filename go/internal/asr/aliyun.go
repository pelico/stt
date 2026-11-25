package asr

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/gorilla/websocket"
)

// Aliyun is a lightweight, self-contained client for DashScope Paraformer
// real-time ASR over WebSocket. No vendor SDK required.
type Aliyun struct {
	cfg      Config
	timeouts Timeouts
}

func NewAliyun(cfg Config, t Timeouts) *Aliyun { return &Aliyun{cfg: cfg, timeouts: t} }
func (a *Aliyun) Name() string                 { return "aliyun" }

type wsMessage struct {
	Header struct {
		Action       string `json:"action"`
		TaskID       string `json:"task_id,omitempty"`
		Streaming    string `json:"streaming,omitempty"`
		Event        string `json:"event,omitempty"`
		ErrorCode    string `json:"error_code,omitempty"`
		ErrorMessage string `json:"error_message,omitempty"`
	} `json:"header"`
	Payload struct {
		TaskGroup  string `json:"task_group,omitempty"`
		Task       string `json:"task,omitempty"`
		Function   string `json:"function,omitempty"`
		Model      string `json:"model,omitempty"`
		Input      any    `json:"input,omitempty"`
		Parameters struct {
			Format                   string   `json:"format,omitempty"`
			SampleRate               int      `json:"sample_rate,omitempty"`
			LanguageHints            []string `json:"language_hints,omitempty"`
			DisfluencyRemovalEnabled bool     `json:"disfluency_removal_enabled,omitempty"`
		} `json:"parameters,omitempty"`
		Output struct {
			Results []struct {
				Text string `json:"text"`
			} `json:"results,omitempty"`
		} `json:"output,omitempty"`
	} `json:"payload"`
}

func newTaskID() string {
	b := make([]byte, 16)
	_, _ = rand.Read(b)
	h := hex.EncodeToString(b)
	return h[:8] + "-" + h[8:12] + "-" + h[12:16] + "-" + h[16:20] + "-" + h[20:]
}

func (a *Aliyun) Recognize(pcm []byte) (string, error) {
	apiKey := a.cfg.Str("dashscope_api_key")
	if apiKey == "" {
		return "", fmt.Errorf("未配置 DASHSCOPE_API_KEY")
	}
	model := a.cfg.Str("asr_model")
	if model == "" {
		model = "paraformer-realtime-v2"
	}
	lang := a.cfg.Str("asr_language")
	connectSec := a.timeouts.ConnectSec
	if connectSec < 1 {
		connectSec = 5
	}
	readSec := a.timeouts.ReadSec
	if readSec < 1 {
		readSec = 30
	}

	ctx, cancel := context.WithTimeout(context.Background(), time.Duration(connectSec+readSec)*time.Second)
	defer cancel()

	// Dashboard docs: wss://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/api-ws/v1/inference
	// The legacy domain remains valid and needs no workspace id.
	wsURL := "wss://dashscope.aliyuncs.com/api-ws/v1/inference"
	hdr := http.Header{}
	hdr.Set("Authorization", "Bearer "+apiKey)
	hdr.Set("User-Agent", "stt-go/1.0")

	dialer := websocket.Dialer{HandshakeTimeout: time.Duration(connectSec) * time.Second}
	conn, resp, err := dialer.DialContext(ctx, wsURL, hdr)
	if err != nil {
		if resp != nil {
			return "", fmt.Errorf("connect_timeout: websocket handshake HTTP %d: %w", resp.StatusCode, err)
		}
		return "", fmt.Errorf("connect_timeout: %w", err)
	}
	defer conn.Close()

	taskID := newTaskID()
	startMsg := wsMessage{}
	startMsg.Header.Action = "run-task"
	startMsg.Header.TaskID = taskID
	startMsg.Header.Streaming = "duplex"
	startMsg.Payload.TaskGroup = "audio"
	startMsg.Payload.Task = "asr"
	startMsg.Payload.Function = "recognition"
	startMsg.Payload.Model = model
	startMsg.Payload.Input = map[string]any{}
	startMsg.Payload.Parameters.Format = "pcm"
	startMsg.Payload.Parameters.SampleRate = 16000
	if lang != "" && lang != "auto" && strings.Contains(model, "v2") {
		startMsg.Payload.Parameters.LanguageHints = []string{lang}
	}
	if err := conn.WriteJSON(startMsg); err != nil {
		return "", fmt.Errorf("write run-task: %w", err)
	}

	// Wait for task-started before sending audio.
	var wg sync.WaitGroup
	startedCh := make(chan error, 1)
	textCh := make(chan string, 1)
	errCh := make(chan error, 1)
	wg.Add(1)
	go func() {
		defer wg.Done()
		text := ""
		for {
			_, data, err := conn.ReadMessage()
			if err != nil {
				if ctx.Err() != nil {
					return
				}
				select {
				case errCh <- fmt.Errorf("read: %w", err):
				default:
				}
				return
			}
			var msg wsMessage
			if err := json.Unmarshal(data, &msg); err != nil {
				continue
			}
			switch msg.Header.Event {
			case "task-started":
				select {
				case startedCh <- nil:
				default:
				}
			case "result-generated":
				for _, r := range msg.Payload.Output.Results {
					if r.Text != "" {
						text += r.Text
					}
				}
			case "task-finished":
				select {
				case textCh <- strings.TrimSpace(text):
				default:
				}
				return
			case "task-failed":
				e := msg.Header.ErrorMessage
				if e == "" {
					e = "unknown task-failed"
				}
				select {
				case errCh <- fmt.Errorf("asr error: %s", e):
				default:
				}
				return
			}
		}
	}()

	select {
	case err := <-startedCh:
		if err != nil {
			return "", err
		}
	case err := <-errCh:
		return "", err
	case <-ctx.Done():
		return "", fmt.Errorf("read_timeout: 等待 task-started 超时")
	}

	// Send raw binary PCM frames (no continue-task wrapper - Paraformer STT specific).
	chunk := 16000 / 10 * 2 // 100ms
	for i := 0; i < len(pcm); i += chunk {
		end := i + chunk
		if end > len(pcm) {
			end = len(pcm)
		}
		if err := conn.WriteMessage(websocket.BinaryMessage, pcm[i:end]); err != nil {
			return "", fmt.Errorf("write audio: %w", err)
		}
	}

	finishMsg := wsMessage{}
	finishMsg.Header.Action = "finish-task"
	finishMsg.Header.TaskID = taskID
	finishMsg.Header.Streaming = "duplex"
	if err := conn.WriteJSON(finishMsg); err != nil {
		return "", fmt.Errorf("write finish-task: %w", err)
	}

	select {
	case text := <-textCh:
		return text, nil
	case err := <-errCh:
		return "", err
	case <-ctx.Done():
		return "", fmt.Errorf("read_timeout: 等待识别结果超时")
	}
}
