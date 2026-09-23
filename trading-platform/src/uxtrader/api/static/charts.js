// Hand-drawn SVG charts. Mark specs follow the dataviz method: 2px lines, a 10% area
// wash, hairline solid gridlines, ≥8px markers with a 2px surface ring, one y-axis,
// selective direct labels, crosshair + tooltip on every plot (keyboard included).

const NS = 'http://www.w3.org/2000/svg';
let uid = 0;

export function niceTicks(min, max, count = 4) {
  if (!(max > min)) { const p = Math.abs(min) * 0.005 || 1; min -= p; max += p; }
  const raw = (max - min) / count;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) || raw;
  const lo = Math.ceil(min / step) * step;
  const out = [];
  for (let v = lo; v <= max + step * 1e-9; v += step) out.push(+v.toFixed(10));
  return out;
}

export function fmtNum(v, digits) {
  if (v == null || !isFinite(v)) return '—';
  const a = Math.abs(v);
  const d = digits ?? (a >= 1000 ? 0 : a >= 100 ? 1 : a >= 1 ? 2 : 4);
  return v.toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });
}

export function fmtMoney(v, { sign = false, compact = false } = {}) {
  if (v == null || !isFinite(v)) return '—';
  const a = Math.abs(v);
  let s;
  if (compact && a >= 1e6) s = (a / 1e6).toFixed(2) + 'M';
  else if (compact && a >= 1e4) s = (a / 1e3).toFixed(1) + 'K';
  else s = a.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const pre = v < 0 ? '−' : sign ? '+' : '';
  return pre + '$' + s;
}

function timeFmt(spanMs) {
  if (spanMs < 15 * 60e3) return (t) => new Date(t).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  if (spanMs < 2 * 864e5) return (t) => new Date(t).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  return (t) => new Date(t).toLocaleDateString([], { month: 'short', day: 'numeric' });
}

function el(tag, attrs = {}, parent) {
  const n = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  if (parent) parent.appendChild(n);
  return n;
}

function text(parent, x, y, str, cls, anchor = 'start') {
  const t = el('text', { x, y, class: cls, 'text-anchor': anchor }, parent);
  t.textContent = str;
  return t;
}

class Base {
  constructor(host, height) {
    this.host = host;
    this.height = height;
    host.classList.add('chart');
    this.svg = el('svg', { height, tabindex: 0, role: 'img' }, host);
    this.tip = document.createElement('div');
    this.tip.className = 'tip';
    host.appendChild(this.tip);
    this.ro = new ResizeObserver(() => this.draw());
    this.ro.observe(host);
    this.svg.addEventListener('pointermove', (e) => this.hover(e));
    this.svg.addEventListener('pointerleave', () => this.clearHover());
    this.svg.addEventListener('blur', () => this.clearHover());
    this.svg.addEventListener('keydown', (e) => this.key(e));
    this.idx = null;
  }
  get width() { return Math.max(this.host.clientWidth, 200); }
  clearHover() { this.idx = null; this.tip.classList.remove('on'); this.hoverLayer?.replaceChildren(); }
  key(e) {
    const n = this.count?.() ?? 0;
    if (!n) return;
    if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
      e.preventDefault();
      const i = this.idx == null ? n - 1 : this.idx + (e.key === 'ArrowRight' ? 1 : -1);
      this.showAt(Math.max(0, Math.min(n - 1, i)));
    }
  }
  placeTip(x, y) {
    const w = this.width;
    this.tip.style.left = Math.max(70, Math.min(w - 70, x)) + 'px';
    this.tip.style.top = Math.max(y, 56) + 'px';
    this.tip.classList.add('on');
  }
  destroy() { this.ro.disconnect(); this.host.replaceChildren(); }
}

// ---------------------------------------------------------------- line / area
export class LineChart extends Base {
  constructor(host, { height = 260, format = (v) => fmtMoney(v), label = 'Series' } = {}) {
    super(host, height);
    this.format = format;
    // Axis ticks: only as many decimals as the tick step needs ($99,900 not $99,900.00).
    this.tickFormat = (v, step) => {
      const d = step >= 1 ? 0 : Math.min(4, Math.ceil(-Math.log10(step)));
      return (v < 0 ? '−$' : '$') + Math.abs(v).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });
    };
    this.label = label;
    this.points = [];
    this.gid = 'g' + (++uid);
    this.svg.setAttribute('aria-label', label + ' over time');
  }
  count() { return this.points.length; }
  update(points) { this.points = points; this.draw(); }

  draw() {
    const W = this.width, H = this.height, P = { l: 4, r: 70, t: 14, b: 26 };
    this.svg.setAttribute('width', W);
    this.svg.replaceChildren();
    const pts = this.points;
    const pw = W - P.l - P.r, ph = H - P.t - P.b;
    if (pts.length < 2) {
      text(this.svg, W / 2, H / 2, 'Waiting for data…', 'tick', 'middle');
      return;
    }
    let lo = Infinity, hi = -Infinity;
    for (const [, v] of pts) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
    const pad = (hi - lo) * 0.12 || Math.abs(hi) * 0.002 || 1;
    const ticks = niceTicks(lo - pad, hi + pad, 4);
    const y0 = Math.min(lo - pad, ticks[0]), y1 = Math.max(hi + pad, ticks[ticks.length - 1]);
    const t0 = pts[0][0], t1 = pts[pts.length - 1][0];
    const X = (t) => P.l + ((t - t0) / (t1 - t0 || 1)) * pw;
    const Y = (v) => P.t + (1 - (v - y0) / (y1 - y0)) * ph;
    this.X = X; this.Y = Y; this.P = P;

    const defs = el('defs', {}, this.svg);
    const g = el('linearGradient', { id: this.gid, x1: 0, x2: 0, y1: 0, y2: 1 }, defs);
    el('stop', { offset: '0%', 'stop-color': 'var(--accent)', 'stop-opacity': 0.16 }, g);
    el('stop', { offset: '100%', 'stop-color': 'var(--accent)', 'stop-opacity': 0 }, g);

    const step = ticks.length > 1 ? ticks[1] - ticks[0] : 1;
    for (const t of ticks) {
      const y = Math.round(Y(t)) + 0.5;
      el('line', { x1: P.l, x2: P.l + pw, y1: y, y2: y, class: 'gridline' }, this.svg);
      text(this.svg, P.l + pw + 10, y + 4, this.tickFormat(t, step), 'tick');
    }
    const tf = timeFmt(t1 - t0);
    const nx = Math.max(2, Math.floor(pw / 110));
    for (let i = 0; i <= nx; i++) {
      const t = t0 + (i / nx) * (t1 - t0);
      text(this.svg, X(t), H - 6, tf(t), 'tick', i === 0 ? 'start' : i === nx ? 'end' : 'middle');
    }
    let d = '';
    pts.forEach(([t, v], i) => { d += (i ? 'L' : 'M') + X(t).toFixed(1) + ',' + Y(v).toFixed(1); });
    const base = P.t + ph;
    el('path', { d: `${d}L${X(t1).toFixed(1)},${base}L${X(t0).toFixed(1)},${base}Z`, fill: `url(#${this.gid})` }, this.svg);
    el('path', { d, fill: 'none', stroke: 'var(--accent)', 'stroke-width': 2, 'stroke-linejoin': 'round', 'stroke-linecap': 'round' }, this.svg);

    const [lt, lv] = pts[pts.length - 1];
    el('circle', { cx: X(lt), cy: Y(lv), r: 4, fill: 'var(--accent)', stroke: 'var(--surface)', 'stroke-width': 2 }, this.svg);
    // Direct end label, parked in the axis gutter so it never collides with the line.
    const ey = Math.max(P.t + 8, Math.min(base - 4, Y(lv)));
    const tag = el('g', {}, this.svg);
    const label = this.format(lv);
    const tw = label.length * 7 + 12;
    el('rect', { x: P.l + pw + 4, y: ey - 11, width: tw, height: 22, rx: 6, fill: 'var(--raised)', stroke: 'var(--border-strong)' }, tag);
    text(tag, P.l + pw + 10, ey + 4, label, 'endlabel');
    this.hoverLayer = el('g', {}, this.svg);
    if (this.idx != null) this.showAt(Math.min(this.idx, pts.length - 1));
  }

  hover(e) {
    if (this.points.length < 2) return;
    const r = this.svg.getBoundingClientRect();
    const x = e.clientX - r.left;
    const t0 = this.points[0][0], t1 = this.points[this.points.length - 1][0];
    const pw = this.width - this.P.l - this.P.r;
    const t = t0 + ((x - this.P.l) / pw) * (t1 - t0);
    let lo = 0, hi = this.points.length - 1;
    while (hi - lo > 1) { const m = (lo + hi) >> 1; if (this.points[m][0] < t) lo = m; else hi = m; }
    this.showAt(Math.abs(this.points[lo][0] - t) < Math.abs(this.points[hi][0] - t) ? lo : hi);
  }

  showAt(i) {
    this.idx = i;
    const [t, v] = this.points[i];
    const x = this.X(t), y = this.Y(v);
    this.hoverLayer.replaceChildren();
    el('line', { x1: Math.round(x) + 0.5, x2: Math.round(x) + 0.5, y1: this.P.t, y2: this.height - this.P.b, class: 'crosshair' }, this.hoverLayer);
    el('circle', { cx: x, cy: y, r: 5, fill: 'var(--accent)', stroke: 'var(--surface)', 'stroke-width': 2 }, this.hoverLayer);
    this.tip.replaceChildren();
    const vEl = document.createElement('div'); vEl.className = 'v'; vEl.textContent = this.format(v);
    const tEl = document.createElement('div'); tEl.className = 't';
    tEl.textContent = new Date(t).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit' });
    this.tip.append(vEl, tEl);
    this.placeTip(x, y);
  }
}

// ---------------------------------------------------------------- candles
export class CandleChart extends Base {
  constructor(host, { height = 380 } = {}) {
    super(host, height);
    this.candles = [];
    this.markers = [];
    this.colorFor = () => 'var(--accent)';
    this.nameFor = (s) => s;
    this.svg.setAttribute('aria-label', 'Price candles with this bot’s fills');
  }
  count() { return this.view?.length ?? 0; }
  update(candles, markers = [], colorFor, nameFor) {
    this.candles = candles;
    this.markers = markers;
    if (colorFor) this.colorFor = colorFor;
    if (nameFor) this.nameFor = nameFor;
    this.draw();
  }

  draw() {
    const W = this.width, H = this.height, P = { l: 4, r: 76, t: 16, b: 26 };
    this.svg.setAttribute('width', W);
    this.svg.replaceChildren();
    const pw = W - P.l - P.r, ph = H - P.t - P.b;
    const maxN = Math.max(20, Math.floor(pw / 8));
    const view = this.candles.slice(-maxN);
    this.view = view;
    if (view.length < 2) { text(this.svg, W / 2, H / 2, 'Waiting for candles…', 'tick', 'middle'); return; }
    const tStart = view[0][0], tEnd = view[view.length - 1][0] + 60;
    const marks = this.markers.filter((m) => m.t >= tStart && m.t < tEnd);
    let lo = Infinity, hi = -Infinity;
    for (const c of view) { lo = Math.min(lo, c[3]); hi = Math.max(hi, c[2]); }
    const pad = (hi - lo) * 0.10 || hi * 0.001;
    const ticks = niceTicks(lo - pad, hi + pad, 5);
    const y0 = Math.min(lo - pad, ticks[0]), y1 = Math.max(hi + pad, ticks[ticks.length - 1]);
    const band = pw / view.length;
    const bw = Math.max(2, Math.min(9, band * 0.62));
    const X = (i) => P.l + band * (i + 0.5);
    const Y = (v) => P.t + (1 - (v - y0) / (y1 - y0)) * ph;
    Object.assign(this, { X, Y, P, band });

    for (const t of ticks) {
      const y = Math.round(Y(t)) + 0.5;
      el('line', { x1: P.l, x2: P.l + pw, y1: y, y2: y, class: 'gridline' }, this.svg);
      text(this.svg, P.l + pw + 10, y + 4, fmtNum(t), 'tick');
    }
    const tf = timeFmt((tEnd - tStart) * 1000);
    const nx = Math.max(2, Math.floor(pw / 120));
    for (let k = 0; k <= nx; k++) {
      const i = Math.round((k / nx) * (view.length - 1));
      text(this.svg, X(i), H - 6, tf(view[i][0] * 1000), 'tick', k === 0 ? 'start' : k === nx ? 'end' : 'middle');
    }
    const g = el('g', {}, this.svg);
    view.forEach((c, i) => {
      const [, o, h, l, cl] = c;
      const up = cl >= o;
      const col = up ? 'var(--good)' : 'var(--bad)';
      const x = Math.round(X(i)) + 0.5;
      el('line', { x1: x, x2: x, y1: Y(h), y2: Y(l), stroke: col, 'stroke-width': 1 }, g);
      const top = Y(Math.max(o, cl)), bot = Y(Math.min(o, cl));
      // Up = hollow, down = filled: direction is readable without colour.
      el('rect', { x: x - bw / 2, y: top, width: bw, height: Math.max(1, bot - top), rx: 1.5,
                   fill: up ? 'var(--surface)' : col, stroke: col, 'stroke-width': 1 }, g);
    });
    const byT = new Map(view.map((c, i) => [Math.floor(c[0] / 60), i]));
    this.marksAt = new Map();
    for (const m of marks) {
      const i = byT.get(Math.floor(m.t / 60));
      if (i == null) continue;
      (this.marksAt.get(i) || this.marksAt.set(i, []).get(i)).push(m);
      const c = view[i], x = X(i), s = 6;
      const pts = m.side === 'buy'
        ? [[x, Y(c[3]) + 6], [x - s, Y(c[3]) + 6 + s * 1.6], [x + s, Y(c[3]) + 6 + s * 1.6]]
        : [[x, Y(c[2]) - 6], [x - s, Y(c[2]) - 6 - s * 1.6], [x + s, Y(c[2]) - 6 - s * 1.6]];
      el('polygon', { points: pts.map((p) => p.join(',')).join(' '), fill: this.colorFor(m.strategy),
                      stroke: 'var(--surface)', 'stroke-width': 2, 'stroke-linejoin': 'round' }, this.svg);
    }
    const last = view[view.length - 1][4];
    const ly = Math.round(Y(last)) + 0.5;
    el('line', { x1: P.l, x2: P.l + pw, y1: ly, y2: ly, stroke: 'var(--ink-2)', 'stroke-width': 1, opacity: 0.35 }, this.svg);
    const tag = fmtNum(last);
    el('rect', { x: P.l + pw + 4, y: ly - 11, width: tag.length * 7 + 12, height: 22, rx: 6, fill: 'var(--ink)' }, this.svg);
    const tt = text(this.svg, P.l + pw + 10, ly + 4, tag, 'endlabel');
    tt.setAttribute('style', 'fill: var(--page)');
    this.hoverLayer = el('g', {}, this.svg);
    if (this.idx != null) this.showAt(Math.min(this.idx, view.length - 1));
  }

  hover(e) {
    if (!this.view || this.view.length < 2) return;
    const x = e.clientX - this.svg.getBoundingClientRect().left;
    this.showAt(Math.max(0, Math.min(this.view.length - 1, Math.floor((x - this.P.l) / this.band))));
  }

  showAt(i) {
    this.idx = i;
    const c = this.view[i];
    const x = this.X(i);
    this.hoverLayer.replaceChildren();
    el('line', { x1: Math.round(x) + 0.5, x2: Math.round(x) + 0.5, y1: this.P.t, y2: this.height - this.P.b, class: 'crosshair' }, this.hoverLayer);
    this.tip.replaceChildren();
    const chg = (c[4] / c[1] - 1) * 100;
    const head = document.createElement('div'); head.className = 'v'; head.textContent = fmtNum(c[4]);
    const when = document.createElement('div'); when.className = 't';
    when.textContent = new Date(c[0] * 1000).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
    this.tip.append(head, when);
    for (const [k, v] of [['Open', c[1]], ['High', c[2]], ['Low', c[3]], ['Change', null]]) {
      const r = document.createElement('div'); r.className = 'row';
      const a = document.createElement('span'); a.className = 't'; a.textContent = k;
      const b = document.createElement('span');
      b.textContent = v == null ? (chg >= 0 ? '+' : '') + chg.toFixed(2) + '%' : fmtNum(v);
      if (v == null) b.className = chg >= 0 ? 'up' : 'down';
      r.append(a, b); this.tip.append(r);
    }
    for (const m of this.marksAt?.get(i) || []) {
      const r = document.createElement('div'); r.className = 'row';
      const a = document.createElement('span');
      const lk = document.createElement('span'); lk.className = 'lk'; lk.style.background = this.colorFor(m.strategy);
      a.append(lk, document.createTextNode(`${this.nameFor(m.strategy)} ${m.side}`));
      const b = document.createElement('span'); b.textContent = fmtNum(m.amount, 4);
      r.append(a, b); this.tip.append(r);
    }
    this.placeTip(x, this.Y(c[2]));
  }
}
