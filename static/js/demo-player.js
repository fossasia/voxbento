/**
 * VoxBento landing page demo player.
 *
 * All HTML is pre-rendered in home.html so Tailwind classes are picked up by
 * the CDN scanner. This module only attaches behaviour: fetches the manifest
 * for audio URLs, wires up play/pause and language switching, and hooks up the
 * hero audio selector. No innerHTML is ever set.
 */

const MANIFEST_URL = "/api/demo/manifest";
const POLL_MS = 3000;

/** @type {Map<string, HTMLAudioElement>} */
const audioEls = new Map();

/** @type {Map<string, {text: string, start_ms: number, end_ms: number}[]>} */
const langSegments = new Map();

/** @type {string|null} */
let activeCode = null;

/** @type {ReturnType<typeof setInterval>|null} */
let captionTimer = null;

/** @type {ReturnType<typeof setInterval>|null} */
let pollTimer = null;

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------

async function init() {
    const manifest = await fetchManifest();
    if (!manifest) return;

    if (manifest.status === "ready") {
        setupDemo(manifest);
    } else {
        startPolling();
    }
}

async function fetchManifest() {
    try {
        const res = await fetch(MANIFEST_URL);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return await res.json();
    } catch (err) {
        console.error("[demo-player] Manifest fetch failed:", err);
        return null;
    }
}

function startPolling() {
    // Loading overlay is shown by default in HTML; poll until ready.
    if (pollTimer) return;
    pollTimer = setInterval(async () => {
        const manifest = await fetchManifest();
        if (manifest && manifest.status === "ready") {
            clearInterval(pollTimer);
            pollTimer = null;
            setupDemo(manifest);
        }
    }, POLL_MS);
}

// ---------------------------------------------------------------------------
// Demo player setup
// ---------------------------------------------------------------------------

function setupDemo(manifest) {
    for (const lang of manifest.languages) {
        if (!audioEls.has(lang.code)) {
            const a = new Audio(lang.audio_url);
            a.preload = "auto";
            audioEls.set(lang.code, a);
        }
        langSegments.set(lang.code, lang.segments || []);
    }

    // Swap loading state → player.
    document.getElementById("demo-loading")?.classList.add("hidden");
    document.getElementById("demo-player")?.classList.remove("hidden");

    // Language buttons (both desktop full labels and mobile short codes).
    document.querySelectorAll("[data-demo-lang]").forEach((btn) => {
        btn.addEventListener("click", () => switchLang(btn.dataset.demoLang));
    });

    // Desktop play/pause.
    document.getElementById("demo-play-btn")?.addEventListener("click", togglePlay);
    // Mobile play/pause.
    document.getElementById("demo-play-btn-mobile")?.addEventListener("click", togglePlay);

    // Hero audio toggle (the pill toggle button in the hero card).
    document.getElementById("hero-audio-toggle")?.addEventListener("click", () => {
        if (isPlaying()) {
            audioEls.get(activeCode)?.pause();
            setPlayState(false);
            stopCaptionTimer();
        } else {
            const code = activeCode ?? (manifest.languages[0]?.code);
            if (!code) return;
            activeCode = code;
            activateLangButton(code);
            const audio = audioEls.get(code);
            if (!audio) return;
            audio.currentTime = 0;
            audio.play().catch((err) => console.error("[demo-player] hero toggle failed:", err));
            setPlayState(true);
            startCaptionTimer(audio);
        }
    });

    // Activate first language.
    if (manifest.languages.length > 0) {
        activeCode = manifest.languages[0].code;
        activateLangButton(activeCode);
    }

    setupHeroSelector();
}

// ---------------------------------------------------------------------------
// Language switching
// ---------------------------------------------------------------------------

function switchLang(code) {
    if (!audioEls.has(code)) return;

    const wasPlaying = isPlaying();
    const currentTime = activeCode ? (audioEls.get(activeCode)?.currentTime ?? 0) : 0;

    audioEls.get(activeCode)?.pause();
    activeCode = code;
    activateLangButton(code);

    const audio = audioEls.get(code);
    audio.currentTime = currentTime;
    if (wasPlaying) {
        audio.play().catch((err) => console.error("[demo-player] play failed:", err));
    }
}

function activateLangButton(code) {
    const activeTokens = ["bg-brand-600", "border-brand-500", "text-white", "shadow-lg"];
    const inactiveTokens = ["border-white/20", "text-white/60", "hover:text-white", "hover:border-white/50", "bg-white/5"];

    document.querySelectorAll("[data-demo-lang]").forEach((btn) => {
        const isActive = btn.dataset.demoLang === code;
        btn.classList.remove(...activeTokens, ...inactiveTokens);
        btn.classList.add(...(isActive ? activeTokens : inactiveTokens));
    });

    document.querySelectorAll("[data-hero-lang]").forEach((btn) => {
        const isActive = btn.dataset.heroLang === code;
        const check = btn.querySelector(".hero-check");
        if (isActive) {
            btn.classList.add("bg-slate-800", "text-white");
            btn.classList.remove("text-slate-400");
            if (check) check.style.opacity = "1";
        } else {
            btn.classList.remove("bg-slate-800", "text-white");
            btn.classList.add("text-slate-400");
            if (check) check.style.opacity = "0";
        }
    });
}

// ---------------------------------------------------------------------------
// Playback
// ---------------------------------------------------------------------------

function togglePlay() {
    if (!activeCode) return;
    const audio = audioEls.get(activeCode);
    if (!audio) return;

    if (isPlaying()) {
        audio.pause();
        setPlayState(false);
        stopCaptionTimer();
    } else {
        audio.play().catch((err) => console.error("[demo-player] play failed:", err));
        setPlayState(true);
        startCaptionTimer(audio);
    }
}

function isPlaying() {
    if (!activeCode) return false;
    return !(audioEls.get(activeCode)?.paused ?? true);
}

function setPlayState(playing) {
    // Desktop
    document.getElementById("demo-play-icon")?.classList.toggle("hidden", playing);
    document.getElementById("demo-pause-icon")?.classList.toggle("hidden", !playing);
    // Mobile
    document.getElementById("demo-play-icon-mobile")?.classList.toggle("hidden", playing);
    document.getElementById("demo-pause-icon-mobile")?.classList.toggle("hidden", !playing);
    // Waveforms
    const waveform = document.getElementById("demo-waveform");
    if (waveform) waveform.style.opacity = playing ? "1" : "0";
    const waveformMobile = document.getElementById("demo-waveform-mobile");
    if (waveformMobile) waveformMobile.style.opacity = playing ? "1" : "0";
    // Hero toggle pill — slide knob right (on) or left (off)
    const toggle = document.getElementById("hero-audio-toggle");
    if (toggle) {
        const knob = toggle.querySelector("div");
        if (knob) knob.style.transform = playing ? "translateX(20px)" : "translateX(0)";
        toggle.style.backgroundColor = playing ? "#2563eb" : "";
    }
}

function startCaptionTimer(audio) {
    stopCaptionTimer();
    const captionEl = document.getElementById("demo-caption");
    const captionMobile = document.getElementById("demo-caption-mobile");
    if (!captionEl && !captionMobile) return;
    captionTimer = setInterval(() => {
        const tMs = audio.currentTime * 1000;
        const segs = langSegments.get(activeCode) ?? [];
        const seg = segs.find((s) => tMs >= s.start_ms && tMs < s.end_ms);
        const text = seg ? seg.text : "";
        if (captionEl) captionEl.textContent = text;
        if (captionMobile) captionMobile.textContent = text || "VoxBento translating live\u2026";
    }, 150);
}

function stopCaptionTimer() {
    if (captionTimer !== null) {
        clearInterval(captionTimer);
        captionTimer = null;
    }
}

// ---------------------------------------------------------------------------
// Hero audio selector
// ---------------------------------------------------------------------------

function setupHeroSelector() {
    document.querySelectorAll("[data-hero-lang]").forEach((btn) => {
        btn.addEventListener("click", () => {
            const code = btn.dataset.heroLang;
            if (!audioEls.has(code)) return;

            const wasPlaying = isPlaying();
            switchLang(code);
            if (!wasPlaying) {
                const audio = audioEls.get(code);
                audio.currentTime = 0;
                audio.play().catch((err) => console.error("[demo-player] hero play failed:", err));
                setPlayState(true);
                startCaptionTimer(audio);
            }
        });
    });
}

document.addEventListener("DOMContentLoaded", init);
