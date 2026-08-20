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

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    ATTR_BLEBOX_ID,
    ATTR_BUTTON,
    ATTR_INPUT,
    EVENT_TYPES,
    SIGNAL_INPUT_EVENT,
)
from .coordinator import BleBoxEventsConfigEntry, BleBoxEventsData
from .entity import build_device_info


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BleBoxEventsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up an event entity for each physical input."""
    data = entry.runtime_data
    device_info = build_device_info(entry, data)
    async_add_entities(
        BleBoxInputEventEntity(entry.entry_id, data, input_id, device_info)
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
    ) -> None:
        """Initialise the entity for one input."""
        self._entry_id = entry_id
        self._data = data
        self._input_id = input_id
        self._attr_unique_id = f"{data.blebox_id}_input_{input_id}"
        self._attr_translation_placeholders = {"number": str(input_id + 1)}
        self._attr_device_info = device_info

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
    def _async_handle_input_event(self, input_id: int, event_type: str) -> None:
        """Record an event pushed by the device."""
        if input_id != self._input_id:
            return
        self._trigger_event(
            event_type,
            {
                ATTR_INPUT: self._input_id,
                ATTR_BUTTON: self._input_id + 1,
                ATTR_BLEBOX_ID: self._data.blebox_id,
            },
        )
        self.async_write_ha_state()
