from __future__ import annotations

import re
import unicodedata

# Whisper invents speech from silence, breath and room noise. The decoder-level
# guards in the local provider (compression ratio, no-speech and log-prob
# thresholds) reduce it but still let short confident phrases through, so the
# text is checked once more before it reaches listeners, the transcript, the
# translation LLM and TTS.

# A longest-word cap catches encoding garbage and runs of concatenated tokens.
# Compounding languages do produce words this long, so the rare long compound is
# dropped to keep garbage out; raise this if a deployment interprets German or
# Finnish and loses real words to it.
MAX_WORD_LENGTH = 40

# Phrases Whisper emits over near-silence, mostly learned from subtitle training
# data. Compared after case folding and stripping punctuation, and only when
# they make up the whole utterance, so the same words inside real speech survive.
_HALLUCINATION_PHRASES = frozenset(
    {
        "thank you",
        "thank you very much",
        "thanks for watching",
        "thank you for watching",
        "please subscribe",
        "subscribe to my channel",
        "like and subscribe",
        "music",
        "applause",
        "silence",
        "blank audio",
        "inaudible",
        "foreign",
        "you",
        "yeah",
        "okay",
        "ok",
        "hmm",
        "uh",
        "um",
        "ah",
        "oh",
        "mm",
        "hm",
        "bye",
    }
)

# A single token repeated with nothing else ("Click click click", "Cough cough").
# Two occurrences are enough: real speech rarely finalises as one word twice.
_MIN_REPEATS = 2

# Letters and digits, dropping punctuation and underscores. Digits are kept so
# that distinct tokens stay distinct: stripping them would collapse "room 1
# room 2" into one repeated word.
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)


def _normalise(text: str) -> str:
    """Case-fold and drop punctuation so phrase matching ignores styling."""
    folded = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(_WORD_RE.findall(folded))


def _is_repeated_single_word(words: list[str]) -> bool:
    return len(words) >= _MIN_REPEATS and len(set(words)) == 1


def has_no_printable_content(text: str) -> bool:
    """Whether the text carries no letters or digits at all.

    Mojibake and stray control characters arrive as punctuation or marks only,
    which would otherwise be published as a caption nobody can read.
    """
    return not any(ch.isalnum() for ch in text)


def has_overlong_word(text: str) -> bool:
    return any(len(word) > MAX_WORD_LENGTH for word in text.split())


def is_hallucination(text: str) -> bool:
    """Whether a finalised transcript looks invented rather than spoken."""
    stripped = text.strip()
    if not stripped:
        return True
    if has_no_printable_content(stripped):
        return True
    if has_overlong_word(stripped):
        return True

    normalised = _normalise(stripped)
    if not normalised:
        return True
    if normalised in _HALLUCINATION_PHRASES:
        return True
    return _is_repeated_single_word(normalised.split())
