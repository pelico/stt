package asr

import (
	"bytes"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"strings"
	"time"
)

const (
	tencentHost    = "asr.tencentcloudapi.com"
	tencentService = "asr"
	tencentVersion = "2019-06-14"
)

func hmacSHA256(key []byte, msg string) []byte {
	h := hmac.New(sha256.New, key)
	h.Write([]byte(msg))
	return h.Sum(nil)
}

func sha256Hex(s string) string {
	h := sha256.Sum256([]byte(s))
	return hex.EncodeToString(h[:])
}

// Tencent implements SentenceRecognition with TC3-HMAC-SHA256 signing.
type Tencent struct {
	cfg      Config
	timeouts Timeouts
	client   *http.Client
}

func NewTencent(cfg Config, t Timeouts) *Tencent {
	return &Tencent{
		cfg: cfg, timeouts: t,
		client: &http.Client{Timeout: time.Duration(t.ConnectSec+t.ReadSec) * time.Second},
	}
}
func (t *Tencent) Name() string { return "tencent" }

func (t *Tencent) serviceType() string {
	switch t.cfg.Str("asr_language") {
	case "en":
		return "16k_en"
	default:
		return "16k_zh"
	}
}

func (t *Tencent) Recognize(pcm []byte) (string, error) {
	secretID := t.cfg.Str("tencent_secret_id")
	secretKey := t.cfg.Str("tencent_secret_key")
	if secretID == "" || secretKey == "" {
		return "", fmt.Errorf("未配置腾讯云 SecretId/SecretKey")
	}
	region := t.cfg.Str("tencent_region")
	if region == "" {
		region = "ap-shanghai"
	}

	body := map[string]any{
		"EngSerViceType": t.serviceType(),
		"SourceType":     1,
		"VoiceFormat":    "pcm",
		"Data":           base64.StdEncoding.EncodeToString(pcm),
		"DataLen":        len(pcm),
		"ProjectId":      0,
		"SubServiceType": 2,
	}
	payload, err := json.Marshal(body)
	if err != nil {
		return "", err
	}
	payloadStr := string(payload)

	now := time.Now().UTC()
	timestamp := now.Unix()
	date := now.Format("2006-01-02")
	contentType := "application/json; charset=utf-8"
	canonicalHeaders := "content-type:" + contentType + "\nhost:" + tencentHost + "\n"
	signedHeaders := "content-type;host"
	canonicalRequest := "POST\n/\n\n" + canonicalHeaders + "\n" + signedHeaders + "\n" + sha256Hex(payloadStr)
	credentialScope := date + "/" + tencentService + "/tc3_request"
	stringToSign := "TC3-HMAC-SHA256\n" + strconv.FormatInt(timestamp, 10) + "\n" +
		credentialScope + "\n" + sha256Hex(canonicalRequest)
	secretDate := hmacSHA256([]byte("TC3"+secretKey), date)
	secretService := hmacSHA256(secretDate, tencentService)
	secretSigning := hmacSHA256(secretService, "tc3_request")
	signature := hex.EncodeToString(hmacSHA256(secretSigning, stringToSign))
	authorization := "TC3-HMAC-SHA256 Credential=" + secretID + "/" + credentialScope +
		", SignedHeaders=" + signedHeaders + ", Signature=" + signature

	req, err := http.NewRequest("POST", "https://"+tencentHost, bytes.NewReader(payload))
	if err != nil {
		return "", err
	}
	req.Header.Set("Authorization", authorization)
	req.Header.Set("Content-Type", contentType)
	req.Header.Set("Host", tencentHost)
	req.Header.Set("X-TC-Action", "SentenceRecognition")
	req.Header.Set("X-TC-Version", tencentVersion)
	req.Header.Set("X-TC-Timestamp", strconv.FormatInt(timestamp, 10))
	req.Header.Set("X-TC-Region", region)

	resp, err := t.client.Do(req)
	if err != nil {
		if strings.Contains(strings.ToLower(err.Error()), "timeout") {
			return "", fmt.Errorf("connect_timeout: %w", err)
		}
		return "", err
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
		Response struct {
			Result string `json:"Result"`
			Error  *struct {
				Code    string `json:"Code"`
				Message string `json:"Message"`
			} `json:"Error"`
		} `json:"Response"`
	}
	if err := json.Unmarshal(data, &parsed); err != nil {
		return "", fmt.Errorf("bad_response: %w", err)
	}
	if parsed.Response.Error != nil {
		return "", fmt.Errorf("腾讯云 ASR %s: %s", parsed.Response.Error.Code, parsed.Response.Error.Message)
	}
	return strings.TrimSpace(parsed.Response.Result), nil
}
