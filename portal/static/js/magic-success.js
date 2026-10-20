// Shown after a magic-link sign-in: tells the original tab the session is ready
// and, for OAuth flows, hands it the /oauth/authorize URL to continue with.

const POPUP_NAME = 'voxbento_oauth';
const POPUP_WIDTH = 600;
const POPUP_HEIGHT = 700;
const AUTO_CLOSE_DELAY_MS = 1500;

const authChannel = typeof BroadcastChannel !== 'undefined' ? new BroadcastChannel('voxbento_auth') : null;
authChannel?.postMessage('logged_in');

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
      parsed.pathname.startsWith('/oauth/authorize') &&
      !parsed.pathname.startsWith('//')
    ) {
      return parsed.href;
    }
  } catch (error) {
    console.error('Malformed redirect URL:', rawUrl, error);
  }
  return '';
}

function openAuthPopup(targetUrl) {
  try {
    const parsed = new URL(targetUrl, window.location.origin);
    if (parsed.origin !== window.location.origin || !parsed.pathname.startsWith('/oauth/authorize')) {
      console.error('Invalid targetUrl origin or path:', targetUrl);
      return null;
    }
  } catch (error) {
    console.error('Malformed targetUrl:', error);
    return null;
  }
  const left = window.screen.width ? Math.round((window.screen.width - POPUP_WIDTH) / 2) : 100;
  const top = window.screen.height ? Math.round((window.screen.height - POPUP_HEIGHT) / 2) : 100;
  const features = `width=${POPUP_WIDTH},height=${POPUP_HEIGHT},top=${top},left=${left},status=yes,toolbar=no,menubar=no,location=yes,resizable=yes,scrollbars=yes`;

  let popup = null;
  try {
    popup = window.open(targetUrl, POPUP_NAME, features);
    popup?.focus();
  } catch (error) {
    console.error('Error opening popup:', error);
  }
  return popup;
}

function notifyOriginalTab(targetUrl) {
  authChannel?.postMessage({ type: 'logged_in', url: targetUrl });
  authChannel?.postMessage('focus_popup');
}

function closeSelf() {
  try {
    window.close();
  } catch (error) {
    // Browsers refuse to close tabs they did not open; the banner stays as a fallback.
    console.debug('Could not close magic-link tab:', error);
  }
}

const statusBanner = document.getElementById('status-banner');
const targetUrl = getSafeRedirectUrl(statusBanner?.dataset.nextUrl ?? '');

if (targetUrl) {
  authChannel?.postMessage({ type: 'logged_in', url: targetUrl });

  const manualLink = document.getElementById('manual-redirect-link');
  if (manualLink) {
    manualLink.href = targetUrl;
    manualLink.addEventListener('click', (event) => {
      event.preventDefault();
      notifyOriginalTab(targetUrl);
      openAuthPopup(targetUrl);
      closeSelf();
    });
  }

  notifyOriginalTab(targetUrl);
  setTimeout(closeSelf, AUTO_CLOSE_DELAY_MS);
}
