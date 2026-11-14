/**
 * Pure helpers for the home-page live demo (live-demo.js) and its capture worklet.
 * No DOM access here, so the logic can be tested in Node.
 */

export const TARGET_SAMPLE_RATE = 16000;
/** Samples per frame sent to the server: 100 ms at 16 kHz. */
export const FRAME_SAMPLES = 1600;

/**
 * Box-filter downsampler from the browser's capture rate to 16 kHz mono int16.
 * Averaging the input samples that fall in each output slot is a cheap low-pass,
 * enough to keep speech recognisable without aliasing hiss.
 *
 * @param {number} inputRate
 * @param {(frame: Int16Array) => void} onFrame called with each full 100 ms frame
 */
export function createDownsampler(inputRate, onFrame) {
  const ratio = inputRate / TARGET_SAMPLE_RATE;
  let phase = 0;
  let acc = 0;
  let count = 0;
  let frame = new Int16Array(FRAME_SAMPLES);
  let filled = 0;

  function emit(value) {
    const clamped = Math.max(-1, Math.min(1, value));
    frame[filled++] = clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff;
    if (filled === FRAME_SAMPLES) {
      onFrame(frame);
      frame = new Int16Array(FRAME_SAMPLES);
      filled = 0;
    }
  }

  return {
    /** @param {Float32Array} samples */
    push(samples) {
      for (let i = 0; i < samples.length; i++) {
        acc += samples[i];
        count++;
        phase += 1;
        if (phase >= ratio) {
          phase -= ratio;
          emit(acc / count);
          acc = 0;
          count = 0;
        }
      }
    },
  };
}

/** RMS of an int16 frame scaled to 0..1, for the mic level ring. */
export function frameLevel(frame) {
  if (!frame.length) return 0;
  let sum = 0;
  for (let i = 0; i < frame.length; i++) sum += frame[i] * frame[i];
  return Math.min(1, Math.sqrt(sum / frame.length) / 6000);
}

/**
 * Toggle a language in the target list, keeping the order chosen and the limit.
 * The language being spoken is never a target.
 *
 * @returns {string[]} the new list (unchanged when the limit is reached)
 */
export function toggleTarget(targets, code, source, max) {
  if (code === source) return targets;
  if (targets.includes(code)) return targets.filter((c) => c !== code);
  if (targets.length >= max) return targets;
  return [...targets, code];
}

/**
 * The language to speak aloud: keep the current one if it is still a voiced target,
 * otherwise the first voiced target, otherwise none.
 */
export function pickListen(current, targets, voiced) {
  if (current && targets.includes(current) && voiced.has(current)) return current;
  return targets.find((code) => voiced.has(code)) || null;
}

/** "English → Korean · Japanese" */
export function routeSummary(source, targets, names) {
  const name = (code) => names.get(code) || code.toUpperCase();
  return { from: name(source), to: targets.map(name).join(' · ') || 'choose languages' };
}

/** Lines to read aloud, for visitors who don't know what to say. English only. */
export const SCRIPTS = Object.freeze({
  free: {
    label: 'Free talk',
    lines: [
      "Today I'd like to share an idea that's been on my mind.",
      'Tell us where you are and what the weather is like.',
      'Describe the best meal you have had this year.',
      'What is one thing you would like to learn next?',
    ],
  },
  opening: {
    label: 'Event opening',
    lines: [
      'Good morning, everyone — welcome, and thank you for being here.',
      'Before we begin, please take a moment to find your seat.',
      'Interpretation is available on your phone in several languages.',
      'Our first speaker will join us on stage in five minutes.',
    ],
  },
  launch: {
    label: 'Product launch',
    lines: [
      'We are excited to show you what our team has been building.',
      'It helps people at events follow every talk in their own language.',
      'It is available today, and you can try it for free.',
      'Thank you, and we look forward to your questions.',
    ],
  },
  lecture: {
    label: 'Classroom lecture',
    lines: [
      "Let's pick up where we left off in last week's lecture.",
      'Please open your notes to the second chapter.',
      'This idea will come up again in the final exam.',
      'Are there any questions before we take a short break?',
    ],
  },
});

/** Messages the server sends on refusal, keyed by its error code. */
export const CLOSE_MESSAGES = Object.freeze({
  4003: 'This demo only works from the VoxBento site.',
  1013: 'The demo is busy with other visitors. Try again in a minute.',
});

/** Text shown when a run ends on its own. */
export function doneMessage(reason, maxSeconds) {
  if (reason === 'time_limit') return `That's the ${maxSeconds}-second limit for one try. Press the mic to go again.`;
  if (reason === 'idle') return 'Stopped after a quiet spell. Press the mic to go again.';
  return '';
}

/** "1:05" */
export function formatClock(seconds) {
  const s = Math.max(0, Math.ceil(seconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

/**
 * Where glossary terms occur in a caption, for highlighting. Case-insensitive; the
 * longest term wins an overlap; Latin-script terms must be whole words.
 *
 * @param {string} text
 * @param {string[]} terms
 * @returns {[number, number][]} sorted, non-overlapping [start, end) ranges
 */
export function findTermRanges(text, terms) {
  const lower = text.toLowerCase();
  const found = [];
  const unique = [...new Set(terms.map((t) => t.trim()).filter(Boolean))].sort((a, b) => b.length - a.length);
  for (const term of unique) {
    const needle = term.toLowerCase();
    let from = 0;
    while (from <= lower.length) {
      const at = lower.indexOf(needle, from);
      if (at < 0) break;
      const end = at + needle.length;
      if (isWordEdge(text, at - 1, term[0]) && isWordEdge(text, end, term[term.length - 1])
          && !found.some(([s, e]) => at < e && end > s)) {
        found.push([at, end]);
      }
      from = at + 1;
    }
  }
  return found.sort((a, b) => a[0] - b[0]);
}

const LATIN_WORD = /[\p{Script=Latin}\p{Script=Cyrillic}\p{Script=Greek}\d]/u;

function isWordEdge(text, index, termEdge) {
  // Scripts written without spaces (Chinese, Japanese, Thai) have no word edges to check.
  if (index < 0 || index >= text.length || !LATIN_WORD.test(termEdge)) return true;
  return !LATIN_WORD.test(text[index]);
}

/**
 * The terms to highlight in one row: source terms for the spoken language, and for a
 * target the translation of each entry that applies to it.
 *
 * @param {{term: string, lang: string, translation: string}[]} glossary
 * @param {string|null} target null for the row of the spoken language
 */
export function glossaryTerms(glossary, target) {
  if (!target) return glossary.map((e) => e.term);
  return glossary.filter((e) => e.lang === 'all' || e.lang === target).map((e) => e.translation || e.term);
}

/** Add a visitor glossary entry, replacing one for the same term and language. */
export function addGlossaryEntry(entries, entry, max) {
  const term = entry.term.trim().replace(/\s+/g, ' ');
  if (!term) return { entries, error: 'Type a term first.' };
  const next = {
    term,
    lang: entry.lang || 'all',
    translation: (entry.translation || '').trim().replace(/\s+/g, ' '),
  };
  const rest = entries.filter((e) => !(e.term.toLowerCase() === term.toLowerCase() && e.lang === next.lang));
  if (rest.length >= max) return { entries, error: `You can add up to ${max} entries.` };
  return { entries: [...rest, next], error: '' };
}
