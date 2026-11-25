package filter

import (
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"
)

// NoiseReason returns a reason if text should be dropped, else empty.
func NoiseReason(text, ignoreWords string) string {
	t := strings.ToLower(strings.TrimSpace(text))
	if t == "" {
		return "empty"
	}
	runes := []rune(t)
	if len(runes) <= 1 {
		return "single-char"
	}
	for _, w := range strings.Split(ignoreWords, ",") {
		w = strings.ToLower(strings.TrimSpace(w))
		if w != "" && t == w {
			return "blacklist"
		}
	}
	return ""
}

// AudioDump saves rejected audio as WAV for debugging.
type AudioDump struct {
	mu      sync.Mutex
	dir     string
	maxKeep int
	running int
}

func NewAudioDump(dir string, maxKeep int) *AudioDump {
	return &AudioDump{dir: dir, maxKeep: maxKeep}
}

func (d *AudioDump) Dump(pcm []byte, reason string) string {
	d.mu.Lock()
	if d.running >= 3 {
		d.mu.Unlock()
		return ""
	}
	d.running++
	d.mu.Unlock()
	defer func() {
		d.mu.Lock()
		d.running--
		d.mu.Unlock()
	}()
	_ = os.MkdirAll(d.dir, 0o755)
	name := time.Now().Format("20060102-150405.000") + "-" + reason + ".wav"
	path := filepath.Join(d.dir, name)
	wav := pcmToWAV(pcm)
	if err := os.WriteFile(path, wav, 0o644); err != nil {
		return ""
	}
	d.prune()
	return name
}

func (d *AudioDump) prune() {
	entries, err := os.ReadDir(d.dir)
	if err != nil {
		return
	}
	files := []os.FileInfo{}
	for _, e := range entries {
		if info, err := e.Info(); err == nil && !info.IsDir() && strings.HasSuffix(info.Name(), ".wav") {
			files = append(files, info)
		}
	}
	if len(files) <= d.maxKeep {
		return
	}
	sort.Slice(files, func(i, j int) bool { return files[i].ModTime().Before(files[j].ModTime()) })
	for i := 0; i < len(files)-d.maxKeep; i++ {
		_ = os.Remove(filepath.Join(d.dir, files[i].Name()))
	}
}

func pcmToWAV(pcm []byte) []byte {
	buf := make([]byte, 0, 44+len(pcm))
	buf = append(buf, []byte("RIFF")...)
	buf = appendU32(buf, uint32(36+len(pcm)))
	buf = append(buf, []byte("WAVEfmt ")...)
	buf = appendU32(buf, 16)
	buf = appendU16(buf, 1)
	buf = appendU16(buf, 1)
	buf = appendU32(buf, 16000)
	buf = appendU32(buf, 32000)
	buf = appendU16(buf, 2)
	buf = appendU16(buf, 16)
	buf = append(buf, []byte("data")...)
	buf = appendU32(buf, uint32(len(pcm)))
	return append(buf, pcm...)
}

func appendU16(b []byte, v uint16) []byte {
	return append(b, byte(v), byte(v>>8))
}
func appendU32(b []byte, v uint32) []byte {
	return append(b, byte(v), byte(v>>8), byte(v>>16), byte(v>>24))
}
