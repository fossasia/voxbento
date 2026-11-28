// What a listener on an interpreter channel hears (#1397): the interpretation while
// the booth is on air, otherwise the room floor. Only used when the server sent a
// floor_fallback for the channel; without one the listener page keeps waiting in silence.
//
// A classic script exposing one global, like whep-listener.js: a module would need CORS,
// which breaks embeds in sandboxed (opaque-origin) iframes.
'use strict';

const ChannelSource = {
  // An interpreter handover drops the stream for a moment. Waiting this long before
  // falling back keeps listeners from bouncing to the floor and back.
  HANDOVER_GUARD_MS: 3000,
  // The new stream plays muted until it reports audio, or until this much time (plus the
  // listener delay) has passed.
  SWITCH_TIMEOUT_MS: 5000,

  /**
   * Which source to play. Returns `current` (null = nothing yet) when nothing should change.
   * @param {{current: 'interpretation'|'floor'|null, onAir: boolean, offAirForMs: number}} input
   * @returns {'interpretation'|'floor'|null}
   */
  chooseSource({ current, onAir, offAirForMs }) {
    if (onAir) return 'interpretation';
    if (offAirForMs >= ChannelSource.HANDOVER_GUARD_MS) return 'floor';
    return current;
  },

  /**
   * Plays one of two WHEP streams through `audioEl`, switching make-before-break: the
   * incoming stream starts on a muted standby element and only takes over the player once
   * it has audio (or after SWITCH_TIMEOUT_MS), so two streams are never audible at once. With a
   * listener delay the incoming stream first plays for that long, so its delay line is full when
   * it takes over and the switch adds no silence.
   *
   * @param {object} opts
   * @param {string} opts.interpretationUrl – the booth's WHEP URL
   * @param {string|null} opts.floorUrl – the room floor's WHEP URL; null when the floor has no
   *   audio, so "floor" means nothing plays
   * @param {function} [opts.onSilent] – called when the player stops for a floor without audio
   * @param {HTMLAudioElement} opts.audioEl – the visible player the listener controls
   * @param {HTMLAudioElement} opts.standbyEl – an element the incoming stream warms up on
   * @param {number} opts.audioDelayMs
   * @param {function} opts.createClient – returns a WHEP client (whep-listener.js)
   * @param {function} opts.onState – (whepState, kind) for the stream in the player
   * @param {function} [opts.now]
   * @param {boolean} [opts.startOnFallback] – play the floor at once instead of after the guard,
   *   for the original channel, where the floor is the default and the "interpretation" is a
   *   two-way booth interpreting back into the spoken language
   */
  create(opts) {
    const now = opts.now || Date.now;
    const audioEl = opts.audioEl;
    const standbyEl = opts.standbyEl;
    standbyEl.muted = true;
    let audioDelayMs = opts.audioDelayMs;
    let onAir = false;
    // Nothing has said the booth is on air yet, so the guard runs from the start.
    let offAirSince = opts.startOnFallback ? now() - ChannelSource.HANDOVER_GUARD_MS : now();
    let active = null;
    let incoming = null;
    let guardTimer = null;
    let stopped = false;
    // Stands in for the floor's client when the floor has no audio.
    const silent = { stop() {}, setAudioDelayMs() {}, resume() {} };

    function wanted() {
      if (incoming) return incoming.kind;
      return active ? active.kind : null;
    }

    function startSlot(kind, element) {
      const slot = { kind, client: opts.createClient(), timer: null, filling: false };
      slot.client.start({
        whepUrl: kind === 'interpretation' ? opts.interpretationUrl : opts.floorUrl,
        audioEl: element,
        audioDelayMs,
        onState: (state) => onSlotState(slot, state),
      });
      return slot;
    }

    function onSlotState(slot, state) {
      if (slot === incoming) {
        if (state.peerConnection === 'connected' && state.audioActive && !slot.filling) {
          slot.filling = true;
          clearTimeout(slot.timer);
          if (audioDelayMs > 0) slot.timer = setTimeout(() => promote(slot), audioDelayMs);
          else promote(slot);
        }
      } else if (slot === active) {
        opts.onState(state, slot.kind);
      }
    }

    function promote(slot) {
      clearTimeout(slot.timer);
      const previous = active;
      // A pause the listener chose carries over. A player with nothing loaded counts as playing.
      const keepPaused = Boolean(audioEl.srcObject) && audioEl.paused;
      incoming = null;
      active = slot;
      previous.client.stop();
      slot.client.setAudioElement(audioEl, !keepPaused);
    }

    function dropIncoming() {
      if (!incoming) return;
      clearTimeout(incoming.timer);
      incoming.client.stop();
      incoming = null;
    }

    function switchTo(kind) {
      dropIncoming();
      if (active && active.kind === kind) return;
      if (kind === 'floor' && !opts.floorUrl) {
        // Nothing to fall back to: stop at once rather than keep a stream that has no audio.
        const previous = active;
        active = { kind, client: silent, timer: null };
        if (previous) previous.client.stop();
        if (opts.onSilent) opts.onSilent();
        return;
      }
      if (!active || active.client === silent) {
        active = startSlot(kind, audioEl);
        return;
      }
      const slot = startSlot(kind, standbyEl);
      incoming = slot;
      slot.timer = setTimeout(() => promote(slot), ChannelSource.SWITCH_TIMEOUT_MS + audioDelayMs);
    }

    function evaluate() {
      clearTimeout(guardTimer);
      guardTimer = null;
      const offAirForMs = onAir ? 0 : now() - offAirSince;
      const target = ChannelSource.chooseSource({ current: wanted(), onAir, offAirForMs });
      if (target && target !== wanted()) switchTo(target);
      if (!onAir && target !== 'floor') {
        guardTimer = setTimeout(evaluate, ChannelSource.HANDOVER_GUARD_MS - offAirForMs);
      }
    }

    evaluate();

    return {
      /** @param {boolean} value – the booth's listener-safe `on_air` */
      setOnAir(value) {
        if (stopped) return;
        const next = value === true;
        if (onAir && !next) offAirSince = now();
        onAir = next;
        evaluate();
      },

      /** The stream in the player: 'interpretation', 'floor', or null while nothing plays. */
      source() {
        return active && active.client !== silent ? active.kind : null;
      },

      setAudioDelayMs(ms) {
        audioDelayMs = ms;
        if (active) active.client.setAudioDelayMs(ms);
        if (incoming) incoming.client.setAudioDelayMs(ms);
      },

      resume() {
        if (active) active.client.resume();
      },

      /** Keeps the stream in the player and stops switching, for when the booth status can no longer arrive. */
      hold() {
        stopped = true;
        clearTimeout(guardTimer);
        guardTimer = null;
        dropIncoming();
      },

      stop() {
        stopped = true;
        clearTimeout(guardTimer);
        guardTimer = null;
        dropIncoming();
        if (active) {
          const previous = active;
          active = null;
          previous.client.stop();
        }
      },
    };
  },
};
