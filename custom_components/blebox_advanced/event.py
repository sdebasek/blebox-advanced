"""Event entities for BleBox physical inputs.

One entity per physical input, e.g. ``event.kitchen_switch_button_1``. Every
entity advertises all four event types regardless of which callbacks are
currently provisioned, so that a URL a user wires up by hand later always has
somewhere to land.

Unlike the other platforms these are not coordinator-backed: they are driven by
callbacks pushed from the device, and must keep recording presses even while a
poll is failing.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.event import DOMAIN as EVENT_DOMAIN
from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    ATTR_BLEBOX_ID,
    ATTR_BUTTON,
    ATTR_INPUT,
    DOMAIN,
    EVENT_TYPES,
    SIGNAL_INPUT_EVENT,
)
from .coordinator import BleBoxEventsConfigEntry, BleBoxEventsData
from .entity import build_device_info


def input_unique_id(blebox_id: str, input_id: int) -> str:
    """Return the unique id of one input's event entity.

    Shared with the registry fix-up below so the two can never drift apart;
    changing it would orphan every existing event entity.
    """
    return f"{blebox_id}_input_{input_id}"


@callback
def _async_enable_selected_inputs(hass: HomeAssistant, data: BleBoxEventsData) -> None:
    """Undo our own disable for inputs that have since been given events.

    ``entity_registry_enabled_default`` is read once, when the entity is first
    registered, and never again. An input that had nothing selected at setup is
    therefore registered disabled and stays that way for good: the user ticks
    its events in the options, the entry reloads, and no entity appears, which
    looks exactly like the integration being broken. The registry entry has to
    be corrected by hand, on every setup, for the option to mean anything.

    Only a disable this integration made is lifted. A user who deliberately
    disabled a button entity would not thank us for switching it back on at
    every reload, so a ``USER`` disable is left exactly as it is.
    """
    registry = er.async_get(hass)
    for input_id in data.inputs:
        if not data.enabled_events.get(input_id):
            continue
        entity_id = registry.async_get_entity_id(
            EVENT_DOMAIN, DOMAIN, input_unique_id(data.blebox_id, input_id)
        )
        if entity_id is None:
            continue
        registry_entry = registry.async_get(entity_id)
        if (
            registry_entry is not None
            and registry_entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
        ):
            registry.async_update_entity(entity_id, disabled_by=None)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BleBoxEventsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up an event entity for each physical input."""
    data = entry.runtime_data
    _async_enable_selected_inputs(hass, data)
    device_info = build_device_info(entry, data)
    async_add_entities(
        BleBoxInputEventEntity(
            entry.entry_id,
            data,
            input_id,
            device_info,
            # An input nobody selected events for is very likely one the
            # hardware does not physically have, such as the optional external
            # terminal on a switchBox. Register it so a hand configured URL
            # still lands, but keep it out of the way until asked for.
            enabled=bool(data.enabled_events.get(input_id)),
        )
        for input_id in data.inputs
    )


class BleBoxInputEventEntity(EventEntity):
    """A single physical input on a BleBox device."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_device_class = EventDeviceClass.BUTTON
    _attr_event_types = EVENT_TYPES
    _attr_translation_key = "button"

    def __init__(
        self,
        entry_id: str,
        data: BleBoxEventsData,
        input_id: int,
        device_info: DeviceInfo,
        *,
        enabled: bool = True,
    ) -> None:
        """Initialise the entity for one input."""
        self._entry_id = entry_id
        self._data = data
        self._input_id = input_id
        self._attr_unique_id = input_unique_id(data.blebox_id, input_id)
        self._attr_translation_placeholders = {"number": str(input_id + 1)}
        self._attr_device_info = device_info
        self._attr_entity_registry_enabled_default = enabled

    async def async_added_to_hass(self) -> None:
        """Subscribe to callbacks received for this device."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_INPUT_EVENT.format(self._entry_id),
                self._async_handle_input_event,
            )
        )

    @callback
    def _async_handle_input_event(
        self, input_id: int, event_type: str, hints: dict[str, Any] | None = None
    ) -> None:
        """Record an event pushed by the device.

        ``hints`` carries whatever device state the callback URL brought along
        (relay state, power), which is the value at the instant of the press
        rather than at the next poll.
        """
        if input_id != self._input_id:
            return
        self._trigger_event(
            event_type,
            {
                ATTR_INPUT: self._input_id,
                ATTR_BUTTON: self._input_id + 1,
                ATTR_BLEBOX_ID: self._data.blebox_id,
                **(hints or {}),
            },
        )
        self.async_write_ha_state()
