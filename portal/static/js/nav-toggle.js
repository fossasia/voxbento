/**
 * Responsive header navigation.
 *
 * `admin.css` hides `.header-nav-toggle` and lays `.header-nav` out inline on
 * wide viewports. Below the collapse breakpoint the nav is `display: none` and
 * this module drives the open/closed state via the `is-open` class, keeping
 * `aria-expanded` in sync so assistive tech agrees with what is on screen.
 *
 * Only pages that load `admin.css` carry the toggle markup. The collapse
 * breakpoint itself is owned by CSS; the resize handler here asks whether the
 * toggle is still being rendered instead of duplicating the value.
 */

const HEADER_SELECTOR = '.admin-header, .portal-home-header';
const TOGGLE_SELECTOR = '.header-nav-toggle';
const OPEN_CLASS = 'is-open';

function isCollapsed(toggle) {
  return toggle.getClientRects().length > 0;
}

function setOpen(header, nav, toggle, open) {
  nav.classList.toggle(OPEN_CLASS, open);
  toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
}

function initHeader(header) {
  const nav = header.querySelector('.header-nav');
  const toggle = header.querySelector(TOGGLE_SELECTOR);
  if (!nav || !toggle) return;

  const isOpen = () => toggle.getAttribute('aria-expanded') === 'true';

  toggle.addEventListener('click', () => setOpen(header, nav, toggle, !isOpen()));

  nav.addEventListener('click', (event) => {
    if (event.target.closest('a')) setOpen(header, nav, toggle, false);
  });

  document.addEventListener('click', (event) => {
    if (isOpen() && !header.contains(event.target)) {
      setOpen(header, nav, toggle, false);
    }
  });

  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape' || !isOpen()) return;
    setOpen(header, nav, toggle, false);
    toggle.focus();
  });

  window.addEventListener('resize', () => {
    if (isOpen() && !isCollapsed(toggle)) setOpen(header, nav, toggle, false);
  });
}

document.querySelectorAll(HEADER_SELECTOR).forEach(initHeader);
