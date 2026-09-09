/* PicoScope 4824A panel.
 *
 * Every control here calls the same /api/<command> route the CLI calls, so the
 * page has no capability the command line lacks. The command list at the
 * bottom of the sidebar is rendered from /describe, i.e. from the same
 * registry the CLI builds its subcommands from.
 *
 * URLs are RELATIVE (no leading slash) so the page works both standalone at /
 * and mounted under /scope by the xsphere-daq panel.
 */
'use strict';

const CHANNELS = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'];
const COLORS = ['#5cf', '#fd6', '#f86', '#8f8', '#c9f', '#6cc', '#fa8', '#aaf'];
const RANGES = ['R_10MV', 'R_20MV', 'R_50MV', 'R_100MV', 'R_200MV', 'R_500MV',
                'R_1V', 'R_2V', 'R_5V', 'R_10V', 'R_20V', 'R_50V'];
const RANGE_LABEL = { R_10MV: '±10 mV', R_20MV: '±20 mV', R_50MV: '±50 mV',
  R_100MV: '±100 mV', R_200MV: '±200 mV', R_500MV: '±500 mV', R_1V: '±1 V',
  R_2V: '±2 V', R_5V: '±5 V', R_10V: '±10 V', R_20V: '±20 V', R_50V: '±50 V' };

const $ = (id) => document.getElementById(id);
const state = { open: false, streaming: false, recording: false,
                channels: {}, traces: {}, window_s: 10 };

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

function say(text, kind) {
  const el = $('status');
  el.textContent = text;
  el.className = 'status' + (kind ? ' ' + kind : '');
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
}

async function pushChannel(ch) {
  const result = await api('channel-set', {
    channel: ch,
    enabled: $(`en-${ch}`).checked,
    range: $(`rng-${ch}`).value,
    coupling: $(`cpl-${ch}`).value,
  }, true);
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
  renderLegend();
}

function enabledChannels() {
  return CHANNELS.filter((ch) => state.channels[ch] && state.channels[ch].enabled);
}

function renderLegend() {
  const host = $('legend');
  host.innerHTML = '';
  enabledChannels().forEach((ch) => {
    const el = document.createElement('span');
    const dot = document.createElement('i');
    dot.style.background = COLORS[CHANNELS.indexOf(ch)];
    el.append(dot, document.createTextNode(
      `${ch}  ${RANGE_LABEL[state.channels[ch].range] || ''}`));
    host.appendChild(el);
  });
}

/* ----------------------------------------------------------------- plot */

function drawPlot() {
  const canvas = $('plot');
  const ratio = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  if (canvas.width !== w * ratio || canvas.height !== h * ratio) {
    canvas.width = w * ratio; canvas.height = h * ratio;
  }
  const ctx = canvas.getContext('2d');
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, w, h);

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

function fmtVolts(v) {
  const a = Math.abs(v);
  if (a >= 1) return v.toFixed(2) + ' V';
  if (a >= 0.001) return (v * 1000).toFixed(1) + ' mV';
  return (v * 1e6).toFixed(0) + ' µV';
}

function fmtTime(s) {
  if (s >= 60) return (s / 60).toFixed(s % 60 ? 1 : 0) + 'm';
  return s.toFixed(s < 10 ? 1 : 0) + 's';
}

function fmtBytes(bps) {
  if (bps >= 1e6) return (bps / 1e6).toFixed(2) + ' MB/s';
  if (bps >= 1e3) return (bps / 1e3).toFixed(1) + ' kB/s';
  return bps.toFixed(0) + ' B/s';
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

async function refresh() {
  const st = await api('status');
  if (!st.ok) { say(st.error || 'unreachable', 'err'); return; }

  state.open = !!st.open;
  const stream = st.stream || {};
  const rec = st.recorder || {};
  state.streaming = !!stream.running;
  state.recording = !!rec.recording;

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
  $('btn-stream').disabled = !state.open || state.streaming;
  $('btn-stream-stop').disabled = !state.streaming;
  $('btn-record').disabled = !state.streaming || state.recording;
  $('btn-record-stop').disabled = !state.recording;
  $('btn-siggen').disabled = !state.open;
  $('btn-siggen-off').disabled = !state.open;

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
    say(`streaming · ${Math.round(stats.measured_rate_hz || 0).toLocaleString()}`
        + ` S/s/ch · captured ${pct}`
        + (stats.dropped_estimate ? ` · ~${stats.dropped_estimate} lost` : ''),
        frac != null && frac < 0.98 ? 'err' : null);
  }

  renderMeasurements(stream.measurements || {});
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

function renderMeasurements(m) {
  const body = $('measure').querySelector('tbody');
  body.innerHTML = '';
  Object.keys(m).sort().forEach((ch) => {
    const v = m[ch];
    const tr = document.createElement('tr');
    tr.innerHTML = `<td style="color:${COLORS[CHANNELS.indexOf(ch)]}">${ch}</td>`
      + `<td class="num">${fmtVolts(v.min)}</td>`
      + `<td class="num">${fmtVolts(v.max)}</td>`
      + `<td class="num">${fmtVolts(v.mean)}</td>`
      + `<td class="num">${fmtVolts(v.rms)}</td>`;
    body.appendChild(tr);
  });
  if (body.children.length) {
    const head = document.createElement('tr');
    head.innerHTML = '<td></td><td class="num sub">min</td>'
      + '<td class="num sub">max</td><td class="num sub">mean</td>'
      + '<td class="num sub">rms</td>';
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

/* ----------------------------------------------------------------- boot */

function boot() {
  buildChannels();
  loadCommands();
  loadRecordings();

  $('btn-open').addEventListener('click', async () => {
    say('connecting…', 'busy');
    const result = await api('open', {}, true);
    if (check(result, 'connect')) say('connected');
    refresh(); refreshPlan();
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

  window.addEventListener('resize', drawPlot);

  refresh();
  refreshPlan();
  // Poll rather than hold a stream open: a scope frame is a few thousand
  // floats, and polling degrades gracefully when the tab is hidden. Same
  // approach as every non-video pane in the DAQ panel.
  setInterval(() => { if (!document.hidden) refresh(); }, 700);
}

boot();
