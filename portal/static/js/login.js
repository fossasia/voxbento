// login.js — Client-side authentication logic for login.html (#903)
// Handles email lookup, authentication method reveal, password login,
// magic-link submission/polling, and safe redirection.

/**
 * Validates and normalizes redirect URLs to prevent open redirects.
 * Only allows same-origin URLs on http/https and rejects protocol-relative URLs.
 *
 * @param {string} rawUrl
 * @returns {string} Safe absolute URL or empty string
 */
export function getSafeRedirectUrl(rawUrl) {
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
  } catch {
    // Malformed URL, fall through to return empty string.
  }
  return '';
}

/**
 * Navigates to the validated return URL or reloads the page as a fallback.
 *
 * @param {string} [customUrl]
 */
export function redirectToNext(customUrl) {
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

/**
 * Polls the magic-link status endpoint until authentication succeeds or expires.
 * Also listens on BroadcastChannel for real-time login across browser tabs.
 *
 * @param {string} pollId
 */
export function startMagicPoll(pollId) {
  if (!pollId) return;

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

  const magicTimer = setInterval(async () => {
    try {
      const res = await fetch('/api/auth/magic-poll?poll_id=' + encodeURIComponent(pollId));
      if (res.ok) {
        const data = await res.json();
        if (data && data.authenticated) {
          clearInterval(magicTimer);
          redirectToNext();
        }
      }
    } catch {
      // Network or parsing errors during polling are ignored until next interval.
    }
  }, 1000);
}

// Preserve window.startMagicPoll for backward compatibility with existing callers/tests.
if (typeof window !== 'undefined') {
  window.startMagicPoll = startMagicPoll;
}

let isInitialized = false;

/**
 * Initializes DOM elements and binds event listeners for the login flow.
 */
export function initLogin() {
  const emailForm = document.getElementById('email-form');
  if (!emailForm || isInitialized) return;
  isInitialized = true;

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
  const forgotPasswordLink = document.getElementById('forgot-password-link');

  let currentEmail = '';

  function showError(msg) {
    if (alertContainer) {
      alertContainer.textContent = msg;
      alertContainer.classList.remove('hidden');
    }
  }

  function showSuccess(msg) {
    if (successContainer) {
      successContainer.textContent = msg;
      successContainer.classList.remove('hidden');
    }
    if (alertContainer) {
      alertContainer.classList.add('hidden');
    }
  }

  emailForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    currentEmail = emailInput ? emailInput.value.trim() : '';
    if (!currentEmail) return;

    if (continueBtn) {
      continueBtn.disabled = true;
      continueBtn.textContent = 'Checking...';
    }
    if (alertContainer) {
      alertContainer.classList.add('hidden');
    }

    try {
      const nextInput = document.getElementById('next_url');
      const nextVal = nextInput ? nextInput.value : '';
      const res = await fetch('/api/auth/check-email', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email: currentEmail, next_url: nextVal }),
      });
      const data = await res.json().catch(() => ({}));

      if (!res.ok) {
        showError(data.detail || 'An error occurred');
        if (continueBtn) {
          continueBtn.disabled = false;
          continueBtn.textContent = 'Continue';
        }
        return;
      }

      // Always show the same authentication options. Any legacy response
      // fields are constant compatibility sentinels, not account state.
      emailForm.classList.add('hidden');
      if (loginMethods) loginMethods.classList.remove('hidden');
      if (userEmailDisplay) userEmailDisplay.textContent = currentEmail;
      if (passEmailInput) passEmailInput.value = currentEmail;
      if (passwordForm) passwordForm.classList.remove('hidden');
      if (magicDivider) magicDivider.classList.remove('hidden');
      const passwordInput = document.getElementById('password');
      if (passwordInput) passwordInput.focus();
    } catch {
      showError('Failed to connect to server');
      if (continueBtn) {
        continueBtn.disabled = false;
        continueBtn.textContent = 'Continue';
      }
    }
  });

  if (magicLinkBtn) {
    magicLinkBtn.addEventListener('click', async () => {
      magicLinkBtn.disabled = true;
      magicLinkBtn.textContent = 'Sending...';
      if (alertContainer) {
        alertContainer.classList.add('hidden');
      }

      try {
        const nextInput = document.getElementById('next_url');
        const nextVal = nextInput ? nextInput.value : '';
        const targetEmail = currentEmail || (emailInput ? emailInput.value.trim() : '');
        const res = await fetch('/api/auth/magic-link', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ email: targetEmail, next_url: nextVal }),
        });

        if (res.ok) {
          const magicData = await res.json().catch(() => ({}));
          if (passwordForm) passwordForm.classList.add('hidden');
          if (magicDivider) magicDivider.classList.add('hidden');
          magicLinkBtn.classList.add('hidden');
          showSuccess("We've sent a magic link to your email. Please check your inbox.");
          if (magicData.poll_id) {
            if (typeof window !== 'undefined' && typeof window.startMagicPoll === 'function') {
              window.startMagicPoll(magicData.poll_id);
            } else {
              startMagicPoll(magicData.poll_id);
            }
          }
        } else {
          const data = await res.json().catch(() => ({}));
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
  }

  if (forgotPasswordLink) {
    forgotPasswordLink.addEventListener('click', async (e) => {
      e.preventDefault();
      if (alertContainer) alertContainer.classList.add('hidden');
      if (successContainer) successContainer.classList.add('hidden');

      try {
        const nextInput = document.getElementById('next_url');
        const nextVal = nextInput ? nextInput.value : '';
        const targetEmail = currentEmail || (emailInput ? emailInput.value.trim() : '');
        const res = await fetch('/api/auth/forgot-password', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ email: targetEmail, next_url: nextVal }),
        });

        if (res.ok) {
          showSuccess('If you have an account with a password, a reset link has been sent.');
        } else {
          showError('Failed to request password reset. Too many requests?');
        }
      } catch {
        showError('Failed to connect to server');
      }
    });
  }
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initLogin);
} else {
  initLogin();
}
