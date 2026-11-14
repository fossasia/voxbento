/**
 * Home-page live demo ("Your voice"): the visitor speaks, and the server's own
 * models return captions, translations and a translated voice.
 *
 * Audio path: microphone -> AudioWorklet (16 kHz int16 frames) -> /ws/demo/live.
 * The worklet produces no sound; only the translated voice from the server is played.
 */
import {
  CLOSE_MESSAGES,
  SCRIPTS,
  addGlossaryEntry,
  doneMessage,
  findTermRanges,
  formatClock,
  frameLevel,
  glossaryTerms,
  pickListen,
  routeSummary,
  toggleTarget,
} from './live-demo-core.js';

const DEFAULT_SOURCE = 'en';
const DEFAULT_TARGETS = ['es', 'fr'];
const SPEAKER_ICON =
  '<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 5 6 9H2v6h4l5 4V5Z"/><path d="M15.5 8.5a5 5 0 0 1 0 7"/><path d="M19 5a10 10 0 0 1 0 14"/></svg>';

const $ = (id) => document.getElementById(id);

const els = {
  panel: $('live-demo-panel'),
  console: document.querySelector('#live-demo-panel .live-demo'),
  tabs: $('demo-mode-tabs'),
  recording: $('demo-player-root'),
  mic: $('live-demo-mic'),
  micLabel: $('live-demo-mic-label'),
  meta: $('live-demo-meta'),
  from: $('live-demo-from'),
  to: $('live-demo-to'),
  change: $('live-demo-change'),
  changeLabel: $('live-demo-change-label'),
  settings: $('live-demo-settings'),
  source: $('live-demo-source'),
  targets: $('live-demo-targets'),
  targetsLabel: $('live-demo-targets-label'),
  scriptRow: $('live-demo-script-row'),
  script: $('live-demo-script'),
  names: $('live-demo-names'),
  read: $('live-demo-read'),
  readLine: $('live-demo-read-line'),
  next: $('live-demo-next'),
  sourceCode: $('live-demo-source-code'),
  sourceText: $('live-demo-source-text'),
  rows: $('live-demo-target-rows'),
  status: $('live-demo-status'),
  term: $('live-demo-term'),
  termLang: $('live-demo-term-lang'),
  termTranslation: $('live-demo-term-translation'),
  termAdd: $('live-demo-term-add'),
  glossaryList: $('live-demo-glossary-list'),
  siteGlossary: $('live-demo-site-glossary'),
};

/** @typedef {{term: string, lang: string, translation: string}} GlossaryEntry */

/** @type {{enabled: boolean, languages: {code: string, name: string, voice: boolean}[], max_targets: number, max_seconds: number, max_glossary: number, glossary: GlossaryEntry[]}} */
let config;
/** @type {Map<string, string>} */
const names = new Map();
/** @type {Set<string>} */
const voiced = new Set();

const state = {
  source: DEFAULT_SOURCE,
  /** @type {string[]} */
  targets: [],
  /** @type {string|null} */
  listen: null,
  script: 'free',
  line: 0,
  /** The visitor's own glossary entries. @type {GlossaryEntry[]} */
  glossary: [],
};

/** The current run, or null when idle. */
let run = null;

// ── Setup ─────────────────────────────────────────────────────────────────────

async function init() {
  if (!els.panel) return;
  if (!navigator.mediaDevices?.getUserMedia || typeof AudioWorkletNode === 'undefined') return;
  try {
    const res = await fetch('/api/demo/live/config', { headers: { Accept: 'application/json' } });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    config = await res.json();
  } catch (err) {
    console.warn('[live-demo] Live demo config unavailable; showing the recording only', err);
    return;
  }
  if (!config.enabled || config.languages.length < 2) return;

  for (const lang of config.languages) {
    names.set(lang.code, lang.name);
    if (lang.voice) voiced.add(lang.code);
  }
  if (!names.has(state.source)) state.source = config.languages[0].code;
  state.targets = DEFAULT_TARGETS.filter((code) => names.has(code) && code !== state.source).slice(
    0,
    config.max_targets,
  );
  if (!state.targets.length) {
    state.targets = config.languages.map((l) => l.code).filter((c) => c !== state.source).slice(0, 1);
  }
  state.listen = pickListen(null, state.targets, voiced);

  buildSourceOptions();
  buildScriptOptions();
  els.targetsLabel.textContent = `Translate into · up to ${config.max_targets}`;
  renderSettings();
  renderRows();
  renderScript();
  renderGlossary();
  renderSiteGlossary();
  bindEvents();

  els.tabs.hidden = false;
  setMode('voice');
}

function buildSourceOptions() {
  els.source.replaceChildren(
    ...config.languages.map((lang) => new Option(lang.name, lang.code, false, lang.code === state.source)),
  );
}

function buildScriptOptions() {
  els.script.replaceChildren(
    ...Object.entries(SCRIPTS).map(([key, s]) => new Option(s.label, key, false, key === state.script)),
  );
}

function bindEvents() {
  els.tabs.querySelectorAll('[data-demo-mode]').forEach((tab) => {
    tab.addEventListener('click', () => setMode(tab.dataset.demoMode));
    tab.addEventListener('keydown', onTabKey);
  });
  els.mic.addEventListener('click', () => (run ? stop() : start()));
  els.change.addEventListener('click', () => {
    const open = els.settings.hidden;
    els.settings.hidden = !open;
    els.change.setAttribute('aria-expanded', String(open));
    els.changeLabel.textContent = open ? 'Done' : 'Change';
  });
  els.source.addEventListener('change', () => {
    state.source = els.source.value;
    state.targets = state.targets.filter((code) => code !== state.source);
    state.listen = pickListen(state.listen, state.targets, voiced);
    renderSettings();
    renderRows();
    renderScript();
    renderGlossary();
  });
  els.script.addEventListener('change', () => {
    state.script = els.script.value;
    state.line = 0;
    renderScript();
  });
  els.termAdd.addEventListener('click', addTerm);
  [els.term, els.termTranslation].forEach((input) => {
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        addTerm();
      }
    });
  });
  els.next.addEventListener('click', () => {
    state.line = (state.line + 1) % SCRIPTS[state.script].lines.length;
    renderScript();
  });
}

function onTabKey(event) {
  if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
  const tabs = [...els.tabs.querySelectorAll('[data-demo-mode]')];
  const index = tabs.indexOf(event.currentTarget);
  const next = tabs[(index + (event.key === 'ArrowRight' ? 1 : tabs.length - 1)) % tabs.length];
  next.focus();
  setMode(next.dataset.demoMode);
}

function setMode(mode) {
  const voice = mode === 'voice';
  els.tabs.querySelectorAll('[data-demo-mode]').forEach((tab) => {
    const selected = tab.dataset.demoMode === mode;
    tab.setAttribute('aria-selected', String(selected));
    tab.tabIndex = selected ? 0 : -1;
  });
  els.panel.hidden = !voice;
  els.recording.hidden = voice;
  if (voice) {
    pauseRecording();
  } else if (run) {
    stop();
  }
}

/** Pause the pre-recorded demo (demo-player.js) so two things never play at once. */
function pauseRecording() {
  const pauseIcon = $('demo-pause-icon');
  if (pauseIcon && !pauseIcon.classList.contains('hidden')) $('demo-play-btn')?.click();
}

// ── Rendering ─────────────────────────────────────────────────────────────────

function renderSettings() {
  const { from, to } = routeSummary(state.source, state.targets, names);
  els.from.textContent = from;
  els.to.textContent = to;
  els.sourceCode.textContent = state.source.toUpperCase();

  const full = state.targets.length >= config.max_targets;
  els.targets.replaceChildren(
    ...config.languages
      .filter((lang) => lang.code !== state.source)
      .map((lang) => {
        const chip = document.createElement('button');
        chip.type = 'button';
        chip.className = 'live-demo__chip';
        const selected = state.targets.includes(lang.code);
        chip.setAttribute('aria-pressed', String(selected));
        chip.disabled = Boolean(run) || (full && !selected);
        chip.textContent = lang.name;
        if (!lang.voice) {
          const tag = document.createElement('span');
          tag.className = 'live-demo__chip-tag';
          tag.textContent = 'captions';
          chip.append(tag);
        }
        chip.addEventListener('click', () => {
          state.targets = toggleTarget(state.targets, lang.code, state.source, config.max_targets);
          state.listen = pickListen(state.listen, state.targets, voiced);
          renderSettings();
          renderRows();
          renderGlossary();
        });
        return chip;
      }),
  );
  els.source.disabled = Boolean(run);
  els.names.disabled = Boolean(run);
}

function renderRows() {
  els.rows.replaceChildren(
    ...state.targets.map((code) => {
      const row = document.createElement('button');
      row.type = 'button';
      row.className = 'live-demo__row live-demo__row--target';
      row.dataset.lang = code;
      const canSpeak = voiced.has(code);
      row.disabled = !canSpeak;
      row.setAttribute('aria-pressed', String(code === state.listen));
      row.setAttribute(
        'aria-label',
        canSpeak ? `${names.get(code)}: listen to this language` : `${names.get(code)}: captions only`,
      );

      const label = document.createElement('span');
      label.className = 'live-demo__code';
      label.innerHTML = canSpeak ? SPEAKER_ICON : '';
      label.append(code.toUpperCase());

      const text = document.createElement('span');
      text.className = 'live-demo__text';
      text.dataset.role = 'text';
      text.textContent = canSpeak ? 'ready' : 'ready · captions only';

      row.append(label, text);
      row.addEventListener('click', () => setListen(code));
      return row;
    }),
  );
  if (!run) setSourceText('your words appear here', 'placeholder');
}

function renderScript() {
  const english = state.source === 'en';
  els.scriptRow.hidden = !english;
  els.read.hidden = !english;
  els.readLine.textContent = SCRIPTS[state.script].lines[state.line];
}

function renderRunState() {
  const phase = run ? (run.stopping ? 'stopping' : run.ready ? 'live' : 'starting') : 'idle';
  els.console.dataset.phase = phase;
  els.mic.setAttribute('aria-pressed', String(phase === 'live' || phase === 'starting'));
  els.mic.disabled = phase === 'stopping';
  els.micLabel.textContent = {
    idle: 'Start speaking',
    starting: 'Starting…',
    live: 'Stop',
    stopping: 'Finishing…',
  }[phase];
  if (phase === 'idle') {
    els.meta.textContent = 'headphones recommended';
    els.mic.style.setProperty('--live-level', '0');
  }
  renderSettings();
  renderGlossary();
}

function setSourceText(text, kind) {
  if (kind === 'placeholder') {
    els.sourceText.textContent = text;
  } else {
    renderHighlighted(els.sourceText, text, glossaryTerms(run?.glossary || [], null));
  }
  els.sourceText.dataset.kind = kind;
}

function setRowText(code, text) {
  const row = els.rows.querySelector(`[data-lang="${code}"] [data-role="text"]`);
  if (row) renderHighlighted(row, text, glossaryTerms(run?.glossary || [], code));
}

/** Show a caption with its glossary terms marked, built from text nodes (never as HTML). */
function renderHighlighted(el, text, terms) {
  const parts = [];
  let at = 0;
  for (const [start, end] of findTermRanges(text, terms)) {
    if (start > at) parts.push(document.createTextNode(text.slice(at, start)));
    const mark = document.createElement('mark');
    mark.className = 'live-demo__term';
    mark.textContent = text.slice(start, end);
    parts.push(mark);
    at = end;
  }
  if (at < text.length) parts.push(document.createTextNode(text.slice(at)));
  el.replaceChildren(...parts);
}

// ── Glossary ──────────────────────────────────────────────────────────────────

/** Everything the run will use: the visitor's entries, their names (kept as is), then the site's. */
function fullGlossary() {
  const typedNames = els.names.value
    .split(/[,\n]/)
    .map((name) => name.trim())
    .filter(Boolean)
    .map((name) => ({ term: name, lang: 'all', translation: name }));
  return [...state.glossary, ...typedNames, ...(config.glossary || [])];
}

function addTerm() {
  const result = addGlossaryEntry(
    state.glossary,
    { term: els.term.value, lang: els.termLang.value, translation: els.termTranslation.value },
    config.max_glossary,
  );
  if (result.error) {
    setStatus(result.error);
    return;
  }
  state.glossary = result.entries;
  els.term.value = '';
  els.termTranslation.value = '';
  setStatus('');
  renderGlossary();
  els.term.focus();
}

function renderGlossary() {
  // An entry for a language that is no longer chosen can't be used.
  state.glossary = state.glossary.filter((e) => e.lang === 'all' || state.targets.includes(e.lang));
  const locked = Boolean(run);

  const chosen = els.termLang.value || 'all';
  els.termLang.replaceChildren(
    new Option('All languages', 'all'),
    ...state.targets.map((code) => new Option(names.get(code) || code, code)),
  );
  els.termLang.value = [...els.termLang.options].some((o) => o.value === chosen) ? chosen : 'all';

  els.glossaryList.replaceChildren(
    ...state.glossary.map((entry) => {
      const item = document.createElement('li');
      item.className = 'live-demo__glossary-item';
      const lang = document.createElement('span');
      lang.className = 'live-demo__glossary-lang';
      lang.textContent = entry.lang === 'all' ? 'all' : entry.lang.toUpperCase();
      const text = document.createElement('span');
      text.textContent = `${entry.term} → ${entry.translation || 'kept as is'}`;
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.className = 'live-demo__glossary-remove';
      remove.setAttribute('aria-label', `Remove ${entry.term}`);
      remove.textContent = '×';
      remove.disabled = locked;
      remove.addEventListener('click', () => {
        state.glossary = state.glossary.filter((e) => e !== entry);
        renderGlossary();
      });
      item.append(lang, text, remove);
      return item;
    }),
  );
  [els.term, els.termLang, els.termTranslation, els.termAdd].forEach((el) => {
    el.disabled = locked;
  });
}

function renderSiteGlossary() {
  const terms = [...new Set((config.glossary || []).map((e) => e.term))];
  if (!terms.length) return;
  const shown = terms.slice(0, 8).join(', ');
  const more = terms.length > 8 ? ` and ${terms.length - 8} more` : '';
  els.siteGlossary.textContent = `Already listening for: ${shown}${more}.`;
  els.siteGlossary.hidden = false;
}

function setStatus(message) {
  els.status.textContent = message;
}

function setListen(code) {
  if (!voiced.has(code) || code === state.listen) return;
  state.listen = code;
  els.rows.querySelectorAll('[data-lang]').forEach((row) => {
    row.setAttribute('aria-pressed', String(row.dataset.lang === code));
  });
  if (run) {
    stopPlayback(run);
    if (run.ws?.readyState === WebSocket.OPEN) run.ws.send(JSON.stringify({ type: 'listen', lang: code }));
  }
}

// ── Run lifecycle ─────────────────────────────────────────────────────────────

async function start() {
  if (run) return;
  setStatus('');
  const r = {
    ready: false,
    stopping: false,
    ended: false,
    /** @type {{lang: string, sample_rate: number}|null} */
    pendingAudio: null,
    nextTime: 0,
    sources: new Set(),
    lastFinal: 0,
    /** @type {Record<string, number>} */
    shown: {},
    /** The glossary this run uses, for highlighting its captions. */
    glossary: fullGlossary(),
  };
  run = r;
  // Created inside the click so browsers allow audio playback later.
  r.ctx = new AudioContext();
  renderRunState();

  try {
    r.stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
    });
    await r.ctx.audioWorklet.addModule(els.panel.dataset.workletUrl);
    await r.ctx.resume();
  } catch (err) {
    console.warn('[live-demo] Microphone could not be opened', err);
    finish(r);
    setStatus(
      err?.name === 'NotAllowedError'
        ? 'Microphone access is blocked. Allow it from the address bar, then press the mic again.'
        : 'Your browser could not open a microphone.',
    );
    return;
  }
  if (run !== r) {
    finish(r);
    return;
  }

  r.input = r.ctx.createMediaStreamSource(r.stream);
  r.node = new AudioWorkletNode(r.ctx, 'live-demo-capture', { numberOfInputs: 1, numberOfOutputs: 1 });
  r.node.port.onmessage = (event) => onFrame(r, event.data);
  // The worklet writes nothing to its output; a muted connection only keeps every
  // browser processing the graph. The microphone itself is never heard.
  r.mute = r.ctx.createGain();
  r.mute.gain.value = 0;
  r.input.connect(r.node);
  r.node.connect(r.mute);
  r.mute.connect(r.ctx.destination);

  openSocket(r);
}

function onFrame(r, buffer) {
  els.mic.style.setProperty('--live-level', frameLevel(new Int16Array(buffer)).toFixed(3));
  if (r.ready && !r.stopping && r.ws?.readyState === WebSocket.OPEN) r.ws.send(buffer);
}

function openSocket(r) {
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const ws = new WebSocket(`${protocol}//${location.host}/ws/demo/live`);
  ws.binaryType = 'arraybuffer';
  r.ws = ws;
  ws.addEventListener('open', () => {
    ws.send(
      JSON.stringify({
        type: 'start',
        source: state.source,
        targets: state.targets,
        listen: state.listen,
        names: els.names.value,
        glossary: state.glossary,
      }),
    );
  });
  ws.addEventListener('message', (event) => {
    if (typeof event.data === 'string') {
      onMessage(r, event.data);
    } else {
      onAudio(r, event.data);
    }
  });
  ws.addEventListener('close', (event) => {
    if (!r.ended && !r.stopping) {
      setStatus(CLOSE_MESSAGES[event.code] || 'The connection to the demo dropped. Press the mic to try again.');
    }
    finish(r);
  });
}

function onMessage(r, raw) {
  let msg;
  try {
    msg = JSON.parse(raw);
  } catch (err) {
    console.warn('[live-demo] Ignoring a malformed message', err);
    return;
  }
  switch (msg.type) {
    case 'status':
      setStatus('Warming up the speech models on the server…');
      break;
    case 'ready':
      r.ready = true;
      r.deadline = performance.now() + msg.max_seconds * 1000;
      r.clock = setInterval(() => tick(r), 250);
      tick(r);
      setStatus('Listening. Start talking.');
      setSourceText('listening…', 'placeholder');
      renderRunState();
      break;
    case 'partial':
      if (msg.id > r.lastFinal) setSourceText(msg.text, 'partial');
      break;
    case 'final':
      r.lastFinal = Math.max(r.lastFinal, msg.id);
      if (msg.text) {
        setSourceText(msg.text, 'final');
        setStatus('');
        state.targets.forEach((code) => setRowText(code, '…'));
      }
      break;
    case 'translation':
      if (msg.text && msg.id >= (r.shown[msg.lang] || 0)) {
        r.shown[msg.lang] = msg.id;
        setRowText(msg.lang, msg.text);
      }
      break;
    case 'audio':
      r.pendingAudio = msg;
      break;
    case 'error':
      r.ended = true;
      setStatus(msg.message);
      break;
    case 'done':
      r.ended = true;
      setStatus(doneMessage(msg.reason, config.max_seconds));
      break;
    default:
      break;
  }
}

function onAudio(r, buffer) {
  const header = r.pendingAudio;
  r.pendingAudio = null;
  if (!header || header.lang !== state.listen || !r.ctx || r.ctx.state === 'closed') return;
  const pcm = new Int16Array(buffer);
  if (!pcm.length) return;
  const audio = r.ctx.createBuffer(1, pcm.length, header.sample_rate);
  const channel = audio.getChannelData(0);
  for (let i = 0; i < pcm.length; i++) channel[i] = pcm[i] / 32768;
  const source = r.ctx.createBufferSource();
  source.buffer = audio;
  source.connect(r.ctx.destination);
  const at = Math.max(r.ctx.currentTime + 0.05, r.nextTime);
  source.start(at);
  r.nextTime = at + audio.duration;
  r.sources.add(source);
  source.addEventListener('ended', () => r.sources.delete(source));
}

function stopPlayback(r) {
  r.sources.forEach((source) => source.stop());
  r.sources.clear();
  r.nextTime = 0;
}

function tick(r) {
  const left = (r.deadline - performance.now()) / 1000;
  els.meta.textContent = `${formatClock(left)} left`;
}

function stop() {
  const r = run;
  if (!r || r.stopping) return;
  r.stopping = true;
  releaseMic(r);
  if (r.ready && r.ws?.readyState === WebSocket.OPEN) {
    // The server captions what was still being said, then sends "done" and closes.
    r.ws.send(JSON.stringify({ type: 'stop' }));
    setStatus('Finishing the last sentence…');
    renderRunState();
  } else {
    r.ws?.close();
    finish(r);
  }
}

function releaseMic(r) {
  r.stream?.getTracks().forEach((track) => track.stop());
  r.input?.disconnect();
  r.node?.disconnect();
  if (r.node) r.node.port.onmessage = null;
  clearInterval(r.clock);
}

/** End the run: release the mic, and close audio once the last translated sentence has played. */
function finish(r) {
  if (r.finished) return;
  r.finished = true;
  r.ended = true;
  releaseMic(r);
  if (r.ws && r.ws.readyState <= WebSocket.OPEN) r.ws.close();
  if (r.ctx && r.ctx.state !== 'closed') {
    const remaining = Math.max(0, r.nextTime - r.ctx.currentTime);
    setTimeout(() => {
      r.ctx.close().catch((err) => console.warn('[live-demo] Audio context did not close', err));
    }, remaining * 1000 + 250);
  }
  if (run === r) {
    run = null;
    renderRunState();
  }
}

init();
