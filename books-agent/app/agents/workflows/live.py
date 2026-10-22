"""Runs turns detached from the HTTP request and fans their events out to whoever is reading.

Streamable HTTP treats a dropped connection as a dropped *reader*, not a cancelled turn. So a turn runs as a
background task (`start`), and every event it emits is stored first (EventLog) and then published here. Readers —
the POST that started the turn, or a GET resuming with Last-Event-ID — replay what is stored, then follow the live
frames until the turn ends (`turns.follow`).

Single process only: with several agent replicas this becomes Redis pub/sub keyed by conversation.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field

# A live frame is (seq, sse frame); None tells readers the turn has ended.
Frame = tuple[int, str] | None


@dataclass
class _RunningTurn:
    turn_id: str
    readers: set[asyncio.Queue[Frame]] = field(default_factory=set)


_running: dict[str, _RunningTurn] = {}  # conversation id -> its running turn (the lock allows one at a time)
_tasks: set[asyncio.Task[None]] = set()


def start(conv_id: str, turn_id: str, frames: AsyncIterator[str], on_done: Callable[[], Awaitable[None]]) -> None:
    """Runs a turn's event generator to completion in the background, whether or not anyone is reading."""
    _running[conv_id] = _RunningTurn(turn_id)

    async def drive() -> None:
        try:
            async for _ in frames:
                pass  # each event was already stored and published by EventLog.emit
        finally:
            turn = _running.pop(conv_id, None)
            for queue in turn.readers if turn else ():
                queue.put_nowait(None)
            await on_done()

    task = asyncio.create_task(drive())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def subscribe(conv_id: str, turn_id: str | None = None) -> asyncio.Queue[Frame] | None:
    """A queue of live frames for the conversation's running turn, or None when no (matching) turn is running."""
    turn = _running.get(conv_id)
    if turn is None or (turn_id is not None and turn.turn_id != turn_id):
        return None
    queue: asyncio.Queue[Frame] = asyncio.Queue()
    turn.readers.add(queue)
    return queue


def unsubscribe(conv_id: str, queue: asyncio.Queue[Frame]) -> None:
    turn = _running.get(conv_id)
    if turn:
        turn.readers.discard(queue)


def publish(conv_id: str, seq: int, frame: str) -> None:
    turn = _running.get(conv_id)
    for queue in turn.readers if turn else ():
        queue.put_nowait((seq, frame))


async def shutdown() -> None:
    """Cancels running turns on shutdown; their `finally` blocks still mark them failed and release locks."""
    for task in _tasks:
        task.cancel()
    await asyncio.gather(*_tasks, return_exceptions=True)
