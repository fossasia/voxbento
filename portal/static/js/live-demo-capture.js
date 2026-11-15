/**
 * AudioWorklet for the home-page live demo: turns microphone audio into 100 ms
 * frames of 16 kHz int16 PCM and posts them to the page. It has no outputs, so
 * the visitor never hears their own microphone.
 */
import { createDownsampler } from './live-demo-core.js';

class LiveDemoCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    // `sampleRate` is the AudioWorkletGlobalScope's context rate.
    this.downsampler = createDownsampler(sampleRate, (frame) => {
      this.port.postMessage(frame.buffer, [frame.buffer]);
    });
  }

  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (channel) this.downsampler.push(channel);
    return true;
  }
}

registerProcessor('live-demo-capture', LiveDemoCapture);
