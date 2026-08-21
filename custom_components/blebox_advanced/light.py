"""Button backlight for BleBox devices that have one.

Some BleBox switches have illuminated buttons with a settable RGB colour,
exposed through ``settings.buttonsBacklight`` and not surfaced by the official
integration.
"""

from __future__ import annotations

from typing import Any, ClassVar

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_RGB_COLOR,
    ColorMode,
    LightEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DEFAULT_BACKLIGHT_COLOR, SETTING_BACKLIGHT
from .coordinator import BleBoxEventsConfigEntry
from .entity import BleBoxDeviceEntity

RGB_HEX_LENGTH = 6
MAX_BRIGHTNESS = 255

DEFAULT_RGB: tuple[int, int, int] = (
    int(DEFAULT_BACKLIGHT_COLOR[0:2], 16),
    int(DEFAULT_BACKLIGHT_COLOR[2:4], 16),
    int(DEFAULT_BACKLIGHT_COLOR[4:6], 16),
)
"""The fallback colour as a triple, derived so the two forms cannot disagree."""


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


def _to_hex(rgb: tuple[int, int, int]) -> str:
    """Format an RGB triple the way the device stores it."""
    return f"{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"


def _rescale(rgb: tuple[int, int, int], factor: float) -> tuple[int, int, int]:
    """Multiply an RGB triple, keeping every channel inside a byte."""
    return (
        min(MAX_BRIGHTNESS, round(rgb[0] * factor)),
        min(MAX_BRIGHTNESS, round(rgb[1] * factor)),
        min(MAX_BRIGHTNESS, round(rgb[2] * factor)),
    )


class BleBoxBacklightLight(BleBoxDeviceEntity, LightEntity):
    """The illuminated buttons on the switch.

    The device stores one ``rrggbb`` value and has no separate brightness
    field, so brightness is *inside* the colour: ``804000`` is the same hue as
    ``ff8000`` at half power. Home Assistant has no colour mode meaning
    "settable colour, no brightness" - every colour mode implies brightness -
    so the two are reported apart and folded back together on the way to the
    device, which is what the hardware can honestly do.
    """

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

    def _stored_rgb(self) -> tuple[int, int, int] | None:
        """Return the colour as the device holds it, brightness included."""
        return _parse_hex(self.setting(SETTING_BACKLIGHT, "color"))

    @property
    def brightness(self) -> int | None:
        """How far up the stored colour is scaled, as Home Assistant counts it."""
        rgb = self._stored_rgb()
        return max(rgb) if rgb is not None else None

    @property
    def rgb_color(self) -> tuple[int, int, int] | None:
        """Current backlight colour, scaled back up to full brightness."""
        rgb = self._stored_rgb()
        if rgb is None or (top := max(rgb)) == 0:
            # All-zero is not a hue, and there is no factor to undo either.
            return None
        return _rescale(rgb, MAX_BRIGHTNESS / top)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable the backlight, optionally setting its colour or brightness."""
        patch: dict[str, Any] = {"enabled": 1}
        rgb = kwargs.get(ATTR_RGB_COLOR)
        brightness = kwargs.get(ATTR_BRIGHTNESS)
        if rgb is not None or brightness is not None:
            # Either half can arrive on its own - the colour picker sends a hue
            # and the slider sends a level - but the device stores only their
            # product, so whichever is missing comes from what it holds now.
            # Falling back to full brightness when it holds nothing (or holds
            # black) keeps "give this backlight a colour" from writing another
            # invisible one; a level of 0 never reaches here, because Home
            # Assistant routes that to `async_turn_off` instead.
            base = rgb if rgb is not None else self.rgb_color or DEFAULT_RGB
            level = brightness if brightness is not None else self.brightness
            factor = (level or MAX_BRIGHTNESS) / MAX_BRIGHTNESS
            patch["color"] = _to_hex(_rescale(base, factor))
        elif self.rgb_color is None:
            # Never enabled before and the device reported no colour; without
            # one it would light up as nothing.
            patch["color"] = DEFAULT_BACKLIGHT_COLOR
        await self.async_patch_settings({SETTING_BACKLIGHT: patch})

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable the backlight."""
        await self.async_patch_settings({SETTING_BACKLIGHT: {"enabled": 0}})
