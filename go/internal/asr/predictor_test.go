package asr

import (
	"math"
	"testing"
)

// mapConfig 测试桩：实现 asr.Config 接口。
type mapConfig map[string]float64

func (m mapConfig) Str(k string) string { return "" }
func (m mapConfig) Int(k string) int    { return int(m[k]) }
func (m mapConfig) Float(k string) float64 {
	if v, ok := m[k]; ok {
		return v
	}
	return -1 // 触发 NewLatencyPredictor 的兜底默认值
}
func (m mapConfig) Bool(k string) bool { return false }

func testConfig() Config {
	return mapConfig{
		"asr_slow_rt_factor":    1.5,
		"asr_slow_overhead_sec": 3.0,
		"asr_slow_margin":       1.3,
		"asr_slow_ms":           10000,
	}
}

// 快服务场景：实测 rt≈0.15，冷启动后预测必须由学习值接管，
// 不能被配置兜底 1.5 永久抬高（回归：预测虚高 5~8 倍）。
func TestPredictorConvergesToLearnedRT(t *testing.T) {
	p := NewLatencyPredictor(testConfig())

	// 冷启动：预测走配置先验
	if got := p.Predict(8.0); math.Abs(got-15.0) > 0.01 {
		t.Fatalf("冷启动预测 = %v, 期望 15.0 (8*1.5+3)", got)
	}

	// 喂 5 个快请求样本：9s 音频 2s 返回 → rt=(2-3)/9 为负，钳到 0.05
	for i := 0; i < 5; i++ {
		p.Observe(2.0, 9.0)
	}

	got := p.Predict(8.0)
	want := 8.0*0.2937 + 3.0 // EMA: 1.5*0.7^5 + 0.05*(1-0.7^5) ≈ 0.2937
	if math.Abs(got-want) > 0.1 {
		t.Fatalf("热身后预测 = %v, 期望 ≈ %v（学习值接管，不受 cfgRT=1.5 影响）", got, want)
	}
	if got > 10 {
		t.Fatalf("热身后预测 %v 仍被配置值抬高的回归未修复", got)
	}
}

// 学习值只在冷启动期作下限保护，热身后服务变慢要能抬回去。
func TestPredictorAdaptsToSlowdown(t *testing.T) {
	p := NewLatencyPredictor(testConfig())
	for i := 0; i < 10; i++ {
		p.Observe(2.0, 9.0)
	}
	// 服务突然变慢：9s 音频 20s 返回 → rt=(20-3)/9≈1.89
	for i := 0; i < 5; i++ {
		p.Observe(20.0, 9.0)
	}
	if got := p.Predict(9.0); got < 10 {
		t.Fatalf("服务变慢后预测 = %v，未跟随抬升", got)
	}
}

// 3s 以下片段不参与学习，避免比例失真。
func TestPredictorIgnoresShortAudio(t *testing.T) {
	p := NewLatencyPredictor(testConfig())
	p.Observe(0.5, 1.5)
	if p.samples != 0 {
		t.Fatalf("短音频不应计入样本, samples = %d", p.samples)
	}
}

// 慢阈值不低于固定基线 slow_ms。
func TestSlowThresholdFloor(t *testing.T) {
	p := NewLatencyPredictor(testConfig())
	if got := p.SlowThreshold(1.0); got != 10 {
		t.Fatalf("短音频慢阈值 = %v, 期望 10", got)
	}
}
