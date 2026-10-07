/**
 * Room settings: list the voices of the selected TTS provider and show that
 * provider's help text. The voices per provider come from the select's
 * data-voices attribute; data-current-voice is the saved voice, if any.
 */

export function initTtsVoicePicker() {
  const providerSelect = document.getElementById('floor_tts_provider');
  const voiceSelect = document.getElementById('floor_tts_voice');
  if (!providerSelect || !voiceSelect) return;

  const helpDeepgram = document.getElementById('tts_provider_help_deepgram');
  const helpSupertonic = document.getElementById('tts_provider_help_supertonic');

  let voicesByProvider = {};
  try {
    voicesByProvider = JSON.parse(voiceSelect.dataset.voices || '{}');
  } catch (err) {
    console.error('Invalid TTS voice list', err);
  }

  function updateVoices() {
    const provider = providerSelect.value;
    const currentVoice = voiceSelect.dataset.currentVoice || '';

    if (helpDeepgram) helpDeepgram.style.display = provider === 'deepgram' ? 'block' : 'none';
    if (helpSupertonic) helpSupertonic.style.display = provider === 'deepgram' ? 'none' : 'block';

    const auto = document.createElement('option');
    auto.value = '';
    auto.textContent = '(Model will pick best voice)';
    voiceSelect.replaceChildren(auto);

    (voicesByProvider[provider] || []).forEach((voice) => {
      const option = document.createElement('option');
      option.value = voice.id;
      option.textContent = voice.name;
      option.selected = voice.id === currentVoice;
      voiceSelect.appendChild(option);
    });
  }

  providerSelect.addEventListener('change', updateVoices);
  updateVoices();
}
