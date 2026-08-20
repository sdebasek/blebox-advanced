"""Button backlight for BleBox devices that have one.

Some BleBox switches have illuminated buttons with a settable RGB colour,
exposed through ``settings.buttonsBacklight`` and not surfaced by the official
integration.
"""

from __future__ import annotations

from typing import Any, ClassVar

from homeassistant.components.light import ColorMode, LightEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DEFAULT_BACKLIGHT_COLOR, SETTING_BACKLIGHT
from .coordinator import BleBoxEventsConfigEntry
from .entity import BleBoxDeviceEntity

RGB_HEX_LENGTH = 6


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BleBoxEventsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the backlight, if this device reports one."""
    snapshot = entry.runtime_data.coordinator.data
    if not snapshot or SETTING_BACKLIGHT not in snapshot.settings:
        return
    async_add_entities([BleBoxBacklightLight(entry)])


def _parse_hex(value: Any) -> tuple[int, int, int] | None:
    """Parse a ``"rrggbb"`` string into an RGB tuple."""
    if not isinstance(value, str) or len(value) != RGB_HEX_LENGTH:
        return None
    try:
        return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))
    except ValueError:
        return None


class BleBoxBacklightLight(BleBoxDeviceEntity, LightEntity):
    """The illuminated buttons on the switch."""

    _attr_translation_key = "buttons_backlight"
    _attr_color_mode = ColorMode.RGB
    _attr_supported_color_modes: ClassVar[set[ColorMode]] = {ColorMode.RGB}

    def __init__(self, entry: BleBoxEventsConfigEntry) -> None:
        """Initialise the backlight entity."""
        super().__init__(entry, "buttons_backlight")

    @property
    def is_on(self) -> bool:
        """Whether the backlight is enabled."""
        return bool(self.setting(SETTING_BACKLIGHT, "enabled"))

    @property
    def rgb_color(self) -> tuple[int, int, int] | None:
        """Current backlight colour."""
        return _parse_hex(self.setting(SETTING_BACKLIGHT, "color"))

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable the backlight, optionally setting its colour."""
        patch: dict[str, Any] = {"enabled": 1}
        if (rgb := kwargs.get("rgb_color")) is not None:
            patch["color"] = f"{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"
        elif self.rgb_color is None:
            # Never enabled before and the device reported no colour; without
            # one it would light up as nothing.
            patch["color"] = DEFAULT_BACKLIGHT_COLOR
        await self.async_patch_settings({SETTING_BACKLIGHT: patch})

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable the backlight."""
        await self.async_patch_settings({SETTING_BACKLIGHT: {"enabled": 0}})
