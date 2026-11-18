// Plain ES module for cross-tab auth synchronization via BroadcastChannel.
// Follows JS instructions: ES module (not IIFE), external script, no empty catch blocks.

if (typeof BroadcastChannel !== 'undefined') {
  try {
    const authChannel = new BroadcastChannel('voxbento_auth');
    authChannel.postMessage('logged_in');
    authChannel.close();
  } catch (err) {
    console.debug('Failed to post auth sync message:', err);
  }
}

// Clear ephemeral auth_sync cookie if present
try {
  document.cookie = 'auth_sync=; Max-Age=0; path=/;';
} catch (err) {
  console.debug('Failed to clear auth_sync cookie:', err);
}

// Clean auth_sync parameter from URL without full page reload
try {
  const cleanUrl = new URL(window.location.href);
  if (cleanUrl.searchParams.has('auth_sync')) {
    cleanUrl.searchParams.delete('auth_sync');
    const newSearch = cleanUrl.search ? cleanUrl.search : '';
    window.history.replaceState({}, document.title, cleanUrl.pathname + newSearch + cleanUrl.hash);
  }
} catch (err) {
  console.debug('Failed to clean auth_sync param from URL:', err);
}
