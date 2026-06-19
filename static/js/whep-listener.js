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

  /** @type {RTCPeerConnection|null} */
  let pc = null;
  /** @type {string|null} WHEP resource URL returned via Location header */
  let resourceUrl = null;
  /** @type {number|null} */
  let reconnectTimer = null;
  /** Exponential back-off delay (ms). */
  let reconnectDelay = 100;

  // Callbacks supplied by caller.
  let _onState = () => {};
  let _onLog   = () => {};
  let _whepUrl = '';
  let _audioEl = null;
  let _audioDelayMs = 0;
  let currentStream = null;
  let delayedAudio = {
    context: null,
    source: null,
    delay: null,
    stream: null,
    resumeHandler: null,
  };

  // ── helpers ────────────────────────────────────────────────────────────

  function log(msg) {
    _onLog(msg);
  }

  function emitState() {
    _onState({
      peerConnection: pc ? pc.connectionState : 'closed',
      ice:            pc ? pc.iceConnectionState : 'closed',
      signaling:      pc ? pc.signalingState : 'closed',
      audioActive:    _audioDelayMs > 0 ? isDelayedAudioActive() : (_audioEl ? !_audioEl.paused && _audioEl.srcObject !== null : false),
      whepUrl:        _whepUrl,
    });
  }

  function isDelayedAudioActive() {
    return (
      (delayedAudio.stream !== null && delayedAudio.context !== null && delayedAudio.context.state !== 'closed') ||
      (_audioEl ? !_audioEl.paused && _audioEl.srcObject !== null : false)
    );
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
    ctx.resume().then(removeResumeHandler).catch((err) => {
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
    delayedAudio.stream = null;
    if (closeContext && delayedAudio.context) {
      const ctx = delayedAudio.context;
      delayedAudio.context = null;
      if (ctx.state !== 'closed') {
        ctx.close().catch((err) => log('AudioContext close failed: ' + err.message));
      }
    }
  }

  function attachDirectStream(stream) {
    cleanupDelayedGraph(true);
    currentStream = stream;
    _audioEl.muted = false;
    _audioEl.srcObject = stream;
    _audioEl.play().catch((err) => log('Autoplay blocked: ' + err.message));
  }

  function attachDelayedStream(stream) {
    cleanupDelayedGraph(false);
    currentStream = stream;
    _audioEl.srcObject = stream;
    _audioEl.muted = true;
    _audioEl.play().catch((err) => log('Muted audio sink blocked: ' + err.message));
    const ctx = prepareDelayedContext();
    if (!ctx) {
      _audioEl.muted = false;
      return;
    }

    const delaySeconds = _audioDelayMs / 1000;
    delayedAudio.stream = stream;
    delayedAudio.source = ctx.createMediaStreamSource(stream);
    delayedAudio.delay = ctx.createDelay(Math.max(delaySeconds, 1));
    delayedAudio.delay.delayTime.value = delaySeconds;
    delayedAudio.source.connect(delayedAudio.delay);
    delayedAudio.delay.connect(ctx.destination);
  }

  // ── WHEP negotiation ──────────────────────────────────────────────────

  /**
   * Wait for ICE gathering to complete (or timeout).
   * Returns a promise that resolves when gathering is "complete" or after
   * the given timeout (whichever comes first).
   */
  function waitForIce(peerConn, timeoutMs) {
    return new Promise((resolve) => {
      if (peerConn.iceGatheringState === 'complete') {
        resolve();
        return;
      }
      const timer = setTimeout(resolve, timeoutMs);
      peerConn.addEventListener('icegatheringstatechange', function handler() {
        if (peerConn.iceGatheringState === 'complete') {
          clearTimeout(timer);
          peerConn.removeEventListener('icegatheringstatechange', handler);
          resolve();
        }
      });
    });
  }

  async function connect() {
    cleanup(false);

    log('Creating RTCPeerConnection...');
    pc = new RTCPeerConnection({
      iceServers: [],         // local-only, no STUN/TURN needed for dev
    });

    // We want to receive audio only.
    pc.addTransceiver('audio', { direction: 'recvonly' });

    // Attach incoming track to <audio>.
    pc.addEventListener('track', (event) => {
      log('Remote track received: kind=' + event.track.kind);
      if (event.track.kind === 'audio') {
        const stream = event.streams[0] || new MediaStream([event.track]);
        if (_audioDelayMs > 0) {
          attachDelayedStream(stream);
        } else {
          attachDirectStream(stream);
        }
      }
      emitState();
    });

    // State change listeners.
    pc.addEventListener('connectionstatechange', () => {
      log('PeerConnection state: ' + pc.connectionState);
      emitState();
      if (pc.connectionState === 'failed' || pc.connectionState === 'disconnected') {
        scheduleReconnect();
      }
    });

    pc.addEventListener('iceconnectionstatechange', () => {
      log('ICE state: ' + pc.iceConnectionState);
      emitState();
    });

    // Create offer.
    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);

    // Wait for ICE candidates (100 ms — local dev needs no STUN).
    await waitForIce(pc, 100);

    const offerSdp = pc.localDescription.sdp;
    log('Sending WHEP offer to ' + _whepUrl);

    let response;
    try {
      response = await fetch(_whepUrl, {
        method: 'POST',
        headers: { 'Content-Type': 'application/sdp' },
        body: offerSdp,
      });
    } catch (err) {
      log('WHEP fetch failed: ' + err.message);
      scheduleReconnect();
      return;
    }

    if (!response.ok) {
      const body = await response.text().catch(() => '');
      log('WHEP error ' + response.status + ': ' + body);
      scheduleReconnect();
      return;
    }

    // Save resource URL for teardown.
    const location = response.headers.get('Location');
    if (location) {
      // Location may be relative or absolute.
      resourceUrl = new URL(location, _whepUrl).href;
    }

    const answerSdp = await response.text();
    await pc.setRemoteDescription({ type: 'answer', sdp: answerSdp });

    reconnectDelay = 100;  // reset back-off on success
    log('WHEP session established');
    emitState();
  }

  // ── reconnect ──────────────────────────────────────────────────────────

  function scheduleReconnect() {
    if (reconnectTimer) return;
    const delay = reconnectDelay;
    reconnectDelay = Math.min(reconnectDelay * 1.5, 8000);
    log('Reconnecting in ' + (delay / 1000).toFixed(1) + 's...');
    emitState();
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      connect();
    }, delay);
  }

  // ── cleanup ────────────────────────────────────────────────────────────

  function cleanup(closeDelayedContext = true) {
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }

    if (resourceUrl) {
      // Best-effort DELETE to release the WHEP session on the server.
      fetch(resourceUrl, { method: 'DELETE' }).catch(() => {});
      resourceUrl = null;
    }

    if (pc) {
      pc.close();
      pc = null;
    }

    if (_audioEl) {
      _audioEl.srcObject = null;
      _audioEl.muted = false;
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
      _whepUrl = opts.whepUrl;
      _audioEl = opts.audioEl;
      _audioDelayMs = Math.max(0, parseInt(opts.audioDelayMs || 0, 10) || 0);
      _onState = opts.onState || (() => {});
      _onLog   = opts.onLog   || (() => {});
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
        if (_audioDelayMs > 0) {
          attachDelayedStream(currentStream);
        } else {
          attachDirectStream(currentStream);
        }
      }
      emitState();
    },

    /** Tear down the session. */
    stop() {
      cleanup();
      emitState();
    },
  };
}

const WhepListener = createWhepClient();
