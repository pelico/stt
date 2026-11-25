'use strict';

const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmtTime = ts => { if (!ts) return ''; const d = new Date(ts.length === 19 ? ts+'Z' : ts); return isNaN(d) ? '' : d.toLocaleTimeString('zh-CN',{hour12:false}); };
const fmtLag = s => s == null ? '—' : s < 60 ? Math.round(s)+'s' : s < 3600 ? Math.round(s/60)+'m' : (s/3600).toFixed(1)+'h';

let ws = null, listening = false, liveItems = 0, logAfter = 0, logPaused = false, logTimer = null;
let settingsCache = null, historyPage = 1, historyPageSize = 20, historyTotal = 0, historyKeyword = '';
let audioCtx = null, audioBuf = [], audioPlaying = false, audioStarted = false, nextPlayTime = 0;
const AUDIO_SR = 16000, AUDIO_CHUNK = 1920, AUDIO_PREROLL = 6400;

async function api(path, opts={}) {
  const r = await fetch(path, {headers:{'Content-Type':'application/json'}, ...opts});
  if (r.status === 401) { location.href = '/login'; throw new Error('unauthorized'); }
  const data = await r.json().catch(()=>({}));
  if (!r.ok) throw new Error(data.detail || ('HTTP '+r.status));
  return data;
}

function toast(msg) {
  const t = $('toast'); t.textContent = msg; t.classList.add('show');
  clearTimeout(t._timer); t._timer = setTimeout(()=>t.classList.remove('show'), 2200);
}

function setStatus(text, level='') {
  $('status').textContent = text;
  $('status-dot').className = 'dot' + (level ? ' '+level : '');
}

function connectWS() {
  const token = new URLSearchParams(location.search).get('token') || '';
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws${token ? '?token='+encodeURIComponent(token) : ''}`);
  ws.onopen = () => { setStatus('已连接', 'ok'); fetchHealth(); };
  ws.onclose = () => { setStatus('重连中…', 'warn'); setTimeout(connectWS, 2000); };
  ws.onerror = () => setStatus('连接异常', 'err');
  ws.onmessage = ev => {
    if (ev.data instanceof ArrayBuffer) {
      if (listening && audioCtx) {
        const i16 = new Int16Array(ev.data);
        for (let i=0;i<i16.length;i++) audioBuf.push(i16[i]);
      }
      return;
    }
    if (ev.data instanceof Blob) { ev.data.arrayBuffer().then(b=>{ if(listening&&audioCtx){const i16=new Int16Array(b);for(let i=0;i<i16.length;i++)audioBuf.push(i16[i]);} }); return; }
    let m; try { m = JSON.parse(ev.data); } catch { return; }
    if (m.type === 'transcript') onTranscript(m);
    else if (m.type === 'vad') onVAD(m);
    else if (m.type === 'asr_state') { setStatus(m.blocked ? '识别熔断' : '已连接', m.blocked ? 'err' : 'ok'); toast(m.blocked ? '识别服务连续失败，熔断 '+m.retry_in+'s' : '识别服务已恢复'); }
    else if (m.type === 'stream') { $('stream-state').textContent = m.paused ? '时段外挂起' : '运行中'; }
  };
}

function onTranscript(m) {
  const box = $('live-content');
  if (box.querySelector('.transcript-empty')) box.innerHTML = '';
  const div = document.createElement('div');
  div.className = 'line new';
  div.innerHTML = `<span class="time">${esc(fmtTime(m.timestamp))}</span><span class="text">${esc(m.text)}</span><span class="tag">${esc(m.duration_sec||0)}s</span>`;
  // newest first: prepend instead of append, keep viewport at top
  box.insertBefore(div, box.firstChild);
  box.scrollTop = 0;
  while (box.children.length > 30) box.removeChild(box.lastChild);
  liveItems++; $('live-count').textContent = liveItems+' 条';
  setTimeout(()=>div.classList.remove('new'), 1500);
  // keep the history panel in sync only when viewing the newest page unfiltered
  if (historyPage === 1 && !historyKeyword) {
    const h = $('history-list');
    if (h.innerText.includes('暂无')) h.innerHTML = '';
    h.insertAdjacentHTML('afterbegin', transcriptItem(m));
    while (h.children.length > historyPageSize) h.removeChild(h.lastChild);
  }
}

function transcriptItem(m) {
  return `<div class="history-item"><div class="meta"><span>${esc(fmtTime(m.timestamp))}</span><span>${esc(m.engine||'')}</span><span>${esc(m.duration_sec||0)}s</span></div>${esc(m.text)}</div>`;
}

// 电平条、门限块、拖动条共用同一个 dBFS 坐标系，避免拖动后门限块与电平条错位。
const RMS_MIN = -80, RMS_MAX = -10;
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const rmsPct = v => clamp((v - RMS_MIN) / (RMS_MAX - RMS_MIN) * 100, 0, 100);

function onVAD(m) {
  const pct = rmsPct(m.level);
  $('vad-level').style.width = pct+'%';
  $('level_dbfs').textContent = m.level.toFixed(1);
  $('vad-badge').textContent = m.active ? '语音中' : (m.speech ? '检测到语音' : '静音');
  $('vad-badge').style.color = m.active ? 'var(--green)' : 'var(--muted)';
  $('vad-stats').textContent = m.segments+' / '+m.dropped;
  $('m-lag').textContent = fmtLag(m.lag_sec);
  $('queue-mode').textContent = m.mode === 'durable' ? '完整优先' : '实时优先';
  $('m-backlog').textContent = m.backlog;
  $('m-totals').textContent = (m.enq||0)+' / '+(m.deq||0);
}

function thresholdPct(v) { return rmsPct(v); }
function syncThresholdMarker() {
  const v = parseInt($('live_rms').value, 10);
  const el = $('vad-threshold');
  if (el) el.style.left = thresholdPct(v)+'%';
  const wrap = $('meter-wrap');
  if (wrap) {
    wrap.setAttribute('aria-valuenow', String(v));
    wrap.setAttribute('aria-valuetext', v+' dBFS');
  }
}
function syncThresholdLabel() {
  const v = $('live_rms').value;
  $('live_rms_val').textContent = v+' dBFS';
  syncThresholdMarker();
}
let thresholdDragging = false, thresholdCommitTimer = null;
function setThresholdFromClientX(clientX) {
  const wrap = $('meter-wrap');
  const r = wrap.getBoundingClientRect();
  if (!r.width) return;
  const pct = clamp((clientX - r.left) / r.width, 0, 1);
  $('live_rms').value = String(Math.round(RMS_MIN + pct * (RMS_MAX - RMS_MIN)));
  syncThresholdLabel();
}
function initThresholdDrag() {
  const wrap = $('meter-wrap');
  if (!wrap) return;
  wrap.addEventListener('pointerdown', e => {
    e.preventDefault();
    thresholdDragging = true;
    wrap.setPointerCapture?.(e.pointerId);
    setThresholdFromClientX(e.clientX);
  });
  wrap.addEventListener('pointermove', e => {
    if (thresholdDragging) setThresholdFromClientX(e.clientX);
  });
  const finish = () => {
    if (!thresholdDragging) return;
    thresholdDragging = false;
    commitThreshold();
  };
  wrap.addEventListener('pointerup', finish);
  wrap.addEventListener('pointercancel', finish);
  wrap.addEventListener('keydown', e => {
    const step = e.key === 'PageUp' || e.key === 'PageDown' ? 5 : 1;
    let v = parseInt($('live_rms').value, 10);
    if (e.key === 'ArrowRight' || e.key === 'ArrowUp' || e.key === 'PageUp') v += step;
    else if (e.key === 'ArrowLeft' || e.key === 'ArrowDown' || e.key === 'PageDown') v -= step;
    else if (e.key === 'Home') v = RMS_MIN;
    else if (e.key === 'End') v = RMS_MAX;
    else return;
    e.preventDefault();
    $('live_rms').value = String(clamp(v, RMS_MIN, RMS_MAX));
    syncThresholdLabel();
    clearTimeout(thresholdCommitTimer);
    thresholdCommitTimer = setTimeout(commitThreshold, 150);
  });
}
async function commitThreshold() {
  const value = parseInt($('live_rms').value,10);
  try {
    await api('/api/settings', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({vad_rms_dbfs:value})});
    // keep the settings drawer field in sync with this quick slider
    const f = $('f-vad_rms_dbfs');
    if (f) { f.value = value; const b=$('f-vad_rms_dbfs-v'); if (b) b.textContent = value; }
    toast('能量门限已设为 '+value+' dBFS');
  } catch(e) { toast('门限保存失败'); }
}

async function fetchHealth() {
  try {
    const h = await api('/api/health');
    const d = h.diagnostics || {};
    $('m-calls').textContent = d.calls || 0;
    $('m-sr').textContent = (d.success||0)+' / '+(d.failed||0);
    $('m-last').textContent = (d.last_sec||'—') + (d.last_sec && d.last_sec !== '—' ? 's' : '');
    $('m-p95').textContent = (d.p95_sec||'—') + (d.p95_sec && d.p95_sec !== '—' ? 's' : '');
    $('diag-summary').textContent = d.queue_ctx || '—';
    $('m-backlog').textContent = h.queue_backlog ?? 0;
    $('m-lag').textContent = fmtLag(h.queue_lag_sec);
    const t = h.queue_totals || {};
    $('m-totals').textContent = (t.enqueued||0)+' / '+(t.done||0);
    $('rtsp-url').textContent = h.rtsp_url || '—';
    $('rtsp-url').title = h.rtsp_url || '';
    $('engine-tag').textContent = h.asr_engine || '—';
    $('engine-line').textContent = (h.asr_engine||'')+' · '+h.queue_mode;
    $('asr-state').textContent = h.asr_blocked ? '熔断中' : (h.asr_enabled ? (h.asr_active ? '识别中' : '时段外') : '已暂停');
    $('asr-state').style.color = h.asr_blocked ? 'var(--red)' : (h.asr_active ? 'var(--green)' : 'var(--muted)');
    $('asr-toggle').textContent = h.asr_enabled ? '⏸ 暂停识别' : '▶ 恢复识别';
    $('stream-toggle').textContent = h.stream_enabled ? '⏸ 停止拉流' : '▶ 开启拉流';
    $('stream-state').textContent = !(h.rtsp_url || '').trim() ? '未配置音频流' : (!h.stream_enabled ? '已手动停止' : (h.stream_paused ? '时段外挂起' : (h.stream_running ? '运行中' : '未启动')));
  } catch(e) {}
}

async function loadHistory(reset=false) {
  if (reset) { historyPage = 1; }
  try {
    const offset = (historyPage-1)*historyPageSize;
    const d = await api(`/api/transcripts?limit=${historyPageSize}&offset=${offset}&keyword=${encodeURIComponent(historyKeyword)}`);
    historyTotal = d.total || 0;
    const pages = Math.max(1, Math.ceil(historyTotal/historyPageSize));
    if (historyPage > pages) { historyPage = pages; return loadHistory(); }
    $('history-total').textContent = historyTotal+' 条';
    $('history-list').innerHTML = d.items.length ? d.items.map(transcriptItem).join('') : '<div style="color:var(--muted);font-size:13px">暂无记录</div>';
    $('history-page').textContent = historyPage+' / '+pages;
    $('history-prev').disabled = historyPage <= 1;
    $('history-next').disabled = historyPage >= pages;
  } catch(e) {}
}

async function clearHistory() {
  if (!confirm('确定清空全部识别历史？此操作不可撤销。')) return;
  try {
    await api('/api/transcripts', {method:'DELETE'});
    historyPage = 1;
    await loadHistory(true);
    toast('识别历史已清空');
  } catch(e) { toast('清空失败：'+(e.message||'未知错误')); }
}

function toggleListen() {
  listening = !listening;
  $('listen-btn').textContent = listening ? '⏹ 停止播放' : '▶ 播放';
  $('listen-btn').classList.toggle('primary', listening);
  if (ws && ws.readyState === 1) ws.send(JSON.stringify({type:'listen', on:listening}));
  if (listening) {
    if (!audioCtx) initAudio();
    else { audioCtx.resume(); audioPlaying = true; audioBuf = []; audioStarted = false; scheduleAudio(); }
  } else if (audioCtx) {
    audioPlaying = false;
    audioBuf = [];
    audioCtx.suspend();
  }
}

function initAudio() {
  audioCtx = new (window.AudioContext||window.webkitAudioContext)({sampleRate:AUDIO_SR});
  audioBuf = [];
  audioPlaying = true;
  audioStarted = false;
  nextPlayTime = 0;
  scheduleAudio();
}

// Schedule every chunk on the AudioContext clock so playback is gap-free.
// A preroll buffer absorbs network jitter; chunks are concatenated at exact times.
function scheduleAudio() {
  if (!audioCtx || !audioPlaying) return;
  if (!audioStarted) {
    if (audioBuf.length < AUDIO_PREROLL) { setTimeout(scheduleAudio, 20); return; }
    audioStarted = true;
    nextPlayTime = audioCtx.currentTime + 0.05;
  }
  // If the timeline fell behind (tab throttled / buffer starved), resync.
  if (nextPlayTime < audioCtx.currentTime) {
    nextPlayTime = audioCtx.currentTime + 0.02;
    if (audioBuf.length < AUDIO_CHUNK) { setTimeout(scheduleAudio, 20); return; }
  }
  while (audioBuf.length >= AUDIO_CHUNK) {
    const chunk = audioBuf.splice(0, AUDIO_CHUNK);
    const buf = audioCtx.createBuffer(1, chunk.length, AUDIO_SR);
    const f32 = new Float32Array(chunk.length);
    for (let i=0;i<chunk.length;i++) f32[i] = chunk[i] / 32768;
    buf.copyToChannel(f32, 0);
    const src = audioCtx.createBufferSource();
    src.buffer = buf;
    src.connect(audioCtx.destination);
    src.start(nextPlayTime);
    nextPlayTime += chunk.length / AUDIO_SR;
  }
  setTimeout(scheduleAudio, 25);
}

async function toggleASR() {
  const cur = await api('/api/settings').catch(()=>null);
  if (!cur) return;
  const next = !cur.values.asr_enabled;
  await api('/api/settings', {method:'POST', body:JSON.stringify({asr_enabled:next})});
  toast(next ? '识别已恢复' : '识别已暂停');
  fetchHealth();
}

async function toggleStream() {
  const cur = await api('/api/settings').catch(()=>null);
  if (!cur) return;
  const next = !cur.values.stream_enabled;
  await api('/api/settings', {method:'POST', body:JSON.stringify({stream_enabled:next})});
  toast(next ? '拉流已开启' : '拉流已停止');
  fetchHealth();
}

// ---------- settings drawer ----------
const FIELDS = [
  ['基础', [
    ['rtsp_url','RTSP 地址','text','摄像头流地址，修改后立即重连'],
    ['asr_engine','识别引擎','select',null,['aliyun','tencent','openai']],
    ['asr_language','识别语言','select',null,['zh','en','auto']],
    ['access_password','访问口令','password','留空表示不修改；设置后全部会话需重新登录'],
  ]],
  ['阿里云百炼', [
    ['dashscope_api_key','DashScope API Key','password','留空表示不修改'],
    ['asr_model','模型','text','paraformer-realtime-v2'],
  ]],
  ['腾讯云', [
    ['tencent_secret_id','SecretId','password','留空表示不修改'],
    ['tencent_secret_key','SecretKey','password','留空表示不修改'],
    ['tencent_region','地域','text','ap-shanghai'],
  ]],
  ['OpenAI 兼容', [
    ['openai_base_url','接口地址','text','https://api.openai.com/v1'],
    ['openai_api_key','API Key','password','留空表示不修改'],
    ['openai_model','模型','text','whisper-1'],
  ]],
  ['VAD 切句', [
    ['vad_engine','VAD 引擎','select','webrtc=WebRTC VAD 质量好；energy=纯能量门限，弱设备(armv7)选它 CPU 低',['webrtc','energy']],
    ['vad_mode','灵敏度 (0-3)','range','越大越严格'],
    ['vad_frame_ms','分析帧长','select',null,['10','20','30']],
    ['vad_silence_frames','断句静音帧数','number','20 帧 × 30ms = 600ms'],
    ['vad_min_speech_frames','最短语音帧数','number','10 帧 = 300ms'],
    ['vad_min_speech_ratio','有效语音占比','range','0-1'],
    ['vad_rms_dbfs','能量门限 (dBFS)','range','-80 ~ -10'],
    ['vad_preroll_ms','句首补帧 (ms)','number','避免吞字头'],
    ['vad_tail_pad_ms','句尾保留 (ms)','number','给 ASR 留出收尾静音'],
    ['vad_max_segment_sec','单段最长 (s)','number','5-60，太长会从词中间切断'],
  ]],
  ['音频处理', [
    ['ffmpeg_highpass','高通滤波 (Hz)','number','0 关闭'],
    ['ffmpeg_timeout','拉流超时 (s)','number','5-120'],
    ['ffmpeg_denoise','轻度降噪','bool',null],
  ]],
  ['识别队列', [
    ['queue_mode','处理模式','select','切换需重启容器',['durable','realtime']],
    ['queue_max_items','队列上限（条）','number','10-10000'],
    ['queue_max_mb','磁盘容量上限 (MB)','number','1-1024'],
    ['queue_max_attempts','最大重试次数','number','0=无限'],
    ['asr_max_queue_age','实时模式最长等待 (s)','number','0=不限'],
    ['asr_merge_gap_sec','段合并间隔 (s)','range','0 关闭，摊薄每请求固定开销'],
    ['asr_merge_max_sec','合并块上限 (s)','number','10-120'],
  ]],
  ['过滤与时段', [
    ['filter_noise_text','过滤语气词/幻觉短词','bool',null],
    ['filter_ignore_words','噪音词黑名单','text','逗号分隔'],
    ['save_rejected_audio','保存被过滤音频（调试）','bool',null],
    ['asr_schedule_enabled','只在指定时段识别','bool',null],
    ['asr_start_time','开始时间','text','HH:MM，按容器本地时间'],
    ['asr_end_time','结束时间','text','HH:MM，支持跨午夜'],
  ]],
];

function fieldHTML(name, label, type, hint, options, val) {
  let control = '';
  const id = 'f-'+name;
  if (type === 'bool') {
    control = `<label class="check"><input type="checkbox" id="${id}" ${val?'checked':''}> 启用</label>`;
  } else if (type === 'select') {
    control = `<select id="${id}">${options.map(o=>`<option value="${esc(o)}" ${String(val)===o?'selected':''}>${esc(o)}</option>`).join('')}</select>`;
  } else if (type === 'range') {
    const min = name==='vad_mode'?0 : name==='vad_rms_dbfs'?-80 : name==='asr_merge_gap_sec'?0 : 0;
    const max = name==='vad_mode'?3 : name==='vad_rms_dbfs'?-10 : name==='asr_merge_gap_sec'?10 : 1;
    const step = (name==='vad_mode'||name==='vad_rms_dbfs')?1:(name==='asr_merge_gap_sec'?0.5:0.1);
    control = `<div style="display:flex;align-items:center;gap:10px"><input type="range" id="${id}" min="${min}" max="${max}" step="${step}" value="${esc(val)}"><b id="${id}-v" style="min-width:48px;text-align:right;font-variant-numeric:tabular-nums">${esc(val)}</b></div>`;
  } else {
    control = `<input type="${type}" id="${id}" value="${esc(val)}">`;
  }
  return `<div class="field"><label>${esc(label)}</label>${control}${hint?`<div class="hint">${esc(hint)}</div>`:''}</div>`;
}

function renderSettings(d) {
  settingsCache = d;
  const v = d.values || {};
  const pwSet = !!v.access_password_set;
  let html = `<div class="group"><h3>访问口令</h3><div class="field"><label>状态</label><div class="vad-row"><span>${pwSet ? '已启用访问口令' : '未启用访问口令'}</span>${pwSet ? '<button class="btn sm danger" id="clear-password">清除口令</button>' : ''}<b class="${d.engine_ready ? 'ok' : 'warn'}">${d.engine_ready ? '引擎已就绪' : '引擎未就绪'}</b></div></div></div>`;
  for (const [group, fields] of FIELDS) {
    html += `<div class="group"><h3>${esc(group)}</h3>`;
    for (const f of fields) html += fieldHTML(f[0], f[1], f[2], f[3], f[4], v[f[0]]);
    html += '</div>';
  }
  $('settings-form').innerHTML = html;
  const clearBtn = $('clear-password');
  if (clearBtn) clearBtn.onclick = async () => {
    if (!confirm('确定清除访问口令？清除后任何人都可以直接访问。')) return;
    try {
      await api('/api/settings', {method:'POST', body:JSON.stringify({access_password:'', clear_access_password:true})});
      toast('访问口令已清除');
      renderSettings(await api('/api/settings'));
    } catch(e) { toast('清除失败'); }
  };
  $('settings-form').querySelectorAll('input[type=range]').forEach(el=>{
    el.addEventListener('input', ()=>{
      const b=$(el.id+'-v'); if(b) b.textContent = el.value;
      // two-way sync between drawer field and quick slider for the same setting
      if (el.id === 'f-vad_rms_dbfs') {
        $('live_rms').value = el.value; syncThresholdLabel();
      }
    });
  });
  injectRtspHistory();
}

// RTSP address history: dropdown + delete, backed by /api/rtsp-history.
function injectRtspHistory() {
  const input = $('f-rtsp_url');
  if (!input) return;
  const field = input.closest('.field');
  if (!field || $('rtsp-hist-row')) return;
  const row = document.createElement('div');
  row.id = 'rtsp-hist-row';
  row.className = 'rtsp-hist-row';
  row.innerHTML = `<select id="rtsp-hist-select"><option value="">最近使用的地址…</option></select><button class="btn sm danger" id="rtsp-hist-del" title="删除选中的历史地址">删除</button>`;
  field.appendChild(row);
  $('rtsp-hist-select').onchange = e => { if (e.target.value) { input.value = e.target.value; e.target.value=''; } };
  $('rtsp-hist-del').onclick = deleteRtspHistory;
  loadRtspHistory();
}

async function loadRtspHistory() {
  try {
    const d = await api('/api/rtsp-history');
    const items = d.items || [];
    // settings drawer dropdown
    const sel = $('rtsp-hist-select');
    if (sel) sel.innerHTML = '<option value="">最近使用的地址…</option>' +
      items.map(u=>`<option value="${esc(u)}">${esc(u)}</option>`).join('');
    // dashboard stream switcher
    const sw = $('rtsp-switch');
    if (sw) {
      const cur = (settingsCache?.values?.rtsp_url) || $('rtsp-url')?.textContent?.trim() || '';
      // ensure current URL is present in the list
      const list = cur && !items.includes(cur) ? [cur, ...items] : items;
      sw.innerHTML = list.length
        ? list.map(u=>`<option value="${esc(u)}" ${u===cur?'selected':''}>${esc(u)}</option>`).join('')
        : '<option value="">暂无可用地址</option>';
    }
  } catch(e) {}
}

async function switchStream() {
  const url = $('rtsp-switch').value;
  if (!url) return;
  try {
    await api('/api/settings', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({rtsp_url:url})});
    // keep settings drawer field in sync
    const f = $('f-rtsp_url'); if (f) f.value = url;
    toast('音频流已切换，正在重连');
    fetchHealth();
    loadRtspHistory();
  } catch(e) { toast('切换失败'); }
}

async function deleteRtspHistory() {
  const sel = $('rtsp-hist-select');
  if (!sel || !sel.value) { toast('请先在下拉框选择要删除的地址'); return; }
  const url = sel.value;
  if (!confirm('确定删除这条历史地址？\n\n'+url)) return;
  try {
    await api('/api/rtsp-history?url='+encodeURIComponent(url), {method:'DELETE'});
    await loadRtspHistory();
    toast('已删除历史地址');
  } catch(e) { toast('删除失败'); }
}

async function openSettings() {
  $('drawer').classList.add('show'); $('drawer-mask').classList.add('show');
  try { renderSettings(await api('/api/settings')); } catch(e) { toast('加载设置失败'); }
}
function closeSettings() { $('drawer').classList.remove('show'); $('drawer-mask').classList.remove('show'); }

async function saveSettings() {
  const patch = {};
  for (const [,fields] of FIELDS) for (const f of fields) {
    const el = $('f-'+f[0]); if (!el) continue;
    patch[f[0]] = f[2] === 'bool' ? el.checked : el.value;
  }
  try {
    const r = await api('/api/settings', {method:'POST', body:JSON.stringify(patch)});
    if ((r.changed||[]).includes('access_password')) {
      toast('访问口令已更新，请重新登录');
      setTimeout(()=>{ location.href = '/login'; }, 800);
      return;
    }
    toast(r.changed.length ? '已保存并应用 '+r.changed.length+' 项' : '设置无变化');
    closeSettings(); fetchHealth();
  } catch(e) { toast('保存失败：'+e.message); }
}

// ---------- logs ----------
// Logs are rendered both in the full-screen panel and in the inline home card.
// The inline card polls continuously so the log is always live on the dashboard.
function openLogs() { $('log-panel').classList.add('show'); $('log-mask').classList.add('show'); fetchLogs(true); }
function closeLogs() { $('log-panel').classList.remove('show'); $('log-mask').classList.remove('show'); }

async function fetchLogs(reset=false) {
  if (logPaused && !reset) return;
  if (reset) { logAfter = 0; $('log-body').innerHTML=''; $('inline-log-body').innerHTML=''; }
  try {
    const d = await api(`/api/logs?after=${logAfter}&limit=500`);
    if (d.items && d.items.length) {
      for (const it of d.items) {
        const line = logLine(it);
        $('log-body').insertAdjacentHTML('beforeend', line);
        $('inline-log-body').insertAdjacentHTML('beforeend', line);
      }
      logAfter = d.last_id;
      // Bound the inline buffer so long-running sessions stay lightweight.
      const ib = $('inline-log-body');
      while (ib.children.length > 400) ib.removeChild(ib.firstChild);
      ib.scrollTop = ib.scrollHeight;
      const b = $('log-body'); b.scrollTop = b.scrollHeight;
      applyLogFilter();
    }
    const h = await api('/api/health').catch(()=>null);
    if (h) $('log-diag').textContent = `调用 ${h.diagnostics?.calls||0} · 成功 ${h.diagnostics?.success||0} · 失败 ${h.diagnostics?.failed||0} · 积压 ${h.queue_backlog||0}`;
  } catch(e) {}
}

function logLine(e) {
  const msg = esc(e.message), full = esc(e.time+' '+e.level+' '+e.message);
  return `<div class="log-line" data-level="${esc(e.level)}" data-msg="${esc(e.message)}" title="${full}"><span class="t">${esc(e.time)}</span><span class="lv ${esc(e.level)}">${esc(e.level)}</span><span class="msg">${msg}</span></div>`;
}
function applyLogFilter() {
  const lv = $('log-level').value, kw = $('log-search').value.toLowerCase();
  $('log-body').querySelectorAll('.log-line').forEach(el=>{
    const ok = (!lv || el.dataset.level === lv) && (!kw || el.dataset.msg.toLowerCase().includes(kw));
    el.style.display = ok ? '' : 'none';
  });
  const ilv = $('inline-log-level').value;
  $('inline-log-body').querySelectorAll('.log-line').forEach(el=>{
    el.style.display = (!ilv || el.dataset.level === ilv) ? '' : 'none';
  });
}

function init() {
  connectWS(); fetchHealth(); loadHistory();
  fetchLogs(true);
  // sync the quick threshold slider + stream switcher from persisted settings
  api('/api/settings').then(d=>{
    settingsCache = d;
    const v = d.values?.vad_rms_dbfs;
    if (v != null) { $('live_rms').value = String(clamp(Math.round(v), RMS_MIN, RMS_MAX)); syncThresholdLabel(); }
    loadRtspHistory();
  }).catch(()=>{});
  initThresholdDrag();
  $('rtsp-switch').addEventListener('change', switchStream);
  $('rtsp-switch-edit').onclick = openSettings;
  syncThresholdMarker();
  setInterval(fetchHealth, 5000);
  setInterval(fetchLogs, 1500);
  $('listen-btn').onclick = toggleListen;
  $('asr-toggle').onclick = toggleASR;
  $('stream-toggle').onclick = toggleStream;
  $('open-settings').onclick = openSettings;
  $('close-settings').onclick = closeSettings;
  $('cancel-settings').onclick = closeSettings;
  $('drawer-mask').onclick = closeSettings;
  $('save-settings').onclick = saveSettings;
  $('open-logs').onclick = openLogs;
  $('log-close').onclick = closeLogs;
  $('log-mask').onclick = closeLogs;
  $('log-pause').onclick = () => { logPaused=!logPaused; $('log-pause').textContent = logPaused?'继续':'暂停'; };
  $('log-clear').onclick = async () => { await api('/api/logs',{method:'DELETE'}); $('log-body').innerHTML=''; $('inline-log-body').innerHTML=''; logAfter=0; };
  $('log-level').onchange = applyLogFilter;
  $('log-search').oninput = applyLogFilter;
  $('inline-log-level').onchange = applyLogFilter;
  $('history-prev').onclick = () => { if (historyPage > 1) { historyPage--; loadHistory(); } };
  $('history-next').onclick = () => { historyPage++; loadHistory(); };
  $('history-clear').onclick = clearHistory;
  let st; $('search').oninput = e => { clearTimeout(st); st=setTimeout(()=>{ historyKeyword=e.target.value; loadHistory(true); }, 350); };
  if (location.hash === '#settings') openSettings();
  if (location.hash === '#logs') openLogs();
}
init();
