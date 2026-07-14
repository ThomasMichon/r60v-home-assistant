"""Startup regression test: setting up the integration must not block the loop.

This is the test that would have caught the original outage. The Rocket R60V
entities used to perform synchronous device socket reads inside their
``__init__`` methods; Home Assistant constructs entities on the event loop, so
that blocking I/O hung HA at startup.

Here we stand up the bundled wire-level emulator (a real asyncio TCP server that
speaks the R60V protocol) on an ephemeral loopback port, point a config entry at
it, and set the integration up through the real HA config-entry machinery. The
whole setup runs inside ``asyncio.timeout(30)`` so that a loop-blocking setup
fails loudly instead of hanging. ``pytest-homeassistant-custom-component`` also
enables HA's blocking-call detector during setup, so a synchronous socket read
on the loop raises rather than silently stalling.

If the pinned ``rocket-r60v==1.2.1`` client cannot decode some of the emulator's
modeled registers, individual entities may report as unavailable; the essential
guarantee this test protects is that setup *completes* (``LOADED``) without
blocking the loop and that entities are created. See ``STRICT_ENTITY_STATE``.
"""
from __future__ import annotations

import asyncio

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PORT, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from r60v_broker.emulator import R60VEmulator

DOMAIN = "rocket_r60v"
SETUP_TIMEOUT = 30

# The always-present entity created by the SWITCH platform, identified by its
# stable unique_id (the entity_id is derived from the entity name and may vary).
STANDBY_UNIQUE_ID = "rocket_r60v_standby"
STANDBY_PLATFORM = "switch"

# When True, also assert the standby switch is not STATE_UNAVAILABLE (i.e. the
# pinned client successfully read live state from the emulator). Kept True; flip
# to False only if a client/emulator register-encoding mismatch makes the read
# unreliable in CI (setup-LOADED + entity-present is the hard guarantee).
STRICT_ENTITY_STATE = True


@pytest.fixture
async def emulator(socket_enabled):
    """Run the R60V wire emulator on an ephemeral loopback port for one test.

    ``socket_enabled`` (from pytest-socket, bundled with the HA test plugin)
    re-enables real sockets, which HA tests block by default; the emulator and
    the integration's client need a genuine loopback socket to talk over.
    """
    emu = R60VEmulator(host="127.0.0.1", port=0)
    await emu.start()
    try:
        yield emu
    finally:
        await emu.stop()


async def test_setup_entry_does_not_block_event_loop(
    hass: HomeAssistant, emulator: R60VEmulator
) -> None:
    """The integration sets up against a live device without blocking the loop."""
    port = emulator.bound_port

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_HOST: "127.0.0.1", CONF_PORT: port},
        unique_id=f"127.0.0.1:{port}",
    )
    entry.add_to_hass(hass)

    # A blocking read on the event loop would either trip HA's blocking-call
    # detector or stall here; the timeout turns a stall into a hard failure.
    async with asyncio.timeout(SETUP_TIMEOUT):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    try:
        assert entry.state is ConfigEntryState.LOADED

        # Entities are registered against this config entry by the platforms.
        ent_reg = er.async_get(hass)
        registered = er.async_entries_for_config_entry(ent_reg, entry.entry_id)
        assert registered, "no entities were registered for the config entry"

        # The standby switch must exist and have fetched real state (i.e. not be
        # unavailable) -- proving initial state was read off-loop before add.
        standby_id = ent_reg.async_get_entity_id(
            STANDBY_PLATFORM, DOMAIN, STANDBY_UNIQUE_ID
        )
        assert standby_id is not None, "standby switch was not registered"

        standby = hass.states.get(standby_id)
        assert standby is not None, f"{standby_id} has no state"

        if STRICT_ENTITY_STATE:
            assert standby.state != STATE_UNAVAILABLE, (
                f"{standby_id} is unavailable -- initial state was not fetched "
                f"off-loop via update_before_add=True"
            )
    finally:
        # Unload so the integration releases its device connection, letting the
        # emulator's client handler finish and the loop tear down cleanly.
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
