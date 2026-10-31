// Floor audio sources the program sender can publish.

export const SOURCES = Object.freeze({DEVICE: 'device', TAB: 'tab'});

/** Program audio must bypass voice DSP, which pumps levels and hurts music and STT. */
export const RAW_AUDIO = Object.freeze({echoCancellation: false, noiseSuppression: false, autoGainControl: false});

/** getDisplayMedia options that offer browser tabs with their audio and keep it audible locally. */
export function tabCaptureOptions() {
  return {
    // Chromium requires a video request; the sender stops the video track immediately.
    video: true,
    audio: {...RAW_AUDIO, suppressLocalAudioPlayback: false},
    selfBrowserSurface: 'exclude',
    surfaceSwitching: 'include',
    systemAudio: 'exclude',
    monitorTypeSurfaces: 'exclude',
  };
}

export function normalizeSource(value) {
  return value === SOURCES.TAB ? SOURCES.TAB : SOURCES.DEVICE;
}

export function tabCaptureSupported(mediaDevices) {
  return Boolean(mediaDevices && typeof mediaDevices.getDisplayMedia === 'function');
}

/** The single audio track to publish, or null when the user did not share tab audio. */
export function pickTabAudio(stream) {
  stream.getVideoTracks().forEach(track => track.stop());
  return stream.getAudioTracks()[0] || null;
}
