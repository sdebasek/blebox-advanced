"""Relay behaviour after a power cut, one entity per relay.

The device reports no constraint metadata for this field, so the value mapping
is inferred from BleBox's convention rather than read from the hardware. If a
device ever disagrees, only this module needs correcting.
"""

from __future__ import annotations

from typing import Any, ClassVar

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .blebox_actions import find_native_action, trigger_type_for_event
from .const import (
    BUTTON_ACTION_OPTIONS,
    MANAGED_BUTTON_EVENTS,
    RESTART_STATE_OPTIONS,
    SETTING_RELAYS,
)
from .coordinator import BleBoxEventsConfigEntry
from .entity import BleBoxDeviceEntity

_BY_VALUE = {value: name for name, value in RESTART_STATE_OPTIONS.items()}
_BUTTON_BY_VALUE = {value: name for name, value in BUTTON_ACTION_OPTIONS.items()}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BleBoxEventsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up one restart-behaviour select per relay the device reports."""
    snapshot = entry.runtime_data.coordinator.data
    if not snapshot:
        return
    relays = snapshot.settings.get(SETTING_RELAYS)
    if not isinstance(relays, list):
        relays = []
    entities: list[BleBoxDeviceEntity] = [
        BleBoxRestartStateSelect(entry, index, multi=len(relays) > 1)
        for index in range(len(relays))
    ]

    data = entry.runtime_data
    if data.manage_buttons:
        entities.extend(
            BleBoxButtonActionSelect(entry, input_id, event_type)
            for input_id in data.inputs
            for event_type in MANAGED_BUTTON_EVENTS
        )

    async_add_entities(entities)


class BleBoxRestartStateSelect(BleBoxDeviceEntity, SelectEntity):
    """What a relay does when the device powers back up."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_options: ClassVar[list[str]] = list(RESTART_STATE_OPTIONS)

    def __init__(
        self, entry: BleBoxEventsConfigEntry, index: int, *, multi: bool
    ) -> None:
        """Initialise the select for one relay.

        Relay 0 keeps the unnumbered unique id it has always had, so adding
        multi-relay support does not orphan entities on single-relay devices.
        """
        super().__init__(
            entry,
            "state_after_restart" if index == 0 else f"state_after_restart_{index}",
            translation_key=(
                "state_after_restart_relay" if multi else "state_after_restart"
            ),
            placeholders={"relay": str(index + 1)} if multi else None,
        )
        self._index = index

    def _relays(self) -> list[Any]:
        relays = self.setting(SETTING_RELAYS)
        return relays if isinstance(relays, list) else []

    @property
    def current_option(self) -> str | None:
        """Current restart behaviour, or None if the device reports something new."""
        relays = self._relays()
        if self._index >= len(relays):
            return None
        relay = relays[self._index]
        if not isinstance(relay, dict):
            return None
        return _BY_VALUE.get(relay.get("stateAfterRestart"))

    async def async_select_option(self, option: str) -> None:
        """Set this relay's restart behaviour.

        The whole relay list is round-tripped with only this field changed, so
        sibling settings (default on-time, icon set) survive the write and the
        other relays keep their own configuration.

        That list comes from the coordinator's settings, which prefer whatever
        any entity on this device wrote most recently. Reading a per-entity copy
        instead used to revert a sibling relay: its select had already written a
        new value, this one still saw the poll from before it and sent it back.
        """
        relays = self._relays()
        if self._index >= len(relays):
            return
        payload = [
            dict(relay) if isinstance(relay, dict) else relay for relay in relays
        ]
        payload[self._index]["stateAfterRestart"] = RESTART_STATE_OPTIONS[option]
        await self.async_patch_settings({SETTING_RELAYS: payload})


class BleBoxButtonActionSelect(BleBoxDeviceEntity, SelectEntity):
    """What a physical button does to the relay, locally on the device.

    Opt-in, because unlike everything else here this edits action slots the
    user configured themselves. Only slots holding a native relay action are
    ever written - see ``blebox_actions.async_set_native_action``.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_options: ClassVar[list[str]] = list(BUTTON_ACTION_OPTIONS)

    def __init__(
        self, entry: BleBoxEventsConfigEntry, input_id: int, event_type: str
    ) -> None:
        """Initialise the select for one button and press type."""
        super().__init__(
            entry,
            f"button_action_{input_id}_{event_type}",
            translation_key=f"button_action_{event_type}",
            placeholders={"button": str(input_id + 1)},
        )
        self._input_id = input_id
        self._trigger = trigger_type_for_event(event_type)

    @property
    def current_option(self) -> str | None:
        """The relay action currently bound to this button and press type."""
        snapshot = self.coordinator.data
        if snapshot is None or snapshot.actions is None:
            return None
        action = find_native_action(snapshot.actions, self._input_id, self._trigger)
        return _BUTTON_BY_VALUE.get(action.get("actionType") if action else 0)

    async def async_select_option(self, option: str) -> None:
        """Rebind this button to a different relay action.

        A full refresh is asked for, not just a refresh: action slots are only
        read on the coordinator's slow cycle, so an ordinary poll would answer
        with the binding from before the write and snap the control back to its
        old value until that cycle came round.

        A rebind is the one write here that can fail for a reason the user can
        fix on the device itself, so a full slot array earns its own message
        rather than the generic "could not write" one.
        """
        with self.write_errors():
            await self._data.manager.async_set_native_action(
                self._input_id,
                self._trigger,
                BUTTON_ACTION_OPTIONS[option],
            )
        self.coordinator.async_request_full_refresh()
        await self.coordinator.async_request_refresh()
