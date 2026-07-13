"""The device governor: the sole owner of the R60V link.

The R60V's single-socket listener is fragile — connection churn or concurrent
callers wedge it (see ``docs/reference/rocket-r60v-protocol.md`` §9). The
governor guarantees there is exactly **one** thing talking to the machine: it
owns the :class:`~r60v_broker.client.R60VClient` and serializes every operation
through a single worker draining a **priority queue**. Callers never touch the
client directly — they submit read/write jobs and await the result.

- **Commands beat polls.** A user command (write) is enqueued at higher
  priority than routine polling reads, so an espresso setting change is not
  stuck behind a queue of temperature reads.
- **Gentle throttle.** A minimum spacing between device operations (on top of
  the client's own inter-request pacing) keeps the conversation calm.
- **Failures are isolated.** A job that fails resolves its own future with the
  exception; the worker keeps running and stays the single owner.
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import time

from .client import R60VClient
from .protocol import Frame

LOGGER = logging.getLogger("r60v.governor")

#: Lower number = higher priority in the queue.
PRIORITY_COMMAND = 0
PRIORITY_POLL = 10


class DeviceGovernor:
    """Serializes and throttles all access to the R60V behind one worker."""

    def __init__(self, client: R60VClient, *, min_interval: float = 0.0) -> None:
        self.client = client
        self.min_interval = min_interval
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._counter = itertools.count()
        self._worker: asyncio.Task | None = None
        self._last_op_at: float = 0.0

    async def start(self) -> None:
        if self._worker is None:
            self._worker = asyncio.create_task(self._run(), name="r60v-governor")

    async def stop(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
            self._worker = None
        await self.client.close()

    # -- submission -------------------------------------------------------

    async def read(self, address: int, length: int,
                   *, priority: int = PRIORITY_POLL) -> list[int]:
        """Enqueue a read and await its result."""
        return await self._submit(priority, ("read", address, length))

    async def write(self, address: int, data: list[int],
                    *, priority: int = PRIORITY_COMMAND) -> Frame:
        """Enqueue a write and await its acknowledgement."""
        return await self._submit(priority, ("write", address, data))

    async def _submit(self, priority: int, op: tuple):
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        # The monotonic counter breaks priority ties in FIFO order and keeps
        # the never-comparable future out of the tuple comparison.
        self._queue.put_nowait((priority, next(self._counter), op, fut))
        return await fut

    # -- worker -----------------------------------------------------------

    async def _run(self) -> None:
        while True:
            priority, _seq, op, fut = await self._queue.get()
            await self._throttle()
            self._last_op_at = time.monotonic()
            try:
                if op[0] == "read":
                    result: object = await self.client.read(op[1], op[2])
                else:
                    result = await self.client.write(op[1], op[2])
            except Exception as exc:  # noqa: BLE001 -- surfaced to the caller
                if not fut.done():
                    fut.set_exception(exc)
            else:
                if not fut.done():
                    fut.set_result(result)
            finally:
                self._queue.task_done()

    async def _throttle(self) -> None:
        if self.min_interval <= 0:
            return
        elapsed = time.monotonic() - self._last_op_at
        if elapsed < self.min_interval:
            await asyncio.sleep(self.min_interval - elapsed)
