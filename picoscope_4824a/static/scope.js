/* PicoScope 4824A panel.
 *
 * Every control here calls the same /api/<command> route the CLI calls, so the
 * page has no capability the command line lacks. The command list at the
 * bottom of the sidebar is rendered from /describe, i.e. from the same
 * registry the CLI builds its subcommands from.
 *
 * Four views. The first three are acquisition modes, and the instrument runs
 * one at a time:
 *   Scope — triggered sweeps (sweep-*): a repetitive signal stands still, and
 *           the trigger level and position are markers you can drag.
 *   Roll  — continuous streaming (stream-*): a rolling min/max envelope, and
 *           the mode that records a long stream to disk.
 *   DAQ   — fixed-length acquisitions written as usphere-DAQ HDF5 files
 *           (daq-*), with the waveform and ASD of each one as it lands.
 *   Files — the recordings library (files, file-waveform, file-spectrum):
 *           waveforms, ASD or PSD, several files at once, or one averaged
 *           curve over a whole run.
 * The page follows the instrument: if another client starts a stream, this
 * page switches to Roll rather than showing controls for a mode that is off.
 *
 * URLs are RELATIVE (no leading slash) so the page works both standalone at /
 * and mounted under /scope by the xsphere-daq panel.
 */
'use strict';

const CHANNELS = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'];
// SIX digits, not the shorter '#5cf' form, and that is load-bearing: the
// envelope band below is filled with `color + '38'` to get 22% alpha, and
// '#5cf38' is a 5-digit hex -- not a valid CSS colour. An invalid fillStyle
// is a SILENT no-op in Canvas2D, so the band kept whatever fillStyle the
// axis labels left behind ('#666') and painted flat opaque grey over the
// grid instead of a translucent channel-coloured band. Expanding to 6
// digits makes `color + '38'` a valid 8-digit hex. Same colours as before
// (3-digit hex expands by doubling each digit), so the traces are unchanged.
const COLORS = ['#55ccff', '#ffdd66', '#ff8866', '#88ff88',
                '#cc99ff', '#66cccc', '#ffaa88', '#aaaaff'];
const RANGES = ['R_10MV', 'R_20MV', 'R_50MV', 'R_100MV', 'R_200MV', 'R_500MV',
                'R_1V', 'R_2V', 'R_5V', 'R_10V', 'R_20V', 'R_50V'];
const RANGE_LABEL = { R_10MV: '±10 mV', R_20MV: '±20 mV', R_50MV: '±50 mV',
  R_100MV: '±100 mV', R_200MV: '±200 mV', R_500MV: '±500 mV', R_1V: '±1 V',
  R_2V: '±2 V', R_5V: '±5 V', R_10V: '±10 V', R_20V: '±20 V', R_50V: '±50 V' };

/** Graticule, as on a bench scope: 10 × 8 divisions. */
const DIV_X = 10;
const DIV_Y = 8;
/** Time/div steps offered, 1-2-5. The API accepts 10 ns .. 10 s. */
const TIME_PER_DIV = [100e-9, 200e-9, 500e-9, 1e-6, 2e-6, 5e-6, 10e-6, 20e-6,
  50e-6, 100e-6, 200e-6, 500e-6, 1e-3, 2e-3, 5e-3, 10e-3, 20e-3, 50e-3,
  100e-3, 200e-3, 500e-3, 1];
/** Corner status, as the server names it -> label and colours. */
const INDICATOR = {
  "trig'd":  { text: "Trig'd",   fg: '#8e8', bg: '#143a24', edge: '#2a6a40' },
  auto:      { text: 'Auto',     fg: '#fd8', bg: '#3a2e14', edge: '#7a5a20' },
  wait:      { text: 'Wait',     fg: '#fd8', bg: '#3a2e14', edge: '#7a5a20' },
  armed:     { text: 'Armed',    fg: '#bbb', bg: '#222',    edge: '#444' },
  'free-run':{ text: 'Free run', fg: '#bbb', bg: '#222',    edge: '#444' },
  stop:      { text: 'Stop',     fg: '#f99', bg: '#3a1414', edge: '#7a3030' },
  error:     { text: 'Error',    fg: '#f99', bg: '#3a1414', edge: '#7a3030' },
};

const $ = (id) => document.getElementById(id);
const state = {
  open: false, streaming: false, recording: false,
  channels: {}, traces: {}, window_s: 10,
  view: 'scope',
  sweepState: null,         // server: sweep-trace .state / status .sweep
  sweepCfg: null,           // current settings, with local edits applied
  cfgHoldUntil: 0,          // ignore server config briefly after a local edit
  sweep: null,              // latest sweep payload
  sweepId: -1,
  drag: null,               // { kind: 'level' | 'position', value }
  geom: null,               // last scope layout, for pointer hit-testing
  daqState: null,           // server: status .daq / daq-preview .state
  daqPreview: null,         // last acquisition, reduced for drawing
  daqId: -1,
  files: { dir: '', listing: null, selected: [], channels: [],
           waveform: null, spectrum: null, busy: false },
};

const VIEW_KEY = 'picoscope.view';

/* ------------------------------------------------------------------ api */

async function api(name, params = {}, mutates = false) {
  const opts = mutates
    ? { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(params) }
    : { method: 'GET' };
  const qs = !mutates && Object.keys(params).length
    ? '?' + new URLSearchParams(params) : '';
  try {
    const resp = await fetch(`api/${name}${qs}`, opts);
    return await resp.json();
  } catch (err) {
    return { ok: false, error: String(err), error_type: 'NetworkError' };
  }
}

let statusHeldUntil = 0;

/** Put a message in the header. An error is held for a few seconds, so the
 *  periodic status line (sayLive) cannot replace it before anyone reads it. */
function say(text, kind, holdMs = kind === 'err' ? 5000 : 0) {
  if (holdMs) statusHeldUntil = Date.now() + holdMs;
  const el = $('status');
  el.textContent = text;
  el.className = 'status' + (kind ? ' ' + kind : '');
}

function sayLive(text, kind) {
  if (Date.now() < statusHeldUntil) return;
  say(text, kind, 0);
}

/** Report a failed command where the user can see it, and return ok-ness. */
function check(result, what) {
  if (result && result.ok) {
    const warn = result.warning || (result.stream && result.stream.warning);
    if (warn) say(warn, 'busy');
    return true;
  }
  say(`${what}: ${(result && result.error) || 'failed'}`, 'err');
  return false;
}

/* ------------------------------------------------------------- channels */

function buildChannels() {
  const host = $('channels');
  host.innerHTML = '';
  CHANNELS.forEach((ch, i) => {
    const row = document.createElement('div');
    row.className = 'chan';
    row.id = `chan-${ch}`;

    const box = document.createElement('input');
    box.type = 'checkbox';
    box.id = `en-${ch}`;
    box.addEventListener('change', () => pushChannel(ch));

    const swatch = document.createElement('span');
    swatch.className = 'swatch';
    swatch.style.background = COLORS[i];

    const name = document.createElement('span');
    name.className = 'name';
    name.textContent = ch;

    const range = document.createElement('select');
    range.id = `rng-${ch}`;
    RANGES.forEach((r) => {
      const opt = document.createElement('option');
      opt.value = r;
      opt.textContent = RANGE_LABEL[r];
      if (r === 'R_5V') opt.selected = true;
      range.appendChild(opt);
    });
    range.addEventListener('change', () => pushChannel(ch));

    const coupling = document.createElement('select');
    coupling.id = `cpl-${ch}`;
    ['DC', 'AC'].forEach((c) => {
      const opt = document.createElement('option');
      opt.value = c; opt.textContent = c;
      coupling.appendChild(opt);
    });
    coupling.addEventListener('change', () => pushChannel(ch));

    row.append(box, swatch, name, range, coupling);
    host.appendChild(row);
  });

  const src = $('t-source');
  CHANNELS.forEach((ch) => {
    const opt = document.createElement('option');
    opt.value = ch; opt.textContent = ch;
    src.appendChild(opt);
  });
}

async function pushChannel(ch) {
  const params = {
    channel: ch,
    enabled: $(`en-${ch}`).checked,
    range: $(`rng-${ch}`).value,
    coupling: $(`cpl-${ch}`).value,
  };
  // Keep an offset set from the CLI; the page has no offset control, and
  // leaving it out would reset it to zero.
  const known = state.channels[ch];
  if (known && known.analogue_offset) params.offset = known.analogue_offset;
  const result = await api('channel-set', params, true);
  if (check(result, `channel ${ch}`)) {
    say(`channel ${ch} updated`);
    refreshPlan();
  }
  refresh();
}

function renderChannels(channels) {
  channels.forEach((c) => {
    state.channels[c.channel] = c;
    const box = $(`en-${c.channel}`);
    const rng = $(`rng-${c.channel}`);
    const cpl = $(`cpl-${c.channel}`);
    const row = $(`chan-${c.channel}`);
    if (!box) return;
    if (document.activeElement !== box) box.checked = c.enabled;
    if (document.activeElement !== rng) rng.value = c.range;
    if (document.activeElement !== cpl) cpl.value = c.coupling;
    row.classList.toggle('off', !c.enabled);
  });
  [...$('t-source').options].forEach((opt) => {
    const c = state.channels[opt.value];
    opt.textContent = opt.value + (c && !c.enabled ? ' (off)' : '');
  });
  renderLegend();
}

function enabledChannels() {
  return CHANNELS.filter((ch) => state.channels[ch] && state.channels[ch].enabled);
}

function renderLegend() {
  const host = $('legend');
  host.innerHTML = '';
  const meas = state.view === 'scope' && state.sweep ? state.sweep.measurements || {} : {};
  enabledChannels().forEach((ch) => {
    const el = document.createElement('span');
    const dot = document.createElement('i');
    dot.style.background = COLORS[CHANNELS.indexOf(ch)];
    let text = `${ch}  ${RANGE_LABEL[state.channels[ch].range] || ''}`;
    const m = meas[ch];
    if (m) {
      text += ` · ${fmtVolts(m.vpp)} pp`;
      if (m.freq_hz) text += ` · ${fmtFreq(m.freq_hz)}`;
    }
    el.append(dot, document.createTextNode(text));
    host.appendChild(el);
  });
}

/* ------------------------------------------------------------ formatting */

function fmtVolts(v) {
  const a = Math.abs(v);
  if (a >= 1) return v.toFixed(2) + ' V';
  if (a >= 0.001) return (v * 1000).toFixed(1) + ' mV';
  if (a === 0) return '0 V';
  return (v * 1e6).toFixed(0) + ' µV';
}

function fmtTime(s) {
  if (s >= 60) return (s / 60).toFixed(s % 60 ? 1 : 0) + 'm';
  return s.toFixed(s < 10 ? 1 : 0) + 's';
}

/** 3 significant figures with an SI prefix: 2.5e-3 -> "2.5 m" + unit. */
function fmtSI(v, unit) {
  if (v === 0 || Math.abs(v) < 1e-15) return '0';
  const a = Math.abs(v);
  const steps = [[1e6, 'M'], [1e3, 'k'], [1, ''], [1e-3, 'm'], [1e-6, 'µ'], [1e-9, 'n']];
  for (const [scale, prefix] of steps) {
    if (a >= scale * 0.9995) {
      return `${Number((v / scale).toPrecision(3))} ${prefix}${unit}`;
    }
  }
  return `${Number((v / 1e-9).toPrecision(3))} n${unit}`;
}

function fmtFreq(hz) { return fmtSI(hz, 'Hz'); }

/** A size in bytes, in whatever unit keeps it readable. */
function fmtSize(bytes) {
  if (bytes >= 1e9) return `${(bytes / 1e9).toFixed(2)} GB`;
  if (bytes >= 1e6) return `${(bytes / 1e6).toFixed(1)} MB`;
  if (bytes >= 1e3) return `${(bytes / 1e3).toFixed(0)} kB`;
  return `${bytes} B`;
}

function fmtBytes(bps) {
  if (bps >= 1e6) return (bps / 1e6).toFixed(2) + ' MB/s';
  if (bps >= 1e3) return (bps / 1e3).toFixed(1) + ' kB/s';
  return bps.toFixed(0) + ' B/s';
}

/** A tidy step for the level: about 1/200 of the screen, on a 1-2-5 grid. */
function niceStep(span) {
  const raw = span / 200;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const r = raw / mag;
  return (r < 1.5 ? 1 : r < 3.5 ? 2 : 5) * mag;
}

function roundTo(v, step) {
  return Number((Math.round(v / step) * step).toPrecision(6));
}

/* ------------------------------------------------------------------ views */

function readStoredView() {
  try { return localStorage.getItem(VIEW_KEY); } catch (err) { return null; }
}

function storeView(view) {
  try { localStorage.setItem(VIEW_KEY, view); } catch (err) { /* private mode */ }
}

function applyView() {
  document.querySelectorAll('[data-view]').forEach((el) => {
    el.hidden = !el.dataset.view.split(',').includes(state.view);
  });
  ['scope', 'roll', 'daq', 'files'].forEach((v) => {
    $(`view-${v}`).setAttribute('aria-checked', String(state.view === v));
  });
  // DAQ and Files draw a waveform above a spectrum; the live modes use one panel.
  $('plot2').hidden = !(state.view === 'daq' || state.view === 'files');
  $('plotnote').textContent = state.view === 'roll'
    ? 'Live min/max envelope. A transient shorter than one bin still shows, '
      + 'because each bin keeps both extremes.'
    : '';
  renderLegend();
  renderMeasurementTable();
  if (state.view === 'files' && !state.files.listing) loadFolder('');
  if (state.view === 'daq') refreshDaqPlan();
  drawPlot();
}

/** Switch views. Switching mode stops the other acquisition — but never a
 *  recording, which is the one thing that would lose data. */
async function setView(view) {
  if (view === state.view) return;
  // Files is a viewer, and DAQ has its own Start button: neither takes the
  // instrument away from a running mode just because someone looked.
  if (view === 'scope' && state.streaming) {
    if (state.recording) {
      say('a recording is running — stop it before switching to Scope', 'err');
      return;
    }
    const r = await api('stream-stop', {}, true);
    if (!check(r, 'stop stream')) return;
    state.streaming = false;
    state.traces = {};
    say('stream stopped — Scope view');
  } else if (view === 'roll' && state.sweepState && state.sweepState.running) {
    const r = await api('sweep-stop', {}, true);
    if (!check(r, 'stop scope')) return;
    renderSweepState(r.sweep);
    say('scope stopped — Roll view');
  }
  state.view = view;
  storeView(view);
  applyView();
  refresh();
}

/* ------------------------------------------------------------- the scope */

function sweepRunning() {
  return !!(state.sweepState && state.sweepState.running);
}

/** Change scope settings. Applied locally at once so controls and markers do
 *  not snap back while the request is in flight. */
async function sweepSet(changes, what = 'setting') {
  if (state.sweepCfg) state.sweepCfg = { ...state.sweepCfg, ...changes };
  state.cfgHoldUntil = Date.now() + 2000;
  syncSweepControls(true);
  drawPlot();
  const result = await api('sweep-set', changes, true);
  state.cfgHoldUntil = 0;
  if (check(result, what)) {
    renderSweepState(result.sweep);
  } else {
    refresh();                            // put the controls back as they are
  }
}

async function runScope(mode) {
  const running = sweepRunning();
  say(mode === 'single' ? 'waiting for one trigger…' : 'starting scope…', 'busy');
  const result = running
    ? await api('sweep-set', { mode }, true)       // Single while running
    : await api('sweep-start', { mode }, true);
  if (check(result, 'scope')) {
    renderSweepState(result.sweep);
    say(mode === 'single' ? 'single sweep armed' : 'scope running');
  }
  pollSweepSoon();
}

async function stopScope() {
  const result = await api('sweep-stop', {}, true);
  if (check(result, 'stop')) {
    renderSweepState(result.sweep);
    say('scope stopped');
  }
}

async function saveSweep() {
  say('saving sweep…', 'busy');
  const result = await api('sweep-save', { name: 'sweep' }, true);
  if (check(result, 'save')) {
    say(`saved ${result.saved.split(/[\\/]/).pop()}`);
    loadRecordings();
  }
}

/** The mode the Run button should use: the trigger-mode control's choice. */
function selectedMode() {
  const on = document.querySelector('#t-mode .seg-btn[aria-checked="true"]');
  return on ? on.dataset.mode : 'auto';
}

function renderSweepState(st) {
  if (!st) return;
  state.sweepState = st;
  if (Date.now() >= state.cfgHoldUntil && !state.drag) {
    state.sweepCfg = { ...st.config };
  }
  syncSweepControls(false);
  renderPlan();
  renderButtons();
}

/** Mirror the settings into the controls, leaving alone whatever the user is
 *  touching. `local` is true when the change came from this page. */
function syncSweepControls(local) {
  const cfg = state.sweepCfg;
  if (!cfg) return;
  const active = document.activeElement;
  // Single is a one-shot, not a standing choice: keep showing the mode that
  // Run will go back to.
  if (cfg.mode !== 'single') {
    document.querySelectorAll('#t-mode .seg-btn').forEach((b) => {
      b.setAttribute('aria-checked', String(b.dataset.mode === cfg.mode));
    });
  }
  if (active !== $('t-source')) $('t-source').value = cfg.source;
  if (active !== $('t-dir')) $('t-dir').value = cfg.direction;
  const lvl = $('t-level');
  if (active !== lvl || local) {
    if (!(state.drag && state.drag.kind === 'level')) lvl.value = cfg.level;
  }
  const rng = state.channels[cfg.source];
  if (rng) lvl.step = String(niceStep(rng.range_volts));
  const auto = $('t-auto');
  if (active !== auto) {
    if (![...auto.options].some((o) => Number(o.value) === cfg.auto_ms)) {
      auto.add(new Option(`${cfg.auto_ms} ms`, String(cfg.auto_ms)));
    }
    auto.value = String(cfg.auto_ms);
  }
  const div = $('tb-div');
  if (active !== div) {
    const hit = [...div.options].find(
      (o) => Math.abs(Number(o.value) - cfg.time_per_div) <= cfg.time_per_div * 1e-6);
    if (!hit) {
      div.add(new Option(`${fmtSI(cfg.time_per_div, 's')}/div`, String(cfg.time_per_div)));
    }
    div.value = hit ? hit.value : String(cfg.time_per_div);
  }
  const smp = $('tb-samples');
  if (active !== smp) {
    if (![...smp.options].some((o) => Number(o.value) === cfg.samples)) {
      smp.add(new Option(cfg.samples.toLocaleString(), String(cfg.samples)));
    }
    smp.value = String(cfg.samples);
  }
  const pos = $('tb-pos');
  const posNow = state.drag && state.drag.kind === 'position' ? state.drag.value : cfg.position;
  if (active !== pos || local) pos.value = posNow;
  $('tb-pos-val').textContent = `${Math.round(posNow)} %`;
}

function renderPlan() {
  const st = state.sweepState;
  const host = $('tb-plan');
  if (!st) { host.textContent = ''; return; }
  const cfg = st.config;
  const lines = [];
  if (st.plan) {
    const p = st.plan;
    lines.push(`<b>${p.n_samples.toLocaleString()}</b> samples at `
      + `<b>${fmtSI(p.rate_hz, 'S/s')}</b> · ${fmtSI(p.capture_s, 's')} per sweep`);
  } else {
    lines.push(`${fmtSI(cfg.sweep_s, 's')} per sweep`);
  }
  if (cfg.mode === 'auto') {
    lines.push(`auto fires after ${fmtSI(cfg.auto_ms_effective / 1000, 's')} without a trigger`
      + (cfg.auto_ms ? '' : ' (automatic)'));
  }
  if (st.running) {
    lines.push(`${st.sweep_rate_hz.toFixed(1)} sweeps/s · ${st.sweeps.toLocaleString()} this run`);
  } else if (st.stopped_reason && st.stopped_reason !== 'stopped') {
    lines.push(st.stopped_reason);
  }
  const warns = (st.warnings || []).slice();
  if (st.error) warns.unshift(st.error);
  warns.forEach((w) => lines.push('⚠ ' + w));
  host.innerHTML = lines.join('<br>');
  host.className = 'plan' + (warns.length ? ' bad' : '');
}

function renderButtons() {
  const open = state.open;
  const running = sweepRunning();
  const other = state.streaming;
  $('btn-run').disabled = !open || running || other;
  $('btn-single').disabled = !open || other;
  $('btn-halt').disabled = !running;
  $('btn-save-sweep').disabled = !state.sweep;
  $('btn-stream').disabled = !open || state.streaming || running;
  $('btn-stream-stop').disabled = !state.streaming;
  $('btn-record').disabled = !state.streaming || state.recording;
  $('btn-record-stop').disabled = !state.recording;
  $('btn-siggen').disabled = !open;
  $('btn-siggen-off').disabled = !open;
  const daqRunning = !!(state.daqState && state.daqState.running);
  $('btn-daq').disabled = !open || daqRunning || other || running;
  $('btn-daq-stop').disabled = !daqRunning;
}

let sweepTimer = null;

/** Fetch the newest sweep. Runs fast while the scope runs, slowly otherwise,
 *  and not at all while the tab is hidden. */
async function pollSweep() {
  clearTimeout(sweepTimer);
  if (state.view === 'scope' && state.open && !document.hidden) {
    const width = state.geom ? Math.round(state.geom.pw) : 900;
    const result = await api('sweep-trace', {
      since: state.sweepId, max_points: Math.max(100, Math.min(4000, width)) });
    if (result.ok) {
      if (result.sweep) {
        state.sweep = result.sweep;
        state.sweepId = result.sweep.id;
        renderLegend();
        renderMeasurementTable();
      }
      renderSweepState(result.state);
      drawPlot();
    }
  }
  sweepTimer = setTimeout(pollSweep, sweepRunning() ? 120 : 700);
}

function pollSweepSoon() {
  clearTimeout(sweepTimer);
  sweepTimer = setTimeout(pollSweep, 30);
}

/* ------------------------------------------------------------------ plot */

function drawPlot() {
  if (state.view === 'scope') drawScope();
  else if (state.view === 'roll') drawRoll();
  else if (state.view === 'daq') drawDaq();
  else drawFiles();
}

function sizeCanvas(canvas) {
  const ratio = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  if (canvas.width !== w * ratio || canvas.height !== h * ratio) {
    canvas.width = w * ratio; canvas.height = h * ratio;
  }
  const ctx = canvas.getContext('2d');
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, w, h);
  return { ctx, w, h };
}

function drawRoll() {
  const canvas = $('plot');
  const { ctx, w, h } = sizeCanvas(canvas);

  const pad = { l: 52, r: 10, t: 10, b: 20 };
  const pw = w - pad.l - pad.r, ph = h - pad.t - pad.b;
  if (pw <= 0 || ph <= 0) return;

  const channels = enabledChannels().filter((ch) => state.traces[ch]);

  // Scale to the largest enabled range so channels stay comparable, and so
  // the axis does not jump around as the signal changes.
  let span = 0.01;
  channels.forEach((ch) => {
    const t = state.traces[ch];
    (t.max || []).forEach((v) => { span = Math.max(span, Math.abs(v)); });
    (t.min || []).forEach((v) => { span = Math.max(span, Math.abs(v)); });
  });
  span *= 1.15;

  const y = (v) => pad.t + ph / 2 - (v / span) * (ph / 2);

  // grid
  ctx.strokeStyle = '#1e1e1e'; ctx.lineWidth = 1;
  ctx.fillStyle = '#666'; ctx.font = '10px system-ui'; ctx.textAlign = 'right';
  for (let i = -2; i <= 2; i++) {
    const v = span * i / 2, py = Math.round(y(v)) + 0.5;
    ctx.beginPath(); ctx.moveTo(pad.l, py); ctx.lineTo(w - pad.r, py); ctx.stroke();
    ctx.fillText(fmtVolts(v), pad.l - 6, py + 3);
  }
  ctx.textAlign = 'center';
  for (let i = 0; i <= 4; i++) {
    const px = pad.l + pw * i / 4;
    ctx.beginPath(); ctx.moveTo(px, pad.t); ctx.lineTo(px, pad.t + ph); ctx.stroke();
    const secs = state.window_s * (1 - i / 4);
    ctx.fillText(secs === 0 ? 'now' : `-${fmtTime(secs)}`, px, h - 6);
  }

  if (!channels.length) {
    ctx.fillStyle = '#555'; ctx.font = '13px system-ui';
    ctx.fillText(state.open ? 'start a stream to see live data'
                            : 'connect the scope to begin', pad.l + pw / 2, pad.t + ph / 2);
    return;
  }

  channels.forEach((ch) => {
    const t = state.traces[ch];
    const n = (t.max || []).length;
    if (!n) return;
    const color = COLORS[CHANNELS.indexOf(ch)];
    const x = (i) => pad.l + (n === 1 ? pw : (pw * i) / (n - 1));

    // Envelope band: min..max per bin. This is the point of the whole design —
    // a spike far shorter than a bin still paints, because the bin kept it.
    // '38' is the alpha byte (22%), which REQUIRES color to be 6-digit hex --
    // see the note on COLORS. A 3-digit color here silently paints opaque grey.
    ctx.fillStyle = color + '38';
    ctx.beginPath();
    ctx.moveTo(x(0), y(t.max[0]));
    for (let i = 1; i < n; i++) ctx.lineTo(x(i), y(t.max[i]));
    for (let i = n - 1; i >= 0; i--) ctx.lineTo(x(i), y(t.min[i]));
    ctx.closePath(); ctx.fill();

    ctx.strokeStyle = color; ctx.lineWidth = 1.2;
    ctx.beginPath();
    for (let i = 0; i < n; i++) {
      const px = x(i), py = y(t.mean[i]);
      i ? ctx.lineTo(px, py) : ctx.moveTo(px, py);
    }
    ctx.stroke();
  });
}

/** Vertical scale in scope view: the largest enabled range, full height —
 *  a scope's screen is its input range, so the axis never jumps with the
 *  signal. */
function scopeSpan() {
  let span = 0;
  enabledChannels().forEach((ch) => { span = Math.max(span, state.channels[ch].range_volts); });
  return span || 5;
}

function dragValue(kind, fallback) {
  return state.drag && state.drag.kind === kind ? state.drag.value : fallback;
}

function drawScope() {
  const canvas = $('plot');
  const { ctx, w, h } = sizeCanvas(canvas);
  const pad = { l: 60, r: 66, t: 18, b: 22 };
  const pw = w - pad.l - pad.r, ph = h - pad.t - pad.b;
  if (pw <= 0 || ph <= 0) { state.geom = null; return; }

  const cfg = state.sweepCfg;
  const sw = state.sweep;
  const span = scopeSpan();
  const tdiv = cfg ? cfg.time_per_div : (sw ? sw.time_per_div : 1e-3);
  const sweepS = tdiv * DIV_X;
  const pos = dragValue('position', cfg ? cfg.position : 50);
  // The window comes from the current settings, not from the sweep, so a
  // stopped trace pans and zooms with the controls, as on a digital scope.
  const tLeft = -pos / 100 * sweepS;
  const x = (t) => pad.l + (t - tLeft) / sweepS * pw;
  const y = (v) => pad.t + ph / 2 - (v / span) * (ph / 2);
  state.geom = { pad, pw, ph, span };

  const seg = (x0, y0, x1, y1) => {
    ctx.beginPath(); ctx.moveTo(x0, y0); ctx.lineTo(x1, y1); ctx.stroke();
  };

  // graticule — centre lines brighter, with minor ticks, as on the bench
  ctx.lineWidth = 1;
  for (let i = 0; i <= DIV_X; i++) {
    const px = Math.round(pad.l + pw * i / DIV_X) + 0.5;
    ctx.strokeStyle = i === DIV_X / 2 ? '#3a3a3a' : '#1e1e1e';
    seg(px, pad.t, px, pad.t + ph);
  }
  for (let j = 0; j <= DIV_Y; j++) {
    const py = Math.round(pad.t + ph * j / DIV_Y) + 0.5;
    ctx.strokeStyle = j === DIV_Y / 2 ? '#3a3a3a' : '#1e1e1e';
    seg(pad.l, py, pad.l + pw, py);
  }
  ctx.strokeStyle = '#3a3a3a';
  const cx = Math.round(pad.l + pw / 2) + 0.5, cy = Math.round(pad.t + ph / 2) + 0.5;
  for (let i = 0; i <= DIV_X * 5; i++) {
    const px = Math.round(pad.l + pw * i / (DIV_X * 5)) + 0.5;
    seg(px, cy - 3, px, cy + 3);
  }
  for (let j = 0; j <= DIV_Y * 5; j++) {
    const py = Math.round(pad.t + ph * j / (DIV_Y * 5)) + 0.5;
    seg(cx - 3, py, cx + 3, py);
  }

  // axis labels
  ctx.fillStyle = '#666'; ctx.font = '10px system-ui'; ctx.textAlign = 'right';
  for (let j = 0; j <= DIV_Y; j++) {
    const v = span * (1 - 2 * j / DIV_Y);
    ctx.fillText(fmtVolts(v), pad.l - 6, pad.t + ph * j / DIV_Y + 3);
  }
  ctx.textAlign = 'center';
  const every = pw / DIV_X < 58 ? 2 : 1;
  for (let i = 0; i <= DIV_X; i += every) {
    const t = tLeft + sweepS * i / DIV_X;
    ctx.fillText(fmtSI(Math.abs(t) < sweepS * 1e-9 ? 0 : t, 's'),
                 pad.l + pw * i / DIV_X, h - 6);
  }

  // traces, clipped to the graticule
  ctx.save();
  ctx.beginPath(); ctx.rect(pad.l, pad.t, pw, ph); ctx.clip();
  if (sw) {
    enabledChannels().filter((ch) => sw.traces[ch]).forEach((ch) => {
      const tr = sw.traces[ch];
      const n = tr.min.length;
      if (!n) return;
      const step = n > 1 ? (sw.t_end_s - sw.t_start_s) / (n - 1) : 0;
      ctx.strokeStyle = COLORS[CHANNELS.indexOf(ch)];
      ctx.lineWidth = 1.3;
      ctx.lineJoin = 'round';
      ctx.beginPath();
      // One vertical stroke per bin from max to min, joined bin to bin: with
      // one sample per bin it is a plain line, with many it is the envelope,
      // and a one-sample glitch still reaches the screen either way.
      for (let i = 0; i < n; i++) {
        const px = x(sw.t_start_s + i * step);
        const hi = y(tr.max[i]), lo = y(tr.min[i]);
        if (i === 0) ctx.moveTo(px, hi); else ctx.lineTo(px, hi);
        if (lo !== hi) ctx.lineTo(px, lo);
      }
      ctx.stroke();
    });
  }
  ctx.restore();

  // trigger markers
  if (cfg && cfg.mode !== 'none') {
    const col = COLORS[CHANNELS.indexOf(cfg.source)] || '#ccc';
    const lvl = dragValue('level', cfg.level);
    const rawY = y(lvl);
    const ly = Math.min(pad.t + ph, Math.max(pad.t, rawY));
    const off = rawY !== ly;
    if (!off) {
      ctx.save();
      ctx.strokeStyle = col; ctx.globalAlpha = state.drag ? 0.9 : 0.5;
      ctx.setLineDash([5, 4]);
      seg(pad.l, Math.round(ly) + 0.5, pad.l + pw, Math.round(ly) + 0.5);
      ctx.restore();
    }
    // level flag on the right edge, pointing at the level
    const fx = pad.l + pw + 3;
    ctx.fillStyle = col;
    ctx.beginPath();
    ctx.moveTo(fx, ly); ctx.lineTo(fx + 7, ly - 7); ctx.lineTo(fx + 21, ly - 7);
    ctx.lineTo(fx + 21, ly + 7); ctx.lineTo(fx + 7, ly + 7); ctx.closePath();
    ctx.fill();
    ctx.fillStyle = '#000'; ctx.font = 'bold 10px system-ui'; ctx.textAlign = 'center';
    ctx.fillText(off ? (rawY < ly ? '▲' : '▼') : 'T', fx + 14, ly + 4);
    ctx.fillStyle = col; ctx.font = '10px system-ui'; ctx.textAlign = 'left';
    ctx.fillText(fmtVolts(lvl), fx + 1, ly + (ly > pad.t + 20 ? -11 : 19));

    // trigger-position marker on the top edge
    const tx = Math.min(pad.l + pw, Math.max(pad.l, x(0)));
    ctx.fillStyle = col;
    ctx.beginPath();
    ctx.moveTo(tx, pad.t); ctx.lineTo(tx - 7, pad.t - 10); ctx.lineTo(tx + 7, pad.t - 10);
    ctx.closePath(); ctx.fill();
    ctx.fillStyle = '#000'; ctx.font = 'bold 8px system-ui'; ctx.textAlign = 'center';
    ctx.fillText('T', tx, pad.t - 3);
  }

  // corner status
  const key = state.sweepState ? state.sweepState.indicator : (state.open ? 'stop' : null);
  const ind = key && INDICATOR[key];
  if (ind) {
    ctx.font = 'bold 11px system-ui';
    const tw = ctx.measureText(ind.text).width + 16;
    const bx = pad.l + pw - tw - 6, by = pad.t + 6;
    ctx.fillStyle = ind.bg; ctx.strokeStyle = ind.edge;
    ctx.fillRect(bx, by, tw, 18); ctx.strokeRect(bx + 0.5, by + 0.5, tw - 1, 17);
    const blink = key === 'wait' && Math.floor(Date.now() / 500) % 2;
    ctx.fillStyle = blink ? ind.bg : ind.fg; ctx.textAlign = 'center';
    ctx.fillText(ind.text, bx + tw / 2, by + 13);
  }

  // readout, bottom-left inside the graticule
  if (cfg) {
    const arrow = { RISING: '↑', FALLING: '↓', RISING_OR_FALLING: '↕',
                    ABOVE: '>', BELOW: '<' }[cfg.direction] || '';
    const parts = [`${fmtSI(span / (DIV_Y / 2), 'V')}/div`, `${fmtSI(tdiv, 's')}/div`];
    if (cfg.mode !== 'none') {
      parts.push(`${cfg.source} ${arrow} ${fmtVolts(dragValue('level', cfg.level))}`);
    }
    ctx.font = '11px system-ui'; ctx.textAlign = 'left'; ctx.fillStyle = '#9a9a9a';
    ctx.fillText(parts.join('   '), pad.l + 8, pad.t + ph - 8);
  }

  if (!sw) {
    ctx.fillStyle = '#555'; ctx.font = '13px system-ui'; ctx.textAlign = 'center';
    ctx.fillText(state.open ? 'press Run to start the scope'
                            : 'connect the scope to begin',
                 pad.l + pw / 2, pad.t + ph / 2 - 14);
  }
}

/* ------------------------------------------------- dragging the markers */

function pointerAt(ev) {
  const r = $('plot').getBoundingClientRect();
  return { px: ev.clientX - r.left, py: ev.clientY - r.top };
}

function hitMarker(px, py) {
  const cfg = state.sweepCfg, g = state.geom;
  if (state.view !== 'scope' || !cfg || cfg.mode === 'none' || !g || !state.open) return null;
  const { pad, pw, ph, span } = g;
  const tx = pad.l + pw * cfg.position / 100;
  if (Math.abs(px - tx) <= 9 && py >= pad.t - 14 && py <= pad.t + 6) return 'position';
  const rawY = pad.t + ph / 2 - (cfg.level / span) * (ph / 2);
  const ly = Math.min(pad.t + ph, Math.max(pad.t, rawY));
  const onFlag = px >= pad.l + pw && px <= pad.l + pw + 28 && Math.abs(py - ly) <= 9;
  const onLine = px >= pad.l && px <= pad.l + pw && Math.abs(py - ly) <= 5;
  return onFlag || onLine ? 'level' : null;
}

function dragTo(px, py) {
  const { pad, pw, ph, span } = state.geom;
  if (state.drag.kind === 'level') {
    const v = (pad.t + ph / 2 - py) / (ph / 2) * span;
    const clamped = Math.max(-span, Math.min(span, v));
    state.drag.value = roundTo(clamped, niceStep(span));
    $('t-level').value = state.drag.value;
  } else {
    const p = Math.max(0, Math.min(100, (px - pad.l) / pw * 100));
    state.drag.value = Math.round(p);
    $('tb-pos').value = state.drag.value;
    $('tb-pos-val').textContent = `${state.drag.value} %`;
  }
  drawPlot();
}

function wirePlotDragging() {
  const canvas = $('plot');
  canvas.addEventListener('pointerdown', (ev) => {
    const { px, py } = pointerAt(ev);
    const kind = hitMarker(px, py);
    if (!kind) return;
    ev.preventDefault();
    canvas.setPointerCapture(ev.pointerId);
    state.drag = { kind, value: state.sweepCfg[kind] };
    dragTo(px, py);
  });
  canvas.addEventListener('pointermove', (ev) => {
    const { px, py } = pointerAt(ev);
    if (state.drag) { dragTo(px, py); return; }
    const kind = hitMarker(px, py);
    canvas.style.cursor = kind === 'level' ? 'ns-resize'
      : kind === 'position' ? 'ew-resize' : '';
  });
  const finish = (commit) => {
    if (!state.drag) return;
    const { kind, value } = state.drag;
    state.drag = null;
    if (commit && state.sweepCfg && value !== state.sweepCfg[kind]) {
      sweepSet({ [kind]: value }, kind === 'level' ? 'trigger level' : 'trigger position');
    } else {
      syncSweepControls(true);
      drawPlot();
    }
  };
  canvas.addEventListener('pointerup', () => finish(true));
  canvas.addEventListener('pointercancel', () => finish(false));
}

/* ------------------------------------------------------------ streaming */

function streamParams() {
  return {
    rate: Number($('s-rate').value),
    downsample: Number($('s-ratio').value),
    mode: $('s-mode').value,
    window: Number($('s-window').value),
  };
}

async function refreshPlan() {
  const p = streamParams();
  const result = await api('stream-plan', {
    rate: p.rate, downsample: p.downsample, mode: p.mode,
    duration: Number($('r-duration').value) || 0,
  });
  const host = $('plan');
  if (!result.ok) { host.textContent = result.error || ''; return; }

  const pr = result.projection;
  const warnings = result.warnings || [];
  const lines = [
    `<b>${(pr.samples_per_second_per_channel).toLocaleString()}</b> S/s per channel`
      + ` × ${pr.channels} ch → <b>${fmtBytes(pr.bytes_per_second)}</b>`,
    `<b>${pr.gb_per_day.toFixed(2)} GB/day</b>`
      + (pr.total_gb ? ` · this run: <b>${pr.total_gb.toFixed(2)} GB</b>` : '')
      + ` · free: ${result.free_gb} GB`,
  ];
  if (pr.aggregated) {
    lines.push('Aggregate keeps the min and max of every bin — full glitch '
             + 'sensitivity at the reduced rate.');
  }
  warnings.forEach((wtext) => lines.push('⚠ ' + wtext));
  host.innerHTML = lines.join('<br>');
  host.className = 'plan' + (warnings.length ? ' bad' : '');
}

async function startStream() {
  const p = streamParams();
  state.window_s = p.window;
  say('starting stream…', 'busy');
  const result = await api('stream-start', {
    rate: p.rate, downsample: p.downsample, mode: p.mode, window: p.window,
  }, true);
  if (check(result, 'stream')) say('streaming');
  refresh();
}

async function stopStream() {
  say('stopping…', 'busy');
  const result = await api('stream-stop', {}, true);
  if (check(result, 'stop')) say('stopped');
  state.traces = {};
  refresh();
}

/* ------------------------------------------------------------ recording */

async function startRecording() {
  say('starting recording…', 'busy');
  const result = await api('record-start', {
    name: $('r-name').value || 'stream',
    duration: Number($('r-duration').value) || 0,
    format: $('r-format').value,
    note: $('r-note').value || '',
  }, true);
  if (check(result, 'record')) say('recording');
  refresh();
}

async function stopRecording() {
  say('finishing recording…', 'busy');
  const result = await api('record-stop', {}, true);
  if (check(result, 'stop recording')) {
    const r = result.recording || {};
    say(`saved ${r.mb_written || 0} MB` +
        (r.chunks_dropped ? ` — ${r.chunks_dropped} chunks dropped` : ''));
  }
  refresh();
  loadRecordings();
}

async function loadRecordings() {
  const result = await api('recordings');
  const host = $('recordings');
  if (!result.ok) { host.textContent = result.error || ''; return; }
  const list = result.recordings || [];
  host.innerHTML = list.length
    ? list.slice(0, 12).map((r) =>
        `${r.name} — ${r.mb} MB <span class="sub">${r.modified}</span>`).join('<br>')
    : 'none yet';
}

/* ------------------------------------------------------------- poll loop */

let lastStreamMeasurements = {};

async function refresh() {
  const st = await api('status');
  if (!st.ok) { say(st.error || 'unreachable', 'err'); return; }

  state.open = !!st.open;
  const stream = st.stream || {};
  const rec = st.recorder || {};
  state.streaming = !!stream.running;
  state.recording = !!rec.recording;
  if (st.sweep) renderSweepState(st.sweep);
  if (st.daq) renderDaqState(st.daq);

  // Follow the instrument: show the controls for the mode it is actually in.
  if (state.streaming && state.view !== 'roll') {
    state.view = 'roll'; applyView();
  } else if (sweepRunning() && state.view !== 'scope') {
    state.view = 'scope'; applyView();
  } else if (state.daqState && state.daqState.running
             && state.view !== 'daq' && state.view !== 'files') {
    state.view = 'daq'; applyView();
  }

  const ident = st.identity;
  $('identity').textContent = ident
    ? `${ident.variant} · ${ident.serial} · driver ${ident.driver}`
    : (state.open ? 'connected' : 'not connected');

  const usb = $('usbwarn');
  if (ident && ident.on_usb2_port) {
    usb.hidden = false;
    usb.textContent = 'USB 2.0 port — streaming capped near 21 MS/s total';
    usb.title = 'This 4824A is a USB 3.0 device plugged into a USB 2.0 port. '
              + 'Moving it to a USB 3.0 port raises the sustainable rate.';
  } else { usb.hidden = true; }

  $('btn-open').disabled = state.open;
  $('btn-close').disabled = !state.open;
  renderButtons();

  if (st.device) renderChannels(st.device.channels || []);
  $('disk').textContent = `${st.free_gb} GB free`;

  const pill = $('recpill');
  pill.hidden = !state.recording;
  if (state.recording) {
    $('rectext').textContent =
      `recording · ${rec.mb_written || 0} MB · ${Math.round(rec.elapsed_s || 0)} s`
      + (rec.chunks_dropped ? ` · ${rec.chunks_dropped} dropped` : '');
  }

  const stats = stream.stats || {};
  $('recstats').innerHTML = state.recording || rec.path
    ? `${rec.path ? rec.path.split(/[\\/]/).pop() : ''}<br>`
      + `written ${rec.mb_written || 0} MB · queue ${rec.queue_depth || 0}`
      + ` (peak ${rec.queue_peak || 0}) · dropped ${rec.chunks_dropped || 0}`
      + (rec.stopped_reason ? `<br>${rec.stopped_reason}` : '')
    : 'not recording';

  if (state.streaming) {
    const frac = stats.capture_fraction;
    const pct = frac == null ? '—' : (frac * 100).toFixed(1) + '%';
    sayLive(`streaming · ${Math.round(stats.measured_rate_hz || 0).toLocaleString()}`
        + ` S/s/ch · captured ${pct}`
        + (stats.dropped_estimate ? ` · ~${stats.dropped_estimate} lost` : ''),
        frac != null && frac < 0.98 ? 'err' : null);
  }

  lastStreamMeasurements = stream.measurements || {};
  renderMeasurementTable();
  if (state.streaming) await refreshTraces();
  drawPlot();
}

async function refreshTraces() {
  const result = await api('stream-traces', { max_points: 900 });
  if (result.ok) {
    state.traces = result.traces || {};
    if (result.window_s) state.window_s = result.window_s;
  }
}

function renderMeasurementTable() {
  const body = $('measure').querySelector('tbody');
  body.innerHTML = '';
  const scope = state.view === 'scope';
  const m = scope ? ((state.sweep && state.sweep.measurements) || {})
                  : lastStreamMeasurements;
  const cols = scope
    ? [['vpp', 'p-p'], ['mean', 'mean'], ['rms', 'rms'], ['freq_hz', 'freq']]
    : [['min', 'min'], ['max', 'max'], ['mean', 'mean'], ['rms', 'rms']];
  const shown = scope ? enabledChannels() : Object.keys(m).sort();
  shown.forEach((ch) => {
    const v = m[ch];
    if (!v) return;
    const tr = document.createElement('tr');
    tr.innerHTML = `<td style="color:${COLORS[CHANNELS.indexOf(ch)]}">${ch}</td>`
      + cols.map(([key]) => {
        const val = v[key];
        const text = val == null ? '—'
          : key === 'freq_hz' ? fmtFreq(val) : fmtVolts(val);
        return `<td class="num">${text}</td>`;
      }).join('');
    if (scope) tr.title = `min ${fmtVolts(v.min)} · max ${fmtVolts(v.max)}`
      + (v.period_s ? ` · period ${fmtSI(v.period_s, 's')}` : '');
    body.appendChild(tr);
  });
  if (body.children.length) {
    const head = document.createElement('tr');
    head.innerHTML = '<td></td>' + cols.map(([, label]) =>
      `<td class="num sub">${label}</td>`).join('');
    body.prepend(head);
  }
}

/* ------------------------------------------------------- command listing */

async function loadCommands() {
  try {
    const resp = await fetch('describe');
    const doc = await resp.json();
    const host = $('commands');
    host.innerHTML = '';
    Object.entries(doc.groups).forEach(([key, label]) => {
      const cmds = doc.commands.filter((c) => c.group === key);
      if (!cmds.length) return;
      const head = document.createElement('div');
      head.className = 'cgroup';
      head.textContent = label;
      host.appendChild(head);
      cmds.forEach((c) => {
        const el = document.createElement('div');
        el.className = 'cmd';
        el.innerHTML = `<code>pico ${c.name}</code> — ${c.summary}`;
        host.appendChild(el);
      });
    });
  } catch (err) { /* the reference list is optional */ }
}

/* ------------------------------------------------------- generic XY plot */
/* The DAQ and Files views need axes the scope display does not: a frequency
 * axis spanning five decades, an amplitude axis spanning ten. One plotter
 * handles both, linear or logarithmic, and draws every series as a min/max
 * band so a single-bin spectral line or a one-sample glitch survives the
 * reduction that got it here. */

function logSafe(v) { return v > 0 ? Math.log10(v) : null; }

function fmtSci(v) {
  const a = Math.abs(v);
  if (a === 0) return '0';
  if (a >= 1e-2 && a < 1e5) return Number(v.toPrecision(3)).toString();
  const e = Math.floor(Math.log10(a));
  const m = v / Math.pow(10, e);
  return `${Number(m.toPrecision(2))}e${e}`;
}

/** Ticks for a log axis: a label per decade, faint lines at 2..9 between. */
function logTicks(lo, hi) {
  const out = [];
  const first = Math.floor(lo), last = Math.ceil(hi);
  for (let e = first; e <= last; e++) {
    const base = Math.pow(10, e);
    out.push({ v: base, major: true });
    for (let m = 2; m <= 9; m++) {
      const v = m * base;
      if (Math.log10(v) >= lo && Math.log10(v) <= hi) out.push({ v, major: false });
    }
  }
  return out.filter((t) => Math.log10(t.v) >= lo - 1e-9 && Math.log10(t.v) <= hi + 1e-9);
}

function linTicks(lo, hi, count) {
  const out = [];
  for (let i = 0; i <= count; i++) out.push({ v: lo + (hi - lo) * i / count, major: true });
  return out;
}

/**
 * cfg: { series: [{label, color, x?[], x0?, dx?, min[], max[]}],
 *        logX, logY, fmtX, fmtY, xLabel, yLabel, empty }
 */
function drawXY(canvas, cfg) {
  const { ctx, w, h } = sizeCanvas(canvas);
  const pad = { l: 66, r: 14, t: 14, b: 26 };
  const pw = w - pad.l - pad.r, ph = h - pad.t - pad.b;
  if (pw <= 0 || ph <= 0) return;

  const series = (cfg.series || []).filter((s) => s.max && s.max.length);
  const xOf = (s, i) => (s.x ? s.x[i] : s.x0 + s.dx * i);

  if (!series.length) {
    ctx.fillStyle = '#555'; ctx.font = '13px system-ui'; ctx.textAlign = 'center';
    ctx.fillText(cfg.empty || 'no data', pad.l + pw / 2, pad.t + ph / 2);
    return;
  }

  let xlo = Infinity, xhi = -Infinity, ylo = Infinity, yhi = -Infinity;
  series.forEach((s) => {
    for (let i = 0; i < s.max.length; i++) {
      const x = xOf(s, i);
      if (cfg.logX && !(x > 0)) continue;
      if (x < xlo) xlo = x;
      if (x > xhi) xhi = x;
      const a = s.min[i], b = s.max[i];
      if (cfg.logY) {
        if (a > 0 && a < ylo) ylo = a;
        if (b > 0 && b > yhi) yhi = b;
      } else {
        if (a < ylo) ylo = a;
        if (b > yhi) yhi = b;
      }
    }
  });
  if (!Number.isFinite(xlo) || !Number.isFinite(ylo)) {
    ctx.fillStyle = '#555'; ctx.font = '13px system-ui'; ctx.textAlign = 'center';
    ctx.fillText('nothing to show on these axes', pad.l + pw / 2, pad.t + ph / 2);
    return;
  }
  if (xhi === xlo) xhi = xlo + 1;
  if (yhi === ylo) { yhi = ylo + Math.abs(ylo || 1) * 0.1; ylo -= Math.abs(ylo || 1) * 0.1; }
  if (!cfg.logY) { const m = (yhi - ylo) * 0.06; ylo -= m; yhi += m; }

  const X0 = cfg.logX ? logSafe(xlo) : xlo, X1 = cfg.logX ? logSafe(xhi) : xhi;
  const Y0 = cfg.logY ? logSafe(ylo) : ylo, Y1 = cfg.logY ? logSafe(yhi) : yhi;
  const px = (x) => {
    const v = cfg.logX ? logSafe(x) : x;
    return pad.l + ((v - X0) / (X1 - X0)) * pw;
  };
  const py = (y) => {
    const v = cfg.logY ? logSafe(y) : y;
    return pad.t + ph - ((v - Y0) / (Y1 - Y0)) * ph;
  };

  // grid and labels
  ctx.font = '10px system-ui'; ctx.lineWidth = 1;
  const xticks = cfg.logX ? logTicks(X0, X1) : linTicks(X0, X1, 5);
  ctx.textAlign = 'center';
  xticks.forEach((t) => {
    const x = Math.round(px(t.v)) + 0.5;
    if (x < pad.l - 1 || x > pad.l + pw + 1) return;
    ctx.strokeStyle = t.major ? '#262626' : '#1a1a1a';
    ctx.beginPath(); ctx.moveTo(x, pad.t); ctx.lineTo(x, pad.t + ph); ctx.stroke();
    if (t.major) {
      ctx.fillStyle = '#777';
      ctx.fillText(cfg.fmtX ? cfg.fmtX(t.v) : fmtSci(t.v), x, h - 8);
    }
  });
  const yticks = cfg.logY ? logTicks(Y0, Y1) : linTicks(Y0, Y1, 5);
  ctx.textAlign = 'right';
  yticks.forEach((t) => {
    const y = Math.round(py(t.v)) + 0.5;
    if (y < pad.t - 1 || y > pad.t + ph + 1) return;
    ctx.strokeStyle = t.major ? '#262626' : '#1a1a1a';
    ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(pad.l + pw, y); ctx.stroke();
    if (t.major) {
      ctx.fillStyle = '#777';
      ctx.fillText(cfg.fmtY ? cfg.fmtY(t.v) : fmtSci(t.v), pad.l - 6, y + 3);
    }
  });

  // axis titles
  ctx.fillStyle = '#666';
  if (cfg.xLabel) { ctx.textAlign = 'right'; ctx.fillText(cfg.xLabel, pad.l + pw, pad.t - 4); }
  if (cfg.yLabel) { ctx.textAlign = 'left'; ctx.fillText(cfg.yLabel, pad.l - 60, pad.t - 4); }

  // series
  ctx.save();
  ctx.beginPath(); ctx.rect(pad.l, pad.t, pw, ph); ctx.clip();
  series.forEach((s) => {
    ctx.strokeStyle = s.color || '#8cf';
    ctx.lineWidth = s.width || 1.2;
    ctx.globalAlpha = s.alpha == null ? 1 : s.alpha;
    ctx.beginPath();
    let started = false;
    for (let i = 0; i < s.max.length; i++) {
      const x = xOf(s, i);
      if (cfg.logX && !(x > 0)) continue;
      const hi = s.max[i], lo = s.min[i];
      if (cfg.logY && !(hi > 0)) { started = false; continue; }
      const X = px(x), YH = py(hi), YL = py(cfg.logY && !(lo > 0) ? hi : lo);
      if (!started) { ctx.moveTo(X, YH); started = true; } else { ctx.lineTo(X, YH); }
      if (YL !== YH) ctx.lineTo(X, YL);
    }
    ctx.stroke();
    ctx.globalAlpha = 1;
  });
  ctx.restore();
}

function seriesColor(label, index) {
  const ch = /(?:^|·\s*)([A-H])\b/.exec(label || '');
  if (ch) return COLORS[CHANNELS.indexOf(ch[1])];
  return COLORS[index % COLORS.length];
}

/* ------------------------------------------------------------------- DAQ */

function daqParams() {
  const trig = document.querySelector('#d-trigger .seg-btn[aria-checked="true"]');
  const p = {
    bits: Number($('d-bits').value),
    rate: Number($('d-rate').value),
    files: Number($('d-files').value),
    name: $('d-name').value || 'run',
    trigger: trig ? trig.dataset.mode : 'off',
    source: $('d-source').value || 'A',
    direction: $('d-dir-edge').value,
    level: Number($('d-level').value) || 0,
    pretrigger: Number($('d-pre').value) || 0,
  };
  const dir = $('d-dir').value.trim();
  if (dir) p.directory = dir;
  return p;
}

async function refreshDaqPlan() {
  const p = daqParams();
  const n = Math.pow(2, p.bits);
  $('d-length').textContent = `${n.toLocaleString()} samples · `
    + `${fmtSI(p.rate ? n / p.rate : 0, 's')}`;
  if (!state.open) { $('d-plan').textContent = 'connect the scope to plan a run'; return; }
  const result = await api('daq-plan', p);
  const host = $('d-plan');
  if (!result.ok) { host.textContent = result.error || 'failed'; host.className = 'plan bad'; return; }
  const pr = result.projection;
  if (pr.error) { host.textContent = pr.error; host.className = 'plan bad'; return; }
  const lines = [
    `<b>${pr.plan.n_samples.toLocaleString()}</b> samples at `
      + `<b>${fmtSI(pr.plan.actual_rate_hz, 'S/s')}</b> = ${fmtSI(pr.plan.duration_s, 's')} per file`,
    `<b>${fmtSize(pr.bytes_per_file)}</b> per file`
      + (pr.total_bytes ? ` · run: <b>${fmtSize(pr.total_bytes)}</b>` : ' · until stopped')
      + (pr.total_seconds ? ` · ${fmtSI(pr.total_seconds, 's')} of data` : '')
      + ` · free: ${(pr.free_bytes / 1e9).toFixed(1)} GB`,
  ];
  (pr.warnings || []).forEach((wtext) => lines.push('⚠ ' + wtext));
  host.innerHTML = lines.join('<br>');
  host.className = 'plan' + ((pr.warnings || []).length ? ' bad' : '');
}

async function startDaq() {
  say('starting the run…', 'busy');
  const result = await api('daq-start', daqParams(), true);
  if (check(result, 'DAQ')) {
    renderDaqState(result.daq);
    say('recording');
  }
  pollDaqSoon();
}

async function stopDaq() {
  const result = await api('daq-stop', {}, true);
  if (check(result, 'stop')) {
    renderDaqState(result.daq);
    say('run stopped');
  }
}

function renderDaqState(st) {
  if (!st) return;
  state.daqState = st;
  const running = !!st.running;
  const target = st.files_target ? `/${st.files_target}` : '';
  let progress = '';
  if (running) {
    const cap = st.capture;
    const pct = cap && cap.fraction != null ? ` · ${(cap.fraction * 100).toFixed(0)}%` : '';
    const what = st.phase === 'waiting' ? 'waiting for a trigger'
      : st.phase === 'writing' ? 'writing' : 'recording';
    progress = `${what} ${st.files_written + 1}${target}${pct}`;
  } else if (st.stopped_reason) {
    progress = st.stopped_reason;
  } else if (st.files_written) {
    progress = `${st.files_written} file${st.files_written === 1 ? '' : 's'}`;
  }
  $('daq-progress').textContent = progress;

  const lines = [];
  if (st.directory) lines.push(`<b>${st.directory.split(/[\\/]/).pop()}</b>`);
  lines.push(`${st.files_written}${target} written`
    + (st.bytes_written ? ` · ${fmtSize(st.bytes_written)}` : '')
    + (st.average_n ? ` · ASD averaged over ${st.average_n}` : ''));
  if (running && st.capture) {
    lines.push(st.capture.fraction == null
      ? `armed ${fmtSI(st.capture.elapsed_s, 's')}`
      : `record ${fmtSI(st.capture.elapsed_s, 's')} of ${fmtSI(st.capture.expected_s, 's')}`
        + (st.gap_s ? ` · ${fmtSI(st.gap_s, 's')} between files` : ''));
  }
  lines.push(`${st.free_gb} GB free`);
  if (st.error) lines.push('⚠ ' + st.error);
  (st.warnings || []).forEach((wtext) => lines.push('⚠ ' + wtext));
  const host = $('d-status');
  host.innerHTML = lines.join('<br>');
  host.className = 'plan' + (st.error || (st.warnings || []).length ? ' bad' : '');
  renderButtons();
}

let daqTimer = null;

async function pollDaq() {
  clearTimeout(daqTimer);
  const running = !!(state.daqState && state.daqState.running);
  if (state.view === 'daq' && state.open && !document.hidden) {
    const result = await api('daq-preview', { since: state.daqId, max_points: 1200 });
    if (result.ok) {
      if (result.preview) {
        state.daqPreview = result.preview;
        state.daqId = result.preview.id;
        renderMeasurementTable();
        renderLegend();
      }
      renderDaqState(result.state);
      drawPlot();
    }
  }
  daqTimer = setTimeout(pollDaq, running ? 400 : 1200);
}

function pollDaqSoon() { clearTimeout(daqTimer); daqTimer = setTimeout(pollDaq, 50); }

function drawDaq() {
  const pv = state.daqPreview;
  const shown = enabledChannels();
  const time = [], spec = [];
  if (pv) {
    Object.keys(pv.time || {}).forEach((ch, i) => {
      if (shown.length && !shown.includes(ch)) return;
      const n = pv.time[ch].max.length;
      time.push({ label: ch, color: seriesColor(ch, i), x0: 0,
                  dx: n > 1 ? pv.duration_s / (n - 1) : 0,
                  min: pv.time[ch].min, max: pv.time[ch].max });
    });
    Object.keys(pv.spectrum || {}).forEach((ch, i) => {
      if (shown.length && !shown.includes(ch)) return;
      const s = pv.spectrum[ch];
      spec.push({ label: ch, color: seriesColor(ch, i), x: s.f,
                  min: s.min, max: s.max });
    });
    if ($('d-average').checked) {
      Object.keys(pv.average || {}).forEach((ch, i) => {
        if (shown.length && !shown.includes(ch)) return;
        const s = pv.average[ch];
        spec.push({ label: `${ch} mean`, color: seriesColor(ch, i), x: s.f,
                    min: s.min, max: s.max, width: 2, alpha: 0.55 });
      });
    }
  }
  drawXY($('plot'), {
    series: time, fmtX: (v) => fmtSI(v, 's'), fmtY: fmtVolts,
    xLabel: 'time', yLabel: 'V',
    empty: state.open ? 'press Start run — the last acquisition is drawn here'
                      : 'connect the scope to begin',
  });
  drawXY($('plot2'), {
    series: spec, logX: true, logY: true,
    fmtX: (v) => fmtSI(v, 'Hz'), fmtY: fmtSci,
    xLabel: 'frequency', yLabel: pv ? pv.units : 'V/√Hz',
    empty: 'the ASD of each acquisition appears here',
  });
  if (pv) {
    $('plotnote').innerHTML =
      `<b>${pv.file}</b> · ${pv.n_samples.toLocaleString()} samples at `
      + `${fmtSI(pv.fsamp, 'S/s')} · ${fmtSI(pv.duration_s, 's')} · `
      + `Welch 2^${Math.round(Math.log2(pv.nperseg))} = ${pv.nperseg.toLocaleString()} pts`
      + (pv.average_n > 1 ? ` · mean of ${pv.average_n} shown thicker` : '');
  }
}

function wireDaqControls() {
  $('btn-daq').addEventListener('click', startDaq);
  $('btn-daq-stop').addEventListener('click', stopDaq);
  ['d-rate', 'd-bits', 'd-files', 'd-name', 'd-dir', 'd-level', 'd-pre'].forEach((id) =>
    $(id).addEventListener('change', refreshDaqPlan));
  $('d-source').addEventListener('change', refreshDaqPlan);
  $('d-dir-edge').addEventListener('change', refreshDaqPlan);
  $('d-average').addEventListener('change', drawPlot);
  document.querySelectorAll('#d-trigger .seg-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('#d-trigger .seg-btn').forEach((b) =>
        b.setAttribute('aria-checked', String(b === btn)));
      refreshDaqPlan();
    });
  });
}

/* ----------------------------------------------------------------- files */

async function loadFolder(dir) {
  const result = await api('files', { dir: dir || '' });
  if (!result.ok) { say(`files: ${result.error}`, 'err'); return; }
  state.files.dir = result.dir;
  state.files.listing = result;
  state.files.selected = [];
  renderFolder();
  renderFileChannels();
}

function renderFolder() {
  const lst = state.files.listing;
  const path = $('f-path');
  const folders = $('f-folders');
  const files = $('f-files');
  folders.innerHTML = '';
  files.innerHTML = '';
  if (!lst) { path.textContent = ''; return; }
  path.textContent = lst.dir ? `recordings / ${lst.dir}` : 'recordings';

  if (lst.parent !== null && lst.parent !== undefined) {
    const up = document.createElement('div');
    up.className = 'frow folder';
    up.innerHTML = '<span></span><span class="nm">..</span><span class="meta"></span>';
    up.addEventListener('click', () => loadFolder(lst.parent));
    folders.appendChild(up);
  }
  (lst.folders || []).forEach((f) => {
    const row = document.createElement('div');
    row.className = 'frow folder';
    row.innerHTML = `<span></span><span class="nm">${f.name}</span>`
      + `<span class="meta">${f.files} file${f.files === 1 ? '' : 's'}</span>`;
    row.addEventListener('click', () => loadFolder(f.path));
    folders.appendChild(row);
  });

  if (!(lst.files || []).length) {
    files.innerHTML = '<div class="empty">no HDF5 files here</div>';
  }
  (lst.files || []).forEach((f) => {
    const row = document.createElement('div');
    row.className = 'frow';
    row.innerHTML = `<input type="checkbox"><span class="nm">${f.name}</span>`
      + `<span class="meta">${f.n_samples.toLocaleString()} · `
      + `${fmtSI(f.fsamp, 'S/s')} · ${fmtSI(f.duration_s, 's')} · ${f.mb} MB</span>`;
    const box = row.querySelector('input');
    const toggle = (on) => {
      row.classList.toggle('on', on);
      const i = state.files.selected.indexOf(f.path);
      if (on && i < 0) state.files.selected.push(f.path);
      if (!on && i >= 0) state.files.selected.splice(i, 1);
      renderFileChannels();
    };
    box.addEventListener('change', () => toggle(box.checked));
    row.addEventListener('click', (ev) => {
      if (ev.target !== box) { box.checked = !box.checked; toggle(box.checked); }
    });
    files.appendChild(row);
  });
  (lst.unreadable || []).forEach((f) => {
    const row = document.createElement('div');
    row.className = 'frow';
    row.innerHTML = `<span></span><span class="nm sub">${f.name}</span>`
      + '<span class="meta">not a usphere file</span>';
    row.title = f.reason;
    files.appendChild(row);
  });
}

/** Channel checkboxes for whatever the selected files recorded. */
function renderFileChannels() {
  const host = $('f-channels');
  const lst = state.files.listing;
  const chosen = new Set();
  (lst ? lst.files : []).forEach((f) => {
    if (!state.files.selected.includes(f.path)) return;
    (f.channels || []).forEach((ai) => chosen.add((f.labels || {})[ai] || ai));
  });
  const available = [...chosen].sort();
  state.files.channels = state.files.channels.filter((c) => available.includes(c));
  if (!state.files.channels.length) state.files.channels = available.slice(0, 2);
  host.innerHTML = '';
  if (!available.length) {
    host.innerHTML = '<div class="sub">select a file to choose channels</div>';
    return;
  }
  available.forEach((ch) => {
    const row = document.createElement('div');
    row.className = 'chan';
    const box = document.createElement('input');
    box.type = 'checkbox';
    box.checked = state.files.channels.includes(ch);
    box.addEventListener('change', () => {
      const i = state.files.channels.indexOf(ch);
      if (box.checked && i < 0) state.files.channels.push(ch);
      if (!box.checked && i >= 0) state.files.channels.splice(i, 1);
    });
    const swatch = document.createElement('span');
    swatch.className = 'swatch';
    swatch.style.background = COLORS[CHANNELS.indexOf(ch)] || '#888';
    const name = document.createElement('span');
    name.className = 'name';
    name.textContent = ch;
    row.append(box, swatch, name, document.createElement('span'));
    host.appendChild(row);
  });
}

function fileBand() {
  if (!$('f-band').checked) return {};
  return { f_low: Number($('f-flow').value), f_high: Number($('f-fhigh').value),
           order: Number($('f-order').value) || 3 };
}

async function plotFiles() {
  const sel = state.files.selected;
  const average = $('f-average').checked;
  if (!sel.length && !average) { say('select a file first', 'err'); return; }
  state.files.busy = true;
  $('files-note').textContent = 'reading…';
  const channels = state.files.channels.join(',');
  const band = fileBand();
  const common = { channels, max_points: 1500, ...band };

  const wave = sel.length
    ? await api('file-waveform', { paths: sel.join(','), ...common })
    : { ok: true, series: [] };
  const spec = await api('file-spectrum', {
    ...(average ? { dir: state.files.dir } : { paths: sel.join(',') }),
    kind: $('f-kind').value,
    nperseg_bits: Number($('f-bits').value),
    average,
    ...common,
  });
  state.files.busy = false;
  if (!check(wave, 'waveform') || !check(spec, 'spectrum')) {
    $('files-note').textContent = '';
    return;
  }
  state.files.waveform = wave;
  state.files.spectrum = spec;
  const notes = [...(wave.notes || []), ...(spec.notes || [])];
  $('files-note').textContent = notes.join(' · ');
  $('f-plan').innerHTML =
    `${spec.series.length} curve${spec.series.length === 1 ? '' : 's'} · `
    + `Welch 2^${spec.nperseg_bits} = ${spec.nperseg.toLocaleString()} pts · `
    + `Δf = ${fmtSI(spec.resolution_hz, 'Hz')}`
    + (spec.averaged ? ` · averaged over ${spec.files} file${spec.files === 1 ? '' : 's'}` : '');
  $('f-plan').className = 'plan';
  say(`plotted ${spec.series.length} curve${spec.series.length === 1 ? '' : 's'}`);
  drawPlot();
  renderFileReadout();
}

function renderFileReadout() {
  const wave = state.files.waveform;
  $('plotnote').innerHTML = wave && wave.series.length
    ? wave.series.map((s) =>
        `<b>${s.label}</b> ${fmtSI(s.duration_s, 's')} at ${fmtSI(s.fsamp, 'S/s')} · `
        + `rms ${fmtVolts(s.stats.rms)} · pp ${fmtVolts(s.stats.vpp)}`).join(' &nbsp; ')
    : '';
}

function drawFiles() {
  const wave = state.files.waveform;
  const spec = state.files.spectrum;
  // Colour by channel when the channels are what differ, and by series when
  // the files are: three traces of channel A in one blue tell you nothing.
  const all = [...(wave ? wave.series : []), ...(spec ? spec.series : [])];
  const nFiles = new Set(all.map((s) => s.file).filter(Boolean)).size;
  const byIndex = nFiles > 1;
  const colorOf = (s, i) => (byIndex ? COLORS[i % COLORS.length]
                                     : seriesColor(s.label, i));
  const time = (wave ? wave.series : []).map((s, i) => ({
    label: s.label, color: colorOf(s, i), x0: 0,
    dx: s.max.length > 1 ? s.duration_s / (s.max.length - 1) : 0,
    min: s.min, max: s.max,
  }));
  const curves = (spec ? spec.series : []).map((s, i) => ({
    label: s.label, color: colorOf(s, i), x: s.f, min: s.min, max: s.max,
    width: s.averaged > 1 ? 1.8 : 1.2,
  }));
  drawXY($('plot'), {
    series: time, fmtX: (v) => fmtSI(v, 's'), fmtY: fmtVolts,
    xLabel: 'time', yLabel: 'V',
    empty: 'tick files and press Plot selected',
  });
  drawXY($('plot2'), {
    series: curves, logX: $('f-logx').checked, logY: $('f-logy').checked,
    fmtX: (v) => fmtSI(v, 'Hz'), fmtY: fmtSci,
    xLabel: 'frequency', yLabel: spec ? spec.units : 'V/√Hz',
    empty: 'the spectrum appears here',
  });
  const host = $('legend');
  host.innerHTML = '';
  (spec ? spec.series : []).forEach((s, i) => {
    const el = document.createElement('span');
    const dot = document.createElement('i');
    dot.style.background = colorOf(s, i);
    el.append(dot, document.createTextNode(s.label));
    host.appendChild(el);
  });
}

function wireFileControls() {
  $('btn-files-refresh').addEventListener('click', () => loadFolder(state.files.dir));
  $('btn-files-plot').addEventListener('click', plotFiles);
  $('f-band').addEventListener('change', () => {
    $('f-band-row').hidden = !$('f-band').checked;
  });
  ['f-logx', 'f-logy'].forEach((id) => $(id).addEventListener('change', drawPlot));
  $('f-bits').addEventListener('change', () => {
    const n = Math.pow(2, Number($('f-bits').value));
    $('f-seg').textContent = `${n.toLocaleString()} points`;
  });
  $('f-bits').dispatchEvent(new Event('change'));
}

/* ----------------------------------------------------------------- boot */

function wireScopeControls() {
  TIME_PER_DIV.forEach((t) => {
    $('tb-div').add(new Option(`${fmtSI(t, 's')}/div`, String(t)));
  });

  $('view-scope').addEventListener('click', () => setView('scope'));
  $('view-roll').addEventListener('click', () => setView('roll'));
  $('view-daq').addEventListener('click', () => setView('daq'));
  $('view-files').addEventListener('click', () => setView('files'));
  $('btn-run').addEventListener('click', () => runScope(selectedMode()));
  $('btn-single').addEventListener('click', () => runScope('single'));
  $('btn-halt').addEventListener('click', stopScope);
  $('btn-save-sweep').addEventListener('click', saveSweep);

  document.querySelectorAll('#t-mode .seg-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('#t-mode .seg-btn').forEach((b) => {
        b.setAttribute('aria-checked', String(b === btn));
      });
      sweepSet({ mode: btn.dataset.mode }, 'trigger mode');
    });
  });
  $('t-source').addEventListener('change', (ev) =>
    sweepSet({ source: ev.target.value }, 'trigger source'));
  $('t-dir').addEventListener('change', (ev) =>
    sweepSet({ direction: ev.target.value }, 'trigger edge'));
  $('t-auto').addEventListener('change', (ev) =>
    sweepSet({ auto_ms: Number(ev.target.value) }, 'auto timeout'));
  $('t-level').addEventListener('change', (ev) => {
    const v = Number(ev.target.value);
    if (Number.isFinite(v)) sweepSet({ level: v }, 'trigger level');
  });
  $('tb-div').addEventListener('change', (ev) =>
    sweepSet({ time_per_div: Number(ev.target.value) }, 'timebase'));
  $('tb-samples').addEventListener('change', (ev) =>
    sweepSet({ samples: Number(ev.target.value) }, 'samples'));
  $('tb-pos').addEventListener('input', (ev) => {
    // Preview while sliding; send once on release.
    state.drag = { kind: 'position', value: Number(ev.target.value) };
    $('tb-pos-val').textContent = `${ev.target.value} %`;
    drawPlot();
  });
  $('tb-pos').addEventListener('change', (ev) => {
    state.drag = null;
    sweepSet({ position: Number(ev.target.value) }, 'trigger position');
  });

  wirePlotDragging();
  wireDaqControls();
  wireFileControls();
}

function boot() {
  buildChannels();
  wireScopeControls();
  loadCommands();
  loadRecordings();

  $('btn-open').addEventListener('click', async () => {
    say('connecting…', 'busy');
    const result = await api('open', {}, true);
    if (check(result, 'connect')) say('connected');
    refresh(); refreshPlan(); pollSweepSoon();
  });
  $('btn-close').addEventListener('click', async () => {
    await api('close', {}, true); say('disconnected'); state.traces = {}; refresh();
  });
  $('btn-stream').addEventListener('click', startStream);
  $('btn-stream-stop').addEventListener('click', stopStream);
  $('btn-record').addEventListener('click', startRecording);
  $('btn-record-stop').addEventListener('click', stopRecording);

  $('btn-siggen').addEventListener('click', async () => {
    const result = await api('siggen', {
      wave: $('g-wave').value, frequency: Number($('g-freq').value),
      amplitude: Number($('g-amp').value), offset: Number($('g-off').value),
    }, true);
    if (check(result, 'siggen')) say('generator on');
  });
  $('btn-siggen-off').addEventListener('click', async () => {
    const result = await api('siggen-off', {}, true);
    if (check(result, 'siggen')) say('generator off');
  });

  ['s-rate', 's-ratio', 's-mode', 'r-duration'].forEach((id) =>
    $(id).addEventListener('change', refreshPlan));
  $('s-window').addEventListener('change', () => {
    state.window_s = Number($('s-window').value);
  });

  CHANNELS.forEach((ch) => {
    const opt = document.createElement('option');
    opt.value = ch; opt.textContent = ch;
    $('d-source').appendChild(opt);
  });

  window.addEventListener('resize', drawPlot);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) pollSweepSoon();
  });

  // The last view this browser used; refresh() overrides it if the
  // instrument is busy in the other mode.
  if (readStoredView() === 'roll') state.view = 'roll';
  applyView();
  refresh();
  refreshPlan();
  pollSweep();
  pollDaq();
  // Poll rather than hold a stream open: a scope frame is a few thousand
  // floats, and polling degrades gracefully when the tab is hidden. Same
  // approach as every non-video pane in the DAQ panel.
  setInterval(() => { if (!document.hidden) refresh(); }, 700);
}

boot();
