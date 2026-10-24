function getSafeRedirectUrl(rawUrl) {
  if (!rawUrl || typeof rawUrl !== 'string') return '';
  const trimmed = rawUrl.trim();
  const lower = trimmed.toLowerCase();
  if (
    trimmed.startsWith('//') ||
    lower.startsWith('javascript:') ||
    lower.startsWith('data:') ||
    lower.startsWith('vbscript:')
  ) {
    return '';
  }
  try {
    const parsed = new URL(trimmed, window.location.origin);
    if (
      parsed.origin === window.location.origin &&
      (parsed.protocol === 'http:' || parsed.protocol === 'https:') &&
      !parsed.pathname.startsWith('//')
    ) {
      return parsed.href;
    }
  } catch {}
  return '';
}

function redirectToNext(customUrl) {
  const nextInput = document.getElementById('next_url');
  let target = customUrl || (nextInput ? nextInput.value : '');
  if (!target || target === 'None') {
    const urlParams = new URLSearchParams(window.location.search);
    target = urlParams.get('next') || '';
  }
  const safeTarget = getSafeRedirectUrl(target);
  if (safeTarget) {
    window.location.href = safeTarget;
  } else {
    window.location.reload();
  }
}

function initCrossTabAuth() {
  if (typeof BroadcastChannel !== 'undefined') {
    const authChannel = new BroadcastChannel('voxbento_auth');
    authChannel.onmessage = (e) => {
      const isLogin = e.data === 'logged_in' || (e.data && e.data.type === 'logged_in');
      if (isLogin) {
        const url = e.data && e.data.url ? e.data.url : null;
        redirectToNext(url);
      }
    };
  }
}

function initLogin() {
  initCrossTabAuth();

  const emailForm = document.getElementById('email-form');
  const emailInput = document.getElementById('email');
  const continueBtn = document.getElementById('continue-btn');

  const loginMethods = document.getElementById('login-methods');
  const passwordForm = document.getElementById('password-form');
  const magicDivider = document.getElementById('magic-divider');
  const magicLinkBtn = document.getElementById('magic-link-btn');
  const passEmailInput = document.getElementById('pass_email');
  const userEmailDisplay = document.getElementById('user-email-display');
  const alertContainer = document.getElementById('alert-container');
  const successContainer = document.getElementById('success-container');
  const waitingContainer = document.getElementById('waiting-container');
  const requestNewLink = document.getElementById('request-new-link');
  const forgotPasswordLink = document.getElementById('forgot-password-link');

  let currentEmail = '';

  function showError(msg) {
    alertContainer.textContent = msg;
    alertContainer.classList.remove('hidden');
  }

  function showSuccess(msg) {
    successContainer.textContent = msg;
    successContainer.classList.remove('hidden');
    alertContainer.classList.add('hidden');
    waitingContainer?.classList.add('hidden');
  }

  function showWaitingNotice() {
    waitingContainer?.classList.remove('hidden');
    alertContainer.classList.add('hidden');
    successContainer.classList.add('hidden');
  }

  if (requestNewLink) {
    requestNewLink.addEventListener('click', (e) => {
      e.preventDefault();
      waitingContainer?.classList.add('hidden');
      if (magicLinkBtn) {
        magicLinkBtn.disabled = false;
        magicLinkBtn.textContent = 'Email me a magic link';
        magicLinkBtn.classList.remove('hidden');
      }
      magicDivider?.classList.remove('hidden');
      passwordForm?.classList.remove('hidden');
    });
  }

  emailForm?.addEventListener('submit', async (e) => {
    e.preventDefault();
    currentEmail = emailInput.value.trim();
    if (!currentEmail) return;

    continueBtn.disabled = true;
    continueBtn.textContent = 'Checking...';
    alertContainer.classList.add('hidden');

    try {
      const nextInput = document.getElementById('next_url');
      const res = await fetch('/api/auth/check-email', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email: currentEmail, next_url: nextInput ? nextInput.value : '' }),
      });
      const data = await res.json();

      if (!res.ok) {
        showError(data.detail || 'An error occurred');
        continueBtn.disabled = false;
        continueBtn.textContent = 'Continue';
        return;
      }

      // Always show the same authentication options. Any legacy response
      // fields are constant compatibility sentinels, not account state.
      emailForm.classList.add('hidden');
      loginMethods.classList.remove('hidden');
      userEmailDisplay.textContent = currentEmail;
      passEmailInput.value = currentEmail;
      passwordForm.classList.remove('hidden');
      magicDivider.classList.remove('hidden');
      document.getElementById('password')?.focus();
    } catch {
      showError('Failed to connect to server');
      continueBtn.disabled = false;
      continueBtn.textContent = 'Continue';
    }
  });

  magicLinkBtn?.addEventListener('click', async () => {
    magicLinkBtn.disabled = true;
    magicLinkBtn.textContent = 'Sending...';
    alertContainer.classList.add('hidden');

    try {
      const nextInput = document.getElementById('next_url');
      const res = await fetch('/api/auth/magic-link', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email: currentEmail, next_url: nextInput ? nextInput.value : '' }),
      });

      if (res.ok) {
        passwordForm.classList.add('hidden');
        magicDivider.classList.add('hidden');
        magicLinkBtn.classList.add('hidden');
        showWaitingNotice();
      } else {
        const data = await res.json();
        showError(data.detail || 'Failed to send magic link');
        magicLinkBtn.disabled = false;
        magicLinkBtn.textContent = 'Email me a magic link';
      }
    } catch {
      showError('Failed to connect to server');
      magicLinkBtn.disabled = false;
      magicLinkBtn.textContent = 'Email me a magic link';
    }
  });

  forgotPasswordLink?.addEventListener('click', async (e) => {
    e.preventDefault();
    alertContainer.classList.add('hidden');
    successContainer.classList.add('hidden');
    waitingContainer?.classList.add('hidden');

    if (forgotPasswordLink) {
      forgotPasswordLink.disabled = true;
    }

    try {
      const nextInput = document.getElementById('next_url');
      const res = await fetch('/api/auth/forgot-password', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email: currentEmail, next_url: nextInput ? nextInput.value : '' }),
      });

      if (res.ok) {
        showSuccess('If you have an account with a password, a reset link has been sent.');
      } else {
        showError('Failed to request password reset. Too many requests?');
      }
    } catch {
      showError('Failed to connect to server');
    } finally {
      if (forgotPasswordLink) {
        forgotPasswordLink.disabled = false;
      }
    }
  });
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initLogin);
} else {
  initLogin();
}
