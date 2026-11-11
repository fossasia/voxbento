// Room page (owner view): tabs, the room audio chooser and the AI settings pickers.
// Values the page needs come from data attributes on #room-detail (no inline script).

import { initTabs } from './tabs.js';

const root = document.getElementById('room-detail');

// ── Tabs ────────────────────────────────────────────────────────────────────────────
// Shared with the booth page (tabs.js). The open tab is remembered per room, so saving
// a setting (which reloads the page) returns to the same tab.

function openingRoomTab(hash) {
  const params = new URLSearchParams(window.location.search);
  let initial = hash;
  // Back from a vocabulary upload: show the result where it was uploaded.
  if (!initial && [...params.keys()].some((key) => key.startsWith('vocab'))) initial = 'ai';
  // Older links used other tab names.
  initial = { source: 'audio', captions: 'ai' }[initial] || initial;
  // Back from room setup: show the saved languages and the next steps.
  if (params.has('created') || params.has('updated')) initial = 'overview';
  return initial;
}

// ── Room audio ──────────────────────────────────────────────────────────
// One way of hearing the room is shown at a time. Choosing one only changes what is
// shown; each source's own buttons connect or disconnect it.

function initAudioChooser() {
  const chooser = document.querySelector('[data-audio-chooser]');
  if (!chooser) return;
  const radios = [...chooser.querySelectorAll('input[name="audio_choice"]')];
  const panels = [...document.querySelectorAll('[data-audio-panel]')];

  function sync() {
    const choice = radios.find((r) => r.checked)?.value || '';
    panels.forEach((panel) => { panel.hidden = panel.dataset.audioPanel !== choice; });
  }

  radios.forEach((radio) => radio.addEventListener('change', sync));
  sync();
}

// ── AI settings ─────────────────────────────────────────────────────────

function initAiSettings() {
  const speechProvider = document.getElementById('floor_transcription_provider');
  const speechModel = document.getElementById('floor_transcription_model');
  if (speechProvider && speechModel) {
    const options = [...speechModel.options];
    const filterModels = () => {
      const visible = options.filter((opt) => opt.dataset.provider === speechProvider.value);
      options.forEach((opt) => {
        opt.hidden = !visible.includes(opt);
        opt.disabled = !visible.includes(opt);
      });
      if (!visible.some((opt) => opt.selected) && visible.length) visible[0].selected = true;
      const modelDetails = speechModel.closest('details');
      if (modelDetails) modelDetails.hidden = visible.length < 2;
    };
    speechProvider.addEventListener('change', filterModels);
    filterModels();
  }

  const transProvider = document.getElementById('floor_translation_provider');
  const transModel = document.getElementById('floor_translation_model');
  if (transProvider && transModel) {
    const savedProvider = transProvider.value;
    const savedModel = root.dataset.initialTranslationModel || '';
    const localNote = document.querySelector('[data-local-translation-note]');
    let catalog = null;
    let loads = 0;

    async function loadModels() {
      const load = ++loads;
      const provider = transProvider.value;
      if (localNote) localNote.hidden = provider !== 'local';
      transModel.replaceChildren(new Option('Recommended for this service', ''));
      if (!provider) return;
      try {
        // Only a list that loaded is kept, so a failed load tries again on the next change.
        if (!catalog) {
          const res = await fetch('/api/admin/providers/translation/models');
          if (res.ok) catalog = await res.json();
          else console.error('Could not load translation models', res.status);
        }
      } catch (err) {
        console.error('Could not load translation models', err);
      }
      // The service may have changed while the list was loading; only the latest call fills the list.
      if (load !== loads) return;
      const listed = catalog?.[provider];
      const models = [...(listed || [])];
      const keep = provider === savedProvider ? savedModel : '';
      // Cloud services accept any model name, but a self-hosted model must be on the server's list.
      // If the list didn't load, keep the saved model and let the server check it on save.
      const mustBeListed = provider === 'self_hosted' && Boolean(listed);
      if (keep && !models.includes(keep) && !mustBeListed) models.unshift(keep);
      models.forEach((model) => transModel.append(new Option(model, model, false, model === keep)));
    }

    transProvider.addEventListener('change', loadModels);
    loadModels();
  }

  const ttsProvider = document.getElementById('floor_tts_provider');
  if (ttsProvider) {
    const voiceGroup = document.getElementById('tts_voice_group');
    const deepgramHelp = document.getElementById('tts_provider_help_deepgram');
    const syncVoice = () => {
      const supertonic = ttsProvider.value === 'supertonic';
      if (voiceGroup) voiceGroup.hidden = !supertonic;
      if (deepgramHelp) deepgramHelp.hidden = ttsProvider.value !== 'deepgram';
    };
    ttsProvider.addEventListener('change', syncVoice);
    syncVoice();
  }
}

if (root) {
  initTabs({
    name: 'room',
    storageKey: `voxbento:room-tab:${root.dataset.roomId}`,
    resolveInitial: openingRoomTab,
    fallback: 'overview',
  });
}
initAudioChooser();
initAiSettings();
