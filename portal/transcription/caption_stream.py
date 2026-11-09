"""Pure caption buffering: raw provider text in, caption events out (#352).

This module decides *what* the subtitle line should say — sentence buffering,
the 5-word split boundary, the 15-second and 50-word forced finalisation. It
performs no I/O: no database, no broadcast, no translation, no TTS. The caller
(`portal.transcription.aggregator.CaptionAggregator`) iterates the events and
applies those side effects at the seam.

Keeping the two apart means a change to subtitle formatting cannot break
translation routing for a live event, and the buffering rules can be tested
without mocking a database or a worker queue.
"""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

# Forced finalisation: a speaker who never pauses still gets a readable line.
MAX_UTTERANCE_SECONDS = 15.0
MAX_UTTERANCE_WORDS = 50

# A sentence shorter than this keeps buffering, so "Hi there." does not become
# its own caption while "Thank you so much everyone." does.
MIN_SENTENCE_WORDS = 5

_SENTENCE_RE = re.compile(r"([^.?!]*[.?!]+)(?:\s+|$)")


@dataclass(frozen=True)
class PartialCaption:
    """In-progress text; replaces whatever the viewer currently sees."""

    booth_id: str
    text: str
    segment_id: str


@dataclass(frozen=True)
class FinalCaption:
    """A settled line. The caller persists, translates and speaks this."""

    booth_id: str
    text: str
    segment_id: str
    seq: int
    # Wall-clock start of the utterance, for the caller's timing metadata.
    utterance_start_time: float


@dataclass(frozen=True)
class ClearCaption:
    """Nothing is being spoken; the viewer's partial line should empty."""

    booth_id: str


CaptionEvent = PartialCaption | FinalCaption | ClearCaption


@dataclass
class CaptionState:
    booth_id: str
    current_utterance: str = ""
    utterance_start_time: float = 0.0
    current_word_count: int = 0
    segment_id: str = field(default_factory=lambda: str(uuid.uuid4()))


class CaptionStream:
    """Per-booth caption buffers. Synchronous and side-effect free."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
        new_segment_id: Callable[[], str] = lambda: str(uuid.uuid4()),
    ) -> None:
        self.states: dict[str, CaptionState] = {}
        self._clock = clock
        self._new_segment_id = new_segment_id
        self._seq_counter = 0

    def state_for(self, booth_id: str) -> CaptionState:
        if booth_id not in self.states:
            self.states[booth_id] = CaptionState(booth_id=booth_id, segment_id=self._new_segment_id())
        return self.states[booth_id]

    def restore(self, event: FinalCaption) -> None:
        """Put a settled utterance back because the caller could not publish it.

        `_finalise` clears the buffer as soon as it yields the event, so without
        this a failed publish would drop the caption entirely and the next
        silence would emit only a clear.

        If the buffer is still empty, the restore is a full one: text, id and
        start time all come back. If something else is already in flight — a
        chunk remainder from the same fragment, or a newer partial from a
        provider that interleaves — the settled text is prepended but the
        buffer's own id and start time are kept. The newer utterance's
        identity is not ours to overwrite, which would make the next final
        publish the newer text under the restored one's id and timing; letting
        the restored sentence ride out under the newer id instead carries no
        corruption and loses no words.
        """
        state = self.state_for(event.booth_id)
        tail = state.current_utterance
        if tail:
            state.current_utterance = f"{event.text} {tail}"
            state.current_word_count = len(state.current_utterance.split())
            # Keep the buffer's id (the newer utterance's identity is not ours
            # to overwrite) but take the earlier start time, so program-playback
            # timing stays pinned to when the restored speech began. The two
            # times being equal is a no-op.
            state.utterance_start_time = min(state.utterance_start_time, event.utterance_start_time)
            return
        state.current_utterance = event.text
        state.utterance_start_time = event.utterance_start_time
        state.current_word_count = len(event.text.split())
        state.segment_id = event.segment_id

    def _is_overdue(self, state: CaptionState) -> bool:
        return (
            state.current_word_count >= MAX_UTTERANCE_WORDS
            or (self._clock() - state.utterance_start_time) >= MAX_UTTERANCE_SECONDS
        )

    def _finalise(self, state: CaptionState, text: str) -> list[CaptionEvent]:
        """Settle *text* and reset the booth's buffer for the next utterance."""
        final_text = text.strip() or state.current_utterance
        if not final_text:
            return []

        self._seq_counter += 1
        event = FinalCaption(
            booth_id=state.booth_id,
            text=final_text,
            segment_id=state.segment_id,
            seq=self._seq_counter,
            utterance_start_time=state.utterance_start_time or self._clock(),
        )

        state.current_utterance = ""
        state.utterance_start_time = 0.0
        state.current_word_count = 0
        state.segment_id = self._new_segment_id()
        return [event]

    def partial(self, booth_id: str, text: str) -> list[CaptionEvent]:
        """Handle an interim hypothesis, which replaces the whole buffer."""
        if not text.strip():
            return []

        state = self.state_for(booth_id)
        if not state.current_utterance:
            state.utterance_start_time = self._clock()
        state.current_utterance = text
        state.current_word_count = len(text.split())

        if self._is_overdue(state):
            return self._finalise(state, text)
        return [PartialCaption(booth_id, state.current_utterance, state.segment_id)]

    def chunk(self, booth_id: str, text: str) -> list[CaptionEvent]:
        """Handle an already-final fragment, appended and split on punctuation.

        Used by local Whisper, which returns settled text in pieces rather than
        revising a running hypothesis.
        """
        if not text.strip():
            return []

        state = self.state_for(booth_id)
        if not state.current_utterance:
            state.utterance_start_time = self._clock()
            state.current_utterance = text.strip()
        else:
            state.current_utterance += " " + text.strip()

        events: list[CaptionEvent] = []
        settled = False
        split_point = self._first_sentence_end(state.current_utterance)
        if split_point is not None:
            sentence = state.current_utterance[:split_point].strip()
            remainder = state.current_utterance[split_point:].strip()
            if sentence:
                events.extend(self._finalise(state, sentence))
                settled = True
            # _finalise cleared the buffer, so carry the tail across.
            state.current_utterance = remainder
            # The remainder is the start of a new utterance; begin its clock now
            # rather than inheriting the previous accumulation's start time.
            if settled and remainder:
                state.utterance_start_time = self._clock()

        state.current_word_count = len(state.current_utterance.split()) if state.current_utterance else 0

        if state.current_utterance:
            # Force a cut if accumulation has run past 50 words or 15 seconds
            # without punctuation. Without this the chunk path could buffer an
            # entire talk if a speaker went without a sentence boundary.
            if self._is_overdue(state):
                events.extend(self._finalise(state, state.current_utterance))
                events.append(ClearCaption(booth_id))
            else:
                events.append(PartialCaption(booth_id, state.current_utterance, state.segment_id))
        elif events:
            # A sentence was settled and nothing is left, so empty the partial
            # line. The previous code then re-tested forced finalisation here,
            # which could only ever call _finalise with an empty buffer and
            # return nothing; that unreachable tail is dropped.
            state.utterance_start_time = 0.0
            events.append(ClearCaption(booth_id))
        return events

    def _first_sentence_end(self, utterance: str) -> int | None:
        """Offset after the first sentence long enough to stand on its own."""
        for match in _SENTENCE_RE.finditer(utterance):
            if len(utterance[: match.end()].strip().split()) >= MIN_SENTENCE_WORDS:
                return match.end()
        return None

    def final(self, booth_id: str, text: str) -> list[CaptionEvent]:
        """Settle the buffer because the provider reported end of speech."""
        return self._finalise(self.state_for(booth_id), text)

    def clear(self, booth_id: str) -> list[CaptionEvent]:
        """Silence was detected. Settle a pending partial rather than lose it."""
        state = self.state_for(booth_id)
        if state.current_utterance:
            return self._finalise(state, state.current_utterance)
        return [ClearCaption(booth_id)]
