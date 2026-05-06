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

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfVolume
from homeassistant.helpers.restore_state import RestoreEntity

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
        IrrigationZoneLastWateringSensor(
            coordinator=entry.runtime_data.coordinator,
            zone_name=row.zone_name or "unknown",
            zone_id=row.zone_id,
        )
        for row in report
    )
    async_add_entities(
        IrrigationZoneWaterUsedSensor(
            coordinator=entry.runtime_data.coordinator,
            zone_name=row.zone_name or "unknown",
            zone_id=row.zone_id,
        )
        for row in report
    )
    async_add_entities(
        IrrigationZoneWaterTotalSensor(
            coordinator=entry.runtime_data.coordinator,
            zone_name=row.zone_name or "unknown",
            zone_id=row.zone_id,
        )
        for row in report
    )


class IrrigationZoneReportEntity(IrrigationMonitorEntity, SensorEntity):
    """
    Represent one irrigation zone backed by the shared report.

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
        self._zone_key = zone_key

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the full watering event metadata as serialized attributes."""
        datapoint = self._report_datapoint
        if datapoint is None:
            return {}
        event_data = datapoint.model_dump(mode="json")
        event_data["event_id"] = self._event_id
        return event_data

    @property
    def _event_id(self) -> str | None:
        """Return a stable identifier for the latest watering event."""
        datapoint = self._report_datapoint
        if datapoint is None:
            return None
        start_time = datapoint.watering_start_time.isoformat()
        return f"{datapoint.zone_id}:{start_time}"

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


class IrrigationZoneLastWateringSensor(IrrigationZoneReportEntity):
    """Represent the latest watering event marker for one zone."""

    def __init__(
        self,
        coordinator: IrrigationMonitorDataUpdateCoordinator,
        zone_name: str,
        zone_id: int | None,
    ) -> None:
        """Create the event timestamp sensor for one irrigation zone."""
        super().__init__(coordinator, zone_name, zone_id)
        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_{self._zone_key}"
        ).lower()
        self._attr_name = f"Irrigation {zone_name} last watering event"
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


class IrrigationZoneWaterUsedSensor(IrrigationZoneReportEntity):
    """Represent gallons used during the latest watering event for one zone."""

    _attr_device_class = SensorDeviceClass.WATER
    _attr_native_unit_of_measurement = UnitOfVolume.GALLONS

    def __init__(
        self,
        coordinator: IrrigationMonitorDataUpdateCoordinator,
        zone_name: str,
        zone_id: int | None,
    ) -> None:
        """Create the per-event water usage sensor for one irrigation zone."""
        super().__init__(coordinator, zone_name, zone_id)
        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_{self._zone_key}_water_used"
        ).lower()
        self._attr_name = f"Irrigation {zone_name} water used"

    @property
    def native_value(self) -> float | None:
        """Expose gallons used during the latest watering event."""
        datapoint = self._report_datapoint
        if datapoint is None:
            return None
        if datapoint.total_gallons_used is None:
            return None
        return float(datapoint.total_gallons_used)


class IrrigationZoneWaterTotalSensor(IrrigationZoneReportEntity, RestoreEntity):
    """Represent the cumulative gallons observed for one irrigation zone."""

    _attr_device_class = SensorDeviceClass.WATER
    _attr_native_unit_of_measurement = UnitOfVolume.GALLONS
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(
        self,
        coordinator: IrrigationMonitorDataUpdateCoordinator,
        zone_name: str,
        zone_id: int | None,
    ) -> None:
        """Create the cumulative water usage sensor for one irrigation zone."""
        super().__init__(coordinator, zone_name, zone_id)
        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_{self._zone_key}_water_total"
        ).lower()
        self._attr_name = f"Irrigation {zone_name} total water used"
        self._accumulated_gallons: float | None = None
        self._last_processed_event_id: str | None = None

    async def async_added_to_hass(self) -> None:
        """Restore the accumulated total and last processed event after restart."""
        await super().async_added_to_hass()

        if (last_state := await self.async_get_last_state()) is not None:
            try:
                self._accumulated_gallons = float(last_state.state)
            except (TypeError, ValueError):
                self._accumulated_gallons = None
            self._last_processed_event_id = last_state.attributes.get("event_id")

        self._apply_latest_event()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the latest event metadata plus total-tracking details."""
        attributes = super().extra_state_attributes
        attributes["last_processed_event_id"] = self._last_processed_event_id
        return attributes

    @property
    def native_value(self) -> float | None:
        """Expose the cumulative gallons seen for this zone."""
        return self._accumulated_gallons

    def _handle_coordinator_update(self) -> None:
        """Advance the cumulative total when a new watering event appears."""
        self._apply_latest_event()
        super()._handle_coordinator_update()

    def _apply_latest_event(self) -> None:
        """Add the latest event once, keyed by the stable event identifier."""
        datapoint = self._report_datapoint
        event_id = self._event_id
        if datapoint is None or event_id is None:
            return

        gallons_used = datapoint.total_gallons_used
        if gallons_used is None:
            return

        if self._last_processed_event_id == event_id:
            return

        if self._accumulated_gallons is None:
            self._accumulated_gallons = 0.0

        self._accumulated_gallons += float(gallons_used)
        self._last_processed_event_id = event_id
