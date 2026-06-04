/**
 * Admin panel client-side utilities.
 * Loaded as an ES module — no jQuery, no inline scripts.
 */

function copyToClipboard(targetId) {
  const el = document.getElementById(targetId);
  if (!el) return;
  const text = el.textContent.trim();
  const fullUrl = text.startsWith('/') ? window.location.origin + text : text;
  navigator.clipboard.writeText(fullUrl).then(() => {
    const btn = el.nextElementSibling;
    if (btn) {
      const orig = btn.textContent;
      btn.textContent = 'Copied!';
      setTimeout(() => { btn.textContent = orig; }, 1500);
    }
  });
}

document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.btn-copy[data-copy-target]').forEach((btn) => {
    btn.addEventListener('click', () => {
      copyToClipboard(btn.dataset.copyTarget);
    });
  });
});
