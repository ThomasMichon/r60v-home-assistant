"""Config flow for Rocket R60V.

A simple user-input flow: the operator enters the control endpoint (defaulting
to the facility's governor-fronted bridge endpoint), and we verify we can reach
it before creating the entry. Replaces the old zero-config discovery flow, which
could only ever reach the machine's own 192.168.1.1 AP.
"""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.data_entry_flow import FlowResult

from rocket_r60v.machine import Machine

from .const import DEFAULT_HOST, DEFAULT_PORT, DOMAIN


def _can_connect(host: str, port: int) -> bool:
    """Blocking reachability probe -- run in an executor."""
    try:
        machine = Machine(address=host, port=port)
        machine.connect()
        machine.disconnect()
        return True
    except Exception:  # noqa: BLE001 -- any failure means "not reachable"
        return False


class RocketR60VConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Rocket R60V."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial (and only) user step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST]
            port = user_input[CONF_PORT]
            await self.async_set_unique_id(f"{host}:{port}")
            self._abort_if_unique_id_configured()
            if await self.hass.async_add_executor_job(_can_connect, host, port):
                return self.async_create_entry(
                    title=f"Rocket R60V ({host})", data=user_input
                )
            errors["base"] = "cannot_connect"

        schema = vol.Schema(
            {
                vol.Required(CONF_HOST, default=DEFAULT_HOST): str,
                vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
            }
        )
        return self.async_show_form(
            step_id="user", data_schema=schema, errors=errors
        )
