"""Uptime and timed-operation countdown.

Both are diagnostic and disabled by default. They change on every poll, so
leaving them on would fill the recorder for values most setups never look at;
enable either one per entity if you want it.

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
    if isinstance(relays, list):
        timed = [
            index
            for index, relay in enumerate(relays)
            if isinstance(relay, dict) and "forTimeLeftS" in relay
        ]
        entities.extend(
            BleBoxCountdownSensor(entry, index, multi=len(relays) > 1)
            for index in timed
        )

    async_add_entities(entities)


class BleBoxUptimeSensor(BleBoxDeviceEntity, SensorEntity):
    """How long the device has been running since its last restart."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
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
    _attr_entity_registry_enabled_default = False
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS

    def __init__(
        self, entry: BleBoxEventsConfigEntry, index: int, *, multi: bool
    ) -> None:
        """Initialise the countdown sensor for one relay."""
        super().__init__(
            entry,
            "countdown" if index == 0 else f"countdown_{index}",
            translation_key="countdown_relay" if multi else "countdown",
            placeholders={"relay": str(index + 1)} if multi else None,
        )
        self._index = index

    @property
    def native_value(self) -> int | None:
        """Seconds remaining before this relay reverts."""
        relays = self.device_state.get("relays")
        if not isinstance(relays, list) or self._index >= len(relays):
            return None
        relay = relays[self._index]
        if not isinstance(relay, dict):
            return None
        value = relay.get("forTimeLeftS")
        return value if isinstance(value, int) else None
