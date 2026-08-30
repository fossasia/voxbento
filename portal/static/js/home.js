document.addEventListener("DOMContentLoaded", () => {
  const capabilities = [
    "Interpretation",
    "Live Translation",
    "AI Captions",
    "Speech-to-Text",
    "Text-to-Speech",
    "Voice Translation",
    "Real-Time Transcription",
    "Multilingual Subtitles",
    "AI Dubbing",
    "Meeting Translation",
    "Conference Interpretation",
    "Voice Cloning",
    "Accessibility Services",
    "Audio Translation",
  ];

  const audiences = [
    "Enterprise",
    "Global Teams",
    "Meetings",
    "Webinars",
    "Conferences",
    "Events",
    "Town Halls",
    "Training Programs",
    "Customer Support",
    "Sales Teams",
    "Healthcare Organizations",
    "Educational Institutions",
    "Government Agencies",
    "International Summits",
    "Hybrid Events",
  ];

  const prefersReducedMotion = window.matchMedia(
    "(prefers-reduced-motion: reduce)"
  ).matches;

  function setupRotator(containerId, words, interval) {
    const container = document.getElementById(containerId);
    if (!container) return;

    let currentIndex = 0;

    setInterval(() => {
      container.classList.remove("opacity-100", "translate-y-0", "blur-0");
      container.classList.add("opacity-0");

      if (!prefersReducedMotion) {
        container.classList.add("-translate-y-4", "blur-sm");
      }

      setTimeout(() => {
        currentIndex = (currentIndex + 1) % words.length;
        container.innerText = words[currentIndex];

        if (!prefersReducedMotion) {
          container.classList.remove("-translate-y-4");
          container.classList.add("translate-y-4");
        }

        void container.offsetWidth;

        container.classList.remove("opacity-0", "translate-y-4", "blur-sm");
        container.classList.add("opacity-100");

        if (!prefersReducedMotion) {
          container.classList.add("translate-y-0", "blur-0");
        }
      }, 700);
    }, interval);
  }

  function setupNavbarShadow() {
    const navbar = document.getElementById("navbar");
    if (!navbar) return;

    const updateNavbar = () => {
      if (window.scrollY > 20) {
        navbar.classList.add("shadow-md");
        navbar.classList.remove("border-transparent");
        navbar.classList.add("border-slate-200/50");
      } else {
        navbar.classList.remove("shadow-md");
        navbar.classList.remove("border-slate-200/50");
        navbar.classList.add("border-transparent");
      }
    };

    updateNavbar();
    window.addEventListener("scroll", updateNavbar);
  }

  if (document.getElementById("rotator-capability")) {
    setupRotator("rotator-capability", capabilities, 3500);
  }

  if (document.getElementById("rotator-audience")) {
    setTimeout(() => {
      setupRotator("rotator-audience", audiences, 3500);
    }, 1750);
  }

  setupNavbarShadow();
});