"""Diagnostic sensors.

Deliberately limited to values the official integration does not already
publish — power and energy stay entirely with it, so nothing here competes for
the same statistics.
"""

from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import BleBoxEventsConfigEntry
from .entity import BleBoxDeviceEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BleBoxEventsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up whichever diagnostic sensors this device supports."""
    snapshot = entry.runtime_data.coordinator.data
    if not snapshot:
        return

    entities: list[BleBoxDeviceEntity] = []
    if snapshot.uptime_s is not None:
        entities.append(BleBoxUptimeSensor(entry))

    relays = snapshot.state.get("relays")
    if (
        isinstance(relays, list)
        and relays
        and isinstance(relays[0], dict)
        and "forTimeLeftS" in relays[0]
    ):
        entities.append(BleBoxCountdownSensor(entry))

    async_add_entities(entities)


class BleBoxUptimeSensor(BleBoxDeviceEntity, SensorEntity):
    """How long the device has been running since its last restart."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS

    def __init__(self, entry: BleBoxEventsConfigEntry) -> None:
        """Initialise the uptime sensor."""
        super().__init__(entry, "uptime")

    @property
    def native_value(self) -> int | None:
        """Seconds since the device booted."""
        snapshot = self.coordinator.data
        return snapshot.uptime_s if snapshot else None


class BleBoxCountdownSensor(BleBoxDeviceEntity, SensorEntity):
    """Time left on a timed relay operation, zero when none is running."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS

    def __init__(self, entry: BleBoxEventsConfigEntry) -> None:
        """Initialise the countdown sensor."""
        super().__init__(entry, "countdown")

    @property
    def native_value(self) -> int | None:
        """Seconds remaining before the relay reverts."""
        relays = self.device_state.get("relays")
        if not isinstance(relays, list) or not relays:
            return None
        first = relays[0]
        if not isinstance(first, dict):
            return None
        value = first.get("forTimeLeftS")
        return value if isinstance(value, int) else None
