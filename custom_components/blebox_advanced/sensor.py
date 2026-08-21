"""What the device measures, and how long it has been measuring it.

Active power and energy are published here because this integration replaces
the official one rather than adding to it; they are created whenever the device
reports the values, and are ordinary, enabled sensors. The energy sensor
deliberately carries no state class - see :class:`BleBoxEnergySensor` for why a
value that resets each period has to stay out of long-term statistics.

Uptime and the timed-operation countdown are diagnostic and disabled by
default. They change on every poll, so leaving them on would fill the recorder
for values most setups never look at; enable either one per entity if you want
it.
"""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfEnergy, UnitOfPower, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import BleBoxEventsConfigEntry, relay_list, timed_relays
from .entity import BleBoxDeviceEntity, BleBoxRelayEntity


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

    if _active_power(snapshot.state) is not None:
        entities.append(BleBoxActivePowerSensor(entry))
    if _energy(snapshot.state) is not None:
        entities.append(BleBoxEnergySensor(entry))

    if snapshot.uptime_s is not None:
        entities.append(BleBoxUptimeSensor(entry))

    # Deliberately the coordinator's predicate rather than a local copy of it:
    # `capability_signature` decides whether the device's remembered shape is
    # still current from the same function, so a second copy drifting would
    # give a device restarted while offline the wrong countdown sensors.
    multi = len(relay_list(snapshot.state)) > 1
    entities.extend(
        BleBoxCountdownSensor(entry, index, multi=multi)
        for index in timed_relays(snapshot.state)
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


class BleBoxCountdownSensor(BleBoxRelayEntity, SensorEntity):
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
            entry, "countdown", index, multi=multi, numbered_key="countdown_relay"
        )

    @property
    def native_value(self) -> int | None:
        """Seconds remaining before this relay reverts."""
        value = self.relay_field(self.device_state, "forTimeLeftS")
        return value if isinstance(value, int) else None


def _active_power(state: dict) -> float | None:
    """Active power in watts, if the device measures it."""
    for sensor in state.get("sensors") or []:
        if isinstance(sensor, dict) and sensor.get("type") == "activePower":
            value = sensor.get("value")
            if isinstance(value, (int, float)):
                return float(value)
    return None


def _energy(state: dict) -> float | None:
    """Energy for the current measurement period, in kWh."""
    measuring = state.get("powerMeasuring")
    if not isinstance(measuring, dict):
        return None
    periods = measuring.get("powerConsumption")
    if not isinstance(periods, list) or not periods:
        return None
    first = periods[0]
    if not isinstance(first, dict):
        return None
    value = first.get("value")
    return float(value) if isinstance(value, (int, float)) else None


class BleBoxActivePowerSensor(BleBoxDeviceEntity, SensorEntity):
    """Power the load is drawing right now."""

    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, entry: BleBoxEventsConfigEntry) -> None:
        """Initialise the active power sensor."""
        super().__init__(entry, "active_power")

    @property
    def native_value(self) -> float | None:
        """Current active power."""
        return _active_power(self.device_state)


class BleBoxEnergySensor(BleBoxDeviceEntity, SensorEntity):
    """Energy used in the device's current measurement period.

    Matches the official integration's definition: no device class and no state
    class, because the value resets each period and would corrupt long-term
    statistics if treated as a total.
    """

    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_suggested_display_precision = 2

    def __init__(self, entry: BleBoxEventsConfigEntry) -> None:
        """Initialise the energy sensor."""
        super().__init__(entry, "power_consumption")

    @property
    def native_value(self) -> float | None:
        """Energy for the current period."""
        return _energy(self.device_state)

    @property
    def extra_state_attributes(self) -> dict[str, int]:
        """Expose how long the current period has been running."""
        measuring = self.device_state.get("powerMeasuring") or {}
        periods = measuring.get("powerConsumption") or [{}]
        period = periods[0].get("periodS") if isinstance(periods[0], dict) else None
        return {"period_s": period} if isinstance(period, int) else {}
