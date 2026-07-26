"""Rocket R60V wire-protocol emulator -- a faithful "other end of the wire".

This is the upgraded successor to the original ``FakeMachine`` object-mock: an
actual asyncio TCP server that speaks the real R60V protocol (``*HELLO*``
greeting, hex framing, checksums, the 115-byte settings block, live registers,
and strict half-duplex request/response). The broker and HA integration can be
developed and CI-tested against it over a real socket, with no physical machine.

An internal machine model backs the memory:
- a 256-byte settings block with sane defaults (temps, PID, profiles, timers);
- live registers whose current temperatures drift toward their setpoints so
  polling behaves like a warming/idling machine.

Encodings marked "(modeled)" below are emulator assumptions for surfaces the
static APK analysis did not fully pin (e.g. the live-register byte layout);
they are centralized here so they can be corrected once the real machine is
sniffed. The settings-block layout and framing are verified against the app.

Run standalone::

    python -m r60v_broker.emulator --host 127.0.0.1 --port 1774
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import datetime

from . import protocol as p
from .protocol import Address

LOGGER = logging.getLogger("r60v.emulator")


class MachineModel:
    """In-memory model of an R60V's readable/writable memory."""

    def __init__(self) -> None:
        self.settings = bytearray(0x100)
        self._init_defaults()
        # Live values (Celsius). Start a bit cold so a poller sees them warm up.
        self.current_brew_temp = 92.0
        self.current_service_temp = 118.0
        self.current_pressure = 0.0
        self.display = "READY"

    def _init_defaults(self) -> None:
        s = self.settings
        s[Address.TEMPERATURE_UNIT] = 0        # Celsius
        s[Address.LANGUAGE] = 0                # English
        s[Address.BREW_BOILER_TEMP] = 105
        s[Address.SERVICE_BOILER_TEMP] = 123
        # PID terms (little-endian 16-bit; low byte carries the value here)
        s[Address.KP_COFFEE] = 15
        s[Address.KP_GROUP] = 40
        s[Address.KI_COFFEE] = 1
        s[Address.KI_GROUP] = 1
        s[Address.KD_COFFEE] = 65
        s[Address.KD_GROUP] = 5
        s[Address.WATER_FEED] = 1              # Reservoir (tank); 0 = HardPlumbed (mains)
        s[Address.ACTIVE_PROFILE] = 0          # profile A
        s[Address.WAND_WASH_TIME] = 15
        s[Address.SERVICE_BOILER_ENABLE] = 1   # steam boiler on
        s[Address.STANDBY] = 0                 # 0 = machine on
        s[Address.GROUP_TEMP] = 95
        # Total coffee count = 500 (0x01F4), little-endian.
        s[Address.TOTAL_COFFEE_COUNT] = 0xF4
        s[Address.TOTAL_COFFEE_COUNT + 1] = 0x01
        # Auto-on 14:00, auto-off 08:00 (matches the original FakeMachine).
        s[Address.AUTO_ON_HOUR] = 14
        s[Address.AUTO_ON_MINUTE] = 0
        s[Address.AUTO_OFF_HOUR] = 8
        s[Address.AUTO_OFF_MINUTE] = 0

    # -- reads ------------------------------------------------------------

    def read(self, address: int, length: int) -> bytes:
        """Return ``length`` bytes starting at ``address``."""
        if address < 0x100:
            chunk = bytes(self.settings[address : address + length])
            return chunk.ljust(length, b"\x00")
        return self._read_live(address, length)

    def _read_live(self, address: int, length: int) -> bytes:
        """Return live/read-only register bytes (modeled encodings)."""
        if address == Address.CURRENT_BREW_TEMP:
            return _int_bytes(round(self.current_brew_temp), length)
        if address == Address.CURRENT_SERVICE_TEMP:
            return _int_bytes(round(self.current_service_temp), length)
        if address == Address.CURRENT_PRESSURE:
            # Pressure modeled as decibar (0.0-12.0 bar -> 0-120).
            return _int_bytes(round(self.current_pressure * 10), length)
        if address == Address.DISPLAY:
            return self.display.encode("ascii", "replace").ljust(length, b" ")[:length]
        if address == Address.DATE_TIME:
            now = datetime.now()  # (modeled) [hh, mm, ss, weekday, dd, mm, yy]
            fields = [now.hour, now.minute, now.second,
                      now.weekday(), now.day, now.month, now.year % 100]
            return bytes(fields).ljust(length, b"\x00")[:length]
        return bytes(length)

    # -- writes -----------------------------------------------------------

    def write(self, address: int, data: list[int]) -> None:
        """Apply a write of ``data`` bytes at ``address`` (settings only)."""
        if address >= 0x100:
            LOGGER.warning("write to read-only region 0x%04X ignored", address)
            return
        for offset, value in enumerate(data):
            self.settings[address + offset] = value & 0xFF

    # -- dynamics ---------------------------------------------------------

    def tick(self) -> None:
        """Advance the simulation one step (temps drift toward setpoints)."""
        on = self.settings[Address.STANDBY] == 0
        brew_target = self.settings[Address.BREW_BOILER_TEMP] if on else 20
        self.current_brew_temp += _step(self.current_brew_temp, brew_target)
        if self.settings[Address.SERVICE_BOILER_ENABLE] and on:
            svc_target = self.settings[Address.SERVICE_BOILER_TEMP]
        else:
            svc_target = 20
        self.current_service_temp += _step(self.current_service_temp, svc_target)


def _step(current: float, target: float, rate: float = 0.15) -> float:
    """One first-order step of ``current`` toward ``target``."""
    return (target - current) * rate


def _int_bytes(value: int, length: int) -> bytes:
    """Encode a non-negative int as ``length`` little-endian bytes (clamped)."""
    value = max(0, value)
    return value.to_bytes(max(length, 1), "little", signed=False)[:length] or bytes(length)


class R60VEmulator:
    """An asyncio TCP server that emulates the R60V control interface."""

    def __init__(self, host: str = "127.0.0.1", port: int = p.DEFAULT_PORT,
                 model: MachineModel | None = None) -> None:
        self.host = host
        self.port = port
        self.model = model or MachineModel()
        self._server: asyncio.AbstractServer | None = None
        self._ticker: asyncio.Task[None] | None = None
        # Track live client connections so stop() can drop them; otherwise a
        # peer that is idle mid-read keeps a handler task alive and blocks a
        # clean shutdown (e.g. at the end of a test).
        self._writers: set[asyncio.StreamWriter] = set()

    async def start(self) -> None:
        """Start the server and the background dynamics ticker."""
        self._server = await asyncio.start_server(
            self._handle_client, self.host, self.port
        )
        self._ticker = asyncio.ensure_future(self._run_ticker())
        sockets = ", ".join(str(s.getsockname()) for s in self._server.sockets or [])
        LOGGER.info("R60V emulator listening on %s", sockets)

    async def serve_forever(self) -> None:
        """Start and serve until cancelled."""
        await self.start()
        assert self._server is not None
        async with self._server:
            await self._server.serve_forever()

    async def stop(self) -> None:
        """Stop the server and ticker, and drop any live client connections."""
        if self._ticker:
            self._ticker.cancel()
        # Close active client connections so their handler tasks unblock and
        # finish, allowing the event loop to shut down cleanly.
        for writer in list(self._writers):
            writer.close()
        self._writers.clear()
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    @property
    def bound_port(self) -> int:
        """The actual port the server bound to (useful with port 0)."""
        if not self._server or not self._server.sockets:
            raise RuntimeError("emulator not started")
        return self._server.sockets[0].getsockname()[1]

    async def _run_ticker(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            self.model.tick()

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        LOGGER.info("client connected: %s", peer)
        self._writers.add(writer)
        writer.write(p.HELLO.encode())
        await writer.drain()
        try:
            while True:
                envelope = await reader.readexactly(9)
                rest = await self._read_rest(reader, envelope)
                response = self._process(envelope.decode("ascii", "replace") + rest)
                if response is None:
                    break
                writer.write(response.encode())
                await writer.drain()
        except (asyncio.IncompleteReadError, asyncio.CancelledError, OSError):
            pass
        finally:
            LOGGER.info("client disconnected: %s", peer)
            self._writers.discard(writer)
            writer.close()

    async def _read_rest(self, reader: asyncio.StreamReader, envelope: bytes) -> str:
        """Read the bytes that follow a 9-byte envelope.

        A write request is followed by ``length`` data bytes (2 hex chars each)
        plus a 2-char checksum. A read request carries no data -- only the
        checksum -- because its ``length`` field is the *read count*, not a
        present payload.
        """
        command = chr(envelope[0])
        if command == p.WRITE:
            try:
                length = int(envelope[5:9], 16)
            except ValueError:
                return ""
            count = length * 2 + 2
        else:
            count = 2
        rest = await reader.readexactly(count)
        return rest.decode("ascii", "replace")

    def _process(self, raw: str) -> str | None:
        """Handle one request frame, returning the raw response (or None)."""
        try:
            frame = p.parse_frame(raw)
        except p.ProtocolError as exc:
            LOGGER.warning("dropping bad frame: %s", exc)
            return None

        if frame.command == p.READ:
            data = self.model.read(frame.address, frame.length)
            LOGGER.debug("read 0x%04X[%d] -> %s", frame.address, frame.length, data.hex())
            return p.build_frame(p.READ, frame.address, frame.length, data)

        # write
        self.model.write(frame.address, frame.data)
        LOGGER.debug("write 0x%04X <- %s", frame.address, bytes(frame.data).hex())
        return p.build_ack(p.WRITE, frame.address, frame.length)


def main(argv: list[str] | None = None) -> None:
    """CLI entry point for the standalone emulator."""
    parser = argparse.ArgumentParser(description="Rocket R60V protocol emulator")
    parser.add_argument("--host", default="127.0.0.1", help="bind address")
    parser.add_argument("--port", type=int, default=p.DEFAULT_PORT, help="TCP port")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    emulator = R60VEmulator(host=args.host, port=args.port)
    try:
        asyncio.run(emulator.serve_forever())
    except KeyboardInterrupt:
        LOGGER.info("emulator stopped")


if __name__ == "__main__":
    main()
