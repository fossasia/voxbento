from __future__ import annotations

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

# Scripts written without spaces between words. A whole sentence in one of these
# is a single whitespace-delimited token, so the length cap would reject ordinary
# speech; tokens containing them are exempt from it.
_UNSPACED_RANGES = (
    (0x0E00, 0x0EFF),  # Thai, Lao
    (0x0F00, 0x0FFF),  # Tibetan
    (0x1000, 0x109F),  # Myanmar
    (0x1780, 0x17FF),  # Khmer
    (0x2E80, 0x9FFF),  # CJK radicals, kana, CJK unified ideographs
    (0xF900, 0xFAFF),  # CJK compatibility ideographs
    (0x20000, 0x323AF),  # CJK extensions B and later
)

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


def _is_unspaced(ch: str) -> bool:
    cp = ord(ch)
    return any(low <= cp <= high for low, high in _UNSPACED_RANGES)


def _keeps_meaning(ch: str) -> bool:
    """Whether a character carries word content rather than punctuation.

    Combining marks are kept: stripping them mangles Devanagari, Thai and Arabic
    into fragments and can collapse distinct words into one repeated token.
    """
    return ch.isalnum() or unicodedata.category(ch)[0] == "M"


def _normalise(text: str) -> str:
    """Case-fold and drop punctuation so phrase matching ignores styling."""
    folded = unicodedata.normalize("NFKC", text).casefold()
    kept = "".join(ch if _keeps_meaning(ch) else " " for ch in folded)
    return " ".join(kept.split())


def _is_repeated_single_word(words: list[str]) -> bool:
    return len(words) >= _MIN_REPEATS and len(set(words)) == 1


def has_no_printable_content(text: str) -> bool:
    """Whether the text carries no letters or digits at all.

    Mojibake and stray control characters arrive as punctuation or marks only,
    which would otherwise be published as a caption nobody can read.
    """
    return not any(ch.isalnum() for ch in text)


def has_overlong_word(text: str) -> bool:
    return any(
        len(word) > MAX_WORD_LENGTH and not any(_is_unspaced(ch) for ch in word)
        for word in text.split()
    )


def hallucination_reason(text: str, *, complete_turn: bool = True) -> str | None:
    """Why a finalised transcript looks invented, or None if it looks spoken.

    The reason names a category only. It is safe to log, unlike the transcript,
    which may carry what someone said in a private session.

    ``complete_turn`` is False for a recogniser result that finalises part of a
    longer turn, as Deepgram's ``is_final`` does before ``speech_final``. Such a
    fragment can legitimately be one short word, so only the structural checks
    apply to it.
    """
    stripped = text.strip()
    if not stripped:
        return "empty"
    if has_no_printable_content(stripped):
        return "no printable content"
    if has_overlong_word(stripped):
        return "overlong word"

    if not complete_turn:
        return None

    normalised = _normalise(stripped)
    if not normalised:
        return "no printable content"
    if normalised in _HALLUCINATION_PHRASES:
        return "subtitle phrase"
    if _is_repeated_single_word(normalised.split()):
        return "repeated word"
    return None


def is_hallucination(text: str, *, complete_turn: bool = True) -> bool:
    """Whether a finalised transcript looks invented rather than spoken."""
    return hallucination_reason(text, complete_turn=complete_turn) is not None
