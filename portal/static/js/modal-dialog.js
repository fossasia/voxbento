// Open and close for <dialog class="custom-modal-overlay"> popups (admin.css).
// The .active class drives the fade and lift, so it is added after showModal()
// and removed a fade's length before close().

const FADE_MS = 200;
const openers = new WeakMap();
const closing = new WeakMap();

// Browsers without <dialog> support (Safari before 15.4) still show the popup through the open attribute.
const isOpen = (dialog) => dialog.hasAttribute('open');

export function openDialog(dialog, focusTarget) {
  if (isOpen(dialog)) return;
  openers.set(dialog, document.activeElement);
  if (typeof dialog.showModal === 'function') dialog.showModal();
  else dialog.setAttribute('open', '');
  // Force reflow so the CSS transition plays
  void dialog.offsetWidth;
  dialog.classList.add('active');
  focusTarget?.focus();
}

export function closeDialog(dialog) {
  if (!isOpen(dialog)) return Promise.resolve();
  if (!closing.has(dialog)) {
    dialog.classList.remove('active');
    closing.set(dialog, new Promise((resolve) => {
      setTimeout(() => {
        closing.delete(dialog);
        if (typeof dialog.close === 'function') dialog.close();
        else dialog.removeAttribute('open');
        const opener = openers.get(dialog);
        openers.delete(dialog);
        if (opener?.isConnected) opener.focus();
        resolve();
      }, FADE_MS);
    }));
  }
  return closing.get(dialog);
}

// Escape fades the dialog out like the close buttons do, unless canClose() says a request is still running.
export function initDialog(dialog, canClose = () => true) {
  dialog.addEventListener('cancel', (event) => {
    event.preventDefault();
    if (canClose()) closeDialog(dialog);
  });
  // Browsers may still close on a repeated Escape; drop .active so the next open fades in again.
  dialog.addEventListener('close', () => dialog.classList.remove('active'));
}
