/**
 * Responsive header navigation.
 *
 * `admin.css` / `auth.css` hide `.header-nav-toggle` and lay `.header-nav`
 * out inline on wide viewports. Below the collapse breakpoint the nav is
 * `display: none` and this module drives the open/closed state via the
 * `is-open` class, keeping `aria-expanded` in sync so assistive tech agrees
 * with what is on screen.
 *
 * Accessibility contract
 * ─────────────────────
 * • The toggle button carries `aria-controls="<nav id>"` and
 *   `aria-expanded` (set by CSS to "false" by default).
 * • When closed the nav receives `aria-hidden="true"` and its focusable
 *   descendants get `tabindex="-1"` so keyboard users cannot Tab into an
 *   invisible menu.
 * • When open those attributes are reversed so the nav is reachable via
 *   Tab and announced by screen readers as a navigation landmark.
 * • Escape closes an open menu and returns focus to the toggle button.
 * • Clicking a nav link closes the menu (common disclosure pattern).
 * • Clicking outside the header closes the menu.
 * • Resizing back above the breakpoint closes the menu (the toggle becomes
 *   `display:none` so `isCollapsed()` returns false).
 *
 * Only pages that load `admin.css` or `auth.css` carry the toggle markup.
 * The collapse breakpoint itself is owned by CSS; the resize handler here
 * asks whether the toggle is still being rendered instead of duplicating the
 * pixel value.
 */

const HEADER_SELECTOR = '.admin-header, .portal-home-header';
const TOGGLE_SELECTOR = '.header-nav-toggle';
const OPEN_CLASS = 'is-open';

/** Returns true when the hamburger button is currently rendered (i.e. we are
 *  below the CSS breakpoint). */
function isCollapsed(toggle) {
  return toggle.getClientRects().length > 0;
}

/**
 * Open or close the nav, keeping all visible/accessibility attributes in sync.
 *
 * @param {Element} header  - The `.admin-header` / `.portal-home-header` root.
 * @param {Element} nav     - The `.header-nav` element.
 * @param {Element} toggle  - The `.header-nav-toggle` button.
 * @param {boolean} open    - Desired open state.
 */
function setOpen(header, nav, toggle, open) {
  nav.classList.toggle(OPEN_CLASS, open);
  toggle.setAttribute('aria-expanded', open ? 'true' : 'false');

  // When closed: hide the nav from the accessibility tree and prevent focus.
  // When open: expose it as a visible navigation region.
  nav.setAttribute('aria-hidden', open ? 'false' : 'true');
  nav.querySelectorAll('a, button, input, select, textarea, [tabindex]').forEach((el) => {
    if (open) {
      el.removeAttribute('tabindex');
    } else {
      el.setAttribute('tabindex', '-1');
    }
  });
}

function initHeader(header) {
  const nav = header.querySelector('.header-nav');
  const toggle = header.querySelector(TOGGLE_SELECTOR);
  if (!nav || !toggle) return;

  header.classList.add('has-js-nav');

  const isOpen = () => toggle.getAttribute('aria-expanded') === 'true';

  // Set the initial closed state so the nav starts inaccessible to keyboards
  // when the toggle button is visible.
  setOpen(header, nav, toggle, false);

  // ── Toggle button ────────────────────────────────────────────────────────
  toggle.addEventListener('click', () => setOpen(header, nav, toggle, !isOpen()));

  // ── Nav link clicks close the menu ──────────────────────────────────────
  // Guard with `instanceof Element` before calling `.closest()` because
  // non-Element nodes (e.g. Text nodes from SVG tspan, comment nodes) do not
  // have that method.  In practice browsers retarget SVG sub-element clicks to
  // the nearest Element, but the explicit guard makes the intent clear and
  // prevents any future breakage from synthetic events.
  nav.addEventListener('click', (event) => {
    if (event.target instanceof Element && event.target.closest('a')) {
      setOpen(header, nav, toggle, false);
    }
  });

  // ── Outside-click closes the menu ───────────────────────────────────────
  document.addEventListener('click', (event) => {
    if (!isOpen()) return;
    if (!(event.target instanceof Element)) return;
    if (!header.contains(event.target)) {
      setOpen(header, nav, toggle, false);
    }
  });

  // ── Escape closes the menu and returns focus to the toggle button ────────
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape' || !isOpen()) return;
    setOpen(header, nav, toggle, false);
    toggle.focus();
  });

  // ── Viewport resize: close when the toggle is no longer rendered ─────────
  window.addEventListener('resize', () => {
    if (isOpen() && !isCollapsed(toggle)) {
      setOpen(header, nav, toggle, false);
    }
  });
}

document.querySelectorAll(HEADER_SELECTOR).forEach(initHeader);
