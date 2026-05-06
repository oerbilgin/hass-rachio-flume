"""
Set up and tear down the Irrigation Monitor integration.

This is the entry point Home Assistant calls when a config entry for this
integration is loaded, unloaded, or reloaded.

The module is responsible for:
- declaring which platforms belong to the integration
- creating shared runtime objects such as the API client and coordinator
- attaching that runtime state to the config entry so other modules can use it
- forwarding setup to platform modules like sensor.py

If you want to understand how the whole component starts up, this is the best
place to begin.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from homeassistant.const import Platform
from homeassistant.loader import async_get_loaded_integration

from .api import IrrigationMonitorApiClient
from .const import DOMAIN, LOGGER
from .coordinator import IrrigationMonitorDataUpdateCoordinator
from .data import IrrigationMonitorData

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .data import IrrigationMonitorConfigEntry

PLATFORMS: list[Platform] = [Platform.SENSOR]
DEFAULT_UPDATE_INTERVAL = timedelta(hours=1)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IrrigationMonitorConfigEntry,
) -> bool:
    """
    Build runtime objects and forward setup to platform modules.

    Home Assistant calls this once for each saved config entry. The function
    creates the coordinator and API client, stores them in entry.runtime_data,
    performs the first refresh, and then forwards setup to the sensor platform.
    """
    coordinator = IrrigationMonitorDataUpdateCoordinator(
        hass=hass,
        logger=LOGGER,
        name=DOMAIN,
        update_interval=DEFAULT_UPDATE_INTERVAL,
    )
    entry.runtime_data = IrrigationMonitorData(
        client=IrrigationMonitorApiClient(entry.data),
        coordinator=coordinator,
        integration=async_get_loaded_integration(hass, entry.domain),
    )

    await coordinator.async_config_entry_first_refresh()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    return True


async def async_unload_entry(
    hass: HomeAssistant,
    entry: IrrigationMonitorConfigEntry,
) -> bool:
    """
    Unload all platform entities created for this config entry.

    This lets Home Assistant cleanly remove the integration without leaving
    stale entities or listeners behind.
    """
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_reload_entry(
    hass: HomeAssistant,
    entry: IrrigationMonitorConfigEntry,
) -> None:
    """Reload an existing config entry after its options or data change."""
    await hass.config_entries.async_reload(entry.entry_id)
