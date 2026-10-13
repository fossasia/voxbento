/**
 * Admin panel client-side utilities.
 * Loaded as an ES module — no jQuery, no inline scripts.
 */

import { initLocalModelDownloader } from './download-model.js';

function managementPrefix() {
  return document.body.dataset.managementPrefix || '/admin';
}

/**
 * Shows a short-lived toast and announces it via the aria-live region in
 * admin/base.html, so both sighted and assistive-tech users get the same
 * feedback.
 */
function showToast(message, kind) {
  const container = document.getElementById('toast-container');
  if (!container) return;
  const toast = document.createElement('div');
  toast.className = `toast toast--${kind}`;
  toast.textContent = message;
  container.appendChild(toast);
  setTimeout(() => { toast.remove(); }, 3000);
}

/**
 * Copies text via navigator.clipboard where available (secure contexts only),
 * falling back to a hidden textarea + execCommand('copy') otherwise.
 * @throws {Error} when neither copy method is available or succeeds.
 */
async function copyText(text) {
  if (navigator.clipboard) {
    try {
      await navigator.clipboard.writeText(text);
      return;
    } catch (error) {
      // navigator.clipboard can exist but still reject (e.g. a permissions-policy
      // block), so fall through to the execCommand path below instead of giving up.
      console.error('navigator.clipboard.writeText rejected, trying execCommand fallback', error);
    }
  }
  // navigator.clipboard is undefined outside secure contexts (plain http on a LAN host).
  // execCommand is deprecated but still the only synchronous fallback for that case.
  const textarea = document.createElement('textarea');
  textarea.value = text;
  textarea.style.position = 'fixed';
  textarea.style.opacity = '0';
  document.body.appendChild(textarea);
  textarea.focus();
  textarea.select();
  let copied = false;
  try {
    copied = document.execCommand('copy');
  } finally {
    textarea.remove();
  }
  if (!copied) {
    throw new Error('execCommand("copy") did not succeed');
  }
}

// Per-button original label and pending reset timer, so repeated clicks on the
// same Copy button restore the true original label instead of whatever
// transient text ("Copied!") happened to be showing at the time of the click.
const copyOriginalLabels = new WeakMap();
const copyResetTimers = new WeakMap();

async function copyToClipboard(targetId, btn) {
  const el = document.getElementById(targetId);
  if (!el) return;
  if (!copyOriginalLabels.has(btn)) {
    copyOriginalLabels.set(btn, btn.textContent);
  }
  const orig = copyOriginalLabels.get(btn);
  clearTimeout(copyResetTimers.get(btn));

  const text = (el instanceof HTMLInputElement ? el.value : el.textContent).trim();
  const fullUrl = text.startsWith('/') ? window.location.origin + text : text;
  try {
    await copyText(fullUrl);
  } catch (error) {
    console.error(`Failed to copy #${targetId} to the clipboard`, error);
    // Select the source field so the admin can copy manually as a last resort.
    if (el instanceof HTMLInputElement) {
      el.select();
    }
    showToast('Could not copy the link. Select the text and copy it manually.', 'error');
    return;
  }
  btn.textContent = 'Copied!';
  copyResetTimers.set(btn, setTimeout(() => { btn.textContent = orig; }, 1500));
  showToast('Link copied to clipboard.', 'success');
}

document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.btn-copy[data-copy-target]').forEach((btn) => {
    btn.addEventListener('click', () => {
      copyToClipboard(btn.dataset.copyTarget, btn);
    });
  });
  initCustomModal();
  initLocalModelDownloader();
  initAsyncSave();
});

const FUNNY_WARNINGS = [
  "Are you sure? We can't undo this, but we can judge you.",
  "Warning: The intern will probably cry if you do this.",
  "We're deleting this forever. And forever is a very long time.",
  "Think of the bytes! Oh the humanity...",
  "Are you absolutely sure? The databases are getting nervous.",
  "There is no 'Ctrl+Z' for this. Proceed with caution.",
  "Deleting this is like dropping your ice cream. Tragic.",
  "Just double checking. My anxiety acts up around delete buttons."
];

function initAsyncSave() {
  document.querySelectorAll('form').forEach(form => {
    const submitBtn = form.querySelector('button[type="submit"]');
    if (submitBtn && submitBtn.textContent.trim() === 'Save Settings') {
      if (form.dataset.asyncSaveInitialized) return;
      form.dataset.asyncSaveInitialized = 'true';
      form.addEventListener('submit', async (e) => {
        if (form.hasAttribute('data-confirm')) return; 
        
        e.preventDefault();
        
        const originalText = submitBtn.textContent;
        const originalWidth = submitBtn.offsetWidth;
        
        if (originalWidth > 0) {
          submitBtn.style.minWidth = originalWidth + 'px';
        }
        
        submitBtn.disabled = true;
        submitBtn.textContent = 'Saving...';
        
        try {
          const formData = new FormData(form);
          const params = new URLSearchParams();
          for (const [key, value] of formData.entries()) {
             params.append(key, value);
          }
          
          const response = await fetch(form.action || window.location.href, {
            method: form.method || 'POST',
            body: params,
            headers: {
              'Content-Type': 'application/x-www-form-urlencoded'
            },
            redirect: 'follow'
          });
          
          const isLoginRedirect = response.redirected && response.url.includes('/login');
          if (response.ok && !isLoginRedirect) {
            submitBtn.textContent = 'Saved ✓';
            submitBtn.classList.remove('btn-primary');
            submitBtn.classList.add('btn-success');
            
            setTimeout(() => {
              window.location.reload();
            }, 600);
          } else {
             submitBtn.disabled = false;
             submitBtn.textContent = originalText;
             alert('Failed to save settings.');
          }
        } catch (err) {
          submitBtn.disabled = false;
          submitBtn.textContent = originalText;
          console.error('Save failed:', err);
          alert('Failed to save settings. Check console for details.');
        }
      });
    }
  });
}

function initCustomModal() {
  const modalOverlay = document.getElementById('custom-confirm-modal');
  const messageEl = document.getElementById('custom-confirm-message');
  const funnyEl = document.getElementById('custom-confirm-funny');
  const btnCancel = document.getElementById('custom-confirm-cancel');
  const btnOk = document.getElementById('custom-confirm-ok');

  if (!modalOverlay) return;

  let pendingForm = null;

  function closeModal() {
    modalOverlay.classList.remove('active');
    pendingForm = null;
    btnOk.disabled = false;
    btnCancel.disabled = false;
  }

  function openModal(message, formElement) {
    messageEl.textContent = message;
    const randomFunny = FUNNY_WARNINGS[Math.floor(Math.random() * FUNNY_WARNINGS.length)];
    funnyEl.textContent = randomFunny;
    pendingForm = formElement;
    btnOk.disabled = false;
    btnCancel.disabled = false;

    // Force reflow before activating so the CSS transition plays
    void modalOverlay.offsetWidth;
    modalOverlay.classList.add('active');
  }

  btnCancel.addEventListener('click', closeModal);
  modalOverlay.addEventListener('click', (e) => {
    if (e.target === modalOverlay) closeModal();
  });

  btnOk.addEventListener('click', () => {
    if (pendingForm && !btnOk.disabled) {
      btnOk.disabled = true;
      btnCancel.disabled = true;
      pendingForm.submit();
    }
  });

  // Intercept all forms with data-confirm
  document.querySelectorAll('form[data-confirm]').forEach(form => {
    form.addEventListener('submit', (e) => {
      e.preventDefault();
      openModal(form.dataset.confirm, form);
    });
  });
}

// ---------------------------------------------------------------------------
// API Keys (Embeddable B2B)
// ---------------------------------------------------------------------------

window.adminAPIKeys = {
  eventId: null,
  
  init() {
    const section = document.getElementById('api-keys-section');
    if (!section) return;
    
    this.eventId = section.dataset.eventId;
    this.loadKeys();

    const form = document.getElementById('api-key-form');
    if (form) {
      form.addEventListener('submit', (e) => {
        e.preventDefault();
        this.generateKey();
      });
    }

    const container = document.getElementById('api-keys-container');
    if (container) {
      container.addEventListener('click', (e) => {
        const btn = e.target.closest('.revoke-key-btn');
        if (btn) {
          this.revokeKey(btn.dataset.keyId, btn.dataset.keyName);
        }
      });
    }
  },

  async loadKeys() {
    const container = document.getElementById('api-keys-container');
    if (!container || !this.eventId) return;

    try {
      const res = await fetch(`${managementPrefix()}/api/events/${this.eventId}/api-keys`);
      if (!res.ok) throw new Error('Failed to load keys');
      const keys = await res.json();
      
      const activeKeys = keys.filter(k => k.active !== false);
      
      if (activeKeys.length === 0) {
        container.innerHTML = '<p class="muted">No API keys generated yet.</p>';
        return;
      }

      let html = '<table class="data-table compact" style="margin-top: 12px;">';
      html += '<thead><tr><th>Name</th><th>Key Preview</th><th>Created</th><th>Actions</th></tr></thead><tbody>';
      
      activeKeys.forEach(k => {
        const d = new Date(k.created_at).toLocaleDateString();
        
        const escapeHTML = str => str.replace(/[&<>'"]/g, 
          tag => ({
              '&': '&amp;',
              '<': '&lt;',
              '>': '&gt;',
              "'": '&#39;',
              '"': '&quot;'
          }[tag]));
        
        const safeName = k.name ? escapeHTML(k.name) : 'Unnamed';
        const displayName = k.name ? escapeHTML(k.name) : '<em>Unnamed</em>';
        html += `
          <tr>
            <td>${displayName}</td>
            <td><code>${k.preview}</code></td>
            <td>${d}</td>
            <td><button class="btn btn-sm btn-danger revoke-key-btn" data-key-id="${k.id}" data-key-name="${safeName}">Revoke</button></td>
          </tr>
        `;
      });
      html += '</tbody></table>';
      container.innerHTML = html;
    } catch (err) {
      container.innerHTML = '<p class="text-danger">Failed to load API keys.</p>';
      console.error(err);
    }
  },

  showModal(id) {
    const modal = document.getElementById(id);
    if (!modal) return;
    
    if (id === 'api-key-modal') {
      document.getElementById('api-key-name').value = '';
    }
    
    modal.style.display = 'flex';
    void modal.offsetWidth; // force reflow
    modal.classList.add('active');
  },

  showCreateModal() {
    this.showModal('api-key-modal');
  },
  
  closeModal(id) {
    const modal = document.getElementById(id);
    if (!modal) return;
    
    modal.classList.remove('active');
    setTimeout(() => {
      modal.style.display = 'none';
    }, 200);
  },

  async generateKey() {
    const nameInput = document.getElementById('api-key-name').value;
    const btn = document.querySelector('#api-key-form button[type="submit"]');
    
    try {
      btn.disabled = true;
      const res = await fetch(`${managementPrefix()}/api/events/${this.eventId}/api-keys`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: nameInput })
      });
      
      if (!res.ok) {
        const errData = await res.json().catch(() => ({}));
        throw new Error(errData.detail || 'Failed to generate');
      }
      
      const data = await res.json();
      
      this.closeModal('api-key-modal');
      
      const valEl = document.getElementById('api-key-raw-value');
      valEl.value = data.raw_key;
      
      const copyBtn = document.getElementById('api-key-copy-btn');
      if (copyBtn) {
        copyBtn.textContent = 'Copy';
      }
      
      this.showModal('api-key-success-modal');
      
      this.loadKeys();
    } catch (err) {
      alert(err.message);
    } finally {
      btn.disabled = false;
    }
  },

  async copyKey() {
    const valEl = document.getElementById('api-key-raw-value');
    try {
      await navigator.clipboard.writeText(valEl.value);
      const copyBtn = document.getElementById('api-key-copy-btn');
      if (copyBtn) {
        copyBtn.innerHTML = '&#10003; Copied';
      }
    } catch (err) {
      console.error('Failed to copy', err);
    }
  },

  revokeKeyId: null,

  revokeKey(keyId, keyName) {
    this.revokeKeyId = keyId;
    
    const msgEl = document.getElementById('api-key-revoke-message');
    if (msgEl) {
      const text = `Are you sure you want to revoke the API key '${keyName}'? Any integrations using it will instantly break.`;
      msgEl.textContent = text;
    }
    
    const funnyEl = document.getElementById('api-key-revoke-funny');
    if (funnyEl && typeof FUNNY_WARNINGS !== 'undefined') {
      const randomQuote = FUNNY_WARNINGS[Math.floor(Math.random() * FUNNY_WARNINGS.length)];
      funnyEl.textContent = randomQuote;
    }
    
    this.showModal('api-key-revoke-modal');
    const confirmBtn = document.getElementById('api-key-revoke-confirm-btn');
    if (confirmBtn) {
      confirmBtn.onclick = () => this.confirmRevokeKey();
    }
  },

  async confirmRevokeKey() {
    if (!this.revokeKeyId) return;
    
    try {
      const res = await fetch(`${managementPrefix()}/api/events/${this.eventId}/api-keys/${this.revokeKeyId}`, {
        method: 'DELETE'
      });
      if (!res.ok) throw new Error('Failed to revoke');
      
      this.closeModal('api-key-revoke-modal');
      
      this.loadKeys();
    } catch (err) {
      alert('Error revoking key: ' + err.message);
    } finally {
      this.revokeKeyId = null;
    }
  }
};

document.addEventListener('DOMContentLoaded', () => {
  if (window.adminAPIKeys) {
    window.adminAPIKeys.init();
  }
});
