"""
Expose irrigation report rows as Home Assistant sensors.

This platform turns the shared irrigation report from the coordinator into one
sensor entity per irrigation zone.

The module contains:
- the async_setup_entry function that creates sensor entities from coordinator data
- the IrrigationZoneSensor entity class
- logic that maps each zone's report row into a sensor state and attributes

If you want to change what each zone sensor shows in Home Assistant, or add
more sensor-specific behavior, this is the module to update.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import SensorEntity

from custom_components.irrigation_monitor.coordinator import (
    IrrigationMonitorDataUpdateCoordinator,
)

from .entity import IrrigationMonitorEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .data import IrrigationMonitorConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IrrigationMonitorConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """
    Create one sensor entity for each row in the coordinator report.

    This runs after __init__.py has already created and refreshed the
    coordinator, so the platform can build entities from the first cached data
    snapshot immediately.
    """
    report = entry.runtime_data.coordinator.data
    if report is None or report.empty:
        return

    async_add_entities(
        IrrigationZoneSensor(
            coordinator=entry.runtime_data.coordinator,
            zone_name=str(row.get("zone_name") or "unknown"),
            zone_id=row.get("zone_id"),
        )
        for _, row in report.iterrows()
    )


class IrrigationZoneSensor(IrrigationMonitorEntity, SensorEntity):
    """
    Represent one irrigation zone as a Home Assistant sensor.

    The entity looks up its current values from the coordinator's shared report
    instead of storing independent state. That keeps refresh logic centralized.
    """

    def __init__(
        self,
        coordinator: IrrigationMonitorDataUpdateCoordinator,
        zone_name: str,
        zone_id: str,
    ) -> None:
        """Store the zone identity used to find this sensor's report row."""
        super().__init__(coordinator)
        self._zone_name = zone_name
        self._zone_id = zone_id
        zone_key = str(zone_id if zone_id is not None else zone_name).replace(" ", "_")
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{zone_key}".lower()
        self._attr_name = f"Irrigation {zone_name}"

    @property
    def native_value(self) -> float | None:
        """Expose the zone's total measured gallons as the sensor state."""
        row = self._report_row
        if row is None:
            return None
        total_gallons = row.get("total_gallons")
        return None if total_gallons is None else float(total_gallons)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the full report row as extra attributes for inspection."""
        row = self._report_row
        if row is None:
            return {}
        return row

    @property
    def _report_row(self) -> dict[str, Any] | None:
        """Find this zone's current row inside the shared coordinator data."""
        report = self.coordinator.data
        if report is None or report.empty:
            return None

        if self._zone_id is not None and "zone_id" in report.columns:
            zone_rows = report.loc[report["zone_id"] == self._zone_id]
        else:
            zone_rows = report.loc[report["zone_name"] == self._zone_name]

        if zone_rows.empty:
            return None
        return zone_rows.iloc[0].to_dict()
