/**
 * WHEP Listener — WebRTC playback client for MediaMTX.
 *
 * Connects to a MediaMTX WHEP endpoint and plays the received audio track
 * through an HTML <audio> element.  Displays connection state, ICE state,
 * and audio-track information for debugging.
 *
 * Usage (from the template):
 *   WhepListener.start({ whepUrl, audioEl, onState, onLog })
 */
'use strict';

function createWhepClient() {

  // Only the current attempt may attach audio, report state or schedule a retry.
  let attempt = null;
  /** @type {number|null} */
  let reconnectTimer = null;
  /** Exponential back-off delay (ms). */
  let reconnectDelay = 100;
  /** True from a failed attempt until a WHEP session is established again. */
  let retrying = false;

  // Callbacks supplied by caller.
  let _onState = () => {};
  let _onLog   = () => {};
  let _whepUrl = '';
  let _audioEl = null;
  let _audioDelayMs = 0;
  let currentStream = null;
  let playOnTrack = true;
  let delayedAudio = {
    context: null,
    source: null,
    delay: null,
    output: null,
    receiver: null,
    resumeHandler: null,
  };

  // ── helpers ────────────────────────────────────────────────────────────

  function log(msg) {
    _onLog(msg);
  }

  function emitState() {
    const pc = attempt ? attempt.peer : null;
    _onState({
      peerConnection: pc ? pc.connectionState : 'closed',
      ice:            pc ? pc.iceConnectionState : 'closed',
      signaling:      pc ? pc.signalingState : 'closed',
      audioActive:    Boolean(_audioEl && !_audioEl.paused && _audioEl.srcObject &&
        (!delayedAudio.output || delayedAudio.context.state === 'running')),
      whepUrl:        _whepUrl,
      retrying:       retrying,
    });
  }

  function removeResumeHandler() {
    if (!delayedAudio.resumeHandler) return;
    document.removeEventListener('click', delayedAudio.resumeHandler);
    document.removeEventListener('touchend', delayedAudio.resumeHandler);
    document.removeEventListener('pointerup', delayedAudio.resumeHandler);
    delayedAudio.resumeHandler = null;
  }

  function resumeDelayedContext() {
    const ctx = delayedAudio.context;
    if (!ctx || ctx.state !== 'suspended') return;
    ctx.resume().then(() => {
      if (delayedAudio.context !== ctx) return;
      if (ctx.state === 'running') removeResumeHandler();
      emitState();
    }).catch((err) => {
      log('AudioContext resume blocked: ' + err.message);
    });
  }

  function prepareDelayedContext() {
    const AudioCtor = window.AudioContext || window.webkitAudioContext;
    if (!AudioCtor) {
      log('Web Audio API unavailable; falling back to direct audio playback');
      return null;
    }
    if (!delayedAudio.context || delayedAudio.context.state === 'closed') {
      delayedAudio.context = new AudioCtor();
    }
    if (delayedAudio.context.state === 'suspended' && !delayedAudio.resumeHandler) {
      delayedAudio.resumeHandler = resumeDelayedContext;
      document.addEventListener('click', delayedAudio.resumeHandler);
      document.addEventListener('touchend', delayedAudio.resumeHandler);
      document.addEventListener('pointerup', delayedAudio.resumeHandler);
    }
    resumeDelayedContext();
    return delayedAudio.context;
  }

  function cleanupDelayedGraph(closeContext) {
    if (closeContext) {
      removeResumeHandler();
    }
    if (delayedAudio.source) {
      delayedAudio.source.disconnect();
      delayedAudio.source = null;
    }
    if (delayedAudio.delay) {
      delayedAudio.delay.disconnect();
      delayedAudio.delay = null;
    }
    if (delayedAudio.receiver) {
      delayedAudio.receiver.pause();
      delayedAudio.receiver.srcObject = null;
      delayedAudio.receiver = null;
    }
    if (delayedAudio.output) {
      delayedAudio.output.stream.getTracks().forEach((track) => track.stop());
      delayedAudio.output = null;
    }
    if (closeContext && delayedAudio.context) {
      const ctx = delayedAudio.context;
      delayedAudio.context = null;
      if (ctx.state !== 'closed') {
        ctx.close().catch((err) => log('AudioContext close failed: ' + err.message));
      }
    }
  }

  function playAudio(autoplay) {
    if (!autoplay) return;
    _audioEl.play().then(emitState).catch((err) => log('Autoplay blocked: ' + err.message));
  }

  function attachDirectStream(stream, autoplay = true) {
    cleanupDelayedGraph(true);
    currentStream = stream;
    _audioEl.srcObject = stream;
    playAudio(autoplay);
  }

  function attachDelayedStream(stream, autoplay = true) {
    cleanupDelayedGraph(false);
    currentStream = stream;
    const ctx = prepareDelayedContext();
    if (!ctx) {
      attachDirectStream(stream, autoplay);
      return;
    }

    // Chromium requires a playing remote-stream sink to deliver WebRTC samples to Web Audio.
    delayedAudio.receiver = document.createElement('audio');
    delayedAudio.receiver.muted = true;
    delayedAudio.receiver.srcObject = stream;
    delayedAudio.receiver.play().catch((err) => log('Muted WebRTC sink blocked: ' + err.message));

    const delaySeconds = _audioDelayMs / 1000;
    delayedAudio.source = ctx.createMediaStreamSource(stream);
    delayedAudio.delay = ctx.createDelay(Math.max(delaySeconds, 1));
    delayedAudio.delay.delayTime.value = delaySeconds;
    delayedAudio.output = ctx.createMediaStreamDestination();
    delayedAudio.source.connect(delayedAudio.delay);
    delayedAudio.delay.connect(delayedAudio.output);
    // The element remains the only audible output, including its playback and volume controls.
    _audioEl.srcObject = delayedAudio.output.stream;
    playAudio(autoplay);
  }

  // ── WHEP negotiation ──────────────────────────────────────────────────

  /**
   * Wait for ICE gathering to complete (or timeout).
   * Returns a promise that resolves when gathering is "complete" or after
   * the given timeout (whichever comes first).
   */
  function waitForIce(current, timeoutMs) {
    const peer = current.peer;
    return new Promise((resolve) => {
      if (peer.iceGatheringState === 'complete') {
        resolve();
        return;
      }
      function finish() {
        clearTimeout(timer);
        peer.removeEventListener('icegatheringstatechange', onChange);
        current.cancelIce = null;
        resolve();
      }
      function onChange() {
        if (peer.iceGatheringState === 'complete') finish();
      }
      const timer = setTimeout(finish, timeoutMs);
      current.cancelIce = finish;
      peer.addEventListener('icegatheringstatechange', onChange);
    });
  }

  function releaseResource(current) {
    if (!current.resourceUrl) return;
    const url = current.resourceUrl;
    current.resourceUrl = null;
    // Teardown is independent of the negotiation's aborted signal.
    fetch(url, { method: 'DELETE' }).catch((err) => log('WHEP session cleanup failed: ' + err.message));
  }

  async function connect() {
    cleanup(false);
    const current = {
      peer: null,
      url: _whepUrl,
      controller: new AbortController(),
      resourceUrl: null,
      cancelIce: null,
    };
    attempt = current;

    try {
      log('Creating RTCPeerConnection...');
      const peer = new RTCPeerConnection({ iceServers: [] });
      current.peer = peer;
      peer.addTransceiver('audio', { direction: 'recvonly' });

      peer.addEventListener('track', (event) => {
        if (attempt !== current) return;
        log('Remote track received: kind=' + event.track.kind);
        if (event.track.kind === 'audio') {
          const stream = event.streams[0] || new MediaStream([event.track]);
          if (_audioEl.srcObject) playOnTrack = !_audioEl.paused;
          if (_audioDelayMs > 0) {
            attachDelayedStream(stream, playOnTrack);
          } else {
            attachDirectStream(stream, playOnTrack);
          }
        }
        emitState();
      });
      peer.addEventListener('connectionstatechange', () => {
        if (attempt !== current) return;
        log('PeerConnection state: ' + peer.connectionState);
        emitState();
        if (peer.connectionState === 'failed' || peer.connectionState === 'disconnected') {
          scheduleReconnect(current);
        }
      });
      peer.addEventListener('iceconnectionstatechange', () => {
        if (attempt !== current) return;
        log('ICE state: ' + peer.iceConnectionState);
        emitState();
      });

      const offer = await peer.createOffer();
      if (attempt !== current) return;
      await peer.setLocalDescription(offer);
      if (attempt !== current) return;
      await waitForIce(current, 100);
      if (attempt !== current) return;

      log('Sending WHEP offer to ' + current.url);
      const response = await fetch(current.url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/sdp' },
        body: peer.localDescription.sdp,
        signal: current.controller.signal,
      });
      if (response.ok) {
        const location = response.headers.get('Location');
        if (location) current.resourceUrl = new URL(location, current.url).href;
      }
      if (attempt !== current) {
        releaseResource(current);
        return;
      }
      if (!response.ok) {
        const body = await response.text();
        if (attempt !== current) return;
        log('WHEP error ' + response.status + ': ' + body);
        scheduleReconnect(current);
        return;
      }

      const answerSdp = await response.text();
      if (attempt !== current) return;
      await peer.setRemoteDescription({ type: 'answer', sdp: answerSdp });
      if (attempt !== current) return;
      reconnectDelay = 100;
      retrying = false;
      log('WHEP session established');
      emitState();
    } catch (err) {
      if (attempt !== current) return;
      log('WHEP negotiation failed: ' + err.message);
      scheduleReconnect(current);
    }
  }

  // ── reconnect ──────────────────────────────────────────────────────────

  function scheduleReconnect(current) {
    if (attempt !== current || reconnectTimer) return;
    retrying = true;
    const delay = reconnectDelay;
    reconnectDelay = Math.min(reconnectDelay * 1.5, 8000);
    log('Reconnecting in ' + (delay / 1000).toFixed(1) + 's...');
    emitState();
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      if (attempt === current) connect();
    }, delay);
  }

  // ── cleanup ────────────────────────────────────────────────────────────

  function cleanup(closeDelayedContext = true) {
    const previous = attempt;
    attempt = null;
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
    if (previous) {
      previous.controller.abort();
      if (previous.cancelIce) previous.cancelIce();
      releaseResource(previous);
      if (previous.peer) previous.peer.close();
    }
    if (_audioEl) {
      if (!closeDelayedContext && _audioEl.srcObject) playOnTrack = !_audioEl.paused;
      _audioEl.srcObject = null;
    }
    currentStream = null;
    cleanupDelayedGraph(closeDelayedContext);
  }

  // ── public API ─────────────────────────────────────────────────────────

  return {
    /**
     * @param {object} opts
     * @param {string}          opts.whepUrl  – full WHEP endpoint URL
     * @param {HTMLAudioElement} opts.audioEl  – target audio element
     * @param {number}           opts.audioDelayMs – optional listener-side delay
     * @param {function}        opts.onState  – called with state object on changes
     * @param {function}        opts.onLog    – called with log string
     */
    start(opts) {
      cleanup();
      reconnectDelay = 100;
      playOnTrack = true;
      _whepUrl = opts.whepUrl;
      _audioEl = opts.audioEl;
      _audioDelayMs = Math.max(0, parseInt(opts.audioDelayMs || 0, 10) || 0);
      _onState = opts.onState || (() => {});
      _onLog   = opts.onLog   || (() => {});
      retrying = false;
      if (_audioDelayMs > 0) {
        prepareDelayedContext();
      }
      connect();
    },

    setAudioDelayMs(audioDelayMs) {
      const nextDelayMs = Math.max(0, parseInt(audioDelayMs || 0, 10) || 0);
      if (nextDelayMs === _audioDelayMs) return;
      _audioDelayMs = nextDelayMs;
      if (currentStream) {
        const autoplay = !_audioEl.paused;
        if (_audioDelayMs > 0) {
          attachDelayedStream(currentStream, autoplay);
        } else {
          attachDirectStream(currentStream, autoplay);
        }
      }
      emitState();
    },

    /** Tear down the session. */
    stop() {
      cleanup();
      retrying = false;
      emitState();
    },
  };
}

const WhepListener = createWhepClient();
