package asr

import "sync"

// SlowPredictor 由支持动态延迟预测的引擎实现（目前 openai 兼容引擎）。
// 预测耗时 = 音频时长 × 实时率 + 开销。
// rt 与开销均自学习：开销用滑动窗口线性回归（延迟对时长做 OLS，
// 斜率=rt、截距=开销），片段时长天然波动提供了分离两者的 leverage；
// 样本不足或时长过于集中时退回 EMA rt + 配置开销兜底。
type SlowPredictor interface {
	// Predict 预计总耗时（秒）。
	Predict(durationSec float64) float64
	// SlowThreshold 慢请求告警阈值：短音频不低于固定基线 slow_ms。
	SlowThreshold(durationSec float64) float64
	LearnedRT() float64
	// LearnedOverhead 当前生效的固定开销估计（回归截距或配置兜底值）。
	LearnedOverhead() float64
	// Observe 用一次成功请求修正模型（EMA rt + 回归窗口）。
	// 1s 以下的片段太短，不参与学习。
	Observe(latencySec, durationSec float64)
}

type sample struct{ d, l float64 }

const (
	warmupSamples = 5    // 冷启动样本数，攒够后 EMA rt 接管配置先验
	regMinSamples = 8    // 回归最少样本
	winMax        = 20   // 回归窗口：只保留最近 N 次，服务变快/变慢都能跟上
	regMinVarD    = 0.25 // 时长方差下限：太集中时斜率/截距不可分
	minSampleDur  = 1.0
)

// LatencyPredictor 是 SlowPredictor 的共享实现。
type LatencyPredictor struct {
	mu        sync.Mutex
	cfgRT     float64
	learnedRT float64
	samples   int // 成功样本数（EMA 用）
	win       []sample
	overhead  float64 // 配置固定开销（冷启动/回归不可用时的兜底）
	margin    float64
	slowSec   float64
}

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

// regress 对最近窗口做 OLS：延迟 = rt×时长 + 开销。
// 返回 ok=false 表示样本不够或时长过于集中，回归不可信。
func (p *LatencyPredictor) regress() (rt, oh float64, ok bool) {
	n := len(p.win)
	if n < regMinSamples {
		return 0, 0, false
	}
	var sd, sl, sdd, sdl float64
	for _, s := range p.win {
		sd += s.d
		sl += s.l
		sdd += s.d * s.d
		sdl += s.d * s.l
	}
	meanD := sd / float64(n)
	meanL := sl / float64(n)
	varD := sdd/float64(n) - meanD*meanD
	if varD < regMinVarD {
		return 0, 0, false
	}
	covD := sdl/float64(n) - meanD*meanL
	rt = covD / varD
	oh = meanL - rt*meanD
	if rt < 0.05 {
		rt = 0.05
	} else if rt > 5.0 {
		rt = 5.0
	}
	if oh < 0.2 {
		oh = 0.2
	} else if oh > 30.0 {
		oh = 30.0
	}
	return rt, oh, true
}

// params 当前生效的 (rt, 开销)：回归可用时用回归；否则冷启动期
// max(配置, 学习) 保守兜底，热身后 EMA rt + 配置开销。
func (p *LatencyPredictor) params() (float64, float64) {
	if rt, oh, ok := p.regress(); ok {
		return rt, oh
	}
	if p.samples >= warmupSamples {
		return p.learnedRT, p.overhead
	}
	return max(p.cfgRT, p.learnedRT), p.overhead
}

func (p *LatencyPredictor) Predict(d float64) float64 {
	p.mu.Lock()
	defer p.mu.Unlock()
	rt, oh := p.params()
	return d*rt + oh
}

func (p *LatencyPredictor) SlowThreshold(d float64) float64 {
	p.mu.Lock()
	defer p.mu.Unlock()
	rt, oh := p.params()
	return max(p.slowSec, (d*rt+oh)*p.margin)
}

// LearnedRT 返回 EMA 实时率（回归生效时它仍同步更新，供日志/健康页展示）。
func (p *LatencyPredictor) LearnedRT() float64 {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.learnedRT
}

// LearnedOverhead 返回当前生效的开销估计（回归截距优先，配置兜底）。
func (p *LatencyPredictor) LearnedOverhead() float64 {
	p.mu.Lock()
	defer p.mu.Unlock()
	if _, oh, ok := p.regress(); ok {
		return oh
	}
	return p.overhead
}

func (p *LatencyPredictor) Observe(latencySec, durationSec float64) {
	if durationSec < minSampleDur {
		return
	}
	p.mu.Lock()
	// 扣当前最优开销估计算瞬时 rt：回归截距比固定 3s 准，
	// 快服务延迟低于 3s 时扣固定值会算出负 rt，被钳到底部收敛偏高。
	_, oh, ok := p.regress()
	if !ok {
		oh = p.overhead
	}
	rt := (latencySec - oh) / durationSec
	if rt < 0.05 {
		rt = 0.05
	} else if rt > 5.0 {
		rt = 5.0
	}
	p.learnedRT = 0.7*p.learnedRT + 0.3*rt
	p.samples++
	p.win = append(p.win, sample{durationSec, latencySec})
	if len(p.win) > winMax {
		p.win = p.win[1:]
	}
	p.mu.Unlock()
}

// ReadTimeout 动态读超时 = max(配置读超时, 预测×容差 + 10s 余量)。
// 固定超时会被长合并块误杀——超时→重试→服务端单 worker 排队→更慢→
// 再超时形成活锁；服务端整体变慢时实测学习的实时率抬高预测，超时跟着放宽。
func (p *LatencyPredictor) ReadTimeout(baseSec int, d float64) int {
	return max(baseSec, int(p.Predict(d)*p.margin)+10)
}
