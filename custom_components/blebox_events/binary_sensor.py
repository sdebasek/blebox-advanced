"""Safety and diagnostic binary sensors.

The overload protection state is the useful one: when the device trips its own
relay it records why, and nothing else in Home Assistant surfaces that.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import SETTING_POWER_MEASURING
from .coordinator import BleBoxEventsConfigEntry
from .entity import BleBoxDeviceEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BleBoxEventsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up whichever binary sensors this device supports."""
    snapshot = entry.runtime_data.coordinator.data
    if not snapshot:
        return

    entities: list[BleBoxDeviceEntity] = []
    switch_state = snapshot.state.get("switch")
    if isinstance(switch_state, dict) and isinstance(switch_state.get("safety"), dict):
        entities.append(BleBoxSafetyBinarySensor(entry))

    power = snapshot.settings.get(SETTING_POWER_MEASURING)
    if isinstance(power, dict) and isinstance(power.get("factoryCalibration"), dict):
        entities.append(BleBoxCalibrationBinarySensor(entry))

    async_add_entities(entities)


class BleBoxSafetyBinarySensor(BleBoxDeviceEntity, BinarySensorEntity):
    """On when the device's overload protection has tripped."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, entry: BleBoxEventsConfigEntry) -> None:
        """Initialise the safety sensor."""
        super().__init__(entry, "safety_triggered")

    def _safety(self) -> dict[str, Any]:
        switch_state = self.device_state.get("switch")
        safety = switch_state.get("safety") if isinstance(switch_state, dict) else None
        return safety if isinstance(safety, dict) else {}

    @property
    def is_on(self) -> bool:
        """Whether a safety condition is currently active."""
        safety = self._safety()
        triggered = safety.get("triggered")
        return bool(triggered) or bool(safety.get("eventReason"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the raw reason so the cause is not lost."""
        safety = self._safety()
        return {
            "event_reason": safety.get("eventReason"),
            "triggered": safety.get("triggered") or [],
        }


class BleBoxCalibrationBinarySensor(BleBoxDeviceEntity, BinarySensorEntity):
    """On when the power measurement has been factory calibrated."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: BleBoxEventsConfigEntry) -> None:
        """Initialise the calibration sensor."""
        super().__init__(entry, "power_calibrated")

    @property
    def is_on(self) -> bool:
        """Whether the device reports itself calibrated."""
        return bool(
            self.setting(SETTING_POWER_MEASURING, "factoryCalibration", "isCalibrated")
        )
