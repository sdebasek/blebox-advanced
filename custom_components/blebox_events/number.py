"""Overload protection threshold.

The device switches its relay off when measured power exceeds this value. Zero
means the protection is disabled; otherwise the device enforces its own minimum,
so the accepted range is read from the device rather than hardcoded.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.number import NumberDeviceClass, NumberEntity
from homeassistant.const import EntityCategory, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    DOMAIN,
    OVERLOAD_MAX,
    OVERLOAD_MIN,
    OVERLOAD_OFF,
    SETTING_POWER_MEASURING,
)
from .coordinator import BleBoxEventsConfigEntry
from .entity import BleBoxDeviceEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BleBoxEventsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the overload threshold, if this device measures power."""
    snapshot = entry.runtime_data.coordinator.data
    if not snapshot:
        return
    power = snapshot.settings.get(SETTING_POWER_MEASURING)
    if not isinstance(power, dict) or not isinstance(power.get("safetyValue"), dict):
        return
    async_add_entities([BleBoxOverloadNumber(entry)])


class BleBoxOverloadNumber(BleBoxDeviceEntity, NumberEntity):
    """Power threshold above which the device cuts its own relay."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_device_class = NumberDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_native_step = 1
    _attr_native_min_value = float(OVERLOAD_OFF)

    def __init__(self, entry: BleBoxEventsConfigEntry) -> None:
        """Initialise the threshold entity."""
        super().__init__(entry, "overload_threshold")

    def _limits(self) -> tuple[int, int]:
        """Return the accepted non-zero range, from the device's own constraints."""
        prefs = self.setting(
            SETTING_POWER_MEASURING, "safetyValue", "fieldsPreferences"
        )
        if isinstance(prefs, list):
            for pref in prefs:
                if isinstance(pref, dict) and pref.get("name") == "activePower":
                    minimum = pref.get("minValue")
                    maximum = pref.get("maxValue")
                    if isinstance(minimum, int) and isinstance(maximum, int):
                        return minimum, maximum
        return OVERLOAD_MIN, OVERLOAD_MAX

    @property
    def native_max_value(self) -> float:
        """Highest threshold the device accepts."""
        return float(self._limits()[1])

    @property
    def native_value(self) -> float | None:
        """Current threshold; 0 means the protection is off."""
        value = self.setting(SETTING_POWER_MEASURING, "safetyValue", "activePower")
        return float(value) if isinstance(value, (int, float)) else None

    async def async_set_native_value(self, value: float) -> None:
        """Set the threshold, rejecting values the device would refuse."""
        target = int(value)
        minimum, maximum = self._limits()
        if target != OVERLOAD_OFF and not minimum <= target <= maximum:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="overload_out_of_range",
                translation_placeholders={
                    "minimum": str(minimum),
                    "maximum": str(maximum),
                },
            )
        await self.async_patch_settings(
            {SETTING_POWER_MEASURING: {"safetyValue": {"activePower": target}}}
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the off sentinel so automations need not hardcode it."""
        return {"disabled_value": OVERLOAD_OFF}
