document
  .getElementById("toggle-captions-btn")
  .addEventListener("click", function () {
    var container = document.getElementById("captions-container");
    container.classList.toggle("minimized");
    if (container.classList.contains("minimized")) {
      this.textContent = "Maximize Captions ▲";
    } else {
      this.textContent = "Minimize Captions ▼";
    }
  });

const eventDataEl = document.getElementById("listener-data");
const eventData = JSON.parse(eventDataEl.textContent);
var eventSlug = eventData.eventSlug;
var boothsData = eventData.booths;

var roomsData = eventData.rooms;

var roomSelect = document.getElementById("room-select");
var languageSelect = document.getElementById("language-select");
var captionModeSelect = document.getElementById("caption-mode-select");
var translationLangSelect = document.getElementById("translation-lang-select");

var audioEl = document.getElementById("audio-player");
var statusEl = document.getElementById("status");
var captionsBox = document.getElementById("live-captions");
var captionsWs = null;

var ttsWs = null;
var audioCtx = null;
var nextStartTime = 0;
var currentAudioDelayMs = 0;
var currentRoomId = null;
var currentSourceType = null;
/** @type {ReturnType<typeof window.AudioScheduler.create>|null} */
var audioScheduler = null;

function setStatus(text, cls) {
  statusEl.innerHTML =
    '<span class="status-badge ' + cls + '">' + text + "</span>";
}

function showAudioPlayer(show) {
  audioEl.style.display = show ? "block" : "none";
  if (show && currentAudioDelayMs === 0) {
    audioEl.setAttribute("controls", "");
  } else {
    audioEl.removeAttribute("controls");
  }
}

function onState(s) {
  if (s.peerConnection === "connected" && s.audioActive) {
    setStatus("Live (WebRTC)", "live");
    showAudioPlayer(true);
  } else if (s.peerConnection === "connecting" || s.ice === "checking") {
    setStatus("Connecting...", "recovering");
  } else if (
    s.peerConnection === "failed" ||
    s.peerConnection === "disconnected"
  ) {
    setStatus("Reconnecting...", "recovering");
  } else if (s.peerConnection === "connected") {
    setStatus("Connected — waiting for audio", "waiting");
  } else if (s.peerConnection === "closed") {
    setStatus("Disconnected", "error");
  }
}
var pendingBoothId = null; // booth currently being watched (pre-live)
var pendingWhepUrl = null; // WHEP URL to start once live
var pendingAudioDelayMs = 0;
var pendingTtsLang = null; // TTS language to start once live
var pendingRoomId = null;
var segmentStore = Object.create(null);
var expectedSeq = 1;
var isSegmentPlaying = false;
var fallbackQueueTimer = null;
var seqWaitTimer = null;

function normalizeAudioDelayMs(value) {
  var delayMs = parseInt(value || 0, 10);
  if (!Number.isFinite(delayMs) || delayMs < 0) return 0;
  return Math.min(delayMs, 10000);
}

function stopCurrentStream() {
  WhepListener.stop();
  stopTtsWs();
  pendingBoothId = null;
  pendingWhepUrl = null;
  pendingAudioDelayMs = 0;
  currentAudioDelayMs = 0;
  pendingTtsLang = null;
  pendingRoomId = null;
  currentRoomId = null;
  currentSourceType = null;

  // Clear all queued segments and timers
  if (seqWaitTimer) {
    clearTimeout(seqWaitTimer);
    seqWaitTimer = null;
  }
  if (fallbackQueueTimer) {
    clearTimeout(fallbackQueueTimer);
    fallbackQueueTimer = null;
  }
  segmentStore = Object.create(null);
  expectedSeq = 1;
  isSegmentPlaying = false;
  if (audioScheduler) {
    audioScheduler.reset();
    audioScheduler = null;
  }

  if (captionsWs) {
    captionsWs.close();
    captionsWs = null;
  }
  showAudioPlayer(false);
  audioEl.muted = false;
  document.getElementById("caption-history").innerHTML = "";
  document.getElementById("caption-current").innerHTML = "";

  setStatus("Waiting for selection...", "waiting");
}

function startTtsWs(roomId, langCode, boothId, audioDelayMs) {
  stopTtsWs();
  if (!audioCtx) {
    audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  }
  if (audioCtx.state === "suspended") {
    audioCtx.resume();
  }

  // Create jitter-buffered audio scheduler for gapless playback
  if (audioScheduler) {
    audioScheduler.reset();
  }
  audioScheduler = window.AudioScheduler.create(audioCtx, {
    jitterBufferSec: 0.25,
    comfortNoiseEnabled: false,
    comfortNoiseLevelDb: -40,
  });

  var wsProto = window.location.protocol === "https:" ? "wss:" : "ws:";
  var wsUrl =
    wsProto +
    "//" +
    window.location.host +
    "/ws/tts/" +
    roomId +
    "/" +
    langCode +
    "/" +
    boothId;
  console.log("Connecting TTS WS to:", wsUrl);
  ttsWs = new WebSocket(wsUrl);
  ttsWs.binaryType = "arraybuffer";

  ttsWs.onmessage = function (event) {
    if (event.data instanceof ArrayBuffer) {
      try {
        var frame = window.TTSParser.parseFrame(event.data);
        var seq = frame.header.seq;
        if (!seq) return;

        // Align sequence counter to the stream if it was reset (e.g. after language switch)
        if (expectedSeq === null || expectedSeq === undefined) {
          expectedSeq = seq;
        }

        segmentStore[seq] = {
          seq: seq,
          caption: frame.header.caption || "",
          translation: frame.header.translation || "",
          error: frame.header.error || null,
          audioBuffer: null,
        };

        if (
          !frame.header.error &&
          frame.audioBytes &&
          frame.audioBytes.byteLength > 0
        ) {
          // Slice the buffer to ensure it is byte-aligned for Int16Array
          var alignedBuffer = frame.audioBytes.buffer.slice(
            frame.audioBytes.byteOffset,
            frame.audioBytes.byteOffset + frame.audioBytes.byteLength,
          );
          var int16 = new Int16Array(alignedBuffer);
          var float32 = new Float32Array(int16.length);
          for (var i = 0; i < int16.length; i++) {
            float32[i] = int16[i] / 32768.0;
          }

          var audioBuffer = audioCtx.createBuffer(1, float32.length, 24000);
          audioBuffer.copyToChannel(float32, 0);

          segmentStore[seq].audioBuffer = audioBuffer;
        }

        // Overflow protection: max depth 5
        var storedSeqs = Object.keys(segmentStore)
          .map(Number)
          .sort((a, b) => a - b);
        if (storedSeqs.length > 5) {
          console.warn(
            "Buffer overflow. Evicting pending sequences up to",
            storedSeqs[0],
          );
          expectedSeq = storedSeqs[0]; // Force flush to the oldest pending
        }

        pumpSegmentQueue();
      } catch (e) {
        console.error("TTS parsing failed", e);
      }
    }
  };

  ttsWs.onclose = function () {
    console.log("TTS WS closed");
  };
}

function stopTtsWs() {
  if (ttsWs) {
    ttsWs.close();
    ttsWs = null;
  }
}

function startWhepStream(whepUrl, audioDelayMs) {
  currentAudioDelayMs = normalizeAudioDelayMs(audioDelayMs);
  WhepListener.start({
    whepUrl: whepUrl,
    audioEl: audioEl,
    audioDelayMs: currentAudioDelayMs,
    onState: onState,
  });
}

function fetchRoomAudioDelay(roomId) {
  return fetch("/listener/" + eventSlug + "/rooms/" + roomId + "/audio-delay")
    .then(function (resp) {
      if (!resp.ok)
        throw new Error("Failed to fetch room audio delay: " + resp.status);
      return resp.json();
    })
    .then(function (data) {
      var delayMs = normalizeAudioDelayMs(data.audio_delay_ms);
      updateRoomDelayData(roomId, delayMs);
      return delayMs;
    });
}

function applyCurrentRoomAudioDelay() {
  if (!currentRoomId) return;
  fetchRoomAudioDelay(currentRoomId)
    .then(function (delayMs) {
      if (delayMs === currentAudioDelayMs) return;
      currentAudioDelayMs = delayMs;
      if (currentSourceType === "whep" && WhepListener.setAudioDelayMs) {
        WhepListener.setAudioDelayMs(delayMs);
      }
    })
    .catch(function (err) {
      console.warn("Could not refresh room audio delay", err);
    });
}

function startWhepAndCaptions(whepUrl, boothId, audioDelayMs) {
  setStatus("Connecting...", "recovering");
  startWhepStream(whepUrl, audioDelayMs);
  openCaptionsWs(boothId);
}

function openCaptionsWs(boothId) {
  if (!boothId) return;
  var wsProto = window.location.protocol === "https:" ? "wss:" : "ws:";
  captionsWs = new WebSocket(
    wsProto + "//" + window.location.host + "/ws/captions/" + boothId,
  );
  captionsWs.onmessage = handleCaptionsMessage;
}

function renderItem(data) {
  var historyBox = document.getElementById("caption-history");
  var currentBox = document.getElementById("caption-current");
  var captionsBox = document.getElementById("live-captions");
  var mode = captionModeSelect ? captionModeSelect.value : "original";

  if (data.type === "caption") {
    if (data.status === "partial" || data.status === "final") {
      if (data.text) {
        var existingBlock = data.segment_id
          ? document.getElementById("segment-" + data.segment_id)
          : null;
        var srcLangOpt = languageSelect.options[languageSelect.selectedIndex];
        var srcLangName = srcLangOpt ? srcLangOpt.text : "Source";

        if (!existingBlock) {
          var block = document.createElement("div");
          block.className =
            "mb-4 pb-4 border-b border-gray-200 last:border-b-0 w-full flex flex-col gap-1";
          if (data.segment_id) {
            block.id = "segment-" + data.segment_id;
          }

          var srcDiv = document.createElement("div");
          srcDiv.id = "src-" + data.segment_id;
          srcDiv.className = "text-[#0d0f10] caption-src-text";
          srcDiv.innerHTML =
            '<span class="font-extrabold lang-label"></span> : <span class="untrusted-text"></span>';
          srcDiv.querySelector(".lang-label").textContent =
            "[" + srcLangName + "]";
          srcDiv.querySelector(".untrusted-text").textContent = data.text;

          if (mode === "translated") {
            srcDiv.style.display = "none";
          }

          block.appendChild(srcDiv);
          historyBox.appendChild(block);
          while (historyBox.children.length > 50)
            historyBox.removeChild(historyBox.firstChild);
        } else {
          var srcDiv = existingBlock.querySelector(".caption-src-text");
          if (srcDiv) {
            srcDiv.innerHTML =
              '<span class="font-extrabold lang-label"></span> : <span class="untrusted-text"></span>';
            srcDiv.querySelector(".lang-label").textContent =
              "[" + srcLangName + "]";
            srcDiv.querySelector(".untrusted-text").textContent = data.text;
          }
        }
      }
      if (data.status === "final") {
        currentBox.textContent = "";
      }
    } else if (data.status === "clear") {
      currentBox.textContent = "";
    }
    captionsBox.scrollTop = captionsBox.scrollHeight;
  } else if (data.type === "translation") {
    if (data.language_code === translationLangSelect.value) {
      var tgtLangOpt =
        translationLangSelect.options[translationLangSelect.selectedIndex];
      var tgtLangName = tgtLangOpt ? tgtLangOpt.text : "Translation";

      var tDiv = document.createElement("div");
      tDiv.className = "text-brand-600 translation-text caption-tgt-text";
      tDiv.innerHTML =
        '<span class="font-extrabold lang-label"></span> : <span class="untrusted-text"></span>';
      tDiv.querySelector(".lang-label").textContent = "[" + tgtLangName + "]";
      tDiv.querySelector(".untrusted-text").textContent = data.text;

      if (mode === "original") {
        tDiv.style.display = "none";
      }

      var block = data.segment_id
        ? document.getElementById("segment-" + data.segment_id)
        : null;
      if (block) {
        var existingTDiv = block.querySelector(".translation-text");
        if (existingTDiv) {
          existingTDiv.innerHTML =
            '<span class="font-extrabold lang-label"></span> : <span class="untrusted-text"></span>';
          existingTDiv.querySelector(".lang-label").textContent =
            "[" + tgtLangName + "]";
          existingTDiv.querySelector(".untrusted-text").textContent = data.text;
        } else {
          block.appendChild(tDiv);
        }
      } else {
        // Fallback if we missed the original caption
        block = document.createElement("div");
        block.className =
          "mb-4 pb-4 border-b border-gray-200 last:border-b-0 w-full flex flex-col gap-1";
        block.appendChild(tDiv);
        historyBox.appendChild(block);
        while (historyBox.children.length > 50)
          historyBox.removeChild(historyBox.firstChild);
      }
      captionsBox.scrollTop = captionsBox.scrollHeight;
    }
  }
}

function pumpSegmentQueue() {
  if (isSegmentPlaying) return;

  var nextSeg = segmentStore[expectedSeq];
  if (!nextSeg) {
    // It hasn't arrived yet. Set the 10s last-resort timeout if not already set.
    if (!seqWaitTimer) {
      seqWaitTimer = setTimeout(function () {
        console.warn(
          "Last-resort timeout hit for seq",
          expectedSeq,
          "- forcefully advancing buffer",
        );
        expectedSeq++;
        seqWaitTimer = null;
        pumpSegmentQueue();
      }, 10000);
    }
    return;
  }

  // It has arrived! Clear the wait timer.
  if (seqWaitTimer) {
    clearTimeout(seqWaitTimer);
    seqWaitTimer = null;
  }

  isSegmentPlaying = true;

  // 1. Render Caption (Original)
  if (nextSeg.caption) {
    renderItem({
      type: "caption",
      status: "final",
      text: nextSeg.caption,
      segment_id: "seq-" + expectedSeq,
    });
  }

  // 2. Render Translation
  var mode = captionModeSelect ? captionModeSelect.value : "original";
  var isTranslationActive =
    Boolean(translationLangSelect.value) && mode !== "original";
  if (isTranslationActive) {
    var tText = nextSeg.translation;
    if (!tText && nextSeg.error === "pipeline_failed") {
      tText = "(Translation failed)";
    } else if (!tText && nextSeg.error === "model_downloading") {
      tText = "(Downloading translation model...)";
    } else if (!tText) {
      tText = "(Translation pending...)";
    }
    if (tText) {
      var fakeData = {
        type: "translation",
        language_code: translationLangSelect.value,
        text: tText,
        segment_id: "seq-" + expectedSeq,
      };
      renderItem(fakeData);
    }
  }

  // 3. Play audio or timeout
  var isTtsActive =
    ttsSyncEnabled &&
    Boolean(translationLangSelect.value) &&
    mode !== "original";

  if (isTtsActive && !nextSeg.error && audioCtx) {
    if (nextSeg.audioBuffer) {
      try {
        // Use the AudioScheduler for jitter-buffered, gapless playback.
        // The scheduler queues each buffer to start exactly when the previous
        // one ends, with a small jitter buffer when re-anchoring after a gap.
        // This eliminates the silence gaps between sentences.
        if (audioScheduler) {
          var timing = audioScheduler.scheduleBuffer(nextSeg.audioBuffer);
          // Advance sequence immediately so the next segment can be scheduled
          // in parallel — don't wait for onended.
          var finishedSeq = expectedSeq;
          isSegmentPlaying = false;
          delete segmentStore[finishedSeq];
          expectedSeq++;
          // Pump the queue again after a short delay to pick up any
          // already-buffered segments.
          fallbackQueueTimer = setTimeout(pumpSegmentQueue, 30);
        } else {
          // Fallback: instant playback if scheduler unavailable
          var source = audioCtx.createBufferSource();
          source.buffer = nextSeg.audioBuffer;
          source.connect(audioCtx.destination);
          source.onended = function () {
            isSegmentPlaying = false;
            delete segmentStore[expectedSeq];
            expectedSeq++;
            fallbackQueueTimer = setTimeout(pumpSegmentQueue, 60);
          };
          source.start(0);
        }
      } catch (e) {
        console.error("Audio playback error:", e);
        finishSegmentWithDelay(nextSeg, expectedSeq);
      }
    } else {
      // Waiting for Stage 2 (Audio Ready).
      // We have already rendered the text in Steps 1 & 2.
      // Pause the queue and wait for the audio bundle to trigger pumpSegmentQueue again.
      isSegmentPlaying = false;
      return;
    }
  } else {
    // No audio (or error or translation-only), use reading delay
    finishSegmentWithDelay(nextSeg, expectedSeq);
  }
}

function finishSegmentWithDelay(seg, seqId) {
  var mode = captionModeSelect ? captionModeSelect.value : "original";
  var isTranslationActive =
    Boolean(translationLangSelect.value) && mode !== "original";

  if (!isTranslationActive) {
    // No translation, no TTS: advance immediately (it shouldn't be here in Original mode, but just in case)
    isSegmentPlaying = false;
    delete segmentStore[seqId];
    expectedSeq++;
    pumpSegmentQueue();
    return;
  }

  var text = seg.translation || seg.caption || "";
  var wordCount = text.trim().split(/\s+/).filter(Boolean).length;
  var readingDelayMs = Math.max(2500, wordCount * 350);
  fallbackQueueTimer = setTimeout(function () {
    isSegmentPlaying = false;
    delete segmentStore[seqId];
    expectedSeq++;
    pumpSegmentQueue();
  }, readingDelayMs);
}

function handleCaptionsMessage(event) {
  try {
    var data = JSON.parse(event.data);
    var waitingText = document.getElementById("waiting-text");

    if (data.type === "booth:state") {
      var bstate = data.state || {};
      // If we were waiting for the broadcast to go live, start WHEP or TTS now.
      if (bstate.ingest_status === "connected") {
        if (pendingWhepUrl) {
          var url = pendingWhepUrl;
          var delayMs = pendingAudioDelayMs;
          pendingWhepUrl = null;
          pendingAudioDelayMs = 0;
          pendingBoothId = null;
          startWhepStream(url, delayMs);
        } else if (pendingTtsLang) {
          var plang = pendingTtsLang;
          var prid = pendingRoomId;
          var ttsDelayMs = pendingAudioDelayMs;
          var pBoothId = pendingBoothId;
          pendingTtsLang = null;
          pendingAudioDelayMs = 0;
          pendingRoomId = null;
          pendingBoothId = null;
          startTtsWs(prid, plang, pBoothId, ttsDelayMs);
          setStatus("Live (TTS Audio)", "live");
        }
      }
      // If broadcast was locked, tear down (floor booths are never locked this way).
      if (bstate.broadcast_unlocked === false) {
        WhepListener.stop();
        showAudioPlayer(false);
        setStatus("Broadcast is currently locked by the organizers.", "error");
        roomSelect.value = "";
        languageSelect.innerHTML =
          '<option value="">-- Select Language --</option>';
        languageSelect.disabled = true;
        captionModeSelect.disabled = true;
        captionModeSelect.value = "original";
        translationLangSelect.innerHTML = "";
        var transGroup = document.getElementById("translation-lang-group");
        if (transGroup) transGroup.style.display = "none";
        applyCaptionMode();
      }
      return;
    }

    if (data.type === "caption") {
      if (waitingText) waitingText.style.display = "none";

      if (data.status === "partial") {
        var sid = data.segment_id;
        if (sid) {
          renderItem({
            type: "caption",
            status: "partial",
            text: data.text,
            segment_id: sid,
          });
        }
      } else if (data.status === "final") {
        var mode = captionModeSelect ? captionModeSelect.value : "original";
        var isTranslationActive =
          Boolean(translationLangSelect.value) && mode !== "original";

        var sid = data.seq
          ? "seq-" + data.seq
          : data.segment_id || "legacy-" + Date.now();
        if (!segmentStore[sid]) {
          segmentStore[sid] = { id: sid };
        }
        segmentStore[sid].caption = data.text;

        // Seamlessly rename the partial block to the seq ID so pumpSegmentQueue can target it
        if (data.segment_id && data.seq) {
          var partialBlock = document.getElementById(
            "segment-" + data.segment_id,
          );
          if (partialBlock) {
            partialBlock.id = "segment-" + sid;
          }
        }

        // Immediately render for original mode to prevent UI flicker
        renderItem({
          type: "caption",
          status: "final",
          text: data.text,
          segment_id: sid,
        });
      } else if (data.status === "clear") {
        var currentBox = document.getElementById("caption-current");
        if (currentBox) currentBox.textContent = "";
      }
    } else if (data.type === "translation") {
      // Legacy support: translations should now come via the atomic bundle on ttsWs.
      // This block is preserved just in case some legacy clients still send it,
      // but under the new architecture it shouldn't be hit for floor audio.
      var sid = data.segment_id;
      renderItem(data);
    } else if (data.type === "ping") {
      if (captionsWs && captionsWs.readyState === WebSocket.OPEN) {
        captionsWs.send("pong");
      }
    }
  } catch (e) {
    console.error(e);
  }
}

// Function to apply visibility of text blocks based on caption mode
function applyCaptionMode() {
  var mode = captionModeSelect.value;
  var historyBox = document.getElementById("caption-history");
  var currentBox = document.getElementById("caption-current");

  // Process current box
  if (mode === "translated") {
    currentBox.style.display = "none";
  } else {
    currentBox.style.display = "block";
  }

  // Process history blocks
  var blocks = historyBox.children;
  for (var i = 0; i < blocks.length; i++) {
    var block = blocks[i];
    var srcDiv =
      block.querySelector(".caption-src-text") ||
      block.querySelector(".text-\\[\\#0d0f10\\]");
    var tgtDiv =
      block.querySelector(".caption-tgt-text") ||
      block.querySelector(".translation-text");

    if (srcDiv) {
      srcDiv.style.display = mode === "translated" ? "none" : "block";
    }
    if (tgtDiv) {
      tgtDiv.style.display = mode === "original" ? "none" : "block";
    }

    // Hide the whole block if it becomes empty
    var hasVisibleSrc = srcDiv && srcDiv.style.display !== "none";
    var hasVisibleTgt = tgtDiv && tgtDiv.style.display !== "none";
    if (!hasVisibleSrc && !hasVisibleTgt) {
      block.style.display = "none";
    } else {
      block.style.display = "flex";
    }
  }
}

captionModeSelect.addEventListener("change", applyCaptionMode);

roomSelect.addEventListener("change", function () {
  var roomId = parseInt(this.value, 10);
  languageSelect.innerHTML =
    '<option value="">-- Select Source Audio --</option>';

  stopCurrentStream();

  if (!roomId) {
    languageSelect.disabled = true;
    captionModeSelect.disabled = true;
    captionModeSelect.value = "original";
    applyCaptionMode();
    var transGroup = document.getElementById("translation-lang-group");
    if (transGroup) transGroup.style.display = "none";
    return;
  }

  languageSelect.disabled = false;
  var roomBooths = boothsData.filter(function (b) {
    return b.room_id === roomId;
  });

  roomBooths.forEach(function (b) {
    var opt = document.createElement("option");
    opt.value = b.whep_url;
    opt.textContent = b.language_name;
    opt.dataset.type = "whep";
    if (b.language_code === "floor") {
      opt.dataset.boothId = eventSlug + "-" + roomId + "-floor";
      opt.dataset.languageCode = "floor";
    } else {
      opt.dataset.boothId = eventSlug + "-" + roomId + "-" + b.language_code;
      opt.dataset.languageCode = b.language_code;
    }
    opt.dataset.boothObjId = b.id;
    languageSelect.appendChild(opt);
  });

  // Reset translation dropdown until language is selected
  captionModeSelect.disabled = true;
  captionModeSelect.value = "original";
  translationLangSelect.innerHTML = "";
  applyCaptionMode();
  var transGroup = document.getElementById("translation-lang-group");
  if (transGroup) transGroup.style.display = "none";
});

var ttsSyncEnabled = false;
var ttsSyncToggle = document.getElementById("tts-sync-toggle");
if (ttsSyncToggle) {
  ttsSyncToggle.addEventListener("change", function () {
    ttsSyncEnabled = this.checked;
    if (languageSelect.value) {
      applyTtsOverlay();
    }
  });
}

translationLangSelect.addEventListener("change", function () {
  if (this.value) {
    if (captionModeSelect.value === "original") {
      captionModeSelect.value = "stacked";
    }
  } else {
    captionModeSelect.value = "original";
  }
  applyCaptionMode();

  if (fallbackQueueTimer) {
    clearTimeout(fallbackQueueTimer);
    fallbackQueueTimer = null;
  }
  if (isSegmentPlaying) {
    isSegmentPlaying = false;
  }

  // Completely clear segment store and reset expected sequence
  segmentStore = {};
  expectedSeq = null;

  console.log(
    "Language switched to:",
    this.value,
    "Selected index:",
    this.selectedIndex,
  );

  if (languageSelect.value) {
    applyTtsOverlay();
  }
});

languageSelect.addEventListener("change", function () {
  var selectedOpt = this.options[this.selectedIndex];
  var boothObjId = selectedOpt ? selectedOpt.dataset.boothObjId : null;
  var sourceData = boothObjId
    ? boothsData.find((b) => b.id.toString() === boothObjId.toString())
    : null;
  var languageCode = selectedOpt ? selectedOpt.dataset.languageCode : null;
  var roomId = parseInt(roomSelect.value, 10);
  var rData = roomsData.find((r) => r.id.toString() === roomId.toString());

  // Setup translation options based on the selected audio source
  var transGroup = document.getElementById("translation-lang-group");
  if (sourceData) {
    var bData = sourceData;
    if (
      bData &&
      bData.translation_enabled &&
      bData.translation_languages &&
      bData.translation_languages.length > 0
    ) {
      if (transGroup) transGroup.style.display = "flex";
      captionModeSelect.disabled = false;

      // Preserve current selection if possible
      var currentSelection = translationLangSelect.value;
      translationLangSelect.innerHTML = "";

      // Add "Original" as the first default option
      var origOpt = document.createElement("option");
      origOpt.value = "";
      origOpt.textContent = "Original";
      translationLangSelect.appendChild(origOpt);

      var optionExists = false;
      bData.translation_languages.forEach((lang) => {
        var opt = document.createElement("option");
        opt.value = lang.code;
        opt.textContent = lang.name;
        translationLangSelect.appendChild(opt);
        if (lang.code === currentSelection) optionExists = true;
      });
      if (optionExists && currentSelection !== "") {
        translationLangSelect.value = currentSelection;
        if (captionModeSelect.value === "original") {
          captionModeSelect.value = "stacked";
        }
      } else {
        // Default to Original
        translationLangSelect.value = "";
        captionModeSelect.value = "original";
      }
    } else {
      if (transGroup) transGroup.style.display = "none";
      captionModeSelect.disabled = true;
      captionModeSelect.value = "original";
      translationLangSelect.innerHTML = "";
    }
  } else {
    if (transGroup) transGroup.style.display = "none";
    captionModeSelect.disabled = true;
    captionModeSelect.value = "original";
    translationLangSelect.innerHTML = "";
  }

  var canSyncTts = languageCode === "floor" && rData && rData.floor_tts_enabled;
  var ttsSyncGroup = document.getElementById("tts-sync-group");
  if (
    canSyncTts &&
    sourceData &&
    sourceData.translation_enabled &&
    sourceData.translation_languages &&
    sourceData.translation_languages.length > 0
  ) {
    ttsSyncGroup.style.display = "flex";
  } else {
    ttsSyncGroup.style.display = "none";
    ttsSyncEnabled = false;
    if (ttsSyncToggle) ttsSyncToggle.checked = false;
  }

  applyCaptionMode();

  // Always start WHEP as the base stream
  stopCurrentStream();
  var whepUrl = this.value;
  var boothId = selectedOpt ? selectedOpt.dataset.boothId : null;
  var selectedAudioDelayMs = normalizeAudioDelayMs(
    sourceData ? sourceData.audio_delay_ms : 0,
  );
  currentRoomId = roomId || null;
  currentSourceType = "whep";

  if (whepUrl) {
    if (boothId && languageCode) {
      openCaptionsWs(boothId);
      if (languageCode === "floor") {
        // Floor audio has no WHIP ingest — start WHEP immediately.
        startWhepAndCaptions(whepUrl, null, selectedAudioDelayMs);
      } else {
        // Check booth live status before starting WHEP to avoid phantom timer.
        fetch("/api/events/" + eventSlug + "/booths/" + languageCode + "/state?room_id=" + roomId)
          .then(function (r) {
            return r.ok ? r.json() : null;
          })
          .then(function (bstate) {
            if (bstate && bstate.ingest_status === "connected") {
              startWhepAndCaptions(whepUrl, null, selectedAudioDelayMs);
            } else {
              // Not live yet — wait for booth:state via WS
              pendingWhepUrl = whepUrl;
              pendingAudioDelayMs = selectedAudioDelayMs;
              pendingBoothId = boothId;
              setStatus("Broadcast not live yet — waiting...", "waiting");
              showAudioPlayer(false);
            }
          })
          .catch(function () {
            // Can't determine state — start anyway
            startWhepAndCaptions(whepUrl, null, selectedAudioDelayMs);
          });
      }
    } else {
      startWhepAndCaptions(whepUrl, null, selectedAudioDelayMs);
    }
  }

  // Apply TTS overlay if it was already enabled
  applyTtsOverlay();
});

function applyTtsOverlay() {
  var selectedOpt = languageSelect.options[languageSelect.selectedIndex];
  var languageCode = selectedOpt ? selectedOpt.dataset.languageCode : null;
  var roomId = parseInt(roomSelect.value, 10);
  var rData = roomsData.find((r) => r.id.toString() === roomId.toString());
  var canSyncTts = languageCode === "floor" && rData && rData.floor_tts_enabled;
  var targetLangValue = translationLangSelect.value;
  var boothObjId = selectedOpt ? selectedOpt.dataset.boothObjId : null;
  var boothId = selectedOpt ? selectedOpt.dataset.boothId : null;
  var sourceData = boothObjId
    ? boothsData.find((b) => b.id.toString() === boothObjId.toString())
    : null;
  var selectedAudioDelayMs = normalizeAudioDelayMs(
    sourceData ? sourceData.audio_delay_ms : 0,
  );

  stopTtsWs(); // Stop any existing TTS stream

  // We need the TTS stream if we want synchronized TTS audio OR if we want translation text
  var mode = captionModeSelect ? captionModeSelect.value : "original";
  var wantsTranslationText = Boolean(targetLangValue) && mode !== "original";
  var wantsTtsAudio = ttsSyncEnabled && Boolean(targetLangValue) && canSyncTts;

  if (wantsTranslationText || wantsTtsAudio) {
    startTtsWs(roomId, targetLangValue, boothId, selectedAudioDelayMs);

    if (wantsTtsAudio) {
      audioEl.muted = true;
      var selOpt =
        translationLangSelect.options[translationLangSelect.selectedIndex];
      var langName = selOpt ? selOpt.text : "Translation";
      setStatus("Live (" + langName + " TTS)", "live");
    } else {
      audioEl.muted = false;
      if (currentRoomId && languageSelect.value && !pendingWhepUrl) {
        setStatus("Live (WebRTC + Translation)", "live");
      }
    }
  } else {
    audioEl.muted = false;
    // If we are currently connected to WHEP, refresh the badge to WebRTC
    if (currentRoomId && languageSelect.value && !pendingWhepUrl) {
      setStatus("Live (WebRTC)", "live");
    }
  }
}

// Clean up on page unload.
window.addEventListener("beforeunload", function () {
  WhepListener.stop();
  stopTtsWs();
});

window.setInterval(applyCurrentRoomAudioDelay, 3000);
