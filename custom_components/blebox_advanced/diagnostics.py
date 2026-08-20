"""Diagnostics for BleBox Advanced.

Reports everything needed to debug a device - model, firmware, detected
inputs, action slot usage, whether automatic configuration is possible and how
each callback is mapped - with the callback token stripped from every URL, so a
diagnostics dump can be shared in a bug report without handing over the secret
that authorises the endpoint.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from .api import callback_url
from .blebox_actions import event_type_for_trigger, is_configured, is_owned
from .const import (
    CONF_API_LEVEL,
    CONF_BASE_URL,
    CONF_DEBOUNCE_MS,
    CONF_DEVICE_CACHE,
    CONF_HW_VERSION,
    CONF_INVERT_EDGES,
    CONF_MODEL,
    CONF_PRODUCT,
    CONF_SW_VERSION,
    EVENT_TYPES,
)
from .coordinator import BleBoxEventsConfigEntry

REDACTED = "**REDACTED**"


def _redact(value: str | None, token: str) -> str | None:
    """Remove the callback token from a URL."""
    if not value:
        return value
    return value.replace(token, REDACTED) if token else value


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: BleBoxEventsConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    data = entry.runtime_data
    token = data.token
    snapshot = data.coordinator.data
    info = snapshot.info if snapshot else None
    actions = snapshot.actions if snapshot else None
    base_url = data.base_url or ""

    slots: dict[str, Any] = {"available": actions is not None}
    device_actions: list[dict[str, Any]] = []
    if actions is not None:
        owned = actions.owned_actions()
        slots.update(
            {
                "total": actions.items_limit,
                "free": len(actions.free_slots()),
                "created_by_this_integration": len(owned),
                "belonging_to_others": len(actions.foreign_actions()),
            }
        )
        for action in actions.actions:
            if not is_configured(action):
                continue
            device_actions.append(
                {
                    "slot": action.get("id"),
                    "input": action.get("input"),
                    "trigger_type": action.get("triggerType"),
                    "action_type": action.get("actionType"),
                    "owned_by_integration": is_owned(action),
                    # Foreign action targets can carry credentials of their own.
                    "param": _redact(action.get("param"), token)
                    if is_owned(action)
                    else REDACTED,
                }
            )

    return {
        "device": {
            "host": entry.data.get("host"),
            "model": entry.data.get(CONF_MODEL),
            "product": entry.data.get(CONF_PRODUCT),
            "firmware_version": info.firmware_version
            if info
            else entry.data.get(CONF_SW_VERSION),
            "hardware_version": info.hardware_version
            if info
            else entry.data.get(CONF_HW_VERSION),
            "api_level": info.api_level if info else entry.data.get(CONF_API_LEVEL),
            "reachable": data.coordinator.last_update_success,
            # Answers the question an unreachable device raises: are these
            # entities here because the device is answering, or because the
            # shape it last had was remembered?
            "capabilities_remembered": bool(entry.data.get(CONF_DEVICE_CACHE)),
        },
        "inputs": {
            "detected": data.inputs,
            "supported_event_types": EVENT_TYPES,
            "enabled_events": {
                str(key): value for key, value in data.enabled_events.items()
            },
        },
        "configuration": {
            "mode": data.mode,
            "automatic_supported": data.provisioning.supported,
            "automatic_attempted": data.provisioning.attempted,
            "automatic_error": data.provisioning.error,
            "debounce_ms": entry.options.get(CONF_DEBOUNCE_MS),
            "invert_edges": entry.options.get(CONF_INVERT_EDGES),
            "base_url_configured": bool(entry.options.get(CONF_BASE_URL)),
        },
        "action_slots": slots,
        "device_actions": device_actions,
        "callback_mappings": [
            {
                "input": input_id,
                "button": input_id + 1,
                "event_type": event_type,
                "blebox_trigger_type": _trigger_for(event_type, data.invert_edges),
                "url": _redact(
                    callback_url(base_url, token, input_id, event_type), token
                )
                if base_url
                else f"<home assistant url>/api/blebox_advanced/{REDACTED}"
                f"/{input_id}/{event_type}",
            }
            for input_id in sorted(data.enabled_events)
            for event_type in EVENT_TYPES
            if event_type in data.enabled_events[input_id]
        ],
    }


def _trigger_for(event_type: str, invert_edges: bool) -> int | None:
    """Report the device trigger type an event maps to, for cross-checking."""
    for trigger_type in range(1, 6):
        if (
            event_type_for_trigger(trigger_type, invert_edges=invert_edges)
            == event_type
        ):
            return trigger_type
    return None
