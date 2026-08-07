/**
 * Admin panel client-side utilities.
 * Loaded as an ES module — no jQuery, no inline scripts.
 */

import { initLocalModelDownloader } from './download-model.js';

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
  initCustomModal();
  initLocalModelDownloader();
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
  }

  function openModal(message, formElement) {
    messageEl.textContent = message;
    const randomFunny = FUNNY_WARNINGS[Math.floor(Math.random() * FUNNY_WARNINGS.length)];
    funnyEl.textContent = randomFunny;
    pendingForm = formElement;

    // Force reflow before activating so the CSS transition plays
    void modalOverlay.offsetWidth;
    modalOverlay.classList.add('active');
  }

  btnCancel.addEventListener('click', closeModal);
  modalOverlay.addEventListener('click', (e) => {
    if (e.target === modalOverlay) closeModal();
  });

  btnOk.addEventListener('click', () => {
    if (pendingForm) {
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
      const res = await fetch(`/admin/api/events/${this.eventId}/api-keys`);
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
      const res = await fetch(`/admin/api/events/${this.eventId}/api-keys`, {
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
      const res = await fetch(`/admin/api/events/${this.eventId}/api-keys/${this.revokeKeyId}`, {
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
