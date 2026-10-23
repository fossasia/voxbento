// User menu in the shared header (#766): a disclosure button that shows the
// person's links. Closes on Escape, on a click outside and when focus leaves it.

function initUserMenu(root) {
  const button = root.querySelector('[data-user-menu-button]');
  const menu = document.getElementById(button?.getAttribute('aria-controls') || '');
  if (!button || !menu) return;

  const isOpen = () => button.getAttribute('aria-expanded') === 'true';

  function setOpen(open, { focusButton = false } = {}) {
    button.setAttribute('aria-expanded', String(open));
    menu.hidden = !open;
    if (!open && focusButton) button.focus();
  }

  button.addEventListener('click', () => setOpen(!isOpen()));

  root.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && isOpen()) {
      event.preventDefault();
      setOpen(false, { focusButton: true });
    }
  });

  document.addEventListener('click', (event) => {
    if (isOpen() && !root.contains(event.target)) setOpen(false);
  });

  root.addEventListener('focusout', (event) => {
    if (isOpen() && event.relatedTarget && !root.contains(event.relatedTarget)) setOpen(false);
  });
}

document.querySelectorAll('[data-user-menu]').forEach(initUserMenu);
