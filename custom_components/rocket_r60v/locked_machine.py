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

It also **reconnects on a dropped link**: the upstream library only retries on a
read timeout and otherwise propagates the socket error, so if the bridge (or the
machine) restarts and the connection dies, the integration would be stuck until
manually reloaded. Here a connection error triggers one reconnect-and-retry, so
the link self-heals.
"""
from __future__ import annotations

import logging
import threading

from rocket_r60v.exceptions import RocketConnectionError
from rocket_r60v.machine import Machine

LOGGER = logging.getLogger(__name__)


class LockedMachine(Machine):
    """``Machine`` whose socket I/O is serialized and self-healing."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # Bypass Machine's settings-aware __setattr__ for our own attribute.
        object.__setattr__(self, "_io_lock", threading.Lock())

    def send_message(self, *args, **kwargs):
        with self._io_lock:
            try:
                return super().send_message(*args, **kwargs)
            except (OSError, RocketConnectionError) as exc:
                # A dead socket (bridge/machine restarted) is not retried by the
                # library. Reconnect once under the same lock and retry; if the
                # reconnect fails it propagates and the next poll cycle retries.
                LOGGER.warning("R60V link error (%s); reconnecting", exc)
                try:
                    self.disconnect()
                except OSError:
                    pass
                self.connect()
                return super().send_message(*args, **kwargs)
