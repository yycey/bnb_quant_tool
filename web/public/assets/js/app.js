/* 策略云行 · Web 控制台 v25 · 鄂渝 Discuz 风格 */
const API = '/api/index.php';
const TOKEN_KEY = 'bnb_quant_api_token';

let refreshTimer = null;
let livePriceTimer = null;
let activeTab = 'positions';
const loadedTabs = new Set();
let authModalShown = false;
const closingIds = new Set();
let lastLivePrice = null;
let livePollBusy = false;
let watcherLiveHint = null;

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

const ACTION_LABELS = {
  LONG: '做多', SHORT: '做空', WAIT: '观望', BUY: '买入', SELL: '卖出', HOLD: '持有',
};

function readCookie(name) {
  const m = document.cookie.match(new RegExp('(?:^|; )' + name + '=([^;]*)'));
  return m ? decodeURIComponent(m[1]) : '';
}

function getToken() {
  try {
    let t = localStorage.getItem(TOKEN_KEY) || '';
    if (!t) {
      t = readCookie(TOKEN_KEY);
      if (t) localStorage.setItem(TOKEN_KEY, t);
    }
    return t;
  } catch {
    return readCookie(TOKEN_KEY);
  }
}

function setToken(v) {
  const val = (v || '').trim();
  try {
    if (val) localStorage.setItem(TOKEN_KEY, val);
    else localStorage.removeItem(TOKEN_KEY);
  } catch { /* ignore */ }
  if (val) {
    document.cookie = `${TOKEN_KEY}=${encodeURIComponent(val)}; path=/; max-age=31536000; SameSite=Lax`;
  } else {
    document.cookie = `${TOKEN_KEY}=; path=/; max-age=0`;
  }
  updateTokenButton();
}

function updateTokenButton() {
  const btn = $('#token-btn');
  if (!btn) return;
  const t = getToken();
  btn.textContent = t ? 'Token ✓' : 'Token';
  btn.classList.toggle('btn-ok', !!t);
}

function authHeaders(extra = {}) {
  const token = getToken();
  const h = { Accept: 'application/json', ...extra };
  if (token) {
    h.Authorization = `Bearer ${token}`;
    h['X-Api-Token'] = token;
  }
  return h;
}

function apiUrl(endpoint, params = {}) {
  const qs = new URLSearchParams({ endpoint, ...params });
  const token = getToken();
  if (token) qs.set('access_token', token);
  return `${API}?${qs}`;
}

function hideAuthModal() {
  authModalShown = false;
  $('#auth-modal')?.classList.add('hidden');
}

function showAuthModal(message) {
  if (authModalShown) return;
  authModalShown = true;
  const hint = $('#auth-hint');
  if (hint && message) hint.textContent = message;
  $('#auth-modal')?.classList.remove('hidden');
}

function fmt(v, d = 2) {
  if (v === null || v === undefined || v === '') return '-';
  const n = Number(v);
  return Number.isFinite(n) ? n.toFixed(d) : String(v);
}

function fmtPnl(v) {
  const n = Number(v);
  if (!Number.isFinite(n)) return '<span class="muted">--</span>';
  return `<span class="${n >= 0 ? 'green' : 'red'}">${n >= 0 ? '+' : ''}${n.toFixed(2)}</span>`;
}

function fmtAction(action) {
  const key = String(action || '').toUpperCase();
  return ACTION_LABELS[key] || action || '-';
}

function actionBadgeClass(action) {
  const key = String(action || '').toUpperCase();
  if (key === 'LONG' || key === 'BUY') return 'badge-long';
  if (key === 'SHORT' || key === 'SELL') return 'badge-short';
  return 'badge-wait';
}

function escapeHtml(text) {
  return String(text ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function statusDot(ok, label) {
  return `<span class="status-row"><span class="dot ${ok ? 'dot-ok' : 'dot-bad'}"></span>${label}</span>`;
}

function showToast(msg, isError = false) {
  let el = $('#toast');
  if (!el) {
    el = document.createElement('div');
    el.id = 'toast';
    el.className = 'toast';
    document.body.appendChild(el);
  }
  el.textContent = msg;
  el.style.borderColor = isError ? 'rgba(239,68,68,0.5)' : '';
  el.classList.add('show');
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.remove('show'), 3200);
}

async function api(endpoint, params = {}, options = {}) {
  const res = await fetch(apiUrl(endpoint, params), {
    headers: authHeaders(options.headers || {}),
    ...options,
  });
  const raw = await res.text();
  let data = {};
  if (raw) {
    try { data = JSON.parse(raw); }
    catch { data = { error: raw.slice(0, 200) || `HTTP ${res.status}` }; }
  }
  if (res.status === 401) {
    showAuthModal(getToken()
      ? 'Token 无效，请核对 config.yaml 中 web.api_token'
      : '请输入与 config.yaml 中 web.api_token 相同的 Token');
    throw new Error(data.error || '需要 Token');
  }
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  hideAuthModal();
  return data;
}

/* ---------- status / overview / advice ---------- */

async function loadStatus() {
  const st = await api('status').catch(() => ({}));
  const banner = $('#watcher-banner');
  if (!banner) return st;
  if (st.watcher_running) {
    banner.className = 'banner banner-ok';
    banner.textContent = `模拟盘监控运行中 · 心跳 ${st.watcher_last_heartbeat || '-'}`;
    watcherLiveHint = '监控运行中';
  } else if ((st.open_positions || 0) > 0) {
    banner.className = 'banner banner-warn';
    banner.textContent = st.hint || `监控未运行（持仓 ${st.open_positions}）`;
    watcherLiveHint = '监控未运行';
  } else {
    banner.className = 'banner banner-warn hidden';
    banner.textContent = '';
    watcherLiveHint = '系统在线';
  }
  return st;
}

function setLiveStrip(state) {
  const strip = $('#live-strip');
  const textEl = $('#live-strip-text');
  const priceEl = $('#live-strip-price');
  if (!strip || !textEl || !priceEl) return;

  const online = !!state.online;
  strip.classList.toggle('is-offline', !online);

  const symbol = state.symbol || 'BNBUSDT';
  const source = state.source && state.source !== 'none' ? state.source : '';
  const tick = state.tickAt ? state.tickAt.toLocaleTimeString() : '';
  const parts = [
    online ? '系统运行中 · 实时检测价格' : '行情检测中断',
    watcherLiveHint || null,
    symbol,
    source ? `源 ${source}` : null,
    tick ? `更新 ${tick}` : null,
  ].filter(Boolean);
  textEl.textContent = parts.join(' · ');

  const price = state.price;
  if (price != null && Number.isFinite(Number(price)) && Number(price) > 0) {
    const n = Number(price);
    const prev = lastLivePrice;
    priceEl.textContent = fmt(n, 2);
    priceEl.classList.remove('is-up', 'is-down', 'flash');
    if (prev != null && Number.isFinite(prev) && prev !== n) {
      priceEl.classList.add(n > prev ? 'is-up' : 'is-down', 'flash');
      clearTimeout(priceEl._flashT);
      priceEl._flashT = setTimeout(() => priceEl.classList.remove('flash'), 450);
    }
    lastLivePrice = n;
    const kpi = $('#stat-price');
    if (kpi) kpi.textContent = fmt(n, 2);
  } else if (!online) {
    priceEl.textContent = '--';
    priceEl.classList.remove('is-up', 'is-down', 'flash');
  }
}

async function pollLivePrice() {
  if (livePollBusy) return;
  livePollBusy = true;
  try {
    const market = await api('market');
    const price = Number(market?.price);
    const ok = Number.isFinite(price) && price > 0;
    setLiveStrip({
      online: ok,
      price: ok ? price : null,
      symbol: market?.symbol || window.__overview?.symbol || 'BNBUSDT',
      source: market?.source,
      tickAt: new Date(),
      change_24h: market?.change_24h ?? market?.change_pct,
    });
    const chg = market?.change_24h ?? market?.change_pct;
    const chgEl = $('#stat-change');
    if (chgEl && chg != null && chg !== '') {
      const n = Number(chg);
      if (Number.isFinite(n)) {
        chgEl.innerHTML = `<span class="${n >= 0 ? 'green' : 'red'}">${n >= 0 ? '+' : ''}${fmt(n, 2)}%</span>`;
      }
    }
  } catch {
    setLiveStrip({ online: false, tickAt: new Date() });
  } finally {
    livePollBusy = false;
  }
}

function updateStatusHero(d, overview) {
  const actionEl = $('#status-action');
  if (!actionEl) return;
  const openN = Number(overview?.open_positions ?? 0);
  const openEl = $('#status-open-inline');
  if (openEl) openEl.textContent = openN > 0 ? `${openN} 笔` : '空仓';

  if (!d || d.found === false) {
    actionEl.textContent = '待分析';
    actionEl.className = 'status-action is-wait';
    $('#status-follow').textContent = '点「分析并跟单」或等全自动';
    $('#status-intent').textContent = '--';
    $('#status-gate').textContent = '--';
    $('#status-reason').textContent = '空闲';
    return;
  }

  const action = String(d.trading_action || d.final_signal || d.ai_signal || 'WAIT').toUpperCase();
  const raw = String(d.raw_action || '').toUpperCase();
  const passed = !!(d.passed_gate ?? d.risk_passed);
  const gates = Array.isArray(d.gate_reasons) ? d.gate_reasons : [];
  const probe = !!(d.learning_phase_probe || d.trade_advice?.learning_phase_probe);

  let label = '观望';
  let cls = 'is-wait';
  if (action === 'LONG' || action === 'BUY') { label = '做多'; cls = 'is-long'; }
  else if (action === 'SHORT' || action === 'SELL') { label = '做空'; cls = 'is-short'; }
  if (probe) label += ' · 试探';

  actionEl.textContent = label;
  actionEl.className = `status-action ${cls}`;
  $('#status-intent').textContent = raw && ['LONG', 'SHORT'].includes(raw) ? fmtAction(raw) : (label.startsWith('观望') ? '无方向' : label.split(' ·')[0]);
  $('#status-gate').innerHTML = passed ? '<span class="green">通过</span>' : '<span class="red">拦截</span>';

  let follow = openN > 0 ? `持仓 ${openN} 笔` : (passed && (action === 'LONG' || action === 'SHORT') ? `可跟单 · ${fmtAction(action)}` : '暂不开仓');
  $('#status-follow').textContent = follow;
  const reason = gates[0] || d.risk_reason || (passed ? '门控通过' : '—');
  $('#status-reason').textContent = String(reason).slice(0, 100);
}

async function loadOverview() {
  const [ov, market, stats] = await Promise.all([
    api('overview').catch(() => ({})),
    api('market').catch(() => ({})),
    api('stats').catch(() => ({})),
  ]);

  const price = market.price ?? ov.price;
  $('#stat-price').textContent = price != null ? fmt(price, 2) : '--';
  const chg = market.change_24h ?? market.change_pct;
  const chgEl = $('#stat-change');
  if (chgEl) {
    if (chg == null || chg === '') chgEl.textContent = ov.symbol || '--';
    else {
      const n = Number(chg);
      chgEl.innerHTML = `<span class="${n >= 0 ? 'green' : 'red'}">${n >= 0 ? '+' : ''}${fmt(n, 2)}%</span>`;
    }
  }

  setLiveStrip({
    online: price != null && Number(price) > 0,
    price,
    symbol: ov.symbol || market.symbol || 'BNBUSDT',
    source: market.source,
    tickAt: new Date(),
  });

  const openN = ov.open_positions ?? 0;
  $('#stat-open').textContent = openN;
  $('#stat-auto').textContent = ov.autopilot_mode ? `自动 ${ov.autopilot_mode}` : '—';

  const pnl = ov.total_pnl ?? stats.total_pnl;
  $('#stat-pnl').innerHTML = fmtPnl(pnl);
  $('#stat-pnl-sub').textContent = '仅自动单';

  const wr = ov.win_rate ?? stats.win_rate;
  const wrPct = wr == null ? null : (Number(wr) <= 1 ? Number(wr) * 100 : Number(wr));
  $('#stat-winrate').textContent = wrPct == null ? '--' : `${fmt(wrPct, 1)}%`;
  $('#stat-trades').textContent = `${ov.total_trades ?? stats.total_trades ?? 0} 笔`;

  const t = $('#update-time');
  if (t) t.textContent = new Date().toLocaleString();

  const pt = $('#pt-stats');
  if (pt) {
    const priceTxt = price != null ? fmt(price, 2) : '--';
    const pnlN = Number(pnl);
    const pnlTxt = Number.isFinite(pnlN) ? `${pnlN >= 0 ? '+' : ''}${pnlN.toFixed(2)}` : '--';
    const wrTxt = wrPct == null ? '--' : `${fmt(wrPct, 1)}%`;
    pt.textContent = `现价 ${priceTxt} | 持仓 ${openN} | 盈亏 ${pnlTxt} | 胜率 ${wrTxt}`;
  }

  syncQuickToggles(ov);
  renderAiProvidersHint(ov);
  window.__overview = ov;
  return ov;
}

function renderAiProvidersHint(ov) {
  const el = $('#ai-providers-hint');
  if (!el) return;
  const p = ov.ai_providers || {};
  const on = [];
  if (p.deepseek) on.push('DeepSeek');
  if (p.qianwen) on.push('千问');
  if (p.volcengine) on.push('豆包');
  const llm = ov.llm || {};
  const mode = llm.mode || '-';
  el.textContent = on.length
    ? `当前开启: ${on.join(' · ')} · 模式 ${mode}`
    : '三家 AI 均未开启 — 请至少开启一家并配置 API Key';
}

function renderAdviceSummary(d) {
  const action = d.trading_action || d.final_signal || d.ai_signal || '-';
  const conf = Number(d.ai_confidence || d.consensus_confidence || 0);
  const confPct = conf <= 1 ? conf * 100 : conf;
  const passed = d.passed_gate ?? d.risk_passed;
  const gates = Array.isArray(d.gate_reasons) ? d.gate_reasons : [];
  const gateOne = !passed && gates.length ? escapeHtml(String(gates[0]).slice(0, 80)) : '';
  return `
    <div class="advice-grid advice-grid-plain">
      <div>结论<strong>${fmtAction(action)}</strong></div>
      <div>置信<strong>${fmt(confPct, 0)}%</strong></div>
      <div>入场 / 止损 / 止盈<strong>${fmt(d.entry_price || d.current_price)} · ${fmt(d.stop_loss)} · ${fmt(d.take_profit)}</strong></div>
      <div>门控<strong>${passed ? '<span class="green">通过</span>' : '<span class="red">拦截</span>'}${gateOne ? ' · ' + gateOne : ''}</strong></div>
      <div>时间<strong class="small">${escapeHtml((d.timestamp || '-').replace('T', ' ').slice(0, 19))}</strong></div>
    </div>`;
}

async function loadLatestAdvice() {
  const d = await api('latest_advice').catch(() => ({ found: false }));
  const el = $('#latest-advice');
  const badge = $('#advice-action-badge');
  if (!el) return;
  if (!d || d.found === false) {
    el.innerHTML = '<div class="empty">暂无决策</div>';
    if (badge) { badge.textContent = '--'; badge.className = 'badge badge-wait'; }
    updateStatusHero(null, window.__overview);
    return;
  }
  const action = d.trading_action || d.final_signal || 'WAIT';
  if (badge) {
    badge.textContent = fmtAction(action);
    badge.className = `badge ${actionBadgeClass(action)}`;
  }
  el.innerHTML = renderAdviceSummary(d);
  updateStatusHero(d, window.__overview);
}

/* ---------- positions ---------- */

async function closePosition(id) {
  if (closingIds.has(id)) return;
  if (!confirm(`确认平仓 #${id}？`)) return;
  closingIds.add(id);
  try {
    const url = apiUrl('close_position');
    const res = await fetch(url, {
      method: 'POST',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ position_id: id }),
    });
    const data = await res.json().catch(() => ({}));
    if (res.status === 401) {
      showAuthModal('平仓需要 Token');
      throw new Error(data.error || '需要 Token');
    }
    if (!res.ok || data.ok === false) throw new Error(data.error || '平仓失败');
    showToast(`已平仓 #${id}`);
    await loadPositions();
    await loadOverview();
  } catch (e) {
    showToast(e.message, true);
  } finally {
    closingIds.delete(id);
  }
}

async function loadPositions() {
  const rows = await api('positions').catch(() => []);
  const el = $('#positions-table');
  if (!el) return;
  const list = Array.isArray(rows) ? rows : (rows.positions || []);
  if (!list.length) {
    el.innerHTML = '<div class="empty">空仓</div>';
    return;
  }
  let html = `<table><tr>
    <th>ID</th><th>方向</th><th>入场</th><th>数量</th>
    <th>止损</th><th>止盈</th><th>浮盈</th><th>开单时间</th><th></th>
  </tr>`;
  list.forEach((p) => {
    // API: side AS action；兼容 side / direction
    const side = String(p.action || p.side || p.direction || '').toUpperCase();
    const tp = p.take_profit ?? p.take_profit1 ?? p.tp1;
    const opened = String(p.opened_at || '').replace('T', ' ').replace(/\+00:00$/, '').slice(0, 19);
    html += `<tr>
      <td>#${p.id}</td>
      <td><span class="badge ${actionBadgeClass(side)}">${fmtAction(side) || '-'}</span></td>
      <td>${fmt(p.entry_price)}</td>
      <td>${fmt(p.qty_remaining ?? p.quantity ?? p.qty_total, 4)}</td>
      <td>${fmt(p.stop_loss ?? p.sl)}</td>
      <td>${fmt(tp)}</td>
      <td>${fmtPnl(p.unrealized_pnl_usdt)}</td>
      <td class="small">${escapeHtml(opened || '-')}</td>
      <td><button class="btn btn-sm btn-danger" onclick="closePosition(${p.id})">平仓</button></td>
    </tr>`;
  });
  el.innerHTML = html + '</table>';
}

async function loadHistory() {
  const data = await api('history', { limit: 80 }).catch(() => ({ items: [] }));
  const el = $('#history-table');
  if (!el) return;
  const list = Array.isArray(data) ? data : (data.items || []);
  if (!list.length) {
    el.innerHTML = '<div class="empty">暂无平仓记录</div>';
    return;
  }
  let html = `<table>
    <tr>
      <th>ID</th><th>方向</th><th>入场</th><th>出场</th>
      <th>盈亏</th><th>R</th><th>原因</th><th>平仓时间</th>
    </tr>`;
  list.forEach((p) => {
    const side = String(p.action || p.side || '').toUpperCase();
    const reason = p.close_reason || '-';
    const closed = String(p.closed_at || '').replace('T', ' ').slice(0, 19);
    const r = p.r_multiple;
    const rStr = (r === null || r === undefined || r === '')
      ? '-'
      : `<span class="${Number(r) >= 0 ? 'green' : 'red'}">${Number(r) >= 0 ? '+' : ''}${Number(r).toFixed(2)}</span>`;
    html += `<tr>
      <td>#${p.id}</td>
      <td><span class="badge ${actionBadgeClass(side)}">${fmtAction(side)}</span></td>
      <td>${fmt(p.entry_price)}</td>
      <td>${fmt(p.close_avg_price)}</td>
      <td>${fmtPnl(p.pnl)}</td>
      <td>${rStr}</td>
      <td class="small">${escapeHtml(reason)}</td>
      <td class="small">${escapeHtml(closed || '-')}</td>
    </tr>`;
  });
  el.innerHTML = html + '</table>';
}

/* ---------- learning ---------- */

function dimBar(label, value) {
  const v = Math.max(0, Math.min(100, Number(value) || 0));
  return `<div class="dim-row">
    <span class="dim-label">${escapeHtml(label)}</span>
    <div class="dim-track"><div class="dim-fill" style="width:${v}%"></div></div>
    <span class="dim-val">${fmt(v, 0)}</span>
  </div>`;
}

async function loadLearning() {
  const [learn, ov, decisions, strategies] = await Promise.all([
    api('ai_learning').catch(() => ({})),
    api('overview').catch(() => window.__overview || {}),
    api('decision_history', { limit: 12 }).catch(() => []),
    api('strategies').catch(() => []),
  ]);

  const g = learn.growth || ov.growth || {};
  const maturity = g.learning_maturity || 'BEGINNER';
  const badge = $('#learn-maturity');
  if (badge) {
    badge.textContent = maturity;
    badge.className = `badge ${maturity === 'EXPERT' || maturity === 'ADVANCED' ? 'badge-long' : 'badge-wait'}`;
  }

  const wr = g.paper_win_rate == null ? null : (Number(g.paper_win_rate) <= 1 ? Number(g.paper_win_rate) * 100 : Number(g.paper_win_rate));
  $('#learn-summary').innerHTML = `
    <div class="learn-kpi">
      <div><span class="muted">分析</span><strong>${learn.analysis_count ?? g.analysis_count ?? 0}</strong></div>
      <div><span class="muted">学习日志</span><strong>${learn.learning_count ?? 0}</strong></div>
      <div><span class="muted">反馈</span><strong>${g.feedback_count ?? 0}</strong></div>
      <div><span class="muted">知识卡</span><strong>${g.knowledge_cards ?? 0}</strong><span class="muted small"> / 已验证 ${g.validated_knowledge_cards ?? 0}</span></div>
      <div><span class="muted">能力分</span><strong>${g.capability_level ?? 0}</strong></div>
      <div><span class="muted">纸面胜率</span><strong>${wr == null ? '--' : fmt(wr, 1) + '%'}</strong></div>
      <div><span class="muted">质量均分</span><strong>${fmt(g.avg_quality_score, 1)}</strong></div>
      <div><span class="muted">模式记忆</span><strong>${g.pattern_memory_count ?? 0}</strong></div>
    </div>`;

  const dims = g.capability_dimensions || {};
  const dimLabels = {
    sample_maturity: '样本成熟度',
    prediction_accuracy: '预测准确',
    knowledge_quality: '知识质量',
    discipline: '纪律',
    evolution_activity: '进化活跃',
  };
  const dimHtml = Object.keys(dimLabels).map((k) => dimBar(dimLabels[k], dims[k])).join('');
  $('#learn-dims').innerHTML = dimHtml || '<div class="empty">暂无维度数据</div>';

  const prov = ov.ai_providers || {};
  const llm = ov.llm || {};
  const provChip = (name, on) =>
    `<span class="chip ${on ? 'chip-on' : 'chip-off'}">${escapeHtml(name)} ${on ? '开' : '关'}</span>`;
  $('#learn-llm').innerHTML = `
    <div class="chip-row">
      ${provChip('DeepSeek', !!prov.deepseek)}
      ${provChip('通义千问', !!prov.qianwen)}
      ${provChip('豆包/火山', !!prov.volcengine)}
    </div>
    <div class="monitor-list" style="margin-top:12px">
      <div class="monitor-kv"><span>LLM 模式</span><strong>${escapeHtml(llm.mode || '-')}</strong></div>
      <div class="monitor-kv"><span>分析家列表</span><strong class="small">${escapeHtml((llm.providers || []).join(', ') || '-')}</strong></div>
      <div class="monitor-kv"><span>综合合成</span><strong>${llm.synthesis ? '开' : '关'} · 最少同意 ${llm.synthesis_min_agree ?? '-'}</strong></div>
      <div class="monitor-kv"><span>智能闭环</span><strong>${ov.intelligence_loop_enabled ? '<span class="green">开</span>' : '关'}</strong></div>
      <div class="monitor-kv"><span>验证开平</span><strong>${ov.validation_trading ? '<span class="green">开</span>' : '关'}</strong></div>
      <div class="monitor-kv"><span>知识复用</span><strong>${ov.reuse_enabled ? '开' : '关'}</strong></div>
    </div>
    <p class="muted small" style="margin-top:8px">开关可在顶部「开关」或「配置」页修改。</p>`;

  const contrib = learn.strategy_contribution || ov.strategy_contribution ||
    (Array.isArray(strategies) ? strategies : (strategies.items || []));
  if (!contrib.length) {
    $('#learn-strategies').innerHTML = '<div class="empty">暂无策略贡献</div>';
  } else {
    let sh = `<table><tr><th>策略</th><th>权重</th><th>胜率</th><th>样本</th></tr>`;
    contrib.slice(0, 10).forEach((s) => {
      const name = s.strategy_name || s.name || '-';
      const wrS = s.win_rate == null ? '-' : (Number(s.win_rate) <= 1 ? fmt(Number(s.win_rate) * 100, 1) + '%' : fmt(s.win_rate, 1) + '%');
      sh += `<tr>
        <td class="small">${escapeHtml(name)}</td>
        <td>${fmt(s.weight ?? s.contribution_pct, 3)}</td>
        <td>${wrS}</td>
        <td>${s.total_predictions ?? s.samples ?? '-'}</td>
      </tr>`;
    });
    $('#learn-strategies').innerHTML = sh + '</table>';
  }

  const logs = learn.recent_logs || [];
  if (!logs.length) {
    $('#learn-logs').innerHTML = '<div class="empty">暂无学习日志</div>';
  } else {
    let lh = `<table><tr><th>时间</th><th>事件</th><th>详情</th></tr>`;
    logs.slice(0, 15).forEach((r) => {
      lh += `<tr>
        <td class="small">${escapeHtml(String(r.timestamp || '').replace('T', ' ').slice(0, 19))}</td>
        <td class="small">${escapeHtml(r.action || '-')}</td>
        <td class="small">${escapeHtml(String(r.details || '').slice(0, 80))}</td>
      </tr>`;
    });
    $('#learn-logs').innerHTML = lh + '</table>';
  }

  const decList = Array.isArray(decisions) ? decisions : (decisions.items || []);
  if (!decList.length) {
    $('#learn-decisions').innerHTML = '<div class="empty">暂无决策记录</div>';
  } else {
    let dh = `<table><tr><th>时间</th><th>方向</th><th>置信</th><th>门控</th><th>模型</th></tr>`;
    decList.forEach((d) => {
      const act = d.trading_action || d.final_signal || d.ai_signal || '-';
      const conf = Number(d.ai_confidence || 0);
      const confPct = conf <= 1 ? conf * 100 : conf;
      const passed = d.passed_gate ?? d.risk_passed;
      dh += `<tr>
        <td class="small">${escapeHtml(String(d.timestamp || '').replace('T', ' ').slice(0, 19))}</td>
        <td><span class="badge ${actionBadgeClass(act)}">${fmtAction(act)}</span></td>
        <td>${fmt(confPct, 0)}%</td>
        <td>${passed ? '<span class="green">过</span>' : '<span class="red">拦</span>'}</td>
        <td class="small">${escapeHtml(d.primary_provider || '-')}</td>
      </tr>`;
    });
    $('#learn-decisions').innerHTML = dh + '</table>';
  }
}

/* ---------- monitor ---------- */

async function loadMonitor() {
  const [mon, breaker] = await Promise.all([
    api('monitor'),
    api('circuit_breaker').catch(() => null),
  ]);
  const w = mon.watcher || {};
  const py = mon.python || {};
  const svc = mon.services || {};
  const tr = mon.trading || {};
  const dbs = mon.databases || {};

  const breakerLine = breaker?.status && breaker.status !== 'unknown'
    ? `<div class="monitor-kv"><span>熔断</span><strong>${escapeHtml(breaker.status)}</strong></div>`
    : '';

  $('#monitor-status').innerHTML = `
    <div class="monitor-list">
      ${statusDot(w.running, `监控 ${w.running ? '运行中' : '未运行'}`)}
      <div class="muted small">心跳 ${w.last_heartbeat || '-'} ${w.age_seconds != null ? `(${w.age_seconds}s)` : ''}</div>
      ${statusDot(py.available, `Python ${py.available ? escapeHtml(String(py.binary || '').split(/[/\\]/).pop()) : '不可用'}`)}
      ${statusDot(!!dbs.paper_trading?.exists, '模拟盘库')}
      ${statusDot(svc.autopilot_mode && svc.autopilot_mode !== 'off', `Autopilot ${svc.autopilot_mode || 'off'}`)}
      ${statusDot(svc.paper_auto_follow, `跟单 ${svc.paper_auto_follow ? '开' : '关'}`)}
      ${breakerLine}
      <div class="monitor-kv"><span>交易对</span><strong>${escapeHtml(tr.symbol || '-')}</strong></div>
      <div class="monitor-kv"><span>持仓</span><strong>${tr.open_positions ?? 0}</strong></div>
      <div class="monitor-kv"><span>AI</span><strong class="small">${escapeHtml(
        ['deepseek', 'qianwen', 'volcengine']
          .filter((k) => (window.__overview?.ai_providers || {})[k])
          .map((k) => ({ deepseek: 'DS', qianwen: '千问', volcengine: '豆包' }[k]))
          .join('/') || '无'
      )}</strong></div>
      <div class="monitor-kv"><span>配置</span><strong class="small">${mon.paths?.config_exists ? 'OK' : '<span class="red">缺失</span>'}</strong></div>
    </div>`;
}

/* ---------- maintenance ---------- */

async function maintenancePost(action, body = {}) {
  const res = await fetch(apiUrl('maintenance'), {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ action, ...body }),
  });
  const data = await res.json().catch(() => ({}));
  if (res.status === 401) {
    showAuthModal('维护需要 Token');
    throw new Error(data.error || '需要 Token');
  }
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

function renderHealthChecks(data) {
  const el = $('#maint-health');
  if (!el) return;
  const checks = data.checks || [];
  if (!checks.length) {
    el.innerHTML = `<pre class="config">${escapeHtml(JSON.stringify(data, null, 2))}</pre>`;
    return;
  }
  let html = '<div class="monitor-list">';
  checks.forEach((c) => {
    const ok = c.ok !== false && c.status !== 'fail' && c.level !== 'error';
    html += `${statusDot(ok, escapeHtml(c.name || c.id || 'check'))}
      <div class="muted small">${escapeHtml(c.message || c.detail || '')}</div>`;
  });
  el.innerHTML = html + '</div>';
}

async function loadMaintenance() {
  const st = await api('maintenance', { action: 'status' }).catch(() => ({}));
  const ver = $('#maint-version');
  if (ver) {
    ver.textContent = [
      st.version ? `版本 ${st.version}` : '',
      st.python ? `Python ${st.python}` : '',
      st.project_root ? st.project_root : '',
    ].filter(Boolean).join(' · ');
  }
}

function setupMaintenanceActions() {
  const run = (id, action, label) => {
    $(`#${id}`)?.addEventListener('click', async () => {
      const btn = $(`#${id}`);
      if (btn) btn.disabled = true;
      try {
        showToast(`${label}…`);
        const data = action === 'health'
          ? await api('maintenance', { action: 'health' })
          : await maintenancePost(action, action === 'backup' ? { label: 'web' } : {});
        if (action === 'health' || data.checks) renderHealthChecks(data);
        else {
          $('#maint-health').innerHTML = `<pre class="config">${escapeHtml(JSON.stringify(data, null, 2))}</pre>`;
        }
        showToast(`${label}完成`);
        await loadMaintenance();
      } catch (e) {
        showToast(e.message, true);
      } finally {
        if (btn) btn.disabled = false;
      }
    });
  };
  run('btn-maint-health', 'health', '健康检查');
  run('btn-maint-fix', 'fix', '自动修复');
  run('btn-maint-optimize', 'optimize', '优化库');
  run('btn-maint-backup', 'backup', '备份');
}

/* ---------- config ---------- */

const SECTION_LABELS = {
  deepseek: 'DeepSeek',
  qianwen: '通义千问',
  volcengine: '豆包 / 火山',
  autopilot: '自动驾驶',
  paper_trading: '模拟盘',
  ai_trading: 'AI 跟单',
  intelligence_loop: '智能闭环',
  capability_memory: '知识复用',
  trading: '交易',
  signal_scanner: '扫盘',
  validation_trading: '验证开平',
  local_growth: '本地成长',
  trade_advisor: '交易顾问',
  analysis: '分析',
  auto_run: '定时分析',
};

const CONFIG_SECTION_ORDER = [
  'deepseek', 'qianwen', 'volcengine',
  'autopilot', 'paper_trading', 'ai_trading',
  'validation_trading', 'intelligence_loop', 'capability_memory',
  'local_growth', 'trading', 'signal_scanner', 'trade_advisor', 'analysis', 'auto_run',
];

function renderConfigForm(config, schema) {
  let html = '<form id="cfg-edit-form" class="config-form">';
  CONFIG_SECTION_ORDER.forEach((section) => {
    const fields = schema?.[section];
    if (!fields || typeof fields !== 'object') return;
    html += `<fieldset><legend>${escapeHtml(SECTION_LABELS[section] || section)}</legend><div class="form-grid">`;
    Object.entries(fields).forEach(([key, rule]) => {
      const val = config?.[section]?.[key];
      const id = `${section}.${key}`;
      const label = rule.label || key;
      if (rule.type === 'bool') {
        html += `<label class="form-check"><input type="checkbox" name="${id}" ${val ? 'checked' : ''}><span>${escapeHtml(label)}</span></label>`;
      } else if (rule.type === 'string') {
        html += `<label>${escapeHtml(label)}<input type="text" name="${id}" value="${escapeHtml(val ?? '')}"></label>`;
      } else {
        const step = rule.type === 'float' ? '0.01' : '1';
        html += `<label>${escapeHtml(label)}<input type="number" name="${id}" value="${val ?? ''}" step="${step}" ${rule.min != null ? `min="${rule.min}"` : ''} ${rule.max != null ? `max="${rule.max}"` : ''}></label>`;
      }
    });
    html += '</div></fieldset>';
  });
  html += '<div class="form-actions"><button type="submit" class="btn btn-primary">保存</button></div></form>';
  return html;
}

async function loadConfig() {
  const d = await api('config');
  const config = d.config || d;
  const schema = d.schema || {};
  $('#config-form').innerHTML = renderConfigForm(config, schema);
  const form = $('#cfg-edit-form');
  if (!form || form.dataset.bound === '1') return;
  form.dataset.bound = '1';
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const patch = {};
    CONFIG_SECTION_ORDER.forEach((section) => {
      const fields = schema[section];
      if (!fields) return;
      patch[section] = {};
      Object.keys(fields).forEach((key) => {
        const el = form.elements[`${section}.${key}`];
        if (!el) return;
        const t = fields[key].type;
        if (t === 'bool') patch[section][key] = el.checked;
        else if (t === 'int') patch[section][key] = Number(el.value);
        else if (t === 'float') patch[section][key] = Number(el.value);
        else patch[section][key] = el.value;
      });
    });
    try {
      const res = await fetch(apiUrl('config_update'), {
        method: 'POST',
        headers: authHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({ patch }),
      });
      const data = await res.json();
      if (res.status === 401) {
        showAuthModal('保存配置需要 Token');
        throw new Error(data.error || '需要 Token');
      }
      if (!res.ok) throw new Error((data.errors || [data.error]).join('; '));
      showToast('已保存 · 请重启交易进程');
      form.dataset.bound = '0';
      loadedTabs.delete('config');
      await loadConfig();
      await refreshCore();
    } catch (err) {
      showToast(err.message || '保存失败', true);
    }
  });
}

/* ---------- toggles / run ---------- */

function syncQuickToggles(ov) {
  const full = $('#toggle-fullauto');
  if (full) {
    const mode = String(ov.autopilot_mode || '').toLowerCase();
    full.checked = ['fullauto', 'unified', 'scheduled', 'legacy'].includes(mode);
  }
  const follow = $('#toggle-auto-follow');
  if (follow) follow.checked = !!(ov.auto_follow_enabled ?? ov.paper_auto_follow);
  const ai = $('#toggle-follow-ai');
  if (ai) ai.checked = !!ov.follow_ai_direction;
  const prov = ov.ai_providers || {};
  const ds = $('#toggle-ai-deepseek');
  if (ds) ds.checked = !!prov.deepseek;
  const qw = $('#toggle-ai-qianwen');
  if (qw) qw.checked = !!prov.qianwen;
  const ve = $('#toggle-ai-volcengine');
  if (ve) ve.checked = !!prov.volcengine;
}

function setupQuickToggles() {
  $$('.quick-toggle input').forEach((input) => {
    input.addEventListener('change', async () => {
      const section = input.dataset.section;
      const key = input.dataset.key;
      let value = input.checked;
      if (input.dataset.boolMode) {
        value = input.checked ? input.dataset.boolMode : 'off';
      }
      try {
        const res = await fetch(apiUrl('config_update'), {
          method: 'POST',
          headers: authHeaders({ 'Content-Type': 'application/json' }),
          body: JSON.stringify({ patch: { [section]: { [key]: value } } }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.error || '保存失败');
        showToast('已更新 · 重启交易进程后完全生效');
        loadedTabs.delete('learning');
        loadedTabs.delete('config');
        await loadOverview();
      } catch (e) {
        showToast(e.message, true);
        input.checked = !input.checked;
      }
    });
  });
}

async function runHeadlessAnalysis(openPaper) {
  const res = await fetch(apiUrl('run_analysis'), {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ open_paper: !!openPaper }),
  });
  const data = await res.json().catch(() => ({}));
  if (res.status === 401) {
    showAuthModal('运行分析需要 Token');
    throw new Error(data.error || '需要 Token');
  }
  if (!res.ok || data.ok === false) throw new Error(data.error || '分析失败');
  return data;
}

function setupRunAnalysisActions() {
  const bind = (id, openPaper) => {
    $(`#${id}`)?.addEventListener('click', async () => {
      const btn = $(`#${id}`);
      if (btn?.disabled) return;
      if (btn) btn.disabled = true;
      try {
        showToast(openPaper ? '分析并评估跟单…' : '分析中…');
        const data = await runHeadlessAnalysis(openPaper);
        const pos = data.position_id ? ` · 开仓 #${data.position_id}` : '';
        showToast(`${fmtAction(data.action || 'WAIT')} · ${data.passed_gate ? '通过' : '拦截'}${pos}`);
        await loadLatestAdvice();
        await loadOverview();
        await loadPositions();
        loadedTabs.delete('history');
        loadedTabs.delete('learning');
        if (activeTab === 'history') await loadHistory();
        if (activeTab === 'learning') await loadLearning();
      } catch (e) {
        showToast(e.message, true);
      } finally {
        if (btn) btn.disabled = false;
      }
    });
  };
  bind('btn-run-analysis', false);
  bind('btn-run-analysis-open', true);
}

/* ---------- tabs / refresh ---------- */

const tabLoaders = {
  positions: loadPositions,
  history: loadHistory,
  learning: loadLearning,
  monitor: loadMonitor,
  maintenance: loadMaintenance,
  config: loadConfig,
};

async function loadTab(name, force = false) {
  const loader = tabLoaders[name];
  if (!loader) return;
  if (!force && loadedTabs.has(name)) return;
  await loader();
  loadedTabs.add(name);
}

async function refreshCore() {
  const results = await Promise.allSettled([
    loadStatus(),
    loadOverview(),
    loadLatestAdvice(),
    loadTab('positions', true),
  ]);
  const failed = results.filter((r) => r.status === 'rejected');
  if (failed.length) throw failed[0].reason;
  if (activeTab === 'monitor') await loadTab('monitor', true);
  if (activeTab === 'history') await loadTab('history', true);
  if (activeTab === 'learning') await loadTab('learning', true);
}

async function refreshAll() {
  const btn = $('#refresh-btn');
  if (btn) btn.disabled = true;
  try {
    loadedTabs.clear();
    await refreshCore();
    await loadTab(activeTab, true);
  } catch (e) {
    showToast('刷新失败: ' + e.message, true);
  } finally {
    if (btn) btn.disabled = false;
  }
}

function switchTab(name, btn) {
  activeTab = name;
  $$('.tab').forEach((t) => t.classList.remove('active'));
  $$('.panel').forEach((p) => p.classList.remove('active'));
  btn.classList.add('active');
  $(`#panel-${name}`)?.classList.add('active');
  loadTab(name).catch((e) => showToast(e.message, true));
}

function setupAuthModal() {
  updateTokenButton();
  $('#token-btn')?.addEventListener('click', () => {
    authModalShown = false;
    $('#token-input').value = getToken();
    $('#auth-hint').textContent = '与 config.yaml 中 web.api_token 一致。留空=清除。';
    $('#auth-modal')?.classList.remove('hidden');
  });
  $('#token-cancel')?.addEventListener('click', hideAuthModal);
  $('#token-save')?.addEventListener('click', () => {
    const val = $('#token-input').value.trim();
    setToken(val);
    hideAuthModal();
    showToast(val ? 'Token 已保存' : 'Token 已清除');
    refreshAll();
  });
}

function setupThemeToggle() {
  const KEY = 'bnb_quant_ui_theme';
  const btn = $('#theme-btn');
  const applyLabel = () => {
    if (!btn) return;
    const dark = document.documentElement.getAttribute('data-theme') === 'dark';
    btn.textContent = dark ? '浅色' : '深色';
    btn.title = dark ? '切换到薄荷纸浅色' : '切换到霓虹深色';
  };
  applyLabel();
  btn?.addEventListener('click', () => {
    const next = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    try { localStorage.setItem(KEY, next); } catch { /* ignore */ }
    applyLabel();
    showToast(next === 'dark' ? '已切换深色霓虹' : '已切换薄荷纸浅色');
  });
}

document.addEventListener('DOMContentLoaded', () => {
  $$('.tab').forEach((btn) => {
    btn.addEventListener('click', () => switchTab(btn.dataset.tab, btn));
  });
  $('#refresh-btn')?.addEventListener('click', refreshAll);
  setupThemeToggle();
  setupAuthModal();
  setupQuickToggles();
  setupRunAnalysisActions();
  setupMaintenanceActions();
  refreshAll();
  refreshTimer = setInterval(refreshCore, 30000);
  pollLivePrice();
  livePriceTimer = setInterval(pollLivePrice, 5000);
});

window.closePosition = closePosition;
window.refreshAll = refreshAll;
