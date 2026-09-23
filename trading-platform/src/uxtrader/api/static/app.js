import { CandleChart, LineChart, fmtMoney, fmtNum } from './charts.js';

// ------------------------------------------------------------------ helpers
const $ = (s, r = document) => r.querySelector(s);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const num = (x) => (x == null ? NaN : +x);
const pct = (x, d = 2, sign = true) => (x == null || !isFinite(x) ? '—' : (sign && x > 0 ? '+' : x < 0 ? '−' : '') + Math.abs(x * 100).toFixed(d) + '%');
const cls = (x) => (x > 0 ? 'up' : x < 0 ? 'down' : '');
const ago = (iso) => {
  if (!iso) return '';
  const s = (Date.now() - new Date(iso)) / 1000;
  if (s < 60) return Math.max(0, Math.round(s)) + 's ago';
  if (s < 3600) return Math.round(s / 60) + 'm ago';
  if (s < 86400) return Math.round(s / 3600) + 'h ago';
  return new Date(iso).toLocaleDateString();
};
const dur = (iso) => {
  if (!iso) return '';
  const s = Math.floor((Date.now() - new Date(iso)) / 1000);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), ss = s % 60;
  return (h ? h + 'h ' : '') + (h || m ? m + 'm ' : '') + ss + 's';
};

// Strategy identity → fixed categorical slot (colour follows the entity, never rank).
const SLOT = { DonchianRegime: 1, DemoCrossover: 2, AdaptiveGrid: 3, VwapMeanReversion: 4,
               FundingCarry: 5, CrossSectionalMomentum: 6, PairsStatArb: 7, SqueezeFade: 8 };
const ID_CLASS = { DEMO: 'DemoCrossover', S1: 'FundingCarry', S2: 'CrossSectionalMomentum', S3: 'PairsStatArb',
                   S4: 'DonchianRegime', S5: 'VwapMeanReversion', S6: 'AdaptiveGrid', S7: 'SqueezeFade' };

const I = {
  overview: '<path d="M3 13h4v8H3zM10 3h4v18h-4zM17 8h4v13h-4z"/>',
  markets: '<path d="M7 3v4M7 17v4M17 5v4M17 15v4"/><rect x="5" y="7" width="4" height="10" rx="1"/><rect x="15" y="9" width="4" height="6" rx="1"/>',
  strategies: '<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9 7 7M17 17l2.1 2.1M4.9 19.1 7 17M17 7l2.1-2.1"/>',
  risk: '<path d="M12 3 4 6v6c0 4.5 3.4 8.3 8 9 4.6-.7 8-4.5 8-9V6z"/>',
  activity: '<path d="M3 12h4l3-8 4 16 3-8h4"/>',
  launch: '<path d="M5 15c-1.5 1.5-2 5-2 5s3.5-.5 5-2M14 4c3-1 6-1 6-1s0 3-1 6l-6 6-5-5z"/><circle cx="15" cy="9" r="1.5"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.5 1.5M17.5 17.5 19 19M5 19l1.5-1.5M17.5 6.5 19 5"/>',
  moon: '<path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"/>',
  auto: '<rect x="3" y="4" width="18" height="13" rx="2"/><path d="M8 21h8M12 17v4"/>',
  lock: '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/>',
  check: '<path d="m5 12 5 5 9-10"/>',
  alert: '<path d="M12 9v4M12 17h.01"/><path d="M10.3 3.9 2.4 18a2 2 0 0 0 1.7 3h15.8a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 16v-4M12 8h.01"/>',
  stop: '<rect x="6" y="6" width="12" height="12" rx="2"/>',
  play: '<path d="m8 5 11 7-11 7z"/>',
  shield: '<path d="M12 3 4 6v6c0 4.5 3.4 8.3 8 9 4.6-.7 8-4.5 8-9V6z"/><path d="m9 12 2 2 4-4"/>',
  x: '<path d="M18 6 6 18M6 6l12 12"/>',
};
const icon = (n, s = 18) => `<svg width="${s}" height="${s}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${I[n]}</svg>`;

// ------------------------------------------------------------------ token + api
function takeToken() {
  const m = location.hash.match(/token=([^&]+)/);
  if (m) {
    try { sessionStorage.setItem('ux-token', decodeURIComponent(m[1])); } catch (e) { /* private mode */ }
    history.replaceState(null, '', location.pathname + '#/');
  }
  try { return sessionStorage.getItem('ux-token') || ''; } catch (e) { return ''; }
}
let TOKEN = takeToken();

async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: 'POST', body: JSON.stringify(body),
    headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + TOKEN },
  };
  const r = await fetch(path, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
  return data;
}

// ------------------------------------------------------------------ global state
const S = { state: null, catalog: null, fills: [], events: [], candles: {}, symbol: null,
            view: 'overview', theme: 'auto', connected: false };
try { S.theme = localStorage.getItem('ux-theme') || 'auto'; } catch (e) { /* ignore */ }

const byClass = () => {
  const m = {};
  for (const s of S.catalog?.strategies || []) m[s.class] = s;
  return m;
};
const stratByName = (name) => (S.state?.strategies || []).find((s) => s.name === name);
const colorOfClass = (c) => `var(--s${SLOT[c] || 1})`;
const colorOfName = (name) => colorOfClass(stratByName(name)?.cls);
const label = (cat) => (cat.id === 'DEMO' ? cat.name : `${cat.id} · ${cat.name}`);
const nameOf = (name) => {
  const st = stratByName(name);
  const cat = st && byClass()[st.cls];
  return cat ? label(cat) : name;
};
const clock = (iso) => (iso ? new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '');
// Event text leads with the strategy id; show the display name instead.
const eventText = (t) => {
  const first = String(t).split(' ')[0];
  return stratByName(first) ? shortName(first) + String(t).slice(first.length) : String(t);
};
const shortName = (name) => {
  const st = stratByName(name);
  const cat = st && byClass()[st.cls];
  return cat ? (cat.id === 'DEMO' ? 'Demo' : cat.id) : name;
};

function applyTheme() {
  const root = document.documentElement;
  if (S.theme === 'auto') root.removeAttribute('data-theme'); else root.setAttribute('data-theme', S.theme);
  document.querySelectorAll('.theme-toggle button').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.theme === S.theme)));
}

// ------------------------------------------------------------------ shell
const NAV = [['overview', 'Overview'], ['markets', 'Markets'], ['strategies', 'Strategies'], ['risk', 'Risk'], ['activity', 'Activity']];

function shell() {
  document.body.innerHTML = `
  <div class="shell">
    <aside class="side">
      <div class="brand">
        <svg width="30" height="30" viewBox="0 0 32 32" aria-hidden="true">
          <rect width="32" height="32" rx="9" fill="var(--ink)"/>
          <path d="M9 20v-5M9 11V9" stroke="var(--page)" stroke-width="2" stroke-linecap="round"/>
          <rect x="7.5" y="11" width="3" height="5" rx="1" fill="var(--page)"/>
          <path d="M16 23v-3M16 11V8" stroke="var(--page)" stroke-width="2" stroke-linecap="round"/>
          <rect x="14.5" y="11" width="3" height="9" rx="1" fill="var(--accent)"/>
          <path d="M23 18v-2M23 9V7" stroke="var(--page)" stroke-width="2" stroke-linecap="round"/>
          <rect x="21.5" y="9" width="3" height="7" rx="1" fill="var(--page)"/>
        </svg>
        <div><b>ux-trader</b><small>systematic crypto</small></div>
      </div>
      <nav class="nav" aria-label="Sections">
        <button data-view="launch">${icon('launch')}<span>Launch</span></button>
        ${NAV.map(([v, l]) => `<button data-view="${v}">${icon(v)}<span>${l}</span></button>`).join('')}
      </nav>
      <div class="side-foot">
        <div class="botcard" id="botcard"></div>
        <div class="theme-toggle" role="group" aria-label="Theme">
          <button data-theme="light" title="Light">${icon('sun', 16)}</button>
          <button data-theme="auto" title="System">${icon('auto', 16)}</button>
          <button data-theme="dark" title="Dark">${icon('moon', 16)}</button>
        </div>
      </div>
    </aside>
    <div class="main">
      <header class="top">
        <h1 id="title">Overview</h1>
        <span id="modepill"></span>
        <span class="spacer"></span>
        <span class="pill hide-sm" id="conn"><span class="dot"></span>connecting</span>
        <span id="topactions" style="display:flex;gap:8px"></span>
      </header>
      <main class="content" id="view"></main>
    </div>
  </div>
  <div class="toasts" id="toasts" aria-live="polite"></div>`;
  document.querySelectorAll('.nav button').forEach((b) => b.addEventListener('click', () => go(b.dataset.view)));
  document.querySelectorAll('.theme-toggle button').forEach((b) => b.addEventListener('click', () => {
    S.theme = b.dataset.theme;
    try { localStorage.setItem('ux-theme', S.theme); } catch (e) { /* ignore */ }
    applyTheme();
  }));
  applyTheme();
}

function toast(msg, kind = '') {
  const t = document.createElement('div');
  t.className = 'toast ' + kind;
  const d = document.createElement('span'); d.className = 'dot';
  const s = document.createElement('span'); s.textContent = msg;
  t.append(d, s);
  $('#toasts').appendChild(t);
  setTimeout(() => t.remove(), 4200);
}

function modal({ title, body, confirm, danger, input, inputLabel, requireText }) {
  return new Promise((resolve) => {
    const bg = document.createElement('div');
    bg.className = 'modal-bg';
    bg.innerHTML = `<div class="modal" role="dialog" aria-modal="true" aria-labelledby="mt">
      <h3 id="mt">${esc(title)}</h3><p>${body}</p>
      ${input !== undefined ? `<label class="field">${esc(inputLabel || 'Reason (logged)')}<input id="mi" value="${esc(input)}" autocomplete="off"></label>` : ''}
      <div class="actions"><button class="btn ghost" data-a="cancel">Cancel</button>
        <button class="btn ${danger ? 'danger' : 'primary'}" data-a="ok">${esc(confirm)}</button></div></div>`;
    document.body.appendChild(bg);
    const inp = $('#mi', bg), ok = $('[data-a=ok]', bg);
    const check = () => { if (requireText) ok.disabled = inp.value.trim() !== requireText; };
    inp?.addEventListener('input', check); check();
    (inp || ok).focus();
    const close = (v) => { bg.remove(); resolve(v); };
    bg.addEventListener('click', (e) => { if (e.target === bg) close(null); });
    $('[data-a=cancel]', bg).addEventListener('click', () => close(null));
    ok.addEventListener('click', () => close(inp ? inp.value.trim() || '(no reason)' : true));
    bg.addEventListener('keydown', (e) => { if (e.key === 'Escape') close(null); if (e.key === 'Enter' && !ok.disabled) ok.click(); });
  });
}

// ------------------------------------------------------------------ chrome updates
function modePill(l, compact = false) {
  if (!l || l.state === 'stopped') return '<span class="pill"><span class="dot"></span>Stopped</span>';
  if (l.state === 'error') return '<span class="pill bad"><span class="dot"></span>Failed to start</span>';
  const m = l.mode || '';
  const name = { demo: 'Demo', paper: 'Paper', live: 'LIVE' }[m] || m;
  const scale = !compact && m === 'demo' && l.time_scale > 1 ? ` · ${l.time_scale}× time` : '';
  return `<span class="pill ${esc(m)}" title="${m === 'demo' ? 'Synthetic market; simulated time runs faster than real time' : ''}"><span class="dot"></span>${esc(name)}${esc(scale)}${l.state === 'running' ? '' : ' · ' + esc(l.state)}</span>`;
}

function updateChrome() {
  const st = S.state, l = st?.launcher;
  $('#modepill').innerHTML = modePill(l);
  const conn = $('#conn');
  conn.className = 'pill hide-sm ' + (S.connected ? 'ok' : 'warn');
  conn.innerHTML = `<span class="dot"></span>${S.connected ? 'Live updates' : 'Reconnecting'}`;
  const running = l?.state === 'running';
  const ta = $('#topactions');
  const want = running ? 'running' : 'idle';
  if (ta.dataset.mode !== want || (running && ta.dataset.kill !== String(st.kill_active))) {
    ta.dataset.mode = want; ta.dataset.kill = String(st?.kill_active);
    ta.innerHTML = running
      ? `<button class="btn sm" id="stopbtn">${icon('stop', 14)}Stop bot</button>
         ${st.kill_active
           ? `<button class="btn sm" id="rearmbtn">${icon('shield', 14)}Rearm</button>`
           : `<button class="btn sm danger" id="killbtn">Kill switch</button>`}`
      : (S.view !== 'launch' ? `<button class="btn sm primary" id="gotolaunch">${icon('play', 14)}Launch bot</button>` : '');
    $('#stopbtn')?.addEventListener('click', stopBot);
    $('#killbtn')?.addEventListener('click', () => control('kill'));
    $('#rearmbtn')?.addEventListener('click', () => control('rearm'));
    $('#gotolaunch')?.addEventListener('click', () => go('launch'));
  }
  const snap = st?.snapshot;
  $('#botcard').innerHTML = `
    <div class="row"><span class="muted" style="font-size:12px">Bot</span>${modePill(l, true)}</div>
    ${running ? `<div class="row"><span class="muted" style="font-size:12px">Uptime</span><span class="num" style="font-size:12.5px;font-weight:600">${esc(dur(l.started_at))}</span></div>` : ''}
    ${snap ? `<div class="row"><span class="muted" style="font-size:12px">Equity</span><span class="num" style="font-size:12.5px;font-weight:600">${fmtMoney(num(snap.equity), { compact: true })}</span></div>` : ''}`;
}

async function control(command) {
  if (!S.state?.control_enabled) { toast('Controls are disabled: no API token configured', 'bad'); return; }
  const copy = {
    kill: ['Engage the kill switch?', 'Every book is flattened at market and new entries are blocked until you rearm. The out-of-process watchdog acts on this too.', 'Kill everything', true, 'Manual kill from dashboard'],
    flatten: ['Flatten all positions?', 'Every book is closed at market. Strategies stay enabled and may re-enter.', 'Flatten now', true, 'Manual flatten from dashboard'],
    rearm: ['Rearm trading?', 'New entries are allowed again. Make sure you know why the switch fired.', 'Rearm', false, 'Reviewed and cleared'],
  }[command];
  const reason = await modal({ title: copy[0], body: esc(copy[1]), confirm: copy[2], danger: copy[3], input: copy[4] });
  if (!reason) return;
  try { await api('/api/control', { command, reason }); toast(`${command[0].toUpperCase() + command.slice(1)} sent`, command === 'rearm' ? 'good' : 'bad'); }
  catch (e) { toast(e.message, 'bad'); }
  refresh();
}

async function stopBot() {
  const bg = document.createElement('div');
  bg.className = 'modal-bg';
  bg.innerHTML = `<div class="modal" role="dialog" aria-modal="true">
    <h3>Stop the bot?</h3><p>Choose what happens to open positions.</p>
    <div class="actions"><button class="btn ghost" data-a="x">Cancel</button>
      <button class="btn" data-a="keep">Stop, keep positions</button>
      <button class="btn primary" data-a="flat">Flatten & stop</button></div></div>`;
  document.body.appendChild(bg);
  const choice = await new Promise((res) => {
    bg.addEventListener('click', (e) => { const a = e.target.closest('[data-a]')?.dataset.a; if (a || e.target === bg) res(a || 'x'); });
    bg.addEventListener('keydown', (e) => { if (e.key === 'Escape') res('x'); });
    $('[data-a=flat]', bg).focus();
  });
  bg.remove();
  if (choice === 'x') return;
  try { await api('/api/launcher/stop', { flatten: choice === 'flat' }); toast('Bot stopped', 'good'); }
  catch (e) { toast(e.message, 'bad'); }
  await refresh();
  go('launch');
}

// ------------------------------------------------------------------ views
const views = {};

views.launch = {
  title: 'Launch',
  sel: null,
  mount(root) {
    const cat = S.catalog;
    if (!cat) { root.innerHTML = '<div class="card card-b empty">Loading…</div>'; return; }
    if (!this.sel) {
      this.sel = { mode: 'demo', venue: cat.venues[0]?.id, equity: 100000, speed: 0.1, stage: 1, strategies: {} };
      for (const s of cat.strategies) this.sel.strategies[s.id] = { on: ['DEMO', 'S4'].includes(s.id), budget: s.budget, symbols: s.symbols };
    }
    const running = S.state?.launcher?.state === 'running';
    root.innerHTML = `
      <section class="card launch-hero"><div class="glow"></div>
        <h2>${running ? 'Your bot is running' : 'Start your trading bot'}</h2>
        <p>${running ? 'Stop it from the top bar to change mode or strategies.'
          : 'Choose how it trades, pick strategies and their risk share, then start. Demo runs anywhere with no keys, so you can see every part working before any money is involved.'}</p>
      </section>
      <div class="steps">
        <section><div class="step-h"><span class="n">1</span><h3>Mode</h3><span class="sub">Where orders go</span></div>
          <div class="modes" role="radiogroup" id="modes"></div></section>
        <section><div class="step-h"><span class="n">2</span><h3>Strategies</h3><span class="sub">Risk budgets share one account — together they may use up to 100%</span></div>
          <div class="picks" id="picks"></div></section>
        <section><div class="step-h"><span class="n">3</span><h3>Settings</h3></div>
          <div class="card card-b settings" id="settings"></div></section>
      </div>
      <div class="startbar" id="startbar"></div>`;
    this.render();
  },
  render() {
    const cat = S.catalog, sel = this.sel;
    const running = S.state?.launcher?.state === 'running';
    $('#modes').innerHTML = cat.modes.map((m) => `
      <button class="mode ${m.id}" role="radio" aria-checked="${sel.mode === m.id}" data-mode="${m.id}" ${running ? 'disabled' : ''}>
        <span class="t"><span class="pill ${m.id}" style="padding:2px 8px"><span class="dot"></span>${esc(m.title)}</span></span>
        <span class="d">${esc(m.blurb)}</span></button>`).join('');
    $('#modes').querySelectorAll('.mode').forEach((b) => b.addEventListener('click', () => {
      sel.mode = b.dataset.mode;
      if (sel.mode !== 'demo') sel.strategies.DEMO.on = false;
      this.render();
    }));
    const visible = cat.strategies.filter((s) => !(s.demo_only && sel.mode !== 'demo'));
    $('#picks').innerHTML = visible.map((s) => {
      const c = sel.strategies[s.id];
      const color = colorOfClass(s.class);
      const locked = !s.launchable;
      return `<div class="pick ${c.on && !locked ? 'on' : ''} ${locked ? 'locked' : ''}" style="--c:${color}">
        <div class="top-row">
          <span class="key" style="background:${color};margin-top:6px"></span>
          <div style="min-width:0"><div style="font-weight:650">${esc(label(s))}</div>
            <div class="muted" style="font-size:12px">${esc(s.tag)} · ${esc(s.timeframe)} bars · ${esc(s.symbols.map((x) => x.split('/')[0]).join(', ') || '—')}</div></div>
          <label class="switch" title="${locked ? 'Not available yet' : 'Enable'}"><input type="checkbox" data-id="${s.id}" ${c.on && !locked ? 'checked' : ''} ${locked || running ? 'disabled' : ''} aria-label="Enable ${esc(s.id)}"><span></span></label>
        </div>
        <div class="desc">${esc(s.description)}</div>
        ${locked ? `<div class="needs">${icon('lock', 14)}<span>Needs ${esc(s.needs)}</span></div>`
          : `<div class="budget"><input type="range" min="1" max="50" value="${Math.round(c.budget * 100)}" style="--v:${((Math.round(c.budget * 100) - 1) / 49) * 100}%" data-budget="${s.id}" ${!c.on || running ? 'disabled' : ''} aria-label="Risk budget for ${esc(s.id)}"><span class="num" style="font-weight:650;width:38px;text-align:right">${Math.round(c.budget * 100)}%</span></div>`}
      </div>`;
    }).join('');
    $('#picks').querySelectorAll('input[type=checkbox]').forEach((i) => i.addEventListener('change', () => { sel.strategies[i.dataset.id].on = i.checked; this.render(); }));
    $('#picks').querySelectorAll('input[type=range]').forEach((i) => i.addEventListener('input', () => {
      sel.strategies[i.dataset.budget].budget = +i.value / 100;
      i.style.setProperty('--v', ((+i.value - 1) / 49) * 100 + '%');
      i.nextElementSibling.textContent = i.value + '%';
      this.renderBar();
    }));
    const venue = cat.venues.find((v) => v.id === sel.venue);
    $('#settings').innerHTML = `
      ${sel.mode !== 'demo' ? `<label class="field">Venue<select id="venue">${cat.venues.map((v) => `<option value="${v.id}" ${v.id === sel.venue ? 'selected' : ''}>${esc(v.id)}${v.has_credentials ? ' · keys found' : ''}</option>`).join('')}</select></label>` : ''}
      ${sel.mode !== 'live' ? `<label class="field">Simulated starting equity (USDT)<input id="equity" type="number" min="1000" step="1000" value="${sel.equity}"></label>` : ''}
      ${sel.mode === 'demo' ? `<div class="field">Market speed<div class="seg" id="speed">${[['Calm', 0.4], ['Normal', 0.1], ['Fast', 0.02]].map(([n, v]) => `<button aria-pressed="${sel.speed === v}" data-v="${v}">${n}</button>`).join('')}</div><span class="muted" style="font-weight:400">One simulated minute every ${sel.speed}s</span></div>` : ''}
      ${sel.mode !== 'demo' ? `<div class="field">Capital stage<div class="seg" id="stage">${[[1, '10%'], [2, '25%'], [3, '50%'], [4, '100%']].map(([n, l]) => `<button aria-pressed="${sel.stage === n}" data-v="${n}">${l}</button>`).join('')}</div><span class="muted" style="font-weight:400">Share of each budget actually used — start at 10% (docs/03 §4)</span></div>` : ''}
      ${sel.mode === 'live' ? `<div class="field">Keys<span style="font-weight:400" class="${venue?.has_credentials ? 'up' : 'down'}">${venue?.has_credentials ? 'Found in environment' : `Missing — set ${esc((sel.venue || '').toUpperCase())}_APIKEY and _SECRET, then restart`}</span></div>` : ''}`;
    $('#venue')?.addEventListener('change', (e) => { sel.venue = e.target.value; this.render(); });
    $('#equity')?.addEventListener('change', (e) => { sel.equity = Math.max(1000, +e.target.value || 100000); });
    $('#speed')?.querySelectorAll('button').forEach((b) => b.addEventListener('click', () => { sel.speed = +b.dataset.v; this.render(); }));
    $('#stage')?.querySelectorAll('button').forEach((b) => b.addEventListener('click', () => { sel.stage = +b.dataset.v; this.render(); }));
    this.renderBar();
  },
  renderBar() {
    const cat = S.catalog, sel = this.sel;
    const running = S.state?.launcher?.state === 'running';
    const on = cat.strategies.filter((s) => s.launchable && sel.strategies[s.id].on && !(s.demo_only && sel.mode !== 'demo'));
    const total = on.reduce((a, s) => a + sel.strategies[s.id].budget, 0);
    const over = total > 1.0001;
    const err = S.state?.launcher?.error;
    $('#startbar').innerHTML = `
      <div class="summary">
        <div class="alloc" role="img" aria-label="Risk allocation">${on.map((s) => `<div style="width:${(sel.strategies[s.id].budget / Math.max(1, total)) * 100}%;background:${colorOfClass(s.class)}" title="${esc(s.id)} ${Math.round(sel.strategies[s.id].budget * 100)}%"></div>`).join('')}</div>
        <div class="txt">${on.length ? on.map((s) => `<span class="legend-item" style="margin-right:12px"><span class="sw" style="background:${colorOfClass(s.class)}"></span>${esc(s.id === 'DEMO' ? 'Demo' : s.id)} ${Math.round(sel.strategies[s.id].budget * 100)}%</span>`).join('') : 'No strategies selected'}
          · <b class="${over ? 'down' : ''}">${Math.round(total * 100)}% of risk used</b>${over ? ' — reduce to 100% or less' : ''}</div>
        ${err ? `<div class="error">Last start failed: ${esc(err)}</div>` : ''}
      </div>
      ${running ? `<button class="btn lg" id="golive">${icon('overview', 16)}View dashboard</button>`
        : `<button class="btn lg ${sel.mode === 'live' ? 'danger' : 'primary'}" id="startbtn" ${!on.length || over ? 'disabled' : ''}>${icon('play', 16)}Start ${sel.mode}</button>`}`;
    $('#golive')?.addEventListener('click', () => go('overview'));
    $('#startbtn')?.addEventListener('click', () => this.start(on));
  },
  async start(on) {
    const sel = this.sel;
    let confirm = null;
    if (sel.mode === 'live') {
      confirm = await modal({ title: 'Trade with real money?', danger: true, confirm: 'Start live trading',
        body: 'Real orders will be placed on <b>' + esc(sel.venue) + '</b>. Venue-native stops, the watchdog and every risk limit stay active, but losses are real. Type <b>LIVE</b> to continue.',
        input: '', inputLabel: 'Type LIVE', requireText: 'LIVE' });
      if (!confirm) return;
    }
    const btn = $('#startbtn'); btn.disabled = true; btn.innerHTML = 'Starting…';
    try {
      await api('/api/launcher/start', {
        mode: sel.mode, venue: sel.venue, starting_equity: sel.equity, speed: sel.speed, confirm,
        strategies: on.map((s) => ({ id: s.id, risk_budget: sel.strategies[s.id].budget,
                                     stage: sel.mode === 'demo' ? 4 : sel.stage, symbols: sel.strategies[s.id].symbols })),
      });
      await refresh();
      if (S.state?.launcher?.state === 'running') { toast(`Bot started in ${sel.mode} mode`, 'good'); go('overview'); }
      else { toast(S.state?.launcher?.error || 'Start failed', 'bad'); this.renderBar(); }
    } catch (e) { toast(e.message, 'bad'); this.renderBar(); }
  },
  update() { if ($('#startbar')) this.renderBar(); },
};

function strategyCard(st, attr, positions, marks) {
  const cat = byClass()[st.cls];
  const color = colorOfClass(st.cls);
  const books = positions.filter((p) => p.strategy === st.name);
  const upnl = books.reduce((a, p) => a + (num(marks[p.symbol]) - num(p.avg_entry)) * num(p.quantity), 0) || 0;
  const pnl = (attr?.net_pnl ?? 0) + upnl;
  const warm = st.warmup_bars ? Math.min(1, st.bars_seen / st.warmup_bars) : 1;
  const disabled = (S.state?.risk?.disabled_strategies || []).includes(st.name);
  const status = disabled ? ['bad', 'Disabled by risk'] : st.warm ? ['ok', 'Active'] : ['warn', `Warming up ${Math.round(warm * 100)}%`];
  return `<article class="card strat" style="--c:${color}">
    <div class="head"><span class="key" style="background:${color};margin-top:6px"></span>
      <div style="min-width:0;flex:1"><div class="title">${esc(cat ? label(cat) : st.name)}</div>
        <div class="tagline">${esc(cat?.tag || '')} · ${esc(st.timeframe)} · ${esc(st.symbols.map((s) => s.split('/')[0]).join(', '))}</div></div>
      <span class="pill ${status[0]}"><span class="dot"></span>${esc(status[1])}</span></div>
    ${!st.warm ? `<div class="meter"><div class="track" style="--m:var(--warn)"><div class="fill" style="width:${warm * 100}%"></div></div><div class="foot"><span>${st.bars_seen} / ${st.warmup_bars} bars</span><span>needs full history before trading</span></div></div>` : ''}
    <div class="stats">
      <div class="stat"><div class="l">P&amp;L</div><div class="v num ${cls(pnl)}">${fmtMoney(pnl, { sign: true, compact: true })}</div></div>
      <div class="stat"><div class="l">Books</div><div class="v num">${books.length}</div></div>
      <div class="stat"><div class="l">Risk budget</div><div class="v num">${(st.risk_budget * 100).toFixed(st.risk_budget < 0.1 ? 1 : 0)}%</div></div>
    </div>
    <div class="why" title="${esc(st.last_reason || '')}">${st.last_reason ? esc(st.last_reason) + ' · ' + esc(clock(st.last_intent_at)) : '<span class="muted">No decisions yet</span>'}</div>
  </article>`;
}

function positionsTable(positions, marks) {
  if (!positions.length) return '<div class="empty">Flat — no open positions.</div>';
  return `<div class="table-wrap"><table><thead><tr><th>Strategy</th><th>Symbol</th><th class="r">Size</th><th class="r">Entry</th><th class="r">Mark</th><th class="r">Unrealized</th></tr></thead><tbody>
    ${positions.map((p) => {
      const mark = num(marks[p.symbol]), q = num(p.quantity), e = num(p.avg_entry);
      const u = (mark - e) * q;
      return `<tr><td><span class="key" style="background:${colorOfName(p.strategy)}"></span>${esc(shortName(p.strategy))}</td>
        <td>${esc(p.symbol)}</td><td class="r"><span class="side-tag ${q > 0 ? 'up' : 'down'}">${q > 0 ? 'LONG' : 'SHORT'}</span> ${fmtNum(Math.abs(q), 4)}</td>
        <td class="r">${fmtNum(e)}</td><td class="r">${fmtNum(mark)}</td><td class="r ${cls(u)}">${fmtMoney(u, { sign: true })}</td></tr>`;
    }).join('')}</tbody></table></div>`;
}

function feedHtml(events, limit = 12) {
  if (!events.length) return '<div class="empty">Nothing yet.</div>';
  const ic = { INFO: 'info', WARN: 'alert', CRIT: 'alert' };
  return `<div class="feed">${events.slice(0, limit).map((e) => `<div class="ev ${esc(e.severity)}">
    <span class="ic">${icon(ic[e.severity] || 'info', 13)}</span>
    <span class="txt">${esc(eventText(e.text))}${e.count > 1 ? `<span class="count">×${e.count}</span>` : ''}</span>
    <span class="when num">${esc(clock(e.ts))}</span></div>`).join('')}</div>`;
}

function meterHtml(label, used, limit, fmt, marks = []) {
  const f = limit ? Math.min(1, Math.max(0, used / limit)) : 0;
  const color = f >= 0.9 ? 'var(--bad)' : f >= 0.6 ? 'var(--warn)' : 'var(--accent)';
  const state = f >= 0.9 ? 'Near limit' : f >= 0.6 ? 'Elevated' : 'Normal';
  return `<div class="limit"><div class="h"><b>${esc(label)}</b><span class="num">${fmt(used)} <span class="muted" style="font-weight:500">/ ${fmt(limit)}</span></span></div>
    <div class="meter"><div class="track" style="--m:${color}"><div class="fill" style="width:${f * 100}%"></div>
    ${marks.map((m) => `<div class="mark" style="left:${Math.min(100, (m / limit) * 100)}%"></div>`).join('')}</div>
    <div class="foot"><span>${state}</span><span>${Math.round(f * 100)}% of limit</span></div></div></div>`;
}

views.overview = {
  title: 'Overview',
  mount(root) {
    root.innerHTML = `
      <div class="grid g-kpi" id="kpis"></div>
      <div class="grid g-2">
        <section class="card"><div class="card-h"><h2>Equity</h2><span class="sub" id="eqsub"></span>
          <div class="right"><button class="btn sm ghost" id="eqtable" aria-pressed="false">Table</button></div></div>
          <div class="card-b"><div id="eqchart"></div><div id="eqtbl" hidden></div></div></section>
        <section class="card"><div class="card-h"><h2>Activity</h2><div class="right"><button class="btn sm ghost" data-go="activity">All</button></div></div>
          <div class="card-b" id="feed"></div></section>
      </div>
      <section><div class="step-h" style="margin-top:4px"><h3>Strategies</h3><span class="sub" id="stratsub"></span></div>
        <div class="grid g-cards" id="strats"></div></section>
      <section class="card"><div class="card-h"><h2>Open positions</h2><span class="sub">per strategy book</span></div><div class="card-b" id="pos"></div></section>`;
    this.chart = new LineChart($('#eqchart'), { height: 330, label: 'Equity' });
    root.querySelectorAll('[data-go]').forEach((b) => b.addEventListener('click', () => go(b.dataset.go)));
    $('#eqtable').addEventListener('click', (e) => {
      const on = e.currentTarget.getAttribute('aria-pressed') !== 'true';
      e.currentTarget.setAttribute('aria-pressed', on);
      $('#eqchart').hidden = on; $('#eqtbl').hidden = !on;
      this.update();
    });
    this.update();
  },
  update() {
    const st = S.state;
    if (!st) return;
    if (st.launcher && st.launcher.state !== 'running' && !st.snapshot) {
      $('#kpis').innerHTML = `<section class="card kpi" style="grid-column:1/-1;align-items:flex-start;gap:12px;padding:28px">
        <div style="font-size:18px;font-weight:650;letter-spacing:-.01em">The bot isn’t running</div>
        <div class="ink2">Start it in demo mode to see this dashboard come alive — no keys needed.</div>
        <button class="btn primary" id="kgo">${icon('play', 15)}Launch bot</button></section>`;
      $('#kgo').addEventListener('click', () => go('launch'));
    }
    const snap = st.snapshot;
    const curve = st.equity_curve.map(([t, v]) => [new Date(t).getTime(), v]);
    this.chart?.update(curve);
    if (!$('#eqtbl').hidden) {
      const rows = curve.slice(-40).reverse();
      $('#eqtbl').innerHTML = `<div class="table-wrap"><table><thead><tr><th>Time</th><th class="r">Equity</th></tr></thead><tbody>${rows.map(([t, v]) => `<tr><td>${new Date(t).toLocaleString()}</td><td class="r">${fmtMoney(v)}</td></tr>`).join('')}</tbody></table></div>`;
    }
    $('#feed').innerHTML = feedHtml(S.events, 8);
    if (!snap) return;
    const eq = num(snap.equity), peak = num(snap.peak_equity), d0 = num(snap.day_start_equity);
    const start = curve.length ? curve[0][1] : eq;
    const day = eq - d0, dd = peak > 0 ? (peak - eq) / peak : 0;
    const risk = st.risk, lim = risk?.limits || {}, use = risk?.usage || {};
    $('#eqsub').textContent = `${fmtMoney(eq - start, { sign: true })} (${pct(start ? eq / start - 1 : 0)}) over this view`;
    $('#kpis').innerHTML = `
      <section class="card kpi hero"><div class="label">Account equity</div><div class="value">${fmtMoney(eq)}</div>
        <div class="delta ${cls(day)}">${fmtMoney(day, { sign: true })} · ${pct(d0 ? day / d0 : 0)} today</div></section>
      <section class="card"><div class="card-h"><h2>Risk limits</h2><span class="sub">live usage · enforced on every order</span>
          <div class="right"><button class="btn sm ghost" data-go="risk">Details</button></div></div>
        <div class="riskrow">
          ${meterHtml('Drawdown', dd, lim.drawdown || 0.18, (x) => pct(x, 1, false), [lim.drawdown_halve || 0.12])}
          ${meterHtml('Gross exposure', use.gross_leverage || 0, lim.gross_leverage || 3, (x) => x.toFixed(2) + '×')}
          ${meterHtml('1-day VaR (99%)', use.var_99_1d || 0, lim.var_99_1d || 0.04, (x) => pct(x, 1, false))}
        </div></section>`;
    $('#kpis').querySelectorAll('[data-go]').forEach((b) => b.addEventListener('click', () => go(b.dataset.go)));
    const attr = snap.attribution || {};
    $('#stratsub').textContent = `${st.strategies.length} running`;
    $('#strats').innerHTML = st.strategies.length ? st.strategies.map((s) => strategyCard(s, attr[s.name], snap.positions, snap.marks)).join('')
      : '<div class="card card-b empty">No strategies running.</div>';
    $('#pos').innerHTML = positionsTable(snap.positions, snap.marks);
  },
};

views.markets = {
  title: 'Markets',
  mount(root) {
    root.innerHTML = `
      <div class="tabs" id="symtabs" role="group" aria-label="Symbol"></div>
      <section class="card"><div class="card-h"><h2 id="symtitle">—</h2><span class="sub" id="symsub"></span>
        <div class="right"><button class="btn sm ghost" id="ctable" aria-pressed="false">Table</button></div></div>
        <div class="card-b"><div id="candles"></div><div id="ctbl" hidden></div>
          <div class="chart-foot" id="clegend"></div></div></section>
      <section class="card"><div class="card-h"><h2>Fills on this market</h2></div><div class="card-b" id="symfills"></div></section>`;
    this.chart = new CandleChart($('#candles'), { height: 400 });
    $('#ctable').addEventListener('click', (e) => {
      const on = e.currentTarget.getAttribute('aria-pressed') !== 'true';
      e.currentTarget.setAttribute('aria-pressed', on);
      $('#candles').hidden = on; $('#ctbl').hidden = !on;
      this.update();
    });
    this.update();
  },
  async update() {
    const syms = S.state?.symbols || [];
    if (!S.symbol || !syms.includes(S.symbol)) S.symbol = syms[0] || null;
    $('#symtabs').innerHTML = syms.map((s) => `<button aria-pressed="${s === S.symbol}" data-s="${esc(s)}">${esc(s)}</button>`).join('')
      || '<span class="muted">No market data yet — start the bot.</span>';
    $('#symtabs').querySelectorAll('button').forEach((b) => b.addEventListener('click', () => { S.symbol = b.dataset.s; this.update(); }));
    if (!S.symbol) return;
    const candles = await api('/api/candles?symbol=' + encodeURIComponent(S.symbol) + '&limit=400');
    const fills = S.fills.filter((f) => f.symbol === S.symbol);
    const markers = fills.map((f) => ({ t: new Date(f.ts).getTime() / 1000, side: f.side, strategy: f.strategy, amount: num(f.amount) }));
    this.chart.update(candles, markers, colorOfName, nameOf);
    const last = candles[candles.length - 1], first = candles[0];
    $('#symtitle').textContent = S.symbol;
    $('#symsub').innerHTML = last ? `<span class="num" style="font-weight:650;color:var(--ink)">${fmtNum(last[4])}</span> <span class="num ${cls(last[4] - first[1])}">${pct(last[4] / first[1] - 1)}</span> over ${candles.length} min` : '';
    const strats = [...new Set(fills.map((f) => f.strategy))];
    $('#clegend').innerHTML = `
      <span class="legend-item"><svg width="10" height="14"><rect x="1" y="2" width="8" height="10" rx="1.5" fill="var(--surface)" stroke="var(--good)"/></svg>Up candle</span>
      <span class="legend-item"><svg width="10" height="14"><rect x="1" y="2" width="8" height="10" rx="1.5" fill="var(--bad)"/></svg>Down candle</span>
      ${strats.map((s) => `<span class="legend-item"><span class="sw" style="background:${colorOfName(s)}"></span>${esc(nameOf(s))}</span>`).join('')}
      ${strats.length ? '<span class="muted">▲ buy · ▼ sell</span>' : ''}`;
    if (!$('#ctbl').hidden) {
      $('#ctbl').innerHTML = `<div class="table-wrap"><table><thead><tr><th>Time</th><th class="r">Open</th><th class="r">High</th><th class="r">Low</th><th class="r">Close</th></tr></thead><tbody>${candles.slice(-40).reverse().map((c) => `<tr><td>${new Date(c[0] * 1000).toLocaleTimeString()}</td>${c.slice(1, 5).map((v) => `<td class="r">${fmtNum(v)}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
    }
    $('#symfills').innerHTML = fillsTable(fills, 12);
  },
};

function fillsTable(fills, limit = 30) {
  if (!fills.length) return '<div class="empty">No fills yet.</div>';
  return `<div class="table-wrap"><table><thead><tr><th>Time</th><th>Strategy</th><th>Side</th><th>Symbol</th><th class="r">Size</th><th class="r">Price</th><th class="r">Fee</th><th class="r">Slippage</th></tr></thead><tbody>
    ${fills.slice(0, limit).map((f) => `<tr><td>${esc(new Date(f.ts).toLocaleTimeString())}</td>
      <td><span class="key" style="background:${colorOfName(f.strategy)}"></span>${esc(shortName(f.strategy))}</td>
      <td><span class="side-tag ${f.side === 'buy' ? 'up' : 'down'}">${f.side === 'buy' ? 'BUY' : 'SELL'}</span></td>
      <td>${esc(f.symbol)}</td><td class="r">${fmtNum(num(f.amount), 4)}</td><td class="r">${fmtNum(num(f.price))}</td>
      <td class="r">${fmtMoney(num(f.fee))}</td><td class="r">${f.slippage_bps == null ? '—' : fmtNum(f.slippage_bps, 1) + ' bps'}</td></tr>`).join('')}</tbody></table></div>`;
}

views.strategies = {
  title: 'Strategies',
  mount(root) {
    root.innerHTML = '<div class="grid g-cards" id="scards"></div><section class="card"><div class="card-h"><h2>Attribution</h2><span class="sub">realised, since this run started</span></div><div class="card-b" id="attr"></div></section>';
    this.update();
  },
  update() {
    const st = S.state, snap = st?.snapshot;
    if (!st?.strategies.length) { $('#scards').innerHTML = '<div class="card card-b empty">No strategies running. Launch the bot to see them here.</div>'; $('#attr').innerHTML = ''; return; }
    const attr = snap?.attribution || {};
    $('#scards').innerHTML = st.strategies.map((s) => {
      const cat = byClass()[s.cls];
      return strategyCard(s, attr[s.name], snap?.positions || [], snap?.marks || {})
        .replace('</article>', `<div class="ink2" style="font-size:12.5px">${esc(cat?.description || s.description)}</div></article>`);
    }).join('');
    const rows = st.strategies.map((s) => ({ s, a: attr[s.name] || { gross_pnl: 0, fees: 0, funding: 0, net_pnl: 0, cost_ratio: 0 } }));
    $('#attr').innerHTML = `<div class="table-wrap"><table><thead><tr><th>Strategy</th><th class="r">Gross</th><th class="r">Fees</th><th class="r">Funding</th><th class="r">Net realised</th><th class="r">Costs / gross</th></tr></thead><tbody>
      ${rows.map(({ s, a }) => `<tr><td><span class="key" style="background:${colorOfClass(s.cls)}"></span>${esc(nameOf(s.name))}</td>
        <td class="r ${cls(a.gross_pnl)}">${fmtMoney(a.gross_pnl, { sign: true })}</td><td class="r">${fmtMoney(-a.fees)}</td>
        <td class="r ${cls(a.funding)}">${fmtMoney(a.funding, { sign: true })}</td><td class="r ${cls(a.net_pnl)}"><b>${fmtMoney(a.net_pnl, { sign: true })}</b></td>
        <td class="r ${a.cost_ratio > 0.35 ? 'down' : ''}">${a.gross_pnl > 0 ? pct(a.cost_ratio, 1, false) : '—'}</td></tr>`).join('')}</tbody></table></div>
      <p class="muted" style="margin:12px 0 0;font-size:12px">Costs above 35% of gross means the strategy is an execution problem, not an edge (docs/03 §5).</p>`;
  },
};

views.risk = {
  title: 'Risk',
  mount(root) {
    root.innerHTML = `<section class="card killpanel" id="kill"></section>
      <section class="card"><div class="card-h"><h2>Limits</h2><span class="sub">enforced by the risk service on every order and every mark</span></div><div class="card-b limits" id="limits"></div></section>
      <div class="grid g-2">
        <section class="card"><div class="card-h"><h2>Recent vetoes</h2></div><div class="card-b" id="vetoes"></div></section>
        <section class="card"><div class="card-h"><h2>Safety layers</h2></div><div class="card-b" id="layers"></div></section>
      </div>`;
    this.update();
  },
  update() {
    const st = S.state, r = st?.risk;
    const killed = st?.kill_active;
    const halted = r?.halted_until && new Date(r.halted_until) > new Date();
    const running = st?.launcher?.state === 'running';
    $('#kill').innerHTML = `<div class="shield ${killed ? 'killed' : 'armed'}">${icon(killed ? 'alert' : 'shield', 26)}</div>
      <div><h3>${killed ? 'Kill switch engaged' : halted ? 'Entries halted until midnight UTC' : 'Armed — trading normally'}</h3>
        <p>${killed ? 'All books were flattened and new entries are blocked. Rearm once you understand why it fired.'
          : halted ? 'The daily loss limit was hit. Positions were flattened; entries resume at ' + esc(new Date(r.halted_until).toLocaleTimeString()) + '.'
          : 'Kill flattens every book and blocks entries; it also reaches the out-of-process watchdog.'}</p></div>
      <div class="actions" style="display:flex;gap:8px;flex-wrap:wrap">
        <button class="btn" data-c="flatten" ${running ? '' : 'disabled'}>Flatten all</button>
        ${killed ? `<button class="btn primary" data-c="rearm">${icon('shield', 15)}Rearm</button>` : `<button class="btn danger" data-c="kill" ${running ? '' : 'disabled'}>Kill switch</button>`}</div>`;
    $('#kill').querySelectorAll('[data-c]').forEach((b) => b.addEventListener('click', () => control(b.dataset.c)));
    const L = r?.limits || {}, U = r?.usage || {};
    const P = (x) => pct(x, 1, false), X = (x) => (x ?? 0).toFixed(2) + '×';
    $('#limits').innerHTML = !r ? '<div class="empty" style="grid-column:1/-1">Start the bot to see live limit usage.</div>' : [
      meterHtml('Peak-to-trough drawdown', U.drawdown || 0, L.drawdown, P, [L.drawdown_halve]),
      meterHtml('Daily loss', U.daily_loss || 0, L.daily_loss, P),
      meterHtml('Weekly loss', U.weekly_loss || 0, L.weekly_loss, P),
      meterHtml('Gross leverage', U.gross_leverage || 0, L.gross_leverage, X),
      meterHtml('Net leverage', U.net_leverage || 0, L.net_leverage, X),
      meterHtml('1-day VaR (99%)', U.var_99_1d || 0, L.var_99_1d, P),
    ].join('');
    const groups = new Map();
    for (const v of S.vetoes || []) {
      const b = (v.breaches || [])[0] || {};
      const k = (b.limit || v.note) + '|' + v.note;
      const g = groups.get(k) || { n: 0, b, v }; g.n++; groups.set(k, g);
    }
    $('#vetoes').innerHTML = groups.size ? `<div class="table-wrap"><table><thead><tr><th>Limit</th><th class="r">Count</th><th>Detail</th></tr></thead><tbody>
      ${[...groups.values()].map(({ n, b, v }) => `<tr><td><b>${esc(b.limit || 'veto')}</b></td><td class="r">×${n}</td><td class="ink2">${esc(v.note)}</td></tr>`).join('')}</tbody></table></div>`
      : '<div class="empty">No vetoes — every order passed.</div>';
    const stale = r?.stale_symbols || [];
    $('#layers').innerHTML = `<div class="feed">
      ${[['L1 · Risk service', 'Checks every intent and every mark; flattens on a breach', killed ? 'Engaged' : 'Armed', !killed],
         ['L2 · Watchdog', 'Separate process & keys; flattens if heartbeats stop or on a global kill', 'Deploy separately', true],
         ['L3 · Venue stops', 'Stop orders resting at the exchange, wider than each strategy’s own stop', 'On every protected book', true],
         ['Market data', stale.length ? 'Stale: ' + stale.join(', ') + ' — entries blocked' : 'All feeds fresh', stale.length ? 'Stale' : 'Fresh', !stale.length]]
        .map(([t, d, s, ok]) => `<div class="ev ${ok ? 'INFO' : 'CRIT'}"><span class="ic">${icon(ok ? 'check' : 'alert', 13)}</span>
          <span class="txt"><b>${esc(t)}</b><br><span class="ink2">${esc(d)}</span></span><span class="when">${esc(s)}</span></div>`).join('')}</div>`;
  },
};

views.activity = {
  title: 'Activity',
  mount(root) {
    root.innerHTML = `<div class="grid g-2">
      <section class="card"><div class="card-h"><h2>Fills</h2><span class="sub">newest first · includes history from the journal</span></div><div class="card-b" id="afills"></div></section>
      <section class="card"><div class="card-h"><h2>Events</h2></div><div class="card-b" id="aevents"></div></section></div>`;
    this.update();
  },
  update() {
    $('#afills').innerHTML = fillsTable(S.fills, 60);
    $('#aevents').innerHTML = feedHtml(S.events, 60);
  },
};

// ------------------------------------------------------------------ router + data
function go(view) {
  if (!views[view]) view = 'overview';
  S.view = view;
  if (location.hash !== '#/' + view) history.replaceState(null, '', '#/' + view);
  document.querySelectorAll('.nav button').forEach((b) => b.setAttribute('aria-current', b.dataset.view === view ? 'page' : 'false'));
  $('#title').textContent = views[view].title;
  views.overview.chart?.destroy?.(); views.markets.chart?.destroy?.();
  views.overview.chart = null; views.markets.chart = null;
  const root = $('#view');
  root.replaceChildren();
  views[view].mount(root);
  window.scrollTo({ top: 0 });
  updateChrome();
}

let inflight = null;
async function refresh() {
  if (inflight) return inflight;
  inflight = (async () => {
    try {
      const [state, fills, events, vetoes] = await Promise.all([
        api('/api/state'), api('/api/fills?limit=200'), api('/api/events?limit=120'), api('/api/vetoes?limit=100')]);
      S.state = state; S.fills = fills; S.events = events; S.vetoes = vetoes;
      if (!S.catalog) S.catalog = (await api('/api/launcher').catch(() => null))?.catalog || null;
      updateChrome();
      await views[S.view].update?.();
    } catch (e) {
      console.warn('refresh failed', e);
    } finally { inflight = null; }
  })();
  return inflight;
}

function connect() {
  const ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws');
  let pending = null;
  ws.onopen = () => { S.connected = true; updateChrome(); };
  ws.onmessage = () => { if (!pending) pending = setTimeout(() => { pending = null; refresh(); }, 350); };
  ws.onclose = () => { S.connected = false; updateChrome(); setTimeout(connect, 2000); };
}

async function boot() {
  shell();
  await refresh();
  const wanted = (location.hash.match(/#\/(\w+)/) || [])[1];
  const running = S.state?.launcher?.state === 'running';
  go(views[wanted] ? wanted : running ? 'overview' : S.state?.launcher ? 'launch' : 'overview');
  connect();
  setInterval(() => { refresh(); }, 5000);
  setInterval(() => { if (S.state?.launcher?.state === 'running') updateChrome(); }, 1000);
}
boot();
