"""Tests for the configuration and diagnostic entities.

Fixtures are the real payloads from a Simon 55 GO, so these also pin down the
settings-patch shapes actually sent to the device.
"""

from __future__ import annotations

import time
from contextlib import ExitStack
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.blebox_events.const import DOMAIN

from .test_integration import (
    BLEBOX_ID,
    DEVICE,
    EXTENDED_STATE,
    MANAGER,
    SETTINGS,
    UPTIME_S,
    _actions_state,
    _entry,
)


def _reads(settings: dict | None = None, state: dict | None = None) -> ExitStack:
    """Patch every device read the coordinator performs."""
    stack = ExitStack()
    stack.enter_context(patch(f"{MANAGER}.async_get_device_info", return_value=DEVICE))
    stack.enter_context(
        patch(f"{MANAGER}.async_get_actions_state", return_value=_actions_state())
    )
    stack.enter_context(
        patch(f"{MANAGER}.async_get_settings", return_value=dict(settings or SETTINGS))
    )
    stack.enter_context(
        patch(
            f"{MANAGER}.async_get_extended_state",
            return_value=dict(state or EXTENDED_STATE),
        )
    )
    stack.enter_context(patch(f"{MANAGER}.async_get_uptime", return_value=UPTIME_S))
    return stack


async def _setup_with(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    settings: dict | None = None,
    state: dict | None = None,
) -> None:
    """Set up an entry against the given device payloads."""
    entry.add_to_hass(hass)
    with _reads(settings, state), patch(f"{MANAGER}.async_save_action"):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()


def _eid(hass: HomeAssistant, domain: str, key: str) -> str:
    """Resolve an entity id by unique id, so display names can change freely."""
    entity_id = er.async_get(hass).async_get_entity_id(
        domain, DOMAIN, f"{BLEBOX_ID}_{key}"
    )
    assert entity_id is not None, f"no {domain} entity for {key}"
    return entity_id


async def _call(
    hass: HomeAssistant, domain: str, service: str, data: dict[str, Any]
) -> list[dict]:
    """Call a service with device reads patched; return the settings patches sent."""
    with _reads(), patch(f"{MANAGER}.async_set_settings", return_value={}) as write:
        await hass.services.async_call(domain, service, data, blocking=True)
        await hass.async_block_till_done()
    return [call.args[0] for call in write.call_args_list]


# --- state reflection -------------------------------------------------------


async def test_entities_reflect_the_device(hass: HomeAssistant) -> None:
    """Every new entity reads its value out of the real payloads."""
    await _setup_with(hass, _entry())

    backlight = hass.states.get(_eid(hass, "light", "buttons_backlight"))
    assert backlight.state == "on"
    assert backlight.attributes["rgb_color"] == (255, 255, 255)

    assert hass.states.get(_eid(hass, "switch", "cloud_tunnel")).state == "on"
    assert hass.states.get(_eid(hass, "switch", "status_led")).state == "off"

    overload = hass.states.get(_eid(hass, "number", "overload_threshold"))
    assert overload.state == "0.0"
    # Range comes from the device's own fieldsPreferences, not a constant.
    assert overload.attributes["max"] == 3680

    assert (
        hass.states.get(_eid(hass, "select", "state_after_restart")).state == "restore"
    )

    safety = hass.states.get(_eid(hass, "binary_sensor", "safety_triggered"))
    assert safety.state == "off"
    assert safety.attributes["event_reason"] == 0

    assert (
        hass.states.get(_eid(hass, "binary_sensor", "power_calibrated")).state == "off"
    )


async def test_safety_trip_is_reported(hass: HomeAssistant) -> None:
    """A tripped overload shows as a problem, with the reason preserved."""
    tripped = {
        **EXTENDED_STATE,
        "switch": {"safety": {"eventReason": 3, "triggered": [0]}},
    }
    await _setup_with(hass, _entry(), state=tripped)

    safety = hass.states.get(_eid(hass, "binary_sensor", "safety_triggered"))
    assert safety.state == "on"
    assert safety.attributes["event_reason"] == 3
    assert safety.attributes["triggered"] == [0]


# --- writes -----------------------------------------------------------------


async def test_backlight_writes(hass: HomeAssistant) -> None:
    """Turning the backlight on/off sends the shapes the device expects."""
    await _setup_with(hass, _entry())
    entity_id = _eid(hass, "light", "buttons_backlight")

    patches = await _call(hass, "light", "turn_off", {"entity_id": entity_id})
    assert patches == [{"buttonsBacklight": {"enabled": 0}}]

    patches = await _call(
        hass, "light", "turn_on", {"entity_id": entity_id, "rgb_color": [255, 0, 0]}
    )
    assert patches == [{"buttonsBacklight": {"enabled": 1, "color": "ff0000"}}]


async def test_cloud_tunnel_can_be_disabled(hass: HomeAssistant) -> None:
    """The cloud tunnel is a plain enabled flag."""
    await _setup_with(hass, _entry())
    patches = await _call(
        hass, "switch", "turn_off", {"entity_id": _eid(hass, "switch", "cloud_tunnel")}
    )
    assert patches == [{"tunnel": {"enabled": 0}}]


async def test_overload_threshold_writes_and_validates(hass: HomeAssistant) -> None:
    """Accepted values are written; values the device would refuse are rejected."""
    await _setup_with(hass, _entry())
    entity_id = _eid(hass, "number", "overload_threshold")

    patches = await _call(
        hass, "number", "set_value", {"entity_id": entity_id, "value": 1500}
    )
    assert patches == [{"powerMeasuring": {"safetyValue": {"activePower": 1500}}}]

    # 0 disables the protection and is always allowed.
    patches = await _call(
        hass, "number", "set_value", {"entity_id": entity_id, "value": 0}
    )
    assert patches == [{"powerMeasuring": {"safetyValue": {"activePower": 0}}}]

    # Between 0 and the device minimum is neither "off" nor valid.
    with pytest.raises(ServiceValidationError):
        await _call(hass, "number", "set_value", {"entity_id": entity_id, "value": 50})


async def test_restart_state_round_trips_sibling_settings(hass: HomeAssistant) -> None:
    """Changing restart behaviour preserves the relay's other settings."""
    await _setup_with(hass, _entry())
    patches = await _call(
        hass,
        "select",
        "select_option",
        {"entity_id": _eid(hass, "select", "state_after_restart"), "option": "on"},
    )

    assert patches == [
        {"relays": [{"stateAfterRestart": 1, "defaultForTime": 0, "iconSet": 38}]}
    ]


# --- capability detection ---------------------------------------------------


async def test_entities_are_skipped_when_unsupported(hass: HomeAssistant) -> None:
    """A device without these settings gets no entities for them."""
    bare = {"deviceName": "Simon GO Switch", "relays": [{"stateAfterRestart": 0}]}
    await _setup_with(hass, _entry(), settings=bare)

    registry = er.async_get(hass)

    def present(domain: str, key: str) -> bool:
        return (
            registry.async_get_entity_id(domain, DOMAIN, f"{BLEBOX_ID}_{key}")
            is not None
        )

    assert not present("light", "buttons_backlight")
    assert not present("switch", "cloud_tunnel")
    assert not present("switch", "status_led")
    assert not present("number", "overload_threshold")
    assert not present("binary_sensor", "power_calibrated")

    # The relay is still there, so this one survives.
    assert present("select", "state_after_restart")
    # Event entities never depend on settings at all.
    assert present("event", "input_0")


async def test_uptime_and_countdown_are_disabled_by_default(
    hass: HomeAssistant,
) -> None:
    """Both are registered but stay out of the state machine until enabled."""
    await _setup_with(hass, _entry())
    registry = er.async_get(hass)

    for key in ("uptime", "countdown"):
        entity_id = _eid(hass, "sensor", key)
        assert (
            registry.async_get(entity_id).disabled_by
            is er.RegistryEntryDisabler.INTEGRATION
        )
        assert hass.states.get(entity_id) is None


async def test_uptime_and_countdown_report_values_once_enabled(
    hass: HomeAssistant,
) -> None:
    """Enabling them yields the device's real values."""
    entry = _entry()
    await _setup_with(hass, entry)
    registry = er.async_get(hass)
    for key in ("uptime", "countdown"):
        registry.async_update_entity(_eid(hass, "sensor", key), disabled_by=None)

    with _reads(), patch(f"{MANAGER}.async_save_action"):
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()

    assert hass.states.get(_eid(hass, "sensor", "uptime")).state == str(UPTIME_S)
    assert hass.states.get(_eid(hass, "sensor", "countdown")).state == "0"


async def test_backlight_reflects_the_write_without_waiting_for_a_poll(
    hass: HomeAssistant,
) -> None:
    """The control moves at once, showing whatever the device stored.

    Regression: the state only changed once a poll landed, which the refresh
    debounce could delay by up to ten seconds, and an in-flight poll could snap
    it back to the old value.
    """
    entry = _entry()
    await _setup_with(hass, entry)
    entity_id = _eid(hass, "light", "buttons_backlight")
    assert hass.states.get(entity_id).attributes["rgb_color"] == (255, 255, 255)

    # The device answers a write with its full settings, colour as it stored it.
    normalised = {**SETTINGS, "buttonsBacklight": {"enabled": 1, "color": "fffefa"}}
    with _reads(), patch(f"{MANAGER}.async_set_settings", return_value=normalised):
        await hass.services.async_call(
            "light",
            "turn_on",
            {"entity_id": entity_id, "rgb_color": [255, 0, 0]},
            blocking=True,
        )
        await hass.async_block_till_done()

    # _reads() still reports the pre-write colour, exactly as a poll already in
    # flight would; the device's own answer must win.
    assert hass.states.get(entity_id).attributes["rgb_color"] == (255, 254, 250)


async def test_backlight_off_is_immediate(hass: HomeAssistant) -> None:
    """Turning the backlight off does not wait for a refresh either."""
    await _setup_with(hass, _entry())
    entity_id = _eid(hass, "light", "buttons_backlight")
    assert hass.states.get(entity_id).state == "on"

    with _reads(), patch(f"{MANAGER}.async_set_settings", return_value={}):
        await hass.services.async_call(
            "light", "turn_off", {"entity_id": entity_id}, blocking=True
        )
        await hass.async_block_till_done()

    # No device answer, so the patch is merged locally rather than guessed at.
    assert hass.states.get(entity_id).state == "off"


async def test_device_settings_win_again_after_the_settle_window(
    hass: HomeAssistant,
) -> None:
    """A change made in the wBox app still reaches Home Assistant."""
    entry = _entry()
    await _setup_with(hass, entry)
    entity_id = _eid(hass, "light", "buttons_backlight")

    with _reads(), patch(f"{MANAGER}.async_set_settings", return_value={}):
        await hass.services.async_call(
            "light", "turn_off", {"entity_id": entity_id}, blocking=True
        )
        await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "off"

    with (
        _reads(),
        patch(
            "custom_components.blebox_events.entity.time.monotonic",
            return_value=time.monotonic() + 3600,
        ),
    ):
        await entry.runtime_data.coordinator.async_refresh()
        await hass.async_block_till_done()

    assert hass.states.get(entity_id).state == "on"
