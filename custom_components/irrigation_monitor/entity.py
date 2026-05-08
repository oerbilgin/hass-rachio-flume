"""
Provide shared entity behavior for Irrigation Monitor platforms.

This module defines the base entity class that platform-specific entities can
inherit from.

The shared base currently sets common metadata such as:
- coordinator integration, so entities automatically track refreshes
- device registry information, so Home Assistant groups related entities
- attribution text displayed with the entity data

As the component grows, common entity behavior should generally be added here
instead of duplicated across sensor, binary_sensor, or switch platforms.
"""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import ATTRIBUTION
from .coordinator import IrrigationMonitorDataUpdateCoordinator


class IrrigationMonitorEntity(
    CoordinatorEntity[IrrigationMonitorDataUpdateCoordinator]
):
    """
    Base class for entities backed by the shared coordinator.

    Subclasses inherit coordinator update behavior plus shared device metadata,
    which keeps platform modules smaller and more consistent.
    """

    _attr_attribution = ATTRIBUTION

    def __init__(self, coordinator: IrrigationMonitorDataUpdateCoordinator) -> None:
        """Attach coordinator state and register a single shared device."""
        super().__init__(coordinator)
        self._attr_device_info = DeviceInfo(
            identifiers={
                (
                    coordinator.config_entry.domain,
                    coordinator.config_entry.entry_id,
                ),
            },
            name="Irrigation Monitor",
        )
