"""
Define typed runtime data stored on the config entry.

This module is small but important: it describes the bundle of objects that
Irrigation Monitor stores in entry.runtime_data after setup.

That bundle currently includes:
- the API client used to fetch and validate data
- the coordinator that caches shared updates for entities
- the loaded integration metadata from Home Assistant

Using a typed container here makes it clearer what state is available to other
modules during normal operation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.loader import Integration

    from .api import IrrigationMonitorApiClient
    from .coordinator import IrrigationMonitorDataUpdateCoordinator


type IrrigationMonitorConfigEntry = ConfigEntry[IrrigationMonitorData]


@dataclass
class IrrigationMonitorData:
    """
    Bundle the long-lived objects shared by the loaded config entry.

    Keeping these references together avoids passing several unrelated objects
    through every setup function and makes entry.runtime_data self-describing.
    """

    client: IrrigationMonitorApiClient
    coordinator: IrrigationMonitorDataUpdateCoordinator
    integration: Integration
