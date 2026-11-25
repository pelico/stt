package asr

import (
	"bytes"
	"encoding/binary"
	"fmt"
)

// Engine converts PCM audio to text.
type Engine interface {
	Name() string
	Recognize(pcm []byte) (string, error)
}

// PCMToWAV wraps raw s16le PCM in a WAV container.
func PCMToWAV(pcm []byte, sampleRate int) []byte {
	dataLen := len(pcm)
	buf := bytes.NewBuffer(make([]byte, 0, 44+dataLen))
	buf.WriteString("RIFF")
	_ = binary.Write(buf, binary.LittleEndian, uint32(36+dataLen))
	buf.WriteString("WAVE")
	buf.WriteString("fmt ")
	_ = binary.Write(buf, binary.LittleEndian, uint32(16))
	_ = binary.Write(buf, binary.LittleEndian, uint16(1))
	_ = binary.Write(buf, binary.LittleEndian, uint16(1))
	_ = binary.Write(buf, binary.LittleEndian, uint32(sampleRate))
	_ = binary.Write(buf, binary.LittleEndian, uint32(sampleRate*2))
	_ = binary.Write(buf, binary.LittleEndian, uint16(2))
	_ = binary.Write(buf, binary.LittleEndian, uint16(16))
	buf.WriteString("data")
	_ = binary.Write(buf, binary.LittleEndian, uint32(dataLen))
	buf.Write(pcm)
	return buf.Bytes()
}

type Config interface {
	Str(key string) string
	Int(key string) int
	Float(key string) float64
	Bool(key string) bool
}

type Timeouts struct {
	ConnectSec int
	ReadSec    int
}

func NewEngine(cfg Config, t Timeouts) (Engine, error) {
	switch cfg.Str("asr_engine") {
	case "aliyun":
		return NewAliyun(cfg, t), nil
	case "tencent":
		return NewTencent(cfg, t), nil
	case "openai":
		return NewOpenAI(cfg, t), nil
	default:
		return nil, fmt.Errorf("unknown asr engine: %s", cfg.Str("asr_engine"))
	}
}
