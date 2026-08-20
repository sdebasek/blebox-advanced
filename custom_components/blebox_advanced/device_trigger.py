"""Device automation triggers for BleBox physical inputs.

Surfaces every input/event combination under Settings -> Automations -> Device,
e.g. "Kitchen switch: Button 1 short pressed", so users never have to listen to
a raw bus event or webhook themselves.

The triggers hang off the bus event fired by :mod:`api` rather than off the
event entity, so they keep working if a user renames or hides the entity.
"""

from __future__ import annotations

import voluptuous as vol
from homeassistant.components.device_automation import DEVICE_TRIGGER_BASE_SCHEMA
from homeassistant.components.homeassistant.triggers import event as event_trigger
from homeassistant.const import (
    CONF_DEVICE_ID,
    CONF_DOMAIN,
    CONF_PLATFORM,
    CONF_TYPE,
)
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.trigger import TriggerActionType, TriggerInfo
from homeassistant.helpers.typing import ConfigType

from .const import (
    ATTR_EVENT_TYPE,
    ATTR_INPUT,
    CONF_INPUTS,
    DOMAIN,
    EVENT_TYPES,
    HA_EVENT,
)

CONF_SUBTYPE = "subtype"


def _button_number(value: object) -> str:
    """Validate the subtype as a 1-based button number."""
    try:
        number = int(cv.string(value))
    except ValueError:
        raise vol.Invalid("Button number must be numeric") from None
    if number < 1:
        raise vol.Invalid("Button numbers start at 1")
    return str(number)


TRIGGER_SCHEMA = DEVICE_TRIGGER_BASE_SCHEMA.extend(
    {
        vol.Required(CONF_TYPE): vol.In(EVENT_TYPES),
        vol.Required(CONF_SUBTYPE): _button_number,
    }
)


async def async_get_triggers(
    hass: HomeAssistant, device_id: str
) -> list[dict[str, str]]:
    """List the triggers this integration offers for a device.

    Inputs are read from the config entry rather than runtime data so triggers
    still appear in the automation editor while the device is unreachable.
    """
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        return []

    inputs: set[int] = set()
    for entry_id in device.config_entries:
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != DOMAIN:
            continue
        for index in entry.data.get(CONF_INPUTS, []):
            inputs.add(int(index))

    return [
        {
            CONF_PLATFORM: "device",
            CONF_DOMAIN: DOMAIN,
            CONF_DEVICE_ID: device_id,
            CONF_TYPE: event_type,
            CONF_SUBTYPE: str(input_id + 1),
        }
        for input_id in sorted(inputs)
        for event_type in EVENT_TYPES
    ]


async def async_attach_trigger(
    hass: HomeAssistant,
    config: ConfigType,
    action: TriggerActionType,
    trigger_info: TriggerInfo,
) -> CALLBACK_TYPE:
    """Attach a trigger to the bus event carrying our input events."""
    event_config = event_trigger.TRIGGER_SCHEMA(
        {
            CONF_PLATFORM: "event",
            event_trigger.CONF_EVENT_TYPE: HA_EVENT,
            event_trigger.CONF_EVENT_DATA: {
                CONF_DEVICE_ID: config[CONF_DEVICE_ID],
                ATTR_INPUT: int(config[CONF_SUBTYPE]) - 1,
                ATTR_EVENT_TYPE: config[CONF_TYPE],
            },
        }
    )
    return await event_trigger.async_attach_trigger(
        hass, event_config, action, trigger_info, platform_type="device"
    )
