"""
Coordinate periodic data refreshes for Irrigation Monitor.

This module contains the DataUpdateCoordinator subclass, which is the standard
Home Assistant pattern for integrations where multiple entities share the same
fetched data.

Its job is to:
- ask the API client for a fresh irrigation report on a schedule
- store the latest shared data snapshot
- convert authentication failures into Home Assistant reauth signals
- convert other fetch failures into update errors

The sensor entities do not fetch data themselves; they read from this shared
coordinator instead.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    IrrigationMonitorApiClientAuthenticationError,
    IrrigationMonitorApiClientError,
)
from .util import WaterReportDataPoint

if TYPE_CHECKING:
    from .data import IrrigationMonitorConfigEntry


class IrrigationMonitorDataUpdateCoordinator(
    DataUpdateCoordinator[list[WaterReportDataPoint]]
):
    """
    Fetch one shared irrigation report and fan it out to all entities.

    This class is the central cache for the integration. Sensor entities read
    from coordinator.data instead of making their own API calls.
    """

    config_entry: IrrigationMonitorConfigEntry

    async def _async_update_data(self) -> list[WaterReportDataPoint]:
        """
        Ask the API client for new data and translate errors for HA.

        The data are stored in coordinator.data and used by all entities,
        so this method is the only place where the API client is called.

        Raising ConfigEntryAuthFailed tells Home Assistant the saved credentials
        are no longer valid. Raising UpdateFailed marks the refresh as failed but
        keeps the entry loaded.
        """
        try:
            return await self.config_entry.runtime_data.client.async_get_data(self.hass)
        except IrrigationMonitorApiClientAuthenticationError as exception:
            raise ConfigEntryAuthFailed(exception) from exception
        except IrrigationMonitorApiClientError as exception:
            raise UpdateFailed(exception) from exception
