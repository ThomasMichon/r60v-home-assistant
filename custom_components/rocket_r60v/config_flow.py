"""Config flow for Rocket R60V."""

from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_entry_flow

from .const import DOMAIN
from rocket_r60v.machine import Machine
from .FakeMachine import FakeMachine

def get_devices():
    machine = Machine()
    try:
        machine.connect()
        return [machine]
    except:
        return []


async def _async_has_devices(hass: HomeAssistant) -> bool:
    """Return if there are devices that can be discovered."""

    devices = await hass.async_add_executor_job(get_devices)
    return len(devices) > 0


config_entry_flow.register_discovery_flow(DOMAIN, "Rocket R60V", _async_has_devices)
