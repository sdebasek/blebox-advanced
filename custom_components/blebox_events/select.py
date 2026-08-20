"""Relay behaviour after a power cut.

The device reports no constraint metadata for this field, so the value mapping
is inferred from BleBox's convention rather than read from the hardware. If a
device ever disagrees, only this module needs correcting.
"""

from __future__ import annotations

from typing import ClassVar

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import RESTART_STATE_OPTIONS, SETTING_RELAYS
from .coordinator import BleBoxEventsConfigEntry
from .entity import BleBoxDeviceEntity

_BY_VALUE = {value: name for name, value in RESTART_STATE_OPTIONS.items()}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BleBoxEventsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the restart-behaviour select, if this device has a relay."""
    snapshot = entry.runtime_data.coordinator.data
    if not snapshot:
        return
    relays = snapshot.settings.get(SETTING_RELAYS)
    if not isinstance(relays, list) or not relays:
        return
    async_add_entities([BleBoxRestartStateSelect(entry)])


class BleBoxRestartStateSelect(BleBoxDeviceEntity, SelectEntity):
    """What the relay does when the device powers back up."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_options: ClassVar[list[str]] = list(RESTART_STATE_OPTIONS)

    def __init__(self, entry: BleBoxEventsConfigEntry) -> None:
        """Initialise the select entity."""
        super().__init__(entry, "state_after_restart")

    @property
    def current_option(self) -> str | None:
        """Current restart behaviour, or None if the device reports something new."""
        relays = self.setting(SETTING_RELAYS)
        if not isinstance(relays, list) or not relays:
            return None
        first = relays[0]
        if not isinstance(first, dict):
            return None
        return _BY_VALUE.get(first.get("stateAfterRestart"))

    async def async_select_option(self, option: str) -> None:
        """Set the restart behaviour.

        The whole relay list is round-tripped with only this field changed, so
        sibling settings (default on-time, icon set) survive the write and
        multi-relay devices keep their other entries intact.
        """
        relays = self.setting(SETTING_RELAYS)
        if not isinstance(relays, list) or not relays:
            return
        payload = [
            dict(entry) if isinstance(entry, dict) else entry for entry in relays
        ]
        payload[0]["stateAfterRestart"] = RESTART_STATE_OPTIONS[option]
        await self.async_patch_settings({SETTING_RELAYS: payload})
