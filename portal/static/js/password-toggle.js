function setupPasswordToggle(passwordInput, toggleBtn) {
  if (!passwordInput || !toggleBtn) return;

  const showIcon = toggleBtn.querySelector('.password-toggle-icon--show');
  const hideIcon = toggleBtn.querySelector('.password-toggle-icon--hide');

  toggleBtn.addEventListener('click', (e) => {
    e.preventDefault();
    const isPassword = passwordInput.type === 'password';
    passwordInput.type = isPassword ? 'text' : 'password';
    const newLabel = isPassword ? 'Hide password' : 'Show password';
    toggleBtn.setAttribute('aria-label', newLabel);
    toggleBtn.setAttribute('title', newLabel);

    if (showIcon && hideIcon) {
      showIcon.classList.toggle('hidden', isPassword);
      hideIcon.classList.toggle('hidden', !isPassword);
    }
  });
}

function setupAllPasswordToggles() {
  if (typeof document === 'undefined') return;
  document.querySelectorAll('.password-toggle-btn').forEach((toggleBtn) => {
    const inputId = toggleBtn.getAttribute('aria-controls');
    setupPasswordToggle(inputId ? document.getElementById(inputId) : null, toggleBtn);
  });
}

if (typeof document !== 'undefined') {
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', setupAllPasswordToggles);
  } else {
    setupAllPasswordToggles();
  }
}

if (typeof window !== 'undefined') {
  window.setupPasswordToggle = setupPasswordToggle;
  window.setupAllPasswordToggles = setupAllPasswordToggles;
}
if (typeof module !== 'undefined' && module.exports) {
  module.exports = { setupPasswordToggle, setupAllPasswordToggles };
}
