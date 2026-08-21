"""Tests for the configuration and diagnostic entities.

Fixtures are the real payloads from a Simon 55 GO, so these also pin down the
settings-patch shapes actually sent to the device.
"""

from __future__ import annotations

import time
from contextlib import ExitStack
from dataclasses import replace
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.blebox_advanced.blebox_actions import BleBoxConnectionError
from custom_components.blebox_advanced.const import (
    DOMAIN,
    OVERLOAD_MAX,
    OVERLOAD_MIN,
    OVERLOAD_OFF,
    SETTING_BACKLIGHT,
    SETTING_POWER_MEASURING,
)

from .test_integration import (
    BLEBOX_ID,
    DEVICE,
    EXTENDED_STATE,
    MANAGER,
    NETWORK,
    SETTINGS,
    UPTIME_S,
    _actions_state,
    _entry,
    _unreachable,
)


def _reads(
    settings: dict | None = None,
    state: dict | None = None,
    uptime: int | None = UPTIME_S,
    network: dict | None = None,
) -> ExitStack:
    """Patch every device read the coordinator performs.

    ``uptime`` and ``network`` are separately settable because both are
    best-effort reads that answer nothing at all for a device that will not
    report them, and both decide whether an entity exists.
    """
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
    stack.enter_context(patch(f"{MANAGER}.async_get_uptime", return_value=uptime))
    stack.enter_context(
        patch(
            f"{MANAGER}.async_get_network",
            return_value=dict(NETWORK if network is None else network),
        )
    )
    return stack


async def _setup_with(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    settings: dict | None = None,
    state: dict | None = None,
    uptime: int | None = UPTIME_S,
    network: dict | None = None,
) -> None:
    """Set up an entry against the given device payloads."""
    entry.add_to_hass(hass)
    with (
        _reads(settings, state, uptime, network),
        patch(f"{MANAGER}.async_save_action"),
    ):
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
    hass: HomeAssistant,
    domain: str,
    service: str,
    data: dict[str, Any],
    settings: dict | None = None,
) -> list[dict]:
    """Call a service with device reads patched; return the settings patches sent.

    ``settings`` has to match whatever the entry was set up against: a write
    asks for a refresh, and the reads patched here are what that refresh finds,
    so leaving them at the shared fixture would quietly hand a device its
    default capabilities back.
    """
    with (
        _reads(settings),
        patch(f"{MANAGER}.async_set_settings", return_value={}) as write,
    ):
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
    # This device's own maximum happens to be the same number the code falls
    # back on, so asserting it here proves the value and nothing about where it
    # came from. Which of the two is used is pinned further down, against a
    # device whose range differs from the constants.
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


async def test_backlight_brightness_is_read_out_of_the_stored_colour(
    hass: HomeAssistant,
) -> None:
    """Brightness is advertised, so it has to be reported and honoured.

    The device stores one ``rrggbb`` value with no brightness field, so a
    half-power orange is stored as ``804000``. Home Assistant is told the hue
    and the level separately, which is the only shape a colour mode allows.
    """
    dim = {**SETTINGS, SETTING_BACKLIGHT: {"enabled": 1, "color": "804000"}}
    await _setup_with(hass, _entry(), settings=dim)
    backlight = hass.states.get(_eid(hass, "light", "buttons_backlight"))

    assert backlight.attributes["brightness"] == 128
    assert backlight.attributes["rgb_color"] == (255, 128, 0)


@pytest.mark.parametrize(
    ("stored", "asked", "written", "why"),
    [
        (
            "804000",
            {"brightness": 255},
            "ff8000",
            "a level on its own rescales the hue the device already holds",
        ),
        (
            "804000",
            {"rgb_color": [0, 255, 0]},
            "008000",
            "a hue on its own keeps the level the device is showing",
        ),
        (
            "804000",
            {"rgb_color": [255, 0, 0], "brightness": 51},
            "330000",
            "both together are simply multiplied",
        ),
        (
            "000000",
            {"rgb_color": [0, 0, 255]},
            "0000ff",
            "a stored black has no level to keep, so the hue goes on at full",
        ),
        (
            "",
            {"brightness": 128},
            "808080",
            "a level with no hue to tint falls back to the default colour",
        ),
    ],
)
async def test_backlight_brightness_is_folded_back_into_the_colour(
    hass: HomeAssistant,
    stored: str,
    asked: dict[str, Any],
    written: str,
    why: str,
) -> None:
    """Setting a level rescales the stored colour instead of being discarded.

    Regression: the entity advertised brightness through its colour mode, the
    frontend drew a slider, and ``async_turn_on`` read only ``rgb_color``, so
    every brightness the user chose was accepted and silently dropped.

    Scaling by a stored zero is the case worth pinning: it would multiply the
    new hue away and enable a backlight showing nothing, which is what a
    control that did not work looks like.
    """
    settings = {**SETTINGS, SETTING_BACKLIGHT: {"enabled": 1, "color": stored}}
    await _setup_with(hass, _entry(), settings=settings)
    entity_id = _eid(hass, "light", "buttons_backlight")

    patches = await _call(
        hass, "light", "turn_on", {"entity_id": entity_id, **asked}, settings
    )
    assert patches == [{SETTING_BACKLIGHT: {"enabled": 1, "color": written}}], why


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


# --- the overload range: device metadata versus the fallback constants -------

NARROW_RANGE = {
    **SETTINGS,
    "powerMeasuring": {
        **SETTINGS[SETTING_POWER_MEASURING],
        "safetyValue": {
            "activePower": 0,
            "fieldsPreferences": [
                {
                    "name": "activePower",
                    "minValue": 100,
                    "maxValue": 2300,
                    "specialValues": {"off": 0},
                }
            ],
        },
    },
}
"""The Simon 55 GO payload with a 10 A device's range substituted in.

Both ends deliberately differ from ``OVERLOAD_MIN``/``OVERLOAD_MAX``. The real
capture's own limits are 200 and 3680, which are exactly the constants the code
falls back on, so nothing measured against it can tell the device's constraint
metadata apart from the hardcoded default.
"""

NO_RANGE = {
    **SETTINGS,
    "powerMeasuring": {
        **SETTINGS[SETTING_POWER_MEASURING],
        "safetyValue": {"activePower": 0},
    },
}
"""A device that offers overload protection but describes no range for it."""


async def test_the_overload_range_is_the_device_s_own(hass: HomeAssistant) -> None:
    """The accepted range comes from the device, not from the constants.

    Design rule 5: value ranges come from the device's own constraint metadata.
    A device rated 10 A accepts a lower threshold than the fallback minimum and
    refuses one the fallback maximum would allow, so reading its
    ``fieldsPreferences`` is the difference between a usable control and one
    that rejects legitimate values.
    """
    await _setup_with(hass, _entry(), settings=NARROW_RANGE)
    entity_id = _eid(hass, "number", "overload_threshold")

    overload = hass.states.get(entity_id)
    assert overload.attributes["max"] == 2300
    assert overload.attributes["max"] != OVERLOAD_MAX

    # Below the constant's minimum, so the hardcoded range would refuse it.
    patches = await _call(
        hass,
        "number",
        "set_value",
        {"entity_id": entity_id, "value": 150},
        settings=NARROW_RANGE,
    )
    assert patches == [{"powerMeasuring": {"safetyValue": {"activePower": 150}}}]

    # Below the device's own minimum, so it is refused - and the message quotes
    # the device's numbers rather than the constants.
    with pytest.raises(ServiceValidationError) as raised:
        await _call(
            hass,
            "number",
            "set_value",
            {"entity_id": entity_id, "value": 50},
            settings=NARROW_RANGE,
        )
    assert raised.value.translation_key == "overload_out_of_range"
    assert raised.value.translation_placeholders == {
        "minimum": "100",
        "maximum": "2300",
    }


@pytest.mark.parametrize(
    ("prefs", "why"),
    [
        (None, "the device describes no range at all"),
        ("3680", "the range is not even a list"),
        ([{"name": "somethingElse", "minValue": 1, "maxValue": 2}], "no such field"),
        (
            [{"name": "activePower", "minValue": "100", "maxValue": "2300"}],
            "the limits are strings",
        ),
    ],
)
async def test_the_overload_range_falls_back_when_the_device_says_nothing(
    hass: HomeAssistant, prefs: object, why: str
) -> None:
    """A device that describes no usable range gets the conservative constants.

    Reading constraints from the device is only safe if not finding them is
    handled: older firmware omits ``fieldsPreferences`` entirely, and a payload
    that carries the field with values of the wrong type has to count as not
    finding them too, or a string minimum would end up compared against an int.
    """
    safety: dict[str, Any] = {"activePower": 0}
    if prefs is not None:
        safety["fieldsPreferences"] = prefs
    settings = {
        **SETTINGS,
        "powerMeasuring": {**SETTINGS[SETTING_POWER_MEASURING], "safetyValue": safety},
    }
    await _setup_with(hass, _entry(), settings=settings)
    entity_id = _eid(hass, "number", "overload_threshold")

    assert hass.states.get(entity_id).attributes["max"] == OVERLOAD_MAX, why

    # 150 is fine on a device that reports a 100 W minimum; with no metadata to
    # go on the conservative constant applies and it has to be refused.
    with pytest.raises(ServiceValidationError) as raised:
        await _call(
            hass,
            "number",
            "set_value",
            {"entity_id": entity_id, "value": 150},
            settings=settings,
        )
    assert raised.value.translation_placeholders == {
        "minimum": str(OVERLOAD_MIN),
        "maximum": str(OVERLOAD_MAX),
    }

    accepted = await _call(
        hass,
        "number",
        "set_value",
        {"entity_id": entity_id, "value": OVERLOAD_MIN},
        settings=settings,
    )
    assert accepted == [
        {"powerMeasuring": {"safetyValue": {"activePower": OVERLOAD_MIN}}}
    ]


async def test_the_overload_slider_still_reaches_the_off_value(
    hass: HomeAssistant,
) -> None:
    """The slider's minimum is the off sentinel, not the device's minimum.

    Zero disables the protection and is the only value outside the device's
    range that it accepts, so the entity's minimum has to be zero however high
    the device's own floor is. Home Assistant refuses a service call outside
    ``min``/``max`` before the entity ever sees it, so a minimum taken from the
    device would make disabling the protection impossible from the UI.
    """
    await _setup_with(hass, _entry(), settings=NARROW_RANGE)
    entity_id = _eid(hass, "number", "overload_threshold")

    overload = hass.states.get(entity_id)
    assert overload.attributes["min"] == OVERLOAD_OFF
    # Neither the device's floor nor the fallback one, which is the whole point.
    assert overload.attributes["min"] not in (100, OVERLOAD_MIN)

    patches = await _call(
        hass,
        "number",
        "set_value",
        {"entity_id": entity_id, "value": OVERLOAD_OFF},
        settings=NARROW_RANGE,
    )
    assert patches == [{"powerMeasuring": {"safetyValue": {"activePower": 0}}}]


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


async def test_a_relay_keeps_what_its_sibling_just_wrote(hass: HomeAssistant) -> None:
    """Setting one relay's restart behaviour must not revert the other one's.

    Regression: the whole relay list is round-tripped on every write, and the
    just-written settings used to be remembered per entity. Relay 2's select
    never saw relay 1's write, so it read the pre-write snapshot and sent relay
    1's old value straight back to the device.
    """
    relay = {"stateAfterRestart": 2, "defaultForTime": 0, "iconSet": 38}
    two_relays = {**SETTINGS, "relays": [dict(relay), dict(relay)]}
    await _setup_with(hass, _entry(), settings=two_relays)

    first = _eid(hass, "select", "state_after_restart")
    second = _eid(hass, "select", "state_after_restart_1")

    # The device answers a write with its full settings, as it really does.
    echoed = {**two_relays, "relays": [{**relay, "stateAfterRestart": 1}, dict(relay)]}
    with (
        _reads(settings=two_relays),
        patch(f"{MANAGER}.async_set_settings", return_value=echoed),
    ):
        await hass.services.async_call(
            "select",
            "select_option",
            {"entity_id": first, "option": "on"},
            blocking=True,
        )
        await hass.async_block_till_done()
    assert hass.states.get(first).state == "on"

    # The reads still report the pre-write list, exactly as a poll already in
    # flight would, so relay 2's write has to build on relay 1's answer.
    with (
        _reads(settings=two_relays),
        patch(f"{MANAGER}.async_set_settings", return_value={}) as write,
    ):
        await hass.services.async_call(
            "select",
            "select_option",
            {"entity_id": second, "option": "off"},
            blocking=True,
        )
        await hass.async_block_till_done()

    payload = write.call_args_list[0].args[0]["relays"]
    assert payload[0]["stateAfterRestart"] == 1, "relay 1 was reverted"
    assert payload[1]["stateAfterRestart"] == 0
    assert hass.states.get(first).state == "on"
    assert hass.states.get(second).state == "off"


# --- device failures --------------------------------------------------------


async def test_a_refused_settings_write_reads_as_a_device_problem(
    hass: HomeAssistant,
) -> None:
    """A settings write the device refuses must not surface as a traceback.

    Regression: every settings-backed control let ``BleBoxError`` escape into
    Home Assistant. A device that was asleep, moved or on a VLAN that stopped
    routing produced an unhandled-exception traceback in the log and a generic
    red toast naming neither the device nor the cause.
    """
    await _setup_with(hass, _entry())

    with (
        _reads(),
        patch(
            f"{MANAGER}.async_set_settings",
            side_effect=BleBoxConnectionError(
                "POST /api/settings/set failed: Connection timeout"
            ),
        ),
        pytest.raises(HomeAssistantError) as raised,
    ):
        await hass.services.async_call(
            "switch",
            "turn_off",
            {"entity_id": _eid(hass, "switch", "cloud_tunnel")},
            blocking=True,
        )

    assert raised.value.translation_domain == DOMAIN
    assert raised.value.translation_key == "device_write_failed"
    # Actionable means naming the device and what it said.
    placeholders = raised.value.translation_placeholders
    assert placeholders["device"] == "Simon GO Switch"
    assert "Connection timeout" in placeholders["error"]
    # The original failure survives as the cause, so the log still explains it.
    assert isinstance(raised.value.__cause__, BleBoxConnectionError)


async def test_every_settings_control_reports_a_refusal_the_same_way(
    hass: HomeAssistant,
) -> None:
    """The backlight, the overload threshold and the selects share the wrapping.

    They all write through ``async_patch_settings``, so one wrap covers them;
    this pins that down rather than leaving it to be re-broken one control at a
    time.
    """
    await _setup_with(hass, _entry())

    calls = (
        ("light", "turn_on", {"entity_id": _eid(hass, "light", "buttons_backlight")}),
        (
            "number",
            "set_value",
            {"entity_id": _eid(hass, "number", "overload_threshold"), "value": 1500},
        ),
        (
            "select",
            "select_option",
            {
                "entity_id": _eid(hass, "select", "state_after_restart"),
                "option": "on",
            },
        ),
    )
    for domain, service, payload in calls:
        with (
            _reads(),
            patch(
                f"{MANAGER}.async_set_settings",
                side_effect=BleBoxConnectionError("device did not answer"),
            ),
            pytest.raises(HomeAssistantError) as raised,
        ):
            await hass.services.async_call(domain, service, payload, blocking=True)
        assert raised.value.translation_key == "device_write_failed", domain


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
    """A change made in the wBox app still reaches Home Assistant.

    The written value is held by the coordinator, so that is where the settle
    window is measured.
    """
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
            "custom_components.blebox_advanced.coordinator.time.monotonic",
            return_value=time.monotonic() + 3600,
        ),
    ):
        await entry.runtime_data.coordinator.async_refresh()
        await hass.async_block_till_done()

    assert hass.states.get(entity_id).state == "on"


# --- device identity --------------------------------------------------------


async def test_a_firmware_update_reaches_the_device_page(hass: HomeAssistant) -> None:
    """A new firmware version shows on the device page without a reload.

    Regression: model, firmware and hardware version were read out of
    `entry.data`, which the config flow writes once and nothing ever updates.
    The coordinator re-read the identity on every slow cycle and the update
    entity showed the new version, but the device page kept the old one
    indefinitely - and that page is exactly where a user looks to confirm a
    firmware update, which this integration can start itself, actually worked.
    """
    entry = _entry()
    await _setup_with(hass, entry)

    registry = dr.async_get(hass)
    device = next(iter(dr.async_entries_for_config_entry(registry, entry.entry_id)))
    assert device.sw_version == "0.1502"

    coordinator = entry.runtime_data.coordinator
    with (
        _reads(),
        patch(
            f"{MANAGER}.async_get_device_info",
            return_value=replace(DEVICE, firmware_version="0.1600"),
        ),
    ):
        # Forced, because identity is only re-read on the slow cycle.
        coordinator.async_request_full_refresh()
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    assert registry.async_get(device.id).sw_version == "0.1600"
    firmware = hass.states.get(_eid(hass, "update", "firmware"))
    # The device page and the update entity next to it must not disagree.
    assert firmware.attributes["installed_version"] == "0.1600"


async def test_a_device_that_never_answered_still_has_a_device_page(
    hass: HomeAssistant,
) -> None:
    """The versions the config flow stored carry an entry that starts offline.

    Live identity is preferred over `entry.data`, but a device that has never
    answered has no live identity to offer: the fallback is all it has, and it
    has to keep producing device info Home Assistant accepts, or an offline
    start would leave the pushed event entities with no device page at all.
    """
    entry = _entry()
    entry.add_to_hass(hass)
    with _unreachable():
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        registry = dr.async_get(hass)
        device = next(iter(dr.async_entries_for_config_entry(registry, entry.entry_id)))
        assert device.model == "switchBox"
        assert device.sw_version == "0.1502"
        assert device.hw_version == "s_KS.swB.1.5.T.p55ST-0.3"

        # Unloaded inside the patch: the retry timer would otherwise fire
        # against a real socket during teardown.
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
