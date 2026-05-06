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

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity

from .entity import IrrigationMonitorEntity

if TYPE_CHECKING:
    import datetime

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import IrrigationMonitorDataUpdateCoordinator
    from .data import IrrigationMonitorConfigEntry
    from .util import WaterReportDataPoint


async def async_setup_entry(
    _hass: HomeAssistant,
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
    if not report:
        return

    async_add_entities(
        IrrigationZoneSensor(
            coordinator=entry.runtime_data.coordinator,
            zone_name=row.zone_name or "unknown",
            zone_id=row.zone_id,
        )
        for row in report
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
        zone_id: int | None,
    ) -> None:
        """Store the zone identity used to find this sensor's report row."""
        super().__init__(coordinator)
        self._zone_name = zone_name
        self._zone_id = zone_id
        zone_key = str(zone_id if zone_id is not None else zone_name).replace(" ", "_")
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{zone_key}".lower()
        self._attr_name = f"Irrigation {zone_name} last watering"
        self._attr_device_class = SensorDeviceClass.TIMESTAMP

    @property
    def native_value(self) -> datetime.datetime | None:
        """
        Expose the zone's watering start time as the sensor state.

        This makes HomeAssistant record each new watering event in the history.
        """
        datapoint = self._report_datapoint
        if datapoint is None:
            return None
        return datapoint.watering_start_time

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the full watering event metadata as serialized attributes."""
        datapoint = self._report_datapoint
        if datapoint is None:
            return {}
        return datapoint.model_dump(mode="json")

    @property
    def _report_datapoint(self) -> WaterReportDataPoint | None:
        """Find this zone's current row inside the shared coordinator data."""
        report = self.coordinator.data
        if not report:
            return None

        for datapoint in report:
            if self._zone_id is not None and datapoint.zone_id == self._zone_id:
                return datapoint
            if datapoint.zone_name == self._zone_name:
                return datapoint

        return None
