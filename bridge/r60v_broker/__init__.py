"""Rocket R60V bridge -- protocol codec, wire-level emulator, and connection governor.

See ``docs/protocol.md`` (in the repository root) for the wire-protocol reference.
"""
from __future__ import annotations

from . import protocol
from .emulator import MachineModel, R60VEmulator

__all__ = ["protocol", "MachineModel", "R60VEmulator"]

__version__ = "1.4.0"
