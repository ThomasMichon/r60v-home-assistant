"""A thread-safe wrapper around the upstream ``rocket_r60v.machine.Machine``.

The upstream library keeps **one** socket and pairs each request with the next
response it reads. Home Assistant, however, updates entities across several
platforms **concurrently** (each in its own executor thread) and they all share
one ``Machine`` instance -- so without serialization two threads interleave their
request/response frames on the single socket and the library raises
``Invalid response envelope`` (it read a reply meant for another thread).

The facility bridge's governor already serializes access at the *machine* end,
but that does not help the *client* end: the shared client socket is still used
by many threads. This wrapper closes that gap by locking the library's single
I/O chokepoint (``send_message``), so every request completes its full
send/read/validate cycle before the next begins.
"""
from __future__ import annotations

import threading

from rocket_r60v.machine import Machine


class LockedMachine(Machine):
    """``Machine`` whose socket I/O is serialized with a lock."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # Bypass Machine's settings-aware __setattr__ for our own attribute.
        object.__setattr__(self, "_io_lock", threading.Lock())

    def send_message(self, *args, **kwargs):
        with self._io_lock:
            return super().send_message(*args, **kwargs)
