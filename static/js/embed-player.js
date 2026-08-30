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

    const whep = createWhepClient();

    whep.start({
      whepUrl: WHEP_URL,
      audioEl: audioEl,
      audioDelayMs: config.audio_delay_ms,
      onState: function(s) {
        if (config.headless) {
          window.parent.postMessage({
            source: 'voxbento-embed',
            v: 1,
            type: 'booth_state',
            payload: { state: s.peerConnection }
          }, config.target_origin);
        }

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
      onLog: function() {},
    });

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

    if (CAPTIONS_ON && captionsEl) {
      var wsProto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
      var urlPath = new URL(CAPTION_URL).pathname;
      var wsUrl = wsProto + '//' + window.location.host + urlPath + '?token=' + encodeURIComponent(EMBED_TOKEN);
      var captionWs = new WebSocket(wsUrl);

      var historyEl = document.getElementById('embed-caption-history');
      var currentEl = document.getElementById('embed-caption-current');
      var maxHistoryDOM = 50;
      
      var captionHistoryHeadless = [];
      var maxHistoryHeadless = 2;

      captionWs.onmessage = function(ev) {
        try {
          var msg = JSON.parse(ev.data);
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
              // Visible UI: build a DOM history like Voxbento's main listener UI
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
              captionsEl.scrollTop = captionsEl.scrollHeight;

            }
          }
        } catch (_) {}
      };

      captionWs.onclose = function(ev) {
        if (ev.code === 4001) { setExpired(); }
      };
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
