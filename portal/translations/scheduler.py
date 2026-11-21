"""One scheduler per translation engine, shared by every room that uses it (#1188).

An engine serves a fixed number of calls at once (CPU threads for a local model, a configured
limit for an API). The scheduler never has more than that in flight against it. Calls beyond
it wait here, where rooms take turns, and then the languages within a room, so one busy room
with many languages cannot hold the engine while a small room waits behind it.

Each channel (one source stream translated into one language, e.g. ``"<booth>:fr"``) keeps the
limits the worker had before: at most ``per_channel`` calls running and ``queue_limit`` jobs
admitted at once. A job is admitted when its segment arrives and counts against its channel
until its call is done.

A scheduler with a lag budget drops a job that cannot start within that many seconds of its
segment arriving (#1189): its language skips that segment and catches up with the speaker,
instead of falling further behind with every segment that waits.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict, deque
from collections.abc import Hashable

#: Calls one channel may have running at once.
PER_CHANNEL = 2
#: Jobs one channel may hold, waiting or running, before new segments are dropped.
QUEUE_LIMIT = 15


class StaleTranslationJob(Exception):
    """The job could not start within its scheduler's lag budget, so it was dropped."""

    def __init__(self, waited: float) -> None:
        super().__init__(f"waited {waited:.1f} s for the engine")
        self.waited = waited


class _Channel:
    __slots__ = ("admitted", "running", "waiters")

    def __init__(self) -> None:
        #: Jobs admitted and not yet done, whether waiting, running or not asking for a slot yet.
        self.admitted = 0
        self.running = 0
        self.waiters: deque[asyncio.Future[None]] = deque()


class TranslationJob:
    """One translation's place in its channel.

    ``async with job:`` waits for the job's turn and holds an engine slot until the block ends.
    The job leaves its channel when the block ends or when ``close()`` is called, whichever
    comes first, so a job that never reaches the engine must be closed. Entering raises
    ``StaleTranslationJob`` when the job cannot start within the scheduler's lag budget.
    """

    __slots__ = ("_scheduler", "_room", "_channel", "_state", "_busy_until", "_arrived_at")

    def __init__(
        self, scheduler: TranslationScheduler, room: Hashable, channel: str, arrived_at: float | None = None
    ) -> None:
        self._scheduler = scheduler
        self._room = room
        self._channel = channel
        # "admitted" -> "running" -> "closed", or straight from "admitted" to "closed".
        self._state = "admitted"
        self._busy_until: asyncio.Future | None = None
        #: When the segment arrived (``time.monotonic()``), or when the job was admitted if not given.
        self._arrived_at = time.monotonic() if arrived_at is None else arrived_at

    @property
    def waited(self) -> float:
        """Seconds since the job's segment arrived."""
        return time.monotonic() - self._arrived_at

    async def __aenter__(self) -> TranslationJob:
        if self._state != "admitted":
            raise RuntimeError("a translation job takes an engine slot only once")
        await self._scheduler._wait_for_turn(self)
        self._state = "running"
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        self.close()

    def keep_slot_until(self, call: asyncio.Future) -> None:
        """Keep the engine slot until ``call`` ends, even after the job itself has moved on.

        For a call the job stopped waiting for (it timed out) that the engine is still working on.
        """
        if self._state == "running" and not call.done():
            self._busy_until = call

    def close(self) -> None:
        """Leave the channel, giving back the engine slot if the job holds one. Safe to call twice."""
        if self._state == "closed":
            return
        running = self._state == "running"
        self._state = "closed"
        busy = self._busy_until
        if running and busy is not None and not busy.done():
            busy.add_done_callback(lambda _: self._scheduler._leave(self, running=True))
            return
        self._scheduler._leave(self, running=running)


class TranslationScheduler:
    """Bounded, fair access to one engine: rooms take turns, then languages within a room.

    ``lag_budget`` is the most seconds a job may wait between its segment arriving and starting;
    a job that would wait longer is dropped as stale. None (or 0) never drops a job for waiting.
    """

    def __init__(
        self,
        capacity: int,
        *,
        per_channel: int = PER_CHANNEL,
        queue_limit: int = QUEUE_LIMIT,
        lag_budget: float | None = None,
    ) -> None:
        if capacity < 1 or per_channel < 1 or queue_limit < 1:
            raise ValueError("capacity, per_channel and queue_limit must be at least 1")
        self.capacity = capacity
        self.per_channel = per_channel
        self.queue_limit = queue_limit
        self.lag_budget = lag_budget
        self.running = 0
        # Rooms, and the channels within each room, in turn order: the front is served next, and
        # whoever is served goes to the back. A room or channel with no jobs left is removed, so
        # when it comes back it joins at the back too, behind everyone already waiting.
        self._rooms: OrderedDict[Hashable, OrderedDict[str, _Channel]] = OrderedDict()

    def admit(self, room: Hashable, channel: str, *, arrived_at: float | None = None) -> TranslationJob | None:
        """A place in ``channel`` for one segment, or None when the channel is already full.

        ``arrived_at`` is when the segment arrived, from ``time.monotonic()``; the lag budget counts
        from it. Without it, the budget counts from now.
        """
        channels = self._rooms.get(room)
        if channels is None:
            channels = self._rooms[room] = OrderedDict()
        state = channels.get(channel)
        if state is None:
            state = channels[channel] = _Channel()
        if state.admitted >= self.queue_limit:
            return None
        state.admitted += 1
        return TranslationJob(self, room, channel, arrived_at)

    def set_capacity(self, capacity: int) -> None:
        """Change how many calls may run at once. Calls already running finish; none are stopped."""
        if capacity < 1:
            raise ValueError("capacity must be at least 1")
        self.capacity = capacity
        self._dispatch()

    def admitted(self, room: Hashable, channel: str) -> int:
        """Jobs ``channel`` holds right now, waiting or running."""
        state = self._rooms.get(room, {}).get(channel)
        return state.admitted if state else 0

    def waiting(self) -> int:
        """Jobs asking for an engine slot that have not been given one yet."""
        return sum(
            not turn.cancelled()
            for channels in self._rooms.values()
            for state in channels.values()
            for turn in state.waiters
        )

    async def _wait_for_turn(self, job: TranslationJob) -> None:
        time_left = self.lag_budget - job.waited if self.lag_budget else None
        if time_left is not None and time_left <= 0:
            raise StaleTranslationJob(job.waited)
        state = self._rooms[job._room][job._channel]
        turn = asyncio.get_running_loop().create_future()
        state.waiters.append(turn)
        self._dispatch()
        try:
            async with asyncio.timeout(time_left):
                await turn
        except (asyncio.CancelledError, TimeoutError) as exc:
            if not turn.cancelled():
                # The slot was given just before the cancel or the deadline reached the job: pass it on.
                self._give_back(state)
            elif turn in state.waiters:
                state.waiters.remove(turn)
            if isinstance(exc, TimeoutError):
                raise StaleTranslationJob(job.waited) from None
            raise
        if self.lag_budget and job.waited >= self.lag_budget:
            # The slot came after the deadline, before the late timeout could fire (the loop ran
            # late). The job still did not start in time: pass the slot on without running it.
            self._give_back(state)
            raise StaleTranslationJob(job.waited)

    def _leave(self, job: TranslationJob, *, running: bool) -> None:
        channels = self._rooms[job._room]
        state = channels[job._channel]
        state.admitted -= 1
        if running:
            self._give_back(state)
        if state.admitted == 0:
            del channels[job._channel]
            if not channels:
                del self._rooms[job._room]

    def _give_back(self, state: _Channel) -> None:
        state.running -= 1
        self.running -= 1
        self._dispatch()

    def _dispatch(self) -> None:
        """Hand free engine slots to waiting jobs, one turn per room and then per language."""
        while self.running < self.capacity:
            picked = self._next_turn()
            if picked is None:
                return
            room, channels, channel, state = picked
            turn = state.waiters.popleft()
            if turn.cancelled():
                # Its job was cancelled and has not run its cleanup yet: nothing to hand over.
                continue
            turn.set_result(None)
            state.running += 1
            self.running += 1
            # Served now, so they go to the back of their queues.
            self._rooms.move_to_end(room)
            channels.move_to_end(channel)

    def _next_turn(self) -> tuple[Hashable, OrderedDict[str, _Channel], str, _Channel] | None:
        for room, channels in self._rooms.items():
            for channel, state in channels.items():
                if state.waiters and state.running < self.per_channel:
                    return room, channels, channel, state
        return None
