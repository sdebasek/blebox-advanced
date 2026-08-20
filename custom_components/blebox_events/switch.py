"""Device configuration switches.

Neither of these is exposed by the official integration. The cloud tunnel one
matters most: BleBox devices hold an outbound tunnel to BleBox's cloud by
default, and turning it off is the difference between a genuinely local device
and one that merely happens to be controlled locally.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import SETTING_STATUS_LED, SETTING_TUNNEL
from .coordinator import BleBoxEventsConfigEntry
from .entity import BleBoxDeviceEntity

SWITCHES: list[tuple[str, str]] = [
    ("cloud_tunnel", SETTING_TUNNEL),
    ("status_led", SETTING_STATUS_LED),
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BleBoxEventsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up whichever configuration switches this device reports."""
    snapshot = entry.runtime_data.coordinator.data
    if not snapshot:
        return
    async_add_entities(
        BleBoxSettingSwitch(entry, key, setting)
        for key, setting in SWITCHES
        if isinstance(snapshot.settings.get(setting), dict)
    )


class BleBoxSettingSwitch(BleBoxDeviceEntity, SwitchEntity):
    """A device setting that is simply on or off."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry: BleBoxEventsConfigEntry, key: str, setting: str) -> None:
        """Initialise the switch for one settings key."""
        super().__init__(entry, key)
        self._setting = setting

    @property
    def is_on(self) -> bool:
        """Whether the setting is enabled."""
        return bool(self.setting(self._setting, "enabled"))

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable the setting."""
        await self.async_patch_settings({self._setting: {"enabled": 1}})

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable the setting."""
        await self.async_patch_settings({self._setting: {"enabled": 0}})
