// Workspace configurator for the OBS / vMix subtitle overlay (/overlay/...).
// The preview iframe is the real overlay page, scaled down from 1920x1080.

const TEST_CAPTIONS = [
  "Welcome everyone, we will begin in a moment.",
  "The next release is planned for the end of next week.",
  "Thank you for joining us today.",
];

const card = document.getElementById("subtitle-overlay-builder");
const frame = document.getElementById("overlay-preview-frame");

if (card && frame) {
  const { eventSlug, joinCode } = card.dataset;
  const channel = document.getElementById("overlay-channel");
  const fontSize = document.getElementById("overlay-font-size");
  const bg = document.getElementById("overlay-bg");
  const lines = document.getElementById("overlay-lines");
  const urlField = document.getElementById("overlay-url");
  const preview = document.getElementById("overlay-preview");
  let testIndex = 0;
  const status = document.getElementById("overlay-status");
  const statuses = {
    connecting: "Connecting to captions…", connected: "Connected — waiting for captions from this source.",
    receiving: "Receiving live captions.", reconnecting: "Connection interrupted — reconnecting automatically…",
    denied: "Access unavailable. Check the event join code and caption channel, then select the channel again or reload this page.",
    stopped: "Preview disconnected.",
  };
  window.addEventListener("message", (event) => {
    if (event.origin !== window.location.origin || event.source !== frame.contentWindow) return;
    if (event.data?.type === "overlay:status" && statuses[event.data.status]) {
      status.textContent = statuses[event.data.status];
    }
  });

  const position = () => card.querySelector('input[name="overlay-position"]:checked').value;

  function appearanceQuery() {
    return new URLSearchParams({
      fontSize: fontSize.value,
      position: position(),
      bg: bg.value,
      lines: lines.value,
    }).toString();
  }

  function overlayPath() {
    // Encode every segment taken from the DOM so it stays a same-origin path.
    const segments = [eventSlug, ...channel.value.split("/")].map(encodeURIComponent);
    const code = new URLSearchParams({ code: joinCode }).toString();
    return `/overlay/${segments.join("/")}?${code}&${appearanceQuery()}`;
  }

  function update({ reload = false } = {}) {
    document.getElementById("overlay-font-size-value").textContent = fontSize.value;
    document.getElementById("overlay-bg-value").textContent = bg.value;
    document.getElementById("overlay-lines-value").textContent = lines.value;
    urlField.value = window.location.origin + overlayPath();
    document.getElementById("overlay-source").textContent = channel.selectedOptions[0]?.dataset.source || "";
    if (reload) {
      status.textContent = statuses.connecting;
      frame.src = overlayPath();
    } else {
      frame.contentWindow?.postMessage(
        { type: "overlay:config", query: appearanceQuery() },
        window.location.origin,
      );
    }
  }

  // A slider moved while the iframe navigates can post to the old document;
  // resend the current appearance once the new one has loaded.
  frame.addEventListener("load", () => update());

  function fitPreview() {
    frame.style.transform = `scale(${preview.clientWidth / 1920})`;
  }

  channel.addEventListener("change", () => update({ reload: true }));
  [fontSize, bg, lines].forEach((input) => input.addEventListener("input", () => update()));
  card.querySelectorAll('input[name="overlay-position"]').forEach((radio) => {
    radio.addEventListener("change", () => update());
  });
  document.getElementById("overlay-test").addEventListener("click", () => {
    const text = TEST_CAPTIONS[testIndex++ % TEST_CAPTIONS.length];
    frame.contentWindow?.postMessage({ type: "overlay:test", text }, window.location.origin);
  });

  new ResizeObserver(fitPreview).observe(preview);
  fitPreview();
  update({ reload: true });
}
