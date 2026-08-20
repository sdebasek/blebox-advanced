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
from .coordinator import BleBoxEventsConfigEntry, CallbackHealth
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

    entities: list[BleBoxDeviceEntity] = [BleBoxCallbackHealthBinarySensor(entry)]
    switch_state = snapshot.state.get("switch")
    if isinstance(switch_state, dict) and isinstance(switch_state.get("safety"), dict):
        entities.append(BleBoxSafetyBinarySensor(entry))

    power = snapshot.settings.get(SETTING_POWER_MEASURING)
    if isinstance(power, dict) and isinstance(power.get("factoryCalibration"), dict):
        entities.append(BleBoxCalibrationBinarySensor(entry))

    async_add_entities(entities)


class BleBoxCallbackHealthBinarySensor(BleBoxDeviceEntity, BinarySensorEntity):
    """On when the device's callbacks are not reaching Home Assistant.

    An HTTP action is fire-and-forget, so without reading back what the device
    recorded there is no way to tell a switch that cannot reach Home Assistant
    from one nobody has pressed. This makes that distinction visible and
    automatable.
    """

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: BleBoxEventsConfigEntry) -> None:
        """Initialise the callback health sensor."""
        super().__init__(entry, "callback_delivery")

    def _health(self) -> CallbackHealth:
        snapshot = self.coordinator.data
        return snapshot.health if snapshot else CallbackHealth()

    @property
    def is_on(self) -> bool:
        """Whether callbacks have fired but none are getting through."""
        return self._health().problem

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Break the verdict down so the cause is actionable."""
        health = self._health()
        return {
            "configured": health.configured,
            "delivered": health.delivered,
            "unreachable": health.unreachable,
            "rejected": health.rejected,
            "last_status": health.last_status,
        }


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
