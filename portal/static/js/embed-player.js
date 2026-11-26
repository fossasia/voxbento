'use strict';

(function() {
  document.addEventListener('DOMContentLoaded', function() {
    const configEl = document.getElementById('embed-config');
    if (!configEl) {
      console.error('Embed configuration not found.');
      return;
    }
    
    let config;
    try {
      config = JSON.parse(configEl.textContent);
    } catch (e) {
      console.error('Failed to parse embed configuration:', e);
      return;
    }

    const WHEP_URL    = config.whep_url;
    const CAPTION_URL = config.caption_url;
    const CAPTIONS_ON = config.captions_enabled;
    const EMBED_TOKEN = config.token;
    const TARGET_LANG = config.target_lang_code;
    const SOURCE_LANG = config.source_lang_code;
    // The floor this interpreter channel plays while its booth is off air (#1397), or null.
    const FLOOR_FALLBACK = config.floor_fallback || null;

    const audioEl    = document.getElementById('embed-audio');
    const playBtn    = document.getElementById('play-btn');
    const statusDot  = document.getElementById('status-dot');
    const statusText = document.getElementById('status-text');
    const captionsEl = document.getElementById('captions-box') || null;

    let playing = false;

    function setStatus(state, text) {
      statusDot.className = 'status-dot ' + (state || '');
      statusText.textContent = text;
    }

    function setExpired() {
      setStatus('error', 'Session expired');
      playBtn.textContent = '\u26a0 Session Expired \u2014 Reload Host Page';
      playBtn.disabled = true;
    }

    // kind is 'floor' while the floor plays in place of an off-air interpreter.
    function onPlayerState(s, kind) {
      if (config.headless) {
        window.parent.postMessage({
          source: 'voxbento-embed',
          v: 1,
          type: 'booth_state',
          payload: { state: s.peerConnection }
        }, config.target_origin);
      }

      if (s.peerConnection === 'connected') {
        setStatus('live', kind === 'floor' ? 'Original audio' : 'Live');
        playBtn.disabled = false;
        playBtn.textContent = playing ? '\u23f8  Pause' : '\u25b6  Play Audio';
      } else if (s.peerConnection === 'failed' || s.peerConnection === 'closed') {
        setStatus('error', 'Stream unavailable');
        playBtn.disabled = false;
      } else {
        setStatus('connecting', 'Connecting\u2026');
      }
    }

    let channelSource = null;
    if (FLOOR_FALLBACK) {
      channelSource = ChannelSource.create({
        interpretationUrl: WHEP_URL,
        floorUrl: FLOOR_FALLBACK.whep_url,
        audioEl: audioEl,
        standbyEl: document.createElement('audio'),
        audioDelayMs: config.audio_delay_ms,
        createClient: createWhepClient,
        onState: onPlayerState,
      });
      setStatus('connecting', 'Waiting for the interpreter\u2026');
    } else {
      createWhepClient().start({
        whepUrl: WHEP_URL,
        audioEl: audioEl,
        audioDelayMs: config.audio_delay_ms,
        onState: function(s) { onPlayerState(s); },
        onLog: function() {},
      });
    }

    function stopChannelSource() {
      if (!channelSource) return;
      channelSource.stop();
      channelSource = null;
    }

    playBtn.addEventListener('click', function() {
      if (!playing) {
        audioEl.play().then(function() {
          playing = true;
          playBtn.textContent = '\u23f8  Pause';
        }).catch(function() {
          setStatus('error', 'Playback blocked \u2014 check browser permissions');
        });
      } else {
        audioEl.pause();
        playing = false;
        playBtn.textContent = '\u25b6  Play Audio';
      }
    });

    const showCaptions = Boolean(CAPTIONS_ON && captionsEl);
    // The caption socket also carries the booth's on_air status, so a fallback needs it even with captions off.
    if (showCaptions || channelSource) {
      var wsProto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
      var urlPath = new URL(CAPTION_URL).pathname;
      var wsUrl = wsProto + '//' + window.location.host + urlPath + '?token=' + encodeURIComponent(EMBED_TOKEN);
      var captionWs = null;
      var captionRetryDelayMs = 1000;

      var historyEl = document.getElementById('embed-caption-history');
      var currentEl = document.getElementById('embed-caption-current');
      var maxHistoryDOM = 50;
      // How close to the bottom (in px) still counts as "following" the captions.
      var FOLLOW_THRESHOLD_PX = 40;
      
      var captionHistoryHeadless = [];
      var maxHistoryHeadless = 2;

      function handleCaptionMessage(ev) {
        try {
          var msg = JSON.parse(ev.data);
          if (msg.type === 'booth:state') {
            if (channelSource) channelSource.setOnAir(Boolean(msg.state) && msg.state.on_air === true);
            return;
          }
          if (!showCaptions) return;
          var isValid = false;
          if (config.headless) {
            // Headless: Forward both the original captions and the requested translations
            if (msg.type === 'caption') isValid = true;
            if (msg.type === 'translation' && msg.language_code === TARGET_LANG) isValid = true;
          } else {
            // Visible UI: Only show one language track to prevent overlapping text
            const isTranslating = TARGET_LANG && TARGET_LANG !== SOURCE_LANG;
            if (isTranslating) {
              if (msg.type === 'translation' && msg.language_code === TARGET_LANG) isValid = true;
            } else {
              if (msg.type === 'caption') isValid = true;
            }
          }
          
          if (isValid) {
            var status = msg.type === 'translation' ? 'final' : (msg.status || 'final');
            var text = (msg.text || '').trim();

            if (config.headless) {
              // Headless: maintain a short array buffer and send concatenated string
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
              var displayText = captionHistoryHeadless.concat(text ? [text] : []).join(' ');

              window.parent.postMessage({
                source: 'voxbento-embed',
                v: 1,
                type: 'subtitle',
                payload: { 
                  text: displayText, 
                  language: msg.language_code || SOURCE_LANG,
                  msg_type: msg.type,
                  raw_status: status,
                  raw_text: msg.text
                }
              }, config.target_origin);
            } else {
              // Visible UI: build a DOM history like Voxbento's main listener UI.
              // Follow new captions only while the viewer is already at the bottom,
              // so scrolling up to re-read a line is not undone by the next partial.
              var followLatest =
                captionsEl.scrollHeight - captionsEl.scrollTop - captionsEl.clientHeight <= FOLLOW_THRESHOLD_PX;
              if (status === 'clear') {
                currentEl.textContent = '';
              } else if (status === 'final') {
                if (text) {
                  var p = document.createElement('div');
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
          }
        } catch (_) {}
      }

      function openCaptionWs() {
        var ws = new WebSocket(wsUrl);
        captionWs = ws;
        ws.onopen = function() {
          captionRetryDelayMs = 1000;
        };
        ws.onmessage = handleCaptionMessage;
        ws.onclose = function(ev) {
          if (ev.code === 4001) { stopChannelSource(); setExpired(); return; }
          // 4003: the token is for another event, so reconnecting cannot help.
          if (ev.code === 4003 && channelSource) {
            stopChannelSource();
            setStatus('error', 'Stream unavailable');
          }
          if (ev.code === 4003 || captionWs !== ws) return;
          // Any other close (network drop, server restart, proxy timeout) would
          // otherwise stop captions until the host page is reloaded.
          captionWs = null;
          // Wait between half and all of the current delay, so embeds dropped by the same
          // restart do not all reconnect at the same moment.
          setTimeout(openCaptionWs, captionRetryDelayMs * (0.5 + Math.random() / 2));
          captionRetryDelayMs = Math.min(captionRetryDelayMs * 2, 15000);
        };
      }

      openCaptionWs();
    }

    if (config.headless) {
      window.addEventListener('message', function(event) {
        if (config.allowed_origins.length > 0 && !config.allowed_origins.includes(event.origin)) return;
        if (!event.data || event.data.source !== 'voxbento-parent') return;

        if (event.data.type === 'play') {
          audioEl.play().then(function() {
            playing = true;
          }).catch(function(err) {
            window.parent.postMessage({
              source: 'voxbento-embed',
              v: 1,
              type: 'error',
              payload: { code: 'autoplay_blocked', message: err.message }
            }, config.target_origin);
          });
        }
        if (event.data.type === 'pause') {
          audioEl.pause();
          playing = false;
        }
        if (event.data.type === 'set_volume') {
          const v = Number(event.data.volume);
          if (v >= 0 && v <= 1) audioEl.volume = v;
        }
      });
    }
  });
})();
