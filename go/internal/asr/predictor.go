package asr

import "sync"

// SlowPredictor 由支持动态延迟预测的引擎实现（目前 openai 兼容引擎）。
// 预测耗时 = 音频时长 × 实时率 + 固定开销；实时率用成功请求的实测值
// EMA 自学习，服务端变快/变慢时阈值自动跟随。
type SlowPredictor interface {
	// Predict 预计总耗时（秒）。取「配置模型」与「实测学习模型」的较大值：
	// 学习结果只抬高预测不降低，避免偶尔的快请求把阈值拉得过敏感。
	Predict(durationSec float64) float64
	// SlowThreshold 慢请求告警阈值：短音频不低于固定基线 slow_ms。
	SlowThreshold(durationSec float64) float64
	LearnedRT() float64
	// Observe 用一次成功请求修正实时率（EMA 平滑，限幅防异常值带偏）。
	// 3s 以下的片段扣掉固定开销后比例失真，不参与学习。
	Observe(latencySec, durationSec float64)
}

// LatencyPredictor 是 SlowPredictor 的共享实现。
type LatencyPredictor struct {
	mu        sync.Mutex
	cfgRT     float64
	learnedRT float64
	samples   int // 成功样本数；攒够 warmupSamples 后实测值接管预测
	overhead  float64
	margin    float64
	slowSec   float64
}

// warmupSamples 冷启动样本数：之前学习值只抬高不降低（max 保护），
// 但配置兜底 rt（1.5）远高于快服务实测值时预测虚高数倍。
// 攒够样本后改由实测 EMA 接管，配置值仅作冷启动先验。
const warmupSamples = 5

// NewLatencyPredictor 从配置构造；缺省值与 Python 版一致。
func NewLatencyPredictor(cfg Config) *LatencyPredictor {
	cfgRT := cfg.Float("asr_slow_rt_factor")
	if cfgRT < 0.1 {
		cfgRT = 1.5
	}
	overhead := cfg.Float("asr_slow_overhead_sec")
	if overhead < 0 {
		overhead = 3.0
	}
	margin := cfg.Float("asr_slow_margin")
	if margin < 1.0 {
		margin = 1.3
	}
	slowSec := float64(cfg.Int("asr_slow_ms")) / 1000
	if slowSec < 1 {
		slowSec = 10
	}
	return &LatencyPredictor{
		cfgRT: cfgRT, learnedRT: cfgRT,
		overhead: overhead, margin: margin, slowSec: slowSec,
	}
}

// effectiveRT 冷启动期用 max(配置, 学习) 保守兜底；样本攒够后由学习值接管，
// 服务真变慢时 EMA（0.3 权重）3~5 个请求内就会把 rt 抬回去。
func (p *LatencyPredictor) effectiveRT() float64 {
	if p.samples >= warmupSamples {
		return p.learnedRT
	}
	return max(p.cfgRT, p.learnedRT)
}

func (p *LatencyPredictor) Predict(d float64) float64 {
	p.mu.Lock()
	defer p.mu.Unlock()
	return d*p.effectiveRT() + p.overhead
}

func (p *LatencyPredictor) SlowThreshold(d float64) float64 {
	p.mu.Lock()
	defer p.mu.Unlock()
	return max(p.slowSec, (d*p.effectiveRT()+p.overhead)*p.margin)
}

func (p *LatencyPredictor) LearnedRT() float64 {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.learnedRT
}

func (p *LatencyPredictor) Observe(latencySec, durationSec float64) {
	if durationSec < 3.0 {
		return
	}
	rt := (latencySec - p.getOverhead()) / durationSec
	// 下限 0.05：快服务（SenseVoice 本地/内网）延迟常低于固定开销，
	// 扣完为负说明 rt 接近 0，钳太严（0.1）会让学习值收敛偏高。
	if rt < 0.05 {
		rt = 0.05
	} else if rt > 5.0 {
		rt = 5.0
	}
	p.mu.Lock()
	p.learnedRT = 0.7*p.learnedRT + 0.3*rt
	p.samples++
	p.mu.Unlock()
}

func (p *LatencyPredictor) getOverhead() float64 {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.overhead
}

// ReadTimeout 动态读超时 = max(配置读超时, 预测×容差 + 10s 余量)。
// 固定超时会被长合并块误杀——超时→重试→服务端单 worker 排队→更慢→
// 再超时形成活锁；服务端整体变慢时实测学习的实时率抬高预测，超时跟着放宽。
func (p *LatencyPredictor) ReadTimeout(baseSec int, d float64) int {
	return max(baseSec, int(p.Predict(d)*p.margin)+10)
}
