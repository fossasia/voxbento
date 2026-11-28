export function isJwtExpired(token, nowSeconds = Date.now() / 1000) {
  if (!token || typeof token !== 'string') return false;
  const parts = token.split('.');
  if (parts.length !== 3) return false;
  try {
    const base64 = parts[1].replace(/-/g, '+').replace(/_/g, '/');
    const padded = base64 + '='.repeat((4 - (base64.length % 4)) % 4);
    const json = typeof atob === 'function'
      ? atob(padded)
      : Buffer.from(padded, 'base64').toString('utf8');
    const payload = JSON.parse(json);
    return typeof payload.exp === 'number' && payload.exp <= nowSeconds;
  } catch (err) {
    console.error('Failed to decode embed token expiry:', err);
    return false;
  }
}

export function initEmbedPlayer(env = {}) {
  const doc = env.document || document;
  const win = env.window || window;
  const WebSocketCtor = env.WebSocket || win.WebSocket;
  const AudioContextCtor = env.AudioContext || win.AudioContext || win.webkitAudioContext;
  const whepFactory = env.createWhepClient || win.createWhepClient;
  const ttsParser = env.TTSParser || win.TTSParser;
  const audioSchedulerFactory = env.AudioScheduler || win.AudioScheduler;
  const schedule = env.setTimeout || win.setTimeout.bind(win);
  const cancel = env.clearTimeout || win.clearTimeout.bind(win);

  const configEl = doc.getElementById('embed-config');
  if (!configEl) {
    console.error('Embed configuration not found.');
    return null;
  }

  let config;
  try {
    config = JSON.parse(configEl.textContent);
  } catch (err) {
    console.error('Failed to parse embed configuration:', err);
    return null;
  }

  const AUDIO_MODE = config.audio_mode || 'whep';
  const WHEP_URL = config.whep_url || '';
  const TTS_URL = config.tts_url || '';
  const CAPTION_URL = config.caption_url || '';
  const CAPTIONS_ON = Boolean(config.captions_enabled);
  const EMBED_TOKEN = config.token || '';
  const TARGET_LANG = config.target_lang_code || '';
  const SOURCE_LANG = config.source_lang_code || '';
  const AUDIO_DELAY_MS = Math.min(Math.max(Number(config.audio_delay_ms) || 0, 0), 10000);

  const audioEl = doc.getElementById('embed-audio');
  const playBtn = doc.getElementById('play-btn');
  const statusDot = doc.getElementById('status-dot');
  const statusText = doc.getElementById('status-text');
  const captionsEl = doc.getElementById('captions-box') || null;

  let playing = false;
  let expired = false;
  let captionWs = null;
  let ttsWs = null;
  let captionRetryTimer = null;
  let ttsRetryTimer = null;

  function postToParent(type, payload) {
    if (!config.headless || !win.parent) return;
    win.parent.postMessage(
      { source: 'voxbento-embed', v: 1, type, payload },
      config.target_origin,
    );
  }

  function setStatus(state, text) {
    statusDot.className = 'status-dot ' + (state || '');
    statusText.textContent = text;
  }

  function setExpired() {
    if (expired) return;
    expired = true;
    playing = false;
    setStatus('error', 'Session expired');
    if (playBtn) {
      playBtn.textContent = '\u26a0 Session Expired \u2014 Reload Host Page';
      playBtn.disabled = true;
    }
    if (captionRetryTimer) { cancel(captionRetryTimer); captionRetryTimer = null; }
    if (ttsRetryTimer) { cancel(ttsRetryTimer); ttsRetryTimer = null; }
    if (captionWs) { const ws = captionWs; captionWs = null; ws.close(); }
    if (ttsWs) { const ws = ttsWs; ttsWs = null; ws.close(); }
    postToParent('error', { code: 'session_expired', message: 'Embed token expired' });
  }

  function buildWsUrl(httpOrWsUrl) {
    const wsProto = win.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const urlPath = new URL(httpOrWsUrl).pathname;
    return wsProto + '//' + win.location.host + urlPath + '?token=' + encodeURIComponent(EMBED_TOKEN);
  }

  // ── Audio Controllers ──────────────────────────────────────────────────

  function createWhepController() {
    const whep = whepFactory();
    whep.start({
      whepUrl: WHEP_URL,
      audioEl,
      audioDelayMs: config.audio_delay_ms,
      onState(s) {
        postToParent('booth_state', { state: s.peerConnection });
        if (s.peerConnection === 'connected') {
          setStatus('live', 'Live');
          playBtn.disabled = false;
          playBtn.textContent = playing ? '\u23f8  Pause' : '\u25b6  Play Audio';
        } else if (s.peerConnection === 'failed' || s.peerConnection === 'closed') {
          setStatus('error', 'Stream unavailable');
          playBtn.disabled = false;
        } else {
          setStatus('connecting', 'Connecting\u2026');
        }
      },
      onLog() {},
    });

    return {
      play() {
        return audioEl.play().then(() => {
          playing = true;
          playBtn.textContent = '\u23f8  Pause';
        });
      },
      pause() {
        audioEl.pause();
        playing = false;
        playBtn.textContent = '\u25b6  Play Audio';
      },
      setVolume(v) {
        audioEl.volume = v;
      },
    };
  }

  function createTtsController() {
    let audioCtx = null;
    let gainNode = null;
    let audioScheduler = null;
    let currentVolume = 1;
    let ttsRetryDelayMs = 1000;
    let segmentStore = Object.create(null);
    let expectedSeq = null;
    let isSegmentPlaying = false;
    let seqWaitTimer = null;
    let fallbackQueueTimer = null;
    const delayTimers = new Set();

    function ensureAudioGraph() {
      if (!audioCtx) {
        audioCtx = new AudioContextCtor();
        gainNode = audioCtx.createGain();
        gainNode.gain.value = currentVolume;
        gainNode.connect(audioCtx.destination);
        audioScheduler = audioSchedulerFactory.create(audioCtx, {
          jitterBufferSec: 0.25,
          comfortNoiseEnabled: false,
          destination: gainNode,
        });
      }
      return audioCtx;
    }

    function clearPendingPlayback() {
      delayTimers.forEach((t) => cancel(t));
      delayTimers.clear();
      if (seqWaitTimer) { cancel(seqWaitTimer); seqWaitTimer = null; }
      if (fallbackQueueTimer) { cancel(fallbackQueueTimer); fallbackQueueTimer = null; }
      segmentStore = Object.create(null);
      expectedSeq = null;
      isSegmentPlaying = false;
      if (audioScheduler) audioScheduler.reset();
    }

    function advanceSeq(seqId) {
      isSegmentPlaying = false;
      delete segmentStore[seqId];
      expectedSeq++;
      fallbackQueueTimer = schedule(pumpSegmentQueue, 30);
    }

    function pumpSegmentQueue() {
      if (!playing || isSegmentPlaying) return;
      const nextSeg = segmentStore[expectedSeq];
      if (!nextSeg) {
        if (!seqWaitTimer) {
          seqWaitTimer = schedule(() => {
            seqWaitTimer = null;
            const storedSeqs = Object.keys(segmentStore).map(Number).filter((n) => !isNaN(n)).sort((a, b) => a - b);
            const nextAvailable = storedSeqs.find((s) => s > expectedSeq);
            if (nextAvailable !== undefined) {
              expectedSeq = nextAvailable;
              pumpSegmentQueue();
            }
          }, 65000);
        }
        return;
      }

      if (seqWaitTimer) { cancel(seqWaitTimer); seqWaitTimer = null; }
      isSegmentPlaying = true;

      if (nextSeg.error || nextSeg.empty) {
        advanceSeq(expectedSeq);
        return;
      }

      if (nextSeg.audioBuffer && audioScheduler) {
        const finishedSeq = expectedSeq;
        audioScheduler.scheduleBuffer(nextSeg.audioBuffer);
        advanceSeq(finishedSeq);
      } else {
        // Waiting for Stage 2 audio: arm seqWaitTimer so a dropped Stage-2 frame cannot stall forever.
        isSegmentPlaying = false;
        if (!seqWaitTimer) {
          const elapsed = Date.now() - (nextSeg.receivedAt || Date.now());
          seqWaitTimer = schedule(() => {
            seqWaitTimer = null;
            if (segmentStore[expectedSeq]) {
              segmentStore[expectedSeq].error = 'tts_timeout';
            }
            pumpSegmentQueue();
          }, Math.max(0, 65000 - elapsed));
        }
      }
    }

    function processFrame(frame) {
      if (!playing) return;
      const seq = frame.header && frame.header.seq;
      if (!seq) return;
      if (expectedSeq === null || expectedSeq === undefined) expectedSeq = seq;
      if (seq < expectedSeq) return;

      const hasAudio = Boolean(!frame.header.error && frame.audioBytes && frame.audioBytes.byteLength > 0);
      let audioBuffer = null;
      if (hasAudio && audioCtx) {
        const aligned = frame.audioBytes.buffer.slice(
          frame.audioBytes.byteOffset,
          frame.audioBytes.byteOffset + frame.audioBytes.byteLength,
        );
        const int16 = new Int16Array(aligned);
        const float32 = new Float32Array(int16.length);
        for (let i = 0; i < int16.length; i++) {
          float32[i] = int16[i] / 32768.0;
        }
        audioBuffer = audioCtx.createBuffer(1, float32.length, 24000);
        audioBuffer.copyToChannel(float32, 0);
      }

      segmentStore[seq] = {
        seq,
        error: frame.header.error || (frame.header.audio_pending === false && !hasAudio ? 'no_audio' : null),
        empty: !frame.header.translation && !frame.header.error && frame.header.audio_pending === false,
        audioBuffer,
        receivedAt: (segmentStore[seq] && segmentStore[seq].receivedAt) || Date.now(),
      };

      const storedSeqs = Object.keys(segmentStore).map(Number).sort((a, b) => a - b);
      if (storedSeqs.length > 5) {
        while (storedSeqs.length > 5) {
          const oldest = storedSeqs.shift();
          delete segmentStore[oldest];
        }
        expectedSeq = storedSeqs[0];
        if (seqWaitTimer) {
          cancel(seqWaitTimer);
          seqWaitTimer = null;
        }
      }
      pumpSegmentQueue();
    }

    function openTtsWs() {
      if (expired || !TTS_URL) return;
      const ws = new WebSocketCtor(buildWsUrl(TTS_URL));
      ws.binaryType = 'arraybuffer';
      ttsWs = ws;
      setStatus('connecting', 'Connecting\u2026');
      postToParent('booth_state', { state: 'connecting' });

      ws.onopen = function () {
        if (ttsWs !== ws) return;
        ttsRetryDelayMs = 1000;
        setStatus('live', 'Live');
        playBtn.disabled = false;
        playBtn.textContent = playing ? '\u23f8  Pause' : '\u25b6  Play Audio';
        postToParent('booth_state', { state: 'connected' });
      };

      ws.onmessage = function (ev) {
        if (ttsWs !== ws || !(ev.data instanceof ArrayBuffer) || !playing) return;
        try {
          const frame = ttsParser.parseFrame(ev.data);
          if (AUDIO_DELAY_MS > 0) {
            const timer = schedule(() => {
              delayTimers.delete(timer);
              processFrame(frame);
            }, AUDIO_DELAY_MS);
            delayTimers.add(timer);
          } else {
            processFrame(frame);
          }
        } catch (err) {
          console.error('TTS frame parse error:', err);
        }
      };

      ws.onclose = function (ev) {
        if (ttsWs !== ws) return;
        ttsWs = null;
        clearPendingPlayback();
        if (ev.code === 4001 || ev.code === 4003 || (ev.code === 1006 && isJwtExpired(EMBED_TOKEN))) {
          setExpired();
          return;
        }
        setStatus('connecting', 'Reconnecting\u2026');
        postToParent('booth_state', { state: 'connecting' });
        ttsRetryTimer = schedule(openTtsWs, ttsRetryDelayMs * (0.5 + Math.random() / 2));
        ttsRetryDelayMs = Math.min(ttsRetryDelayMs * 2, 15000);
      };
    }

    openTtsWs();

    return {
      play() {
        const ctx = ensureAudioGraph();
        return ctx.resume().then(() => {
          if (ctx.state !== 'running') {
            throw new Error('AudioContext resume blocked');
          }
          playing = true;
          playBtn.textContent = '\u23f8  Pause';
        });
      },
      pause() {
        playing = false;
        clearPendingPlayback();
        if (audioCtx && audioCtx.state === 'running') {
          audioCtx.suspend().catch((err) => console.error('AudioContext suspend failed:', err));
        }
        playBtn.textContent = '\u25b6  Play Audio';
      },
      setVolume(v) {
        currentVolume = v;
        if (gainNode) gainNode.gain.value = v;
      },
    };
  }

  function createNoneController() {
    if (playBtn) playBtn.hidden = true;
    setStatus('connecting', 'Connecting captions\u2026');
    return {
      play() { return Promise.resolve(); },
      pause() {},
      setVolume() {},
    };
  }

  const audioController =
    AUDIO_MODE === 'tts'
      ? createTtsController()
      : AUDIO_MODE === 'none'
        ? createNoneController()
        : createWhepController();

  if (playBtn && AUDIO_MODE !== 'none') {
    playBtn.addEventListener('click', function () {
      if (!playing) {
        audioController.play().catch((err) => {
          console.error('Playback blocked:', err);
          setStatus('error', 'Playback blocked \u2014 check browser permissions');
        });
      } else {
        audioController.pause();
      }
    });
  }

  // ── Captions WebSocket ─────────────────────────────────────────────────

  if (CAPTIONS_ON && captionsEl && CAPTION_URL) {
    const wsUrl = buildWsUrl(CAPTION_URL);
    let captionRetryDelayMs = 1000;
    const historyEl = doc.getElementById('embed-caption-history');
    const currentEl = doc.getElementById('embed-caption-current');
    const maxHistoryDOM = 50;
    const FOLLOW_THRESHOLD_PX = 40;
    let captionHistoryHeadless = [];
    const maxHistoryHeadless = 2;

    function isCaptionRelevant(msg) {
      if (config.headless) {
        if (msg.type === 'caption') return true;
        if (msg.type === 'translated_caption' && (!msg.language_code || msg.language_code === TARGET_LANG)) return true;
        if (msg.type === 'translation' && msg.language_code === TARGET_LANG) return true;
        return false;
      }
      const isTranslating = Boolean(TARGET_LANG && TARGET_LANG !== SOURCE_LANG) || AUDIO_MODE !== 'whep';
      if (isTranslating) {
        if (msg.type === 'translated_caption' && (!msg.language_code || msg.language_code === TARGET_LANG)) return true;
        if (msg.type === 'translation' && msg.language_code === TARGET_LANG) return true;
        return false;
      }
      return msg.type === 'caption';
    }

    function handleCaptionMessage(ev) {
      let msg;
      try {
        msg = JSON.parse(ev.data);
      } catch (err) {
        console.error('Invalid caption JSON:', err);
        return;
      }
      if (!isCaptionRelevant(msg)) return;

      const status = (msg.type === 'translation' || msg.type === 'translated_caption')
        ? 'final'
        : (msg.status || 'final');
      let text = (msg.text || '').trim();

      if (config.headless) {
        if (status === 'clear') {
          captionHistoryHeadless = [];
          text = '';
        } else if (status === 'final') {
          if (text) {
            captionHistoryHeadless.push(text);
            if (captionHistoryHeadless.length > maxHistoryHeadless) {
              captionHistoryHeadless.shift();
            }
          }
          text = '';
        }
        const displayText = captionHistoryHeadless.concat(text ? [text] : []).join(' ');
        postToParent('subtitle', {
          text: displayText,
          language: msg.language_code || TARGET_LANG || SOURCE_LANG,
          msg_type: msg.type,
          raw_status: status,
          raw_text: msg.text,
        });
        return;
      }

      const followLatest =
        captionsEl.scrollHeight - captionsEl.scrollTop - captionsEl.clientHeight <= FOLLOW_THRESHOLD_PX;
      if (status === 'clear') {
        currentEl.textContent = '';
      } else if (status === 'final') {
        if (text) {
          const p = doc.createElement('div');
          p.textContent = text;
          historyEl.appendChild(p);
          while (historyEl.children.length > maxHistoryDOM) {
            historyEl.removeChild(historyEl.firstChild);
          }
        }
        currentEl.textContent = '';
      } else if (status === 'partial') {
        currentEl.textContent = text;
      }

      if (historyEl.children.length === 0 && !currentEl.textContent) {
        captionsEl.classList.add('empty');
      } else {
        captionsEl.classList.remove('empty');
      }
      if (followLatest) {
        captionsEl.scrollTop = captionsEl.scrollHeight;
      }
    }

    function openCaptionWs() {
      if (expired) return;
      const ws = new WebSocketCtor(wsUrl);
      captionWs = ws;
      ws.onopen = function () {
        if (captionWs !== ws) return;
        captionRetryDelayMs = 1000;
        if (AUDIO_MODE === 'none') {
          setStatus('captions', 'Captions only');
          postToParent('booth_state', { state: 'connected' });
        }
      };
      ws.onmessage = handleCaptionMessage;
      ws.onclose = function (ev) {
        if (ev.code === 4001) { setExpired(); return; }
        if (ev.code === 4003 || captionWs !== ws) return;
        captionWs = null;
        if (AUDIO_MODE === 'none') {
          setStatus('connecting', 'Reconnecting\u2026');
          postToParent('booth_state', { state: 'connecting' });
        }
        captionRetryTimer = schedule(openCaptionWs, captionRetryDelayMs * (0.5 + Math.random() / 2));
        captionRetryDelayMs = Math.min(captionRetryDelayMs * 2, 15000);
      };
    }

    openCaptionWs();
  }

  // ── Headless Parent Commands ───────────────────────────────────────────

  if (config.headless) {
    win.addEventListener('message', function (event) {
      if (config.allowed_origins.length > 0 && !config.allowed_origins.includes(event.origin)) return;
      if (!event.data || event.data.source !== 'voxbento-parent') return;

      if (event.data.type === 'play') {
        audioController.play().catch((err) => {
          postToParent('error', { code: 'autoplay_blocked', message: err.message });
        });
      }
      if (event.data.type === 'pause') {
        audioController.pause();
      }
      if (event.data.type === 'set_volume') {
        const v = Number(event.data.volume);
        if (v >= 0 && v <= 1) audioController.setVolume(v);
      }
    });
  }

  return { audioController };
}

if (typeof document !== 'undefined') {
  document.addEventListener('DOMContentLoaded', function () {
    initEmbedPlayer();
  });
}
