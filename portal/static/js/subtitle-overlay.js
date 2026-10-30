// Transparent subtitle layer for OBS / vMix browser sources.
// Appearance comes from the query string; the workspace preview can also
// update it, and inject test captions, with same-origin postMessage.

const POSITIONS = ["top", "center", "bottom"];
const IDLE_CLEAR_MS = 8000;

const root = document.getElementById("overlay");
const linesBox = document.getElementById("overlay-lines");
const boothId = root.dataset.boothId;
const token = root.dataset.token || new URLSearchParams(window.location.search).get("token") || "";

const config = { position: "bottom", fontSize: 36, bg: 70, lines: 2 };
let finals = [];
let partial = "";
let idleTimer = null;
let retryDelayMs = 1000;

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
  POSITIONS.forEach((p) => root.classList.toggle("overlay--" + p, p === config.position));
  root.style.setProperty("--overlay-font-size", config.fontSize + "px");
  root.style.setProperty("--overlay-bg-alpha", String(config.bg / 100));
  render();
}

function render() {
  const visible = finals.concat(partial ? [partial] : []).slice(-config.lines);
  linesBox.replaceChildren(
    ...visible.map((text, i) => {
      const line = document.createElement("div");
      line.className = "overlay-line";
      if (i < visible.length - 1) line.classList.add("overlay-line--older");
      line.textContent = text;
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

function handleCaption(data) {
  if (data.type !== "caption") return;
  if (data.status === "partial") {
    partial = data.text || "";
  } else if (data.status === "final") {
    if (data.text) finals = finals.concat([data.text]).slice(-4);
    partial = "";
  } else if (data.status === "clear") {
    partial = "";
  }
  render();
  touch();
}

function connect() {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  const params = new URLSearchParams();
  if (token) params.set("token", token);
  const query = params.toString() ? `?${params.toString()}` : "";
  const ws = new WebSocket(`${proto}//${window.location.host}/ws/captions/${encodeURIComponent(boothId)}${query}`);
  ws.onopen = () => {
    retryDelayMs = 1000;
  };
  ws.onmessage = (event) => {
    try {
      handleCaption(JSON.parse(event.data));
    } catch {
      // Ignore frames that are not caption JSON.
    }
  };
  ws.onclose = (event) => {
    if (event.code === 4001 || event.code === 4003) return;
    setTimeout(connect, retryDelayMs);
    retryDelayMs = Math.min(retryDelayMs * 2, 15000);
  };
}

window.addEventListener("message", (event) => {
  if (event.origin !== window.location.origin || !event.data) return;
  if (event.data.type === "overlay:config") {
    Object.assign(config, readConfig(new URLSearchParams(event.data.query || ""), config));
    applyConfig();
  } else if (event.data.type === "overlay:test" && typeof event.data.text === "string") {
    handleCaption({ type: "caption", status: "final", text: event.data.text });
  }
});

Object.assign(config, readConfig(new URLSearchParams(window.location.search), config));
applyConfig();
connect();
