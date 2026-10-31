// Transparent subtitle layer for OBS / vMix browser sources.
// Appearance comes from the query string; the workspace preview can also
// update it, and inject test captions, with same-origin postMessage.

const POSITIONS = ["top", "center", "bottom"];
const IDLE_CLEAR_MS = 8000;
const CAPTION_TYPES = ["caption", "translated_caption", "translation"];

const root = document.getElementById("overlay");
const linesBox = document.getElementById("overlay-lines");
const boothId = root ? root.dataset.boothId : "";
const token = root ? (root.dataset.token || "") : "";

const config = { position: "bottom", fontSize: 36, bg: 70, lines: 2 };
let finals = [];
let partial = "";
let idleTimer = null;


function clampInt(value, min, max, fallback) {
  const n = parseInt(value, 10);
  return Number.isFinite(n) ? Math.min(max, Math.max(min, n)) : fallback;
}

export function readConfig(params, base) {
  const position = params.get("position");
  return {
    position: POSITIONS.includes(position) ? position : base.position,
    fontSize: clampInt(params.get("fontSize"), 16, 96, base.fontSize),
    bg: clampInt(params.get("bg"), 0, 100, base.bg),
    lines: clampInt(params.get("lines"), 1, 4, base.lines),
  };
}

function applyConfig() {
  if (!root) return;
  POSITIONS.forEach((p) => root.classList.toggle("overlay--" + p, p === config.position));
  root.style.setProperty("--overlay-font-size", config.fontSize + "px");
  root.style.setProperty("--overlay-bg-alpha", String(config.bg / 100));
  root.style.setProperty("--overlay-line-limit", String(config.lines));
  render();
}

function render() {
  if (!linesBox) return;
  const visible = finals.map((f) => f.text).concat(partial ? [partial] : []).slice(-config.lines);
  linesBox.replaceChildren(
    ...visible.map((text, i) => {
      const line = document.createElement("div");
      line.className = "overlay-line";
      if (i < visible.length - 1) line.classList.add("overlay-line--older");
      line.textContent = text;
      line.dir = "auto";
      line.style.setProperty("--caption-line-limit", String(i === visible.length - 1 ? config.lines - visible.length + 1 : 1));
      return line;
    }),
  );
}

function touch() {
  clearTimeout(idleTimer);
  idleTimer = setTimeout(() => {
    finals = [];
    partial = "";
    render();
  }, IDLE_CLEAR_MS);
}

export function handleCaption(data) {
  if (!data || !CAPTION_TYPES.includes(data.type)) return;
  const rawText = typeof data.text === "string" ? data.text : (typeof data.translated_text === "string" ? data.translated_text : "");
  const text = rawText.trim();
  const status = data.status || (data.type === "caption" ? "partial" : "final");

  if (status === "partial") {
    partial = text;
  } else if (status === "final") {
    if (text) {
      // A booth whose language is also a translation target gets the same segment
      // twice (caption + translated_caption); replace it instead of showing it twice.
      const id = data.segment_id || null;
      const existing = id ? finals.findIndex((f) => f.id === id) : -1;
      if (existing >= 0) {
        finals = finals.map((f, i) => (i === existing ? { id, text } : f));
      } else {
        finals = finals.concat([{ id, text }]).slice(-4);
      }
    }
    partial = "";
  } else if (status === "clear") {
    partial = "";
  }
  render();
  touch();
}

// Reauthorize through the original page URL on reconnect, rather than retaining
// an expired JWT for the entire broadcast. Credentials never leave this origin.
export function startConnection({ boothId, initialToken, location, onCaption, onStatus,
  WebSocketClass = WebSocket, fetchSession = fetch, schedule = setTimeout, cancel = clearTimeout }) {
  let socket = null;
  let retryTimer = null;
  let timeoutTimer = null;
  let controller = null;
  let stopped = false;
  let retryDelay = 1000;
  let authFailures = 0;

  function retry(minimum = 0) {
    if (stopped || retryTimer !== null) return;
    onStatus("reconnecting");
    retryTimer = schedule(() => {
      retryTimer = null;
      refresh();
    }, Math.max(minimum, retryDelay));
    retryDelay = Math.min(retryDelay * 2, 30000);
  }

  async function refresh() {
    if (stopped) return;
    controller = new AbortController();
    const timeout = schedule(() => controller?.abort(), 10000);
    try {
      const url = new URL(location.href);
      url.pathname = url.pathname.replace(/\/$/, "") + "/session";
      url.hash = "";
      // Appearance parameters are not credentials.
      const code = url.searchParams.get("code");
      url.search = "";
      if (code) url.searchParams.set("code", code);
      const response = await fetchSession(url.href, {
        cache: "no-store", credentials: "same-origin", signal: controller.signal,
        headers: { Accept: "application/json" }, redirect: "error",
      });
      if (stopped) return;
      if ([401, 403, 404].includes(response.status)) {
        stop("denied");
        return;
      }
      if (response.status === 429) {
        const seconds = Number(response.headers.get("Retry-After"));
        retry(Number.isFinite(seconds) ? Math.min(300, Math.max(1, seconds)) * 1000 : 60000);
        return;
      }
      if (!response.ok) {
        retry();
        return;
      }
      const data = await response.json();
      if (data.booth_id !== boothId || typeof data.token !== "string" || !data.token) {
        stop("denied");
        return;
      }
      if (!stopped) connect(data.token);
    } catch {
      // Network failures and timeouts are retried; never log credential URLs.
      retry();
    } finally {
      cancel(timeout);
      controller = null;
    }
  }

  function connect(accessToken) {
    if (stopped) return;
    onStatus("connecting");
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    // OBS 30 and earlier embed Chromium 103, which lacks URLSearchParams.size.
    const query = accessToken ? `?token=${encodeURIComponent(accessToken)}` : "";
    const ws = new WebSocketClass(`${proto}//${location.host}/ws/captions/${encodeURIComponent(boothId)}${query}`);
    socket = ws;
    timeoutTimer = schedule(() => ws.close(), 10000);
    ws.onopen = () => {
      cancel(timeoutTimer);
      onStatus("connected");
    };
    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (CAPTION_TYPES.includes(data?.type)) {
          retryDelay = 1000;
          authFailures = 0;
          onCaption(data);
          onStatus("receiving");
        }
      } catch {
        // Non-JSON frames are not captions.
      }
    };
    ws.onclose = (event) => {
      cancel(timeoutTimer);
      socket = null;
      if (stopped) return;
      if (event.code === 4003 || (event.code === 4001 && ++authFailures > 1)) {
        stop("denied");
        return;
      }
      retry(event.code === 1008 ? 60000 : 0);
    };
  }

  function stop(status = "stopped") {
    stopped = true;
    cancel(retryTimer);
    cancel(timeoutTimer);
    controller?.abort();
    socket?.close();
    onStatus(status);
  }

  connect(initialToken);
  return stop;
}

function reportStatus(status) {
  if (root) root.dataset.status = status;
  if (window.parent !== window) {
    window.parent.postMessage({ type: "overlay:status", status }, window.location.origin);
  }
}

if (typeof window !== "undefined") {
  window.addEventListener("message", (event) => {
    if (event.origin !== window.location.origin || event.source !== window.parent || window.parent === window || !event.data) return;
    if (event.data.type === "overlay:config") {
      Object.assign(config, readConfig(new URLSearchParams(event.data.query || ""), config));
      applyConfig();
    } else if (event.data.type === "overlay:test" && typeof event.data.text === "string") {
      handleCaption({ type: "caption", status: "final", text: event.data.text });
    }
  });

  if (root) {
    Object.assign(config, readConfig(new URLSearchParams(window.location.search), config));
    applyConfig();
    if (boothId) {
      const stop = startConnection({ boothId, initialToken: token, location: window.location,
        onCaption: handleCaption, onStatus: reportStatus });
      window.addEventListener("pagehide", () => { stop(); clearTimeout(idleTimer); });
    }
  }
}
