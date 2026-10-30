// Plain ES module for the magic-link sign-in success page.
// Reads the OAuth redirect target from a data attribute so no inline script is needed.
// Follows JS instructions: ES module (not IIFE), external script, no empty catch blocks.

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
  } catch (err) {
    console.debug('getSafeRedirectUrl: could not parse URL', err);
  }
  return '';
}

function openAuthPopup(targetUrl) {
  if (!targetUrl) return null;
  try {
    const parsed = new URL(targetUrl, window.location.origin);
    if (parsed.origin !== window.location.origin || !parsed.pathname.startsWith('/oauth/authorize')) {
      console.error('Invalid targetUrl origin or path:', targetUrl);
      return null;
    }
  } catch (err) {
    console.error('Malformed targetUrl:', err);
    return null;
  }

  const width = 600;
  const height = 700;
  const left = window.screen.width ? Math.round((window.screen.width - width) / 2) : 100;
  const top = window.screen.height ? Math.round((window.screen.height - height) / 2) : 100;
  const features = `width=${width},height=${height},top=${top},left=${left},status=yes,toolbar=no,menubar=no,location=yes,resizable=yes,scrollbars=yes`;

  let popup = null;
  try {
    popup = window.open(targetUrl, 'voxbento_oauth', features);
    if (popup) {
      popup.focus();
    }
  } catch (err) {
    console.error('Error opening popup:', err);
  }
  return popup;
}

function initMagicSuccess() {
  const wrapper = document.getElementById('magic-success-wrapper');
  const channelName = (wrapper && wrapper.dataset.channel) || 'voxbento_auth';
  const rawNextUrl = wrapper ? wrapper.dataset.nextUrl : '';
  const targetUrl = getSafeRedirectUrl(rawNextUrl);

  let authChannel = null;
  if (typeof BroadcastChannel !== 'undefined') {
    try {
      authChannel = new BroadcastChannel(channelName);
      authChannel.postMessage('logged_in');
    } catch (err) {
      console.debug('BroadcastChannel initialisation failed:', err);
    }
  }

  if (authChannel && targetUrl) {
    authChannel.postMessage({ type: 'logged_in', url: targetUrl });
  }

  if (targetUrl) {
    const manualLink = document.getElementById('manual-redirect-link');
    if (manualLink) {
      manualLink.href = targetUrl;
      manualLink.addEventListener('click', (e) => {
        e.preventDefault();
        if (authChannel) {
          authChannel.postMessage({ type: 'logged_in', url: targetUrl });
          authChannel.postMessage('focus_popup');
        }
        openAuthPopup(targetUrl);
        try { window.close(); } catch (err) {
          console.debug('window.close() blocked by browser:', err);
        }
      });
    }

    if (authChannel) {
      authChannel.postMessage({ type: 'logged_in', url: targetUrl });
      authChannel.postMessage('focus_popup');
    }

    setTimeout(() => {
      try { window.close(); } catch (err) {
        console.debug('Deferred window.close() blocked by browser:', err);
      }
    }, 1500);
  }
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initMagicSuccess);
} else {
  initMagicSuccess();
}
