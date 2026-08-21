"""Tests for callback health, enriched events, relay reporting and button control."""

from __future__ import annotations

import logging
import time
from dataclasses import replace
from typing import Any
from unittest.mock import patch

import pytest
import voluptuous as vol
from homeassistant.components.device_automation import (
    trigger as device_automation_trigger,
)
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.network import NoURLAvailableError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.blebox_advanced import device_trigger
from custom_components.blebox_advanced.api import async_register_token
from custom_components.blebox_advanced.blebox_actions import (
    ACTION_HTTP_GET,
    ACTION_RELAY_OFF,
    ACTION_RELAY_TOGGLE,
    TRIGGER_FALLING_EDGE,
    TRIGGER_LONG_CLICK,
    TRIGGER_RISING_EDGE,
    TRIGGER_SHORT_CLICK,
    ActionsState,
    BleBoxConnectionError,
    InsufficientSlotsError,
    find_native_action,
    relay_state_from,
)
from custom_components.blebox_advanced.const import (
    BLEBOX_DOMAIN,
    CONF_BASE_URL,
    CONF_BLEBOX_ID,
    CONF_CALLBACK_TOKEN,
    CONF_CLEANUP_ON_REMOVE,
    CONF_ENABLED_EVENTS,
    CONF_INPUTS,
    CONF_INVERT_EDGES,
    CONF_MANAGE_BUTTONS,
    CONF_MODE,
    CONF_SUPPORTS_ACTIONS,
    DOMAIN,
    HA_EVENT,
    MODE_AUTOMATIC,
    MODE_MANUAL,
    SETTING_BACKLIGHT,
    SETTING_RELAYS,
)
from custom_components.blebox_advanced.coordinator import callback_health

from .test_integration import (
    BASE_URL,
    BLEBOX_ID,
    DEVICE,
    EXTENDED_STATE,
    MANAGER,
    NETWORK,
    SETTINGS,
    TOKEN,
    _actions_state,
    _device_reads,
    _entry,
    _setup,
    _slot,
)
from .test_settings_entities import _call, _eid, _reads, _setup_with

API = "custom_components.blebox_advanced.api"
COORDINATOR = "custom_components.blebox_advanced.coordinator"

OUR_URL = f"http://192.168.10.50:8123/api/{DOMAIN}/{TOKEN}/0/short_press"


def _owned(slot_id: int, last_call: dict | None) -> dict:
    action = _slot(
        slot_id,
        name="HA IN1 short_press",
        input=0,
        triggerType=TRIGGER_SHORT_CLICK,
        actionType=ACTION_HTTP_GET,
        param=OUR_URL,
    )
    action["lastCall"] = last_call or {"timeElapsedS": -1}
    return action


# --- callback health --------------------------------------------------------


def test_health_distinguishes_unreachable_from_rejected() -> None:
    """The two silent failures are told apart by what the device recorded."""
    unreachable = _actions_state(
        [_owned(0, {"timeElapsedS": 5, "response": {"status": 0, "errorCode": 2}})]
    )
    health = callback_health(unreachable)
    assert health.unreachable == 1 and health.delivered == 0
    assert health.problem is True

    rejected = _actions_state(
        [_owned(0, {"timeElapsedS": 5, "response": {"status": 404, "errorCode": 3}})]
    )
    health = callback_health(rejected)
    assert health.rejected == 1 and health.unreachable == 0
    assert health.problem is True
    assert health.last_status == 404

    working = _actions_state(
        [_owned(0, {"timeElapsedS": 5, "response": {"status": 200, "errorCode": 0}})]
    )
    assert callback_health(working).problem is False

    # Never fired says nothing either way, so it is not a problem.
    assert callback_health(_actions_state([_owned(0, None)])).problem is False
    assert callback_health(None).problem is False


async def test_unreachable_callbacks_raise_a_repair(hass: HomeAssistant) -> None:
    """A device that cannot reach Home Assistant surfaces in Repairs."""
    broken = _actions_state(
        [_owned(0, {"timeElapsedS": 5, "response": {"status": 0, "errorCode": 2}})]
    )
    entry = _entry()
    entry.add_to_hass(hass)
    with _reads(), patch(f"{MANAGER}.async_get_actions_state", return_value=broken):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    issues = ir.async_get(hass)
    assert issues.async_get_issue(DOMAIN, f"callbacks_unreachable_{entry.entry_id}")
    assert (
        issues.async_get_issue(DOMAIN, f"callbacks_rejected_{entry.entry_id}") is None
    )

    health = hass.states.get(_eid(hass, "binary_sensor", "callback_delivery"))
    assert health.state == "on"
    assert health.attributes["unreachable"] == 1


# --- enriched events --------------------------------------------------------


async def test_event_carries_device_state(hass: HomeAssistant, hass_client_no_auth):
    """A callback's query parameters become event attributes.

    The relay state is deliberately not among them, even when a callback still
    carries one. Slots written by an earlier version ask for `{s_state.0}`, and
    that placeholder was measured to be a constant on real hardware, so a value
    arriving from a device whose slots have not been rewritten yet must not be
    published as though it meant something.
    """
    await _setup(hass, _entry())
    client = await hass_client_no_auth()

    response = await client.get(f"/api/{DOMAIN}/{TOKEN}/0/short_press?s=1&p=42.5")
    assert response.status == 200
    await hass.async_block_till_done()

    state = hass.states.get(
        er.async_get(hass).async_get_entity_id("event", DOMAIN, f"{BLEBOX_ID}_input_0")
    )
    assert state.attributes["power_w"] == 42.5
    assert "relay_state" not in state.attributes


async def test_unsubstituted_placeholders_are_ignored(
    hass: HomeAssistant, hass_client_no_auth
):
    """Firmware that passes a placeholder through verbatim is not read as zero."""
    await _setup(hass, _entry())
    client = await hass_client_no_auth()

    response = await client.get(
        f"/api/{DOMAIN}/{TOKEN}/0/short_press?s=%7Bs_state.0%7D&p=%7Bpower_w.0%7D"
    )
    assert response.status == 200
    await hass.async_block_till_done()

    state = hass.states.get(
        er.async_get(hass).async_get_entity_id("event", DOMAIN, f"{BLEBOX_ID}_input_0")
    )
    assert "relay_state" not in state.attributes
    assert "power_w" not in state.attributes


# --- the callback endpoint --------------------------------------------------


async def test_a_non_ascii_token_is_still_a_bare_404(
    hass: HomeAssistant, hass_client_no_auth
):
    """An odd token must be rejected, not crash the unauthenticated endpoint.

    Regression: tokens were compared with ``hmac.compare_digest`` on ``str``,
    which only accepts ASCII and raises ``TypeError`` on anything else. The
    token comes straight out of the URL path, so anyone able to reach Home
    Assistant could turn an unauthenticated GET into a 500 and a logged
    traceback with a single accented character.
    """
    await _setup(hass, _entry())
    client = await hass_client_no_auth()

    for token in ("zażółć", "токен", "🔌", f"{TOKEN}é"):
        response = await client.get(f"/api/{DOMAIN}/{token}/0/short_press")
        assert response.status == 404, token

    # The real token still works, so the comparison was not simply broken.
    assert (await client.get(f"/api/{DOMAIN}/{TOKEN}/0/short_press")).status == 200


# --- relay state reporting --------------------------------------------------
def test_native_action_lookup_ignores_foreign_types() -> None:
    """Only relay actions are candidates; HTTP and unknown types are invisible."""
    ours = _owned(0, None)
    theirs = _slot(
        1,
        name="IN2 - OUT OFF",
        input=1,
        triggerType=TRIGGER_SHORT_CLICK,
        actionType=ACTION_RELAY_OFF,
    )
    unknown = _slot(
        2, name="mystery", input=2, triggerType=TRIGGER_SHORT_CLICK, actionType=52
    )
    state = _actions_state([ours, theirs, unknown])

    # Input 0 has only our HTTP action, so there is no native action to edit.
    assert find_native_action(state, 0, TRIGGER_SHORT_CLICK) is None
    assert find_native_action(state, 1, TRIGGER_SHORT_CLICK)["id"] == 1
    # Action type 52 is not understood, so it is not offered up for rewriting.
    assert find_native_action(state, 2, TRIGGER_SHORT_CLICK) is None
    assert find_native_action(state, 1, TRIGGER_LONG_CLICK) is None


async def test_button_selects_are_opt_in(hass: HomeAssistant) -> None:
    """No button controls appear until the option is switched on."""
    await _setup_with(hass, _entry())
    registry = er.async_get(hass)
    assert (
        registry.async_get_entity_id(
            "select", DOMAIN, f"{BLEBOX_ID}_button_action_0_short_press"
        )
        is None
    )


async def test_button_select_reads_and_writes(hass: HomeAssistant) -> None:
    """The select reflects the device's own action and rebinds it on change."""
    theirs = _slot(
        0,
        name="IN1 - OUT OFF",
        input=0,
        triggerType=TRIGGER_SHORT_CLICK,
        actionType=ACTION_RELAY_OFF,
    )
    entry = _entry(**{CONF_MANAGE_BUTTONS: True})
    entry.add_to_hass(hass)
    state = _actions_state([theirs])
    with _reads(), patch(f"{MANAGER}.async_get_actions_state", return_value=state):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    entity_id = _eid(hass, "select", "button_action_0_short_press")
    assert hass.states.get(entity_id).state == "relay_off"

    with (
        _reads(),
        patch(f"{MANAGER}.async_get_actions_state", return_value=state),
        patch(f"{MANAGER}.async_save_action") as save,
    ):
        await hass.services.async_call(
            "select",
            "select_option",
            {"entity_id": entity_id, "option": "toggle"},
            blocking=True,
        )
        await hass.async_block_till_done()

    written = save.call_args_list[0].args[0]
    assert written["id"] == 0  # their slot, rebound in place
    assert written["actionType"] == ACTION_RELAY_TOGGLE
    assert written["input"] == 0
    assert written["triggerType"] == TRIGGER_SHORT_CLICK


async def test_button_select_never_touches_our_own_callbacks(
    hass: HomeAssistant,
) -> None:
    """Rebinding a button must not overwrite the HTTP action on the same trigger."""
    entry = _entry(**{CONF_MANAGE_BUTTONS: True})
    entry.add_to_hass(hass)
    # Input 0 short click carries only our callback, and 6 free slots follow.
    state: ActionsState = _actions_state([_owned(0, None)])
    with _reads(), patch(f"{MANAGER}.async_get_actions_state", return_value=state):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    entity_id = _eid(hass, "select", "button_action_0_short_press")
    assert hass.states.get(entity_id).state == "nothing"

    with (
        _reads(),
        patch(f"{MANAGER}.async_get_actions_state", return_value=state),
        patch(f"{MANAGER}.async_save_action") as save,
    ):
        await hass.services.async_call(
            "select",
            "select_option",
            {"entity_id": entity_id, "option": "relay_on"},
            blocking=True,
        )
        await hass.async_block_till_done()

    written = save.call_args_list[0].args[0]
    assert written["id"] != 0, "must not overwrite our own callback slot"
    assert written["actionType"] == 1


async def test_button_select_shows_the_new_binding_on_the_next_poll(
    hass: HomeAssistant,
) -> None:
    """Rebinding a button must not leave the control showing the old action.

    Regression: action slots are only read on the coordinator's slow cycle, and
    the write asked for a plain refresh. That refresh answered with the binding
    from before the write, so the control snapped back for up to a minute.
    """
    theirs = _slot(
        0,
        name="IN1 - OUT OFF",
        input=0,
        triggerType=TRIGGER_SHORT_CLICK,
        actionType=ACTION_RELAY_OFF,
    )
    entry = _entry(**{CONF_MANAGE_BUTTONS: True})
    entry.add_to_hass(hass)
    before = _actions_state([theirs])
    with _reads(), patch(f"{MANAGER}.async_get_actions_state", return_value=before):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    entity_id = _eid(hass, "select", "button_action_0_short_press")
    assert hass.states.get(entity_id).state == "relay_off"

    after = _actions_state([{**theirs, "actionType": ACTION_RELAY_TOGGLE}])
    with (
        _reads(),
        patch(f"{MANAGER}.async_get_actions_state", return_value=after),
        patch(f"{MANAGER}.async_save_action"),
    ):
        await hass.services.async_call(
            "select",
            "select_option",
            {"entity_id": entity_id, "option": "toggle"},
            blocking=True,
        )
        await hass.async_block_till_done()
        # The next poll, which is what the control depends on: a fast one would
        # not fetch the actions at all.
        await entry.runtime_data.coordinator.async_refresh()
        await hass.async_block_till_done()

    assert hass.states.get(entity_id).state == "toggle"


async def _setup_with_buttons(
    hass: HomeAssistant, state: ActionsState
) -> MockConfigEntry:
    """Set up an entry with the button controls on, against a slot layout."""
    entry = _entry(**{CONF_MANAGE_BUTTONS: True})
    entry.add_to_hass(hass)
    with _reads(), patch(f"{MANAGER}.async_get_actions_state", return_value=state):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def test_a_button_rebind_the_device_refuses_reads_as_a_device_problem(
    hass: HomeAssistant,
) -> None:
    """A rebind against an unreachable device must not surface as a traceback.

    Regression: the select called the manager bare, so ``BleBoxError`` escaped
    into Home Assistant as an unhandled exception rather than as a message
    saying which device could not be written to.
    """
    theirs = _slot(
        0,
        name="IN1 - OUT OFF",
        input=0,
        triggerType=TRIGGER_SHORT_CLICK,
        actionType=ACTION_RELAY_OFF,
    )
    state = _actions_state([theirs])
    await _setup_with_buttons(hass, state)
    entity_id = _eid(hass, "select", "button_action_0_short_press")

    with (
        _reads(),
        patch(f"{MANAGER}.async_get_actions_state", return_value=state),
        patch(
            f"{MANAGER}.async_save_action",
            side_effect=BleBoxConnectionError("POST /api/actions/set failed: timeout"),
        ),
        pytest.raises(HomeAssistantError) as raised,
    ):
        await hass.services.async_call(
            "select",
            "select_option",
            {"entity_id": entity_id, "option": "toggle"},
            blocking=True,
        )

    assert raised.value.translation_domain == DOMAIN
    assert raised.value.translation_key == "device_write_failed"
    assert raised.value.translation_placeholders["device"] == "Simon GO Switch"
    assert isinstance(raised.value.__cause__, BleBoxConnectionError)


async def test_a_rebind_with_no_free_slot_says_how_to_free_one(
    hass: HomeAssistant,
) -> None:
    """A full device needs its own explanation, not "the write failed".

    Nothing is wrong with the device or the network here: every action slot is
    taken, and the only fix is in the wBox app. ``InsufficientSlotsError`` is a
    ``BleBoxError`` subclass, so getting this message rather than the generic
    one also pins down the order of the two handlers.
    """
    # Input 0's short click carries our own HTTP action, which is never a
    # candidate for rebinding, and every remaining slot is somebody else's.
    full = _actions_state(
        [
            _owned(0, None),
            *(
                _slot(
                    index,
                    name=f"IN2 rule {index}",
                    input=1,
                    triggerType=TRIGGER_LONG_CLICK,
                    actionType=ACTION_RELAY_OFF,
                )
                for index in range(1, 6)
            ),
        ]
    )
    await _setup_with_buttons(hass, full)
    entity_id = _eid(hass, "select", "button_action_0_short_press")
    assert hass.states.get(entity_id).state == "nothing"

    with (
        _reads(),
        patch(f"{MANAGER}.async_get_actions_state", return_value=full),
        patch(f"{MANAGER}.async_save_action") as save,
        pytest.raises(HomeAssistantError) as raised,
    ):
        await hass.services.async_call(
            "select",
            "select_option",
            {"entity_id": entity_id, "option": "relay_on"},
            blocking=True,
        )

    assert raised.value.translation_key == "no_free_action_slots"
    assert isinstance(raised.value.__cause__, InsufficientSlotsError)
    # Refused before writing anything, so no slot of anyone else's was touched.
    assert save.call_count == 0


def test_relay_state_is_read_from_the_command_response() -> None:
    """The control endpoint answers with the resulting state; we parse it."""
    payload = {"relays": [{"relay": 0, "state": 1, "forTimeLeftS": 0}]}
    assert relay_state_from(payload, 0) is True
    assert relay_state_from({"relays": [{"relay": 0, "state": 0}]}, 0) is False
    # A relay the payload does not mention, or a shape we do not recognise.
    assert relay_state_from(payload, 1) is None
    assert relay_state_from({}, 0) is None
    assert relay_state_from(None, 0) is None


async def test_rapid_toggle_survives_a_stale_poll(hass: HomeAssistant) -> None:
    """A poll in flight when a command lands must not resurrect the old state.

    Regression: toggling faster than the round-trip left the switch showing the
    opposite of reality until the next poll.
    """
    entry = _entry()
    await _setup_with(hass, entry)
    entity_id = _eid(hass, "switch", "relay")
    assert hass.states.get(entity_id).state == "on"  # fixture reports state 1

    with _reads(), patch(f"{MANAGER}.async_set_relay", return_value=False) as command:
        await hass.services.async_call(
            "switch", "turn_off", {"entity_id": entity_id}, blocking=True
        )
        await hass.async_block_till_done()
    assert command.await_args.args == (0, False)
    assert hass.states.get(entity_id).state == "off"

    # The fixture still reports the pre-command state, exactly as a refresh
    # already in flight would.
    with _reads():
        await entry.runtime_data.coordinator.async_refresh()
        await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "off"


async def test_device_is_believed_again_after_the_settle_window(
    hass: HomeAssistant,
) -> None:
    """A relay changed at the wall still reaches Home Assistant."""
    entry = _entry()
    await _setup_with(hass, entry)
    entity_id = _eid(hass, "switch", "relay")

    with _reads(), patch(f"{MANAGER}.async_set_relay", return_value=False):
        await hass.services.async_call(
            "switch", "turn_off", {"entity_id": entity_id}, blocking=True
        )
        await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "off"

    # Well past the settle window, a contradicting observation is real news.
    with (
        _reads(),
        patch(
            "custom_components.blebox_advanced.switch.time.monotonic",
            return_value=time.monotonic() + 3600,
        ),
    ):
        await entry.runtime_data.coordinator.async_refresh()
        await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "on"


async def test_a_relay_command_that_fails_reads_as_a_device_problem(
    hass: HomeAssistant,
) -> None:
    """Switching a relay that cannot be reached must not raise a traceback.

    Regression: the command was issued bare, so ``BleBoxError`` escaped into
    Home Assistant. This is the write a user makes most often, and the device
    is at the far end of a LAN, so it failing is ordinary rather than a bug.
    """
    await _setup_with(hass, _entry())
    entity_id = _eid(hass, "switch", "relay")
    assert hass.states.get(entity_id).state == "on"

    with (
        _reads(),
        patch(
            f"{MANAGER}.async_set_relay",
            side_effect=BleBoxConnectionError("GET /s/0/0 failed: no route to host"),
        ),
        pytest.raises(HomeAssistantError) as raised,
    ):
        await hass.services.async_call(
            "switch", "turn_off", {"entity_id": entity_id}, blocking=True
        )

    assert raised.value.translation_key == "device_write_failed"
    assert isinstance(raised.value.__cause__, BleBoxConnectionError)
    # A command that never landed must not leave the switch claiming it did.
    assert hass.states.get(entity_id).state == "on"


# --- entities that replace the official integration's -----------------------


async def test_power_and_energy_sensors(hass: HomeAssistant) -> None:
    """Power and energy come from the state payload already being polled.

    The sensor array is a list of measurements identified by their ``type``,
    not a fixed shape, so the one wanted has to be picked out of whatever else
    the device happens to publish alongside it.
    """
    state = {
        **EXTENDED_STATE,
        "sensors": [
            # Neither of these is the measurement being looked for: one is a
            # different quantity, the other is the reading a meter reports
            # before it has measured anything.
            {"type": "apparentPower", "value": 141, "trend": 0, "state": 2},
            {"type": "activePower", "value": None, "trend": 0, "state": 0},
            {"type": "activePower", "value": 137, "trend": 0, "state": 2},
        ],
        "powerMeasuring": {
            "enabled": 1,
            "powerConsumption": [{"periodS": 1800, "value": 0.42}],
        },
    }
    await _setup_with(hass, _entry(), state=state)

    power = hass.states.get(_eid(hass, "sensor", "active_power"))
    assert power.state == "137.0"
    assert power.attributes["device_class"] == "power"
    assert power.attributes["state_class"] == "measurement"
    assert power.attributes["unit_of_measurement"] == "W"

    energy = hass.states.get(_eid(hass, "sensor", "power_consumption"))
    assert energy.state == "0.42"
    assert energy.attributes["unit_of_measurement"] == "kWh"
    assert energy.attributes["period_s"] == 1800
    # Deliberately no state_class: the value resets each period, so recording it
    # as a total would corrupt long-term statistics.
    assert "state_class" not in energy.attributes


async def test_firmware_entity_reports_up_to_date(hass: HomeAssistant) -> None:
    """With nothing newer available the device is its own latest version."""
    await _setup_with(hass, _entry())
    state = hass.states.get(_eid(hass, "update", "firmware"))
    assert state.state == "off"
    assert state.attributes["installed_version"] == "0.1502"
    assert state.attributes["latest_version"] == "0.1502"


async def test_firmware_update_offered_and_needs_the_tunnel(
    hass: HomeAssistant,
) -> None:
    """A newer version shows as available, but installing needs the tunnel."""
    newer = replace(DEVICE, raw={"availableFv": "0.1600"})
    tunnel_off = {**SETTINGS, "tunnel": {"enabled": 0, "logEnabled": 0}}

    entry = _entry()
    entry.add_to_hass(hass)
    with (
        _reads(settings=tunnel_off),
        patch(f"{MANAGER}.async_get_device_info", return_value=newer),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    entity_id = _eid(hass, "update", "firmware")
    assert hass.states.get(entity_id).state == "on"
    assert hass.states.get(entity_id).attributes["latest_version"] == "0.1600"

    with (
        _reads(settings=tunnel_off),
        patch(f"{MANAGER}.async_get_device_info", return_value=newer),
        patch(f"{MANAGER}.async_install_firmware") as install,
        pytest.raises(HomeAssistantError),
    ):
        await hass.services.async_call(
            "update", "install", {"entity_id": entity_id}, blocking=True
        )
    assert install.call_count == 0


async def test_a_firmware_install_the_device_refuses_reads_as_a_device_problem(
    hass: HomeAssistant,
) -> None:
    """A device that rejects the update request must not raise a traceback.

    The install endpoint is undocumented and the device reboots into the new
    image, so a refusal or a dropped connection is a normal outcome and has to
    read as one.
    """
    newer = replace(DEVICE, raw={"availableFv": "0.1600"})
    entry = _entry()
    entry.add_to_hass(hass)
    with _reads(), patch(f"{MANAGER}.async_get_device_info", return_value=newer):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    entity_id = _eid(hass, "update", "firmware")
    assert hass.states.get(entity_id).state == "on"

    with (
        _reads(),
        patch(f"{MANAGER}.async_get_device_info", return_value=newer),
        patch(
            f"{MANAGER}.async_install_firmware",
            side_effect=BleBoxConnectionError("POST /api/ota/update failed: timeout"),
        ),
        pytest.raises(HomeAssistantError) as raised,
    ):
        await hass.services.async_call(
            "update", "install", {"entity_id": entity_id}, blocking=True
        )

    assert raised.value.translation_domain == DOMAIN
    assert raised.value.translation_key == "update_failed"
    assert isinstance(raised.value.__cause__, BleBoxConnectionError)


async def test_access_point_switch(hass: HomeAssistant) -> None:
    """The device's own access point is readable and can be turned off."""
    await _setup_with(hass, _entry())
    entity_id = _eid(hass, "switch", "access_point")

    state = hass.states.get(entity_id)
    assert state.state == "on"
    assert state.attributes["ssid"] == "SimonGOSwitch-ae0bfbf927ba"
    assert state.attributes["protected"] is False  # empty apPasswd

    with _reads(), patch(f"{MANAGER}.async_set_ap_enabled") as ap:
        await hass.services.async_call(
            "switch", "turn_off", {"entity_id": entity_id}, blocking=True
        )
        await hass.async_block_till_done()
    assert ap.await_args.args == (False,)


async def test_access_point_shows_the_new_state_without_waiting_for_a_poll(
    hass: HomeAssistant,
) -> None:
    """Turning the access point off must stick, not spring back to on.

    Regression, reported on 0.5.0: switching it off showed it back on, and only
    a second attempt appeared to work. Nothing wrote a state after the command,
    and the network object is only re-read on the coordinator's slow cycle, so
    Home Assistant went on reporting the old value and the frontend reverted
    the toggle. The device answers the write with its whole network object, so
    that answer is what the entity now publishes straight away.
    """
    await _setup_with(hass, _entry())
    entity_id = _eid(hass, "switch", "access_point")
    assert hass.states.get(entity_id).state == "on"

    echoed = {**NETWORK, "apEnable": False}
    with (
        _reads(),
        patch(f"{MANAGER}.async_set_ap_enabled", return_value=echoed) as ap,
    ):
        await hass.services.async_call(
            "switch", "turn_off", {"entity_id": entity_id}, blocking=True
        )
        await hass.async_block_till_done()

    assert ap.await_args.args == (False,)
    assert hass.states.get(entity_id).state == "off"


async def test_a_poll_in_flight_cannot_revert_the_access_point(
    hass: HomeAssistant,
) -> None:
    """A refresh carrying pre-write network state must not undo the command.

    The read that answers a poll already running when the write lands describes
    the device from before it, so believing it would snap the toggle back for
    up to a full slow cycle. Past the settle window the device is believed
    again, so turning the access point on in the wBox app still reaches us.
    """
    await _setup_with(hass, _entry())
    entity_id = _eid(hass, "switch", "access_point")

    echoed = {**NETWORK, "apEnable": False}
    with (
        _reads(),
        patch(f"{MANAGER}.async_set_ap_enabled", return_value=echoed),
    ):
        await hass.services.async_call(
            "switch", "turn_off", {"entity_id": entity_id}, blocking=True
        )
        await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "off"

    # A poll still reporting the access point as on, as an in-flight one would.
    coordinator = hass.config_entries.async_entries(DOMAIN)[0].runtime_data.coordinator
    with _reads():
        await coordinator.async_refresh()
        await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "off"

    # Past the settle window the device wins again.
    with (
        _reads(),
        patch(
            f"{COORDINATOR}.time.monotonic",
            return_value=time.monotonic() + 3600,
        ),
    ):
        await coordinator.async_refresh()
        await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "on"


async def test_a_failed_access_point_change_reads_as_a_device_problem(
    hass: HomeAssistant,
) -> None:
    """Turning the access point off on an unreachable device is not a crash.

    Regression: the switch called the manager bare, so ``BleBoxError`` escaped
    into Home Assistant as an unhandled exception.
    """
    await _setup_with(hass, _entry())

    with (
        _reads(),
        patch(
            f"{MANAGER}.async_set_ap_enabled",
            side_effect=BleBoxConnectionError("GET /api/device/network failed"),
        ),
        pytest.raises(HomeAssistantError) as raised,
    ):
        await hass.services.async_call(
            "switch",
            "turn_off",
            {"entity_id": _eid(hass, "switch", "access_point")},
            blocking=True,
        )

    assert raised.value.translation_key == "device_write_failed"
    assert isinstance(raised.value.__cause__, BleBoxConnectionError)


async def test_input_without_events_is_disabled_by_default(
    hass: HomeAssistant,
) -> None:
    """An input nobody selected events for stays out of the way.

    That is how an optional external terminal, present in the device's input
    list but not wired to anything, avoids cluttering the device page.
    """
    entry = _entry(
        **{CONF_ENABLED_EVENTS: {"0": ["short_press", "long_press"], "1": []}}
    )
    await _setup_with(hass, entry)
    registry = er.async_get(hass)

    used = registry.async_get(_eid(hass, "event", "input_0"))
    spare = registry.async_get(_eid(hass, "event", "input_1"))

    assert used.disabled_by is None
    assert spare.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    # Registered either way, so a hand configured URL still has somewhere to land.
    assert hass.states.get(spare.entity_id) is None


async def test_an_input_given_events_later_gets_its_entity_back(
    hass: HomeAssistant,
) -> None:
    """Selecting events for an unused input must actually produce an entity.

    Regression: ``entity_registry_enabled_default`` is consulted only when an
    entity is first registered, so an input registered disabled stayed disabled
    for good. The user ticked its events in the options, the entry reloaded,
    and nothing appeared, which looks exactly like a broken integration.
    """
    entry = _entry(**{CONF_ENABLED_EVENTS: {"0": ["short_press"], "1": []}})
    await _setup_with(hass, entry)
    registry = er.async_get(hass)
    spare = _eid(hass, "event", "input_1")
    assert registry.async_get(spare).disabled_by is er.RegistryEntryDisabler.INTEGRATION

    with _reads(), patch(f"{MANAGER}.async_save_action"):
        hass.config_entries.async_update_entry(
            entry,
            options={
                **entry.options,
                CONF_ENABLED_EVENTS: {"0": ["short_press"], "1": ["long_press"]},
            },
        )
        await hass.async_block_till_done()

    assert registry.async_get(spare).disabled_by is None
    assert hass.states.get(spare) is not None


async def test_an_input_the_user_disabled_stays_disabled(hass: HomeAssistant) -> None:
    """A deliberate disable is never undone by the re-enable fix-up.

    The fix-up exists only to undo the integration's own disable, so it has to
    tell the two apart. An entity switched off in the UI must survive every
    reload, however many events the input has selected.
    """
    entry = _entry()
    await _setup_with(hass, entry)
    registry = er.async_get(hass)
    entity_id = _eid(hass, "event", "input_0")
    # This input does have events selected, so only the disabling reason keeps
    # the fix-up off it.
    assert entry.runtime_data.enabled_events[0]
    registry.async_update_entity(entity_id, disabled_by=er.RegistryEntryDisabler.USER)

    with _reads(), patch(f"{MANAGER}.async_save_action"):
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()

    assert registry.async_get(entity_id).disabled_by is er.RegistryEntryDisabler.USER
    assert hass.states.get(entity_id) is None


# --- coordinator refresh ----------------------------------------------------


async def test_a_failed_poll_keeps_a_requested_full_refresh_pending(
    hass: HomeAssistant,
) -> None:
    """One unanswered poll must not cancel a full refresh someone asked for.

    Regression: the flag and the slow-cycle counter were consumed before the
    fetch that can fail, so a write followed by a timed-out poll left the
    control showing its old value for up to the whole slow-refresh window.
    """
    entry = _entry()
    await _setup_with(hass, entry)
    coordinator = entry.runtime_data.coordinator
    entity_id = _eid(hass, "switch", "status_led")
    assert hass.states.get(entity_id).state == "off"

    coordinator.async_request_full_refresh()
    with (
        _reads(),
        patch(
            f"{MANAGER}.async_get_extended_state",
            side_effect=BleBoxConnectionError("timed out"),
        ),
    ):
        await coordinator.async_refresh()
        await hass.async_block_till_done()
    assert coordinator.last_update_success is False

    # The device answers again and reports the setting as changed; the pending
    # full refresh is what carries that back, since settings are otherwise only
    # read on the slow cycle.
    changed = {**SETTINGS, "statusLed": {"enabled": 1}}
    with _reads(settings=changed):
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    assert hass.states.get(entity_id).state == "on"


# --- self-healing of the device's callbacks ---------------------------------
#
# The switch is not the only thing that writes its action slots: so does the
# wBox app, a factory reset and a restored backup. Automatic mode therefore has
# to notice its callbacks going missing and put them back, and - because this
# is the one path here that writes to hardware on a timer, unprompted - it has
# to be equally sure about when *not* to.

OUR_CALLBACKS: list[tuple[int, int, str]] = [
    (0, TRIGGER_SHORT_CLICK, "short_press"),
    (0, TRIGGER_LONG_CLICK, "long_press"),
    (1, TRIGGER_SHORT_CLICK, "short_press"),
]
"""Exactly what `_entry()`'s event selection asks the device to call."""


def _provisioned_slots(
    wanted: list[tuple[int, int, str]] | None = None,
) -> list[dict[str, Any]]:
    """Return the slots a completed automatic run leaves on the device."""
    return [
        _slot(
            index,
            name=f"HA IN{input_id + 1} {event_type}",
            input=input_id,
            triggerType=trigger,
            actionType=ACTION_HTTP_GET,
            param=f"{BASE_URL}/api/{DOMAIN}/{TOKEN}/{input_id}/{event_type}",
        )
        for index, (input_id, trigger, event_type) in enumerate(wanted or OUR_CALLBACKS)
    ]


async def test_a_callback_edited_away_on_the_device_is_restored(
    hass: HomeAssistant,
) -> None:
    """Callbacks that vanished from the device are written back.

    The wBox app can edit or delete an action slot, a factory reset empties all
    of them, and restoring a backup taken before setup does the same. None of
    those tell Home Assistant anything, so without this the switch simply stops
    working while its entities keep looking perfectly healthy.
    """
    entry = _entry(mode=MODE_AUTOMATIC)
    await _setup(hass, entry, _actions_state(_provisioned_slots()))
    coordinator = entry.runtime_data.coordinator

    # `_device_reads` reports six empty slots: the device now holds nothing of
    # ours. Only a full refresh reads the action slots at all, so an ordinary
    # poll could never notice.
    coordinator.async_request_full_refresh()
    with _device_reads(), patch(f"{MANAGER}.async_save_action") as save:
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    written = [call.args[0] for call in save.call_args_list]
    assert {(a["input"], a["triggerType"]) for a in written} == {
        (input_id, trigger) for input_id, trigger, _ in OUR_CALLBACKS
    }
    # Restored carrying the real URLs, rather than merely reserving the slots.
    assert all(f"/api/{DOMAIN}/{TOKEN}/" in action["param"] for action in written)


@pytest.mark.parametrize(
    ("options", "wanted"),
    [
        ({}, OUR_CALLBACKS),
        # Edge inversion changes which trigger type each event asks for, so a
        # comparison that ignored the option would find every slot wrong on
        # every poll of an inverted device and never stop repairing them.
        (
            {
                CONF_ENABLED_EVENTS: {"0": ["press", "release"], "1": []},
                CONF_INVERT_EDGES: True,
            },
            [
                (0, TRIGGER_FALLING_EDGE, "press"),
                (0, TRIGGER_RISING_EDGE, "release"),
            ],
        ),
    ],
    ids=["as configured", "with the edges inverted"],
)
async def test_callbacks_the_device_still_has_are_left_alone(
    hass: HomeAssistant,
    options: dict[str, Any],
    wanted: list[tuple[int, int, str]],
) -> None:
    """A device holding exactly what it should is not touched at all.

    Healing runs on a timer, so a comparison that never quite matched would go
    back to the device every minute for as long as the integration ran. The
    reconciler is idempotent and would write nothing, but it re-reads the slot
    array under its own lock to get there, and that read is real traffic to a
    device that may be on a slow or congested link.
    """
    intact = _actions_state(_provisioned_slots(wanted))
    entry = _entry(mode=MODE_AUTOMATIC, **options)
    save = await _setup(hass, entry, intact)
    assert save.call_count == 0, "the initial provisioning already disagreed"

    coordinator = entry.runtime_data.coordinator
    coordinator.async_request_full_refresh()
    with (
        _device_reads(),
        patch(f"{MANAGER}.async_get_actions_state", return_value=intact) as read,
        patch(f"{MANAGER}.async_save_action") as save,
    ):
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    assert save.call_count == 0
    # One read is the poll's own. A second one is the reconciler being asked to
    # fix something, which is how a comparison that silently stopped matching
    # shows up while it is still only costing requests.
    assert read.call_count == 1


async def test_a_callback_pointed_somewhere_else_counts_as_missing(
    hass: HomeAssistant,
) -> None:
    """A slot of ours edited to call a different URL is repaired too.

    Ownership comes from the URL prefix, so an action still marked as ours but
    pointed at the wrong address is exactly the failure that looks like working
    hardware and delivers nothing.
    """
    slots = _provisioned_slots()
    slots[0] = {**slots[0], "param": f"{BASE_URL}/api/{DOMAIN}/{TOKEN}/0/release"}
    edited = _actions_state(slots)

    entry = _entry(mode=MODE_AUTOMATIC)
    await _setup(hass, entry, edited)
    coordinator = entry.runtime_data.coordinator

    coordinator.async_request_full_refresh()
    with (
        _device_reads(),
        patch(f"{MANAGER}.async_get_actions_state", return_value=edited),
        patch(f"{MANAGER}.async_save_action") as save,
    ):
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    written = [call.args[0] for call in save.call_args_list]
    # Only the tampered slot; the two that are still right stay untouched.
    assert len(written) == 1
    assert written[0]["param"].endswith("/0/short_press")


async def test_nothing_is_healed_before_the_first_provisioning(
    hass: HomeAssistant,
) -> None:
    """The first refresh happens before setup provisions, and must not race it.

    Setup refreshes the coordinator before it writes the device's callbacks, so
    at that moment they are legitimately absent. Treating that as drift would
    have two provisioning runs in flight at once, both allocating out of the
    same slot array.
    """
    entry = _entry(mode=MODE_AUTOMATIC)
    entry.add_to_hass(hass)

    with (
        _device_reads(),
        patch(f"{MANAGER}.async_save_action"),
        patch(f"{COORDINATOR}.async_apply_provisioning") as healed,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    # Setup's own provisioning call is not this one: it holds its own reference.
    assert healed.call_count == 0

    # And once that has run, the very same missing callbacks do get restored,
    # so this is a guard on timing rather than a path that never fires.
    coordinator = entry.runtime_data.coordinator
    coordinator.async_request_full_refresh()
    with (
        _device_reads(),
        patch(f"{MANAGER}.async_save_action"),
        patch(f"{COORDINATOR}.async_apply_provisioning") as healed,
    ):
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    assert healed.call_count == 1


async def test_manual_mode_is_never_healed(hass: HomeAssistant) -> None:
    """Manual mode does not write to the device, on any schedule.

    Manual mode exists for people who configure their device themselves. Slots
    appearing in the wBox app that nobody put there would be a breach of that,
    and an unattended one, since healing runs on a poll rather than on a user's
    action. Three separate checks stand between a manual entry and a write -
    the mode, the fact that provisioning was never attempted, and provisioning
    refusing the mode itself - so this pins the outcome rather than any one of
    them.
    """
    entry = _entry(mode=MODE_MANUAL)
    await _setup(hass, entry)
    coordinator = entry.runtime_data.coordinator

    coordinator.async_request_full_refresh()
    with (
        _device_reads(),
        patch(f"{MANAGER}.async_save_action") as save,
        patch(f"{COORDINATOR}.async_apply_provisioning") as healed,
    ):
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    assert save.call_count == 0
    # Provisioning refuses manual mode itself as well, so the device is safe
    # either way; this pins that the decision is taken before it gets there.
    assert healed.call_count == 0


async def test_nothing_is_healed_without_a_home_assistant_url(
    hass: HomeAssistant,
) -> None:
    """With no URL to put in them, restoring the callbacks is not possible.

    Home Assistant cannot always work its own address out, and the user has not
    supplied one here. Writing anyway would fill the device's slots with
    callbacks to nowhere, which is worse than the empty slots it has.
    """
    entry = _entry(mode=MODE_AUTOMATIC, **{CONF_BASE_URL: ""})
    entry.add_to_hass(hass)

    with (
        _device_reads(),
        patch(f"{API}.get_url", side_effect=NoURLAvailableError),
        patch(f"{MANAGER}.async_save_action") as save,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert save.call_count == 0, "setup provisioned without a URL"

        coordinator = entry.runtime_data.coordinator
        coordinator.async_request_full_refresh()
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    assert save.call_count == 0
    # The poll itself still succeeded. Having no URL is a configuration problem
    # to be reported, not a reason to mark the device unreachable.
    assert coordinator.last_update_success is True


# --- device triggers --------------------------------------------------------


async def test_a_button_number_that_is_not_one_is_refused(hass: HomeAssistant) -> None:
    """The subtype is a 1-based button number, and is validated as one.

    A device trigger ends up in the user's automation YAML, where it can be
    hand-edited and copied between devices. The number is turned into an input
    index by subtracting one, so a subtype that is not a number, or is below
    one, would reach the event trigger and quietly listen for an input the
    device cannot have.
    """
    entry = _entry()
    await _setup(hass, entry)
    device = dr.async_get(hass).async_get_device(
        identifiers={(BLEBOX_DOMAIN, BLEBOX_ID)}
    )

    base = {
        "platform": "device",
        "domain": DOMAIN,
        "device_id": device.id,
        "type": "short_press",
    }
    # Which rule refused it is part of the contract: the two failures need
    # different corrections, and this message is all the user gets.
    for subtype, complaint in (
        ("one", "must be numeric"),
        ("", "must be numeric"),
        ("0", "start at 1"),
        ("-1", "start at 1"),
    ):
        with pytest.raises(vol.Invalid) as raised:
            await device_automation_trigger.async_validate_trigger_config(
                hass, {**base, "subtype": subtype}
            )
        assert complaint in str(raised.value), subtype

    # A button number is accepted however it was written, and comes back as the
    # string the trigger works in.
    validated = await device_automation_trigger.async_validate_trigger_config(
        hass, {**base, "subtype": 2}
    )
    assert validated["subtype"] == "2"


async def test_triggers_are_only_listed_for_devices_that_are_ours(
    hass: HomeAssistant,
) -> None:
    """A device this integration was never set up for is offered no triggers.

    Our entities deliberately share a device with the official integration's,
    and Home Assistant asks every integration involved with a device what it
    offers. Answering for a device we hold no config entry for would fill the
    automation editor with triggers that can never fire.
    """
    await _setup(hass, _entry())

    official_entry = MockConfigEntry(domain=BLEBOX_DOMAIN, unique_id="a-different-one")
    official_entry.add_to_hass(hass)
    stranger = dr.async_get(hass).async_get_or_create(
        config_entry_id=official_entry.entry_id,
        identifiers={(BLEBOX_DOMAIN, "a-different-one")},
        name="Somebody else's BleBox",
    )

    assert await device_trigger.async_get_triggers(hass, stranger.id) == []
    # And a device id the registry has never heard of, which is what a stale
    # automation pointing at a deleted device asks about.
    assert await device_trigger.async_get_triggers(hass, "no-such-device") == []


# --- more of the callback endpoint -------------------------------------------


async def test_a_callback_posted_instead_of_fetched_is_accepted(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    """POST works as well as GET, for hand-written and proxied callbacks.

    BleBox devices only ever issue a GET. The POST route exists for the people
    who put something in front of it - a reverse proxy, a script, another
    automation system relaying a press - and it has to behave identically.
    """
    await _setup(hass, _entry())
    client = await hass_client_no_auth()

    response = await client.post(f"/api/{DOMAIN}/{TOKEN}/1/long_press?s=0")
    assert response.status == 200
    await hass.async_block_till_done()

    state = hass.states.get(_eid(hass, "event", "input_1"))
    assert state.attributes["event_type"] == "long_press"
    assert "relay_state" not in state.attributes


async def test_a_callback_carrying_nonsense_state_is_still_delivered(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    """A state hint that is not a number is dropped, not guessed at.

    The hints are a convenience: they save the press being correlated with the
    next poll. A press is never worth losing over one, so anything unparseable
    is simply left out of the event.
    """
    await _setup(hass, _entry())
    client = await hass_client_no_auth()

    response = await client.get(f"/api/{DOMAIN}/{TOKEN}/0/short_press?s=yes&p=lots")
    assert response.status == 200
    await hass.async_block_till_done()

    state = hass.states.get(_eid(hass, "event", "input_0"))
    assert state.attributes["event_type"] == "short_press"
    assert "relay_state" not in state.attributes
    assert "power_w" not in state.attributes


async def test_a_callback_for_an_entry_that_is_not_running_asks_for_a_retry(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    """A token with nothing behind it answers 503, which invites a retry.

    Unloading revokes the token, so this is a race rather than a state anyone
    can arrange: a request already in flight while the entry goes away. It
    matters which refusal it gets. A 404 tells the device the URL is wrong,
    which is what the repairs dashboard reports as a rejected callback; a 503
    says "not now", and the device retries.
    """
    await _setup(hass, _entry())
    client = await hass_client_no_auth()

    async_register_token(hass, "orphaned-token", "an-entry-that-is-gone")
    response = await client.get(f"/api/{DOMAIN}/orphaned-token/0/short_press")
    assert response.status == 503

    # An unknown token still gets the bare 404 that reveals nothing.
    assert (await client.get(f"/api/{DOMAIN}/never-issued/0/short_press")).status == 404


# --- removing the integration -----------------------------------------------


async def test_removal_clears_the_actions_this_integration_created(
    hass: HomeAssistant,
) -> None:
    """Deleting the integration takes its callbacks off the device with it.

    Leaving them behind means a switch that keeps calling an address that no
    longer answers, and slots the user has to find and clear in the wBox app.
    """
    entry = _entry(mode=MODE_AUTOMATIC)
    await _setup(hass, entry)

    with patch(
        f"{MANAGER}.async_remove_owned_actions", return_value=[0, 1, 2]
    ) as clear:
        assert await hass.config_entries.async_remove(entry.entry_id)
        await hass.async_block_till_done()

    assert clear.call_count == 1


async def test_removal_can_be_told_to_leave_the_device_alone(
    hass: HomeAssistant,
) -> None:
    """The cleanup is an option, because sometimes the device is the point.

    Someone replacing this integration with hand-written callbacks wants the
    slots exactly as they are, and re-adding the integration afterwards would
    rewrite them anyway.
    """
    entry = _entry(mode=MODE_AUTOMATIC, **{CONF_CLEANUP_ON_REMOVE: False})
    await _setup(hass, entry)

    with patch(f"{MANAGER}.async_remove_owned_actions") as clear:
        assert await hass.config_entries.async_remove(entry.entry_id)
        await hass.async_block_till_done()

    assert clear.call_count == 0


async def test_removal_finishes_even_when_the_device_cannot_be_reached(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A device that is unplugged must not block deleting the integration.

    Half the reason to remove it is that the device is gone. The actions stay
    on the hardware and the user is told where to find them, which is the
    honest outcome: silently guessing at device configuration would be worse
    than a stale slot. What it must not read as is a failure of the removal,
    which is what letting the error escape produces - a logged traceback about
    an integration that has, in fact, been removed perfectly successfully.
    """
    entry = _entry(mode=MODE_AUTOMATIC)
    await _setup(hass, entry)
    caplog.clear()

    with patch(
        f"{MANAGER}.async_remove_owned_actions",
        side_effect=BleBoxConnectionError("no route to host"),
    ):
        assert await hass.config_entries.async_remove(entry.entry_id)
        await hass.async_block_till_done()

    assert not hass.config_entries.async_entries(DOMAIN)
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert "no route to host" in caplog.text
    # Actionable: the slots are still there and the wBox app is where they go.
    assert "wBox app" in caplog.text


# --- capability detection ---------------------------------------------------


async def test_a_device_with_no_relay_gets_no_relay_controls(
    hass: HomeAssistant,
) -> None:
    """Inputs without a relay are a whole product, not a broken payload.

    A buttonBox has buttons and nothing to switch, and does not report an
    uptime either. Design rule 5 says entities appear because the device
    reported the underlying capability, so none of the relay controls may be
    created here - while the event entities, which never depended on any of it,
    come up exactly as always.
    """
    no_relays = {key: value for key, value in SETTINGS.items() if key != SETTING_RELAYS}
    no_relay_state = {
        key: value for key, value in EXTENDED_STATE.items() if key != "relays"
    }
    await _setup_with(
        hass,
        _entry(),
        settings=no_relays,
        state=no_relay_state,
        uptime=None,
        # A device that will not describe its network has no access point to
        # offer either, so that switch is gated on the same evidence.
        network={},
    )

    registry = er.async_get(hass)

    def present(domain: str, key: str) -> bool:
        return (
            registry.async_get_entity_id(domain, DOMAIN, f"{BLEBOX_ID}_{key}")
            is not None
        )

    assert not present("select", "state_after_restart")
    assert not present("switch", "relay")
    assert not present("sensor", "countdown")
    assert not present("sensor", "uptime")
    assert not present("switch", "access_point")
    assert present("event", "input_0")
    # The cloud tunnel has nothing to do with relays and stays.
    assert present("switch", "cloud_tunnel")


@pytest.mark.parametrize(
    ("state", "why"),
    [
        ({"relays": EXTENDED_STATE["relays"]}, "no meter at all"),
        (
            {**EXTENDED_STATE, "sensors": [], "powerMeasuring": {"enabled": 0}},
            "a meter that reports no readings",
        ),
        (
            {
                **EXTENDED_STATE,
                "sensors": [],
                "powerMeasuring": {"enabled": 1, "powerConsumption": []},
            },
            "a meter that has not completed a period yet",
        ),
    ],
)
async def test_a_device_that_reports_no_power_gets_no_power_sensors(
    hass: HomeAssistant, state: dict[str, Any], why: str
) -> None:
    """Power and energy sensors exist only where there is something to read.

    Most switchBox hardware has no meter, and a meter that has not measured
    anything yet reports the same nothing. Creating the sensors anyway would
    leave two permanently unknown entities on the device page and, worse, two
    unknown values in anybody's energy dashboard.
    """
    await _setup_with(hass, _entry(), state=state)
    registry = er.async_get(hass)

    for key in ("active_power", "power_consumption"):
        assert (
            registry.async_get_entity_id("sensor", DOMAIN, f"{BLEBOX_ID}_{key}") is None
        ), f"{key} was created for {why}"

    # Still a working device: the relay it does have is there.
    assert hass.states.get(_eid(hass, "switch", "relay")).state == "on"


async def test_button_controls_report_nothing_when_the_action_api_is_silent(
    hass: HomeAssistant,
) -> None:
    """A device that will not describe its action slots leaves the control blank.

    The action API is undocumented and may disappear with a firmware update.
    When it does, what a button is bound to is genuinely unknown, and saying so
    is better than showing "do nothing" - which is a real option the user could
    reasonably believe.
    """
    entry = _entry(**{CONF_MANAGE_BUTTONS: True})
    entry.add_to_hass(hass)
    with (
        _reads(),
        patch(
            f"{MANAGER}.async_get_actions_state",
            side_effect=BleBoxConnectionError("no action api"),
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    entity_id = _eid(hass, "select", "button_action_0_short_press")
    assert hass.states.get(entity_id).state == "unknown"


# --- writes that only exist in one direction --------------------------------


@pytest.mark.parametrize(
    ("backlight", "why"),
    [
        ({"enabled": 0}, "the device has never been given one"),
        ({"enabled": 0, "color": ""}, "it reports the colour as empty"),
        ({"enabled": 0, "color": "ffff"}, "the colour is the wrong length"),
        ({"enabled": 0, "color": "ffffgg"}, "the colour is not hexadecimal"),
    ],
)
async def test_a_backlight_with_no_usable_colour_is_given_one(
    hass: HomeAssistant, backlight: dict[str, Any], why: str
) -> None:
    """Turning the backlight on always sends a colour it can actually show.

    Enabling it without one lights the buttons as nothing at all, which is
    indistinguishable from a control that did not work. Anything the device
    reports that cannot be read back as a colour counts as not having one.
    """
    dark = {**SETTINGS, SETTING_BACKLIGHT: backlight}
    await _setup_with(hass, _entry(), settings=dark)
    entity_id = _eid(hass, "light", "buttons_backlight")

    assert hass.states.get(entity_id).state == "off"
    assert hass.states.get(entity_id).attributes.get("rgb_color") is None, why

    patches = await _call(
        hass, "light", "turn_on", {"entity_id": entity_id}, settings=dark
    )
    assert patches == [{SETTING_BACKLIGHT: {"enabled": 1, "color": "ffffff"}}]


async def test_every_switch_can_be_turned_back_on(hass: HomeAssistant) -> None:
    """On and off are separate methods on every switch, so both are exercised.

    Each of these sends a different thing to a different endpoint, and turning
    one back on is the half a user reaches for after discovering what turning
    it off did.
    """
    await _setup_with(hass, _entry())

    patches = await _call(
        hass, "switch", "turn_on", {"entity_id": _eid(hass, "switch", "status_led")}
    )
    assert patches == [{"statusLed": {"enabled": 1}}]

    with _reads(), patch(f"{MANAGER}.async_set_relay", return_value=True) as relay:
        await hass.services.async_call(
            "switch",
            "turn_on",
            {"entity_id": _eid(hass, "switch", "relay")},
            blocking=True,
        )
        await hass.async_block_till_done()
    assert relay.await_args.args == (0, True)

    with _reads(), patch(f"{MANAGER}.async_set_ap_enabled") as access_point:
        await hass.services.async_call(
            "switch",
            "turn_on",
            {"entity_id": _eid(hass, "switch", "access_point")},
            blocking=True,
        )
        await hass.async_block_till_done()
    assert access_point.await_args.args == (True,)


async def test_a_firmware_install_asks_for_the_new_version_at_once(
    hass: HomeAssistant,
) -> None:
    """After an install the next poll re-reads the device's identity.

    Firmware is only fetched on the coordinator's slow cycle, so without asking
    for a full refresh the entity would go on offering an update the device has
    already applied for up to a minute after it rebooted.
    """
    newer = replace(DEVICE, raw={"availableFv": "0.1600"})
    entry = _entry()
    entry.add_to_hass(hass)
    with _reads(), patch(f"{MANAGER}.async_get_device_info", return_value=newer):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    entity_id = _eid(hass, "update", "firmware")
    assert hass.states.get(entity_id).state == "on"

    # The device reboots into the new image, so by the time the refresh the
    # install asked for lands it is answering as the upgraded device.
    upgraded = replace(DEVICE, firmware_version="0.1600", raw={})
    with (
        _reads(),
        patch(f"{MANAGER}.async_get_device_info", return_value=upgraded),
        patch(f"{MANAGER}.async_install_firmware") as install,
    ):
        await hass.services.async_call(
            "update", "install", {"entity_id": entity_id}, blocking=True
        )
        await hass.async_block_till_done()

    assert install.call_count == 1
    # An ordinary poll fetches state alone and would leave the entity offering
    # an update the device has already applied.
    state = hass.states.get(entity_id)
    assert state.attributes["installed_version"] == "0.1600"
    assert state.state == "off"


# --- payloads that stop making sense ----------------------------------------


async def test_entities_go_quiet_when_the_device_changes_shape(
    hass: HomeAssistant,
) -> None:
    """A payload that no longer parses leaves entities blank, not broken.

    Everything polled here comes out of an undocumented API, and what answers
    on a given address can change under Home Assistant's feet: a device
    replaced with a different model, a firmware downgrade, a two-relay device
    swapped for a one-relay one. Every one of these entities reads into the
    payload by index, so the shape changing has to end in an unknown value
    rather than an exception inside a coordinator callback, which would take
    the rest of the platform's update down with it.
    """
    relay = {"stateAfterRestart": 2, "defaultForTime": 0, "iconSet": 38}
    two_relays = {**SETTINGS, SETTING_RELAYS: [dict(relay), dict(relay)]}
    reported = EXTENDED_STATE["relays"][0]
    two_states = {
        **EXTENDED_STATE,
        "relays": [dict(reported), {**reported, "relay": 1, "state": 0}],
    }

    entry = _entry()
    await _setup_with(hass, entry, settings=two_relays, state=two_states)

    # The countdowns are diagnostic and off by default; this is about what they
    # do with a payload, so they have to be in the state machine to be seen.
    registry = er.async_get(hass)
    for key in ("countdown", "countdown_1"):
        registry.async_update_entity(_eid(hass, "sensor", key), disabled_by=None)
    with _reads(settings=two_relays, state=two_states):
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()

    assert hass.states.get(_eid(hass, "switch", "relay_1")).state == "off"
    assert hass.states.get(_eid(hass, "select", "state_after_restart_1")).state == (
        "restore"
    )
    assert hass.states.get(_eid(hass, "sensor", "countdown_1")).state == "0"

    # Whatever answers now describes one relay, and describes it with something
    # that is not an object at all.
    nonsense_settings = {**SETTINGS, SETTING_RELAYS: ["?"]}
    nonsense_state = {
        **EXTENDED_STATE,
        "relays": ["?"],
        "sensors": [],
        "powerMeasuring": {"enabled": 1, "powerConsumption": ["?"]},
    }
    coordinator = entry.runtime_data.coordinator
    coordinator.async_request_full_refresh()
    with _reads(settings=nonsense_settings, state=nonsense_state):
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    assert coordinator.last_update_success is True
    assert hass.states.get(_eid(hass, "select", "state_after_restart")).state == (
        "unknown"
    )
    assert hass.states.get(_eid(hass, "select", "state_after_restart_1")).state == (
        "unknown"
    )
    assert hass.states.get(_eid(hass, "sensor", "countdown")).state == "unknown"
    assert hass.states.get(_eid(hass, "sensor", "countdown_1")).state == "unknown"
    assert hass.states.get(_eid(hass, "sensor", "active_power")).state == "unknown"
    assert hass.states.get(_eid(hass, "sensor", "power_consumption")).state == "unknown"

    # A relay the device no longer describes cannot be written to either, so the
    # select for it sends nothing rather than an index that is not there.
    with (
        _reads(settings=nonsense_settings),
        patch(f"{MANAGER}.async_set_settings", return_value={}) as write,
    ):
        await hass.services.async_call(
            "select",
            "select_option",
            {
                "entity_id": _eid(hass, "select", "state_after_restart_1"),
                "option": "on",
            },
            blocking=True,
        )
        await hass.async_block_till_done()
    assert write.call_count == 0


# --- device registry wiring -------------------------------------------------


async def test_a_device_id_that_is_not_a_mac_advertises_no_connection(
    hass: HomeAssistant,
) -> None:
    """A device numbering itself some other way still gets a device entry.

    BleBox device ids happen to be MAC addresses without separators, and that
    is what links our entities to the official integration's by connection as
    well as by identifier. It is a convention rather than a promise, so an id
    that is not one has to end in no connection at all rather than in a
    nonsense one, which the registry would then link other devices to.
    """
    odd_id = "SN-0001-not-a-mac"
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Odd BleBox",
        unique_id=odd_id,
        data={
            CONF_HOST: "192.168.1.100",
            CONF_PORT: 80,
            CONF_BLEBOX_ID: odd_id,
            CONF_CALLBACK_TOKEN: TOKEN,
            CONF_INPUTS: [0],
            CONF_SUPPORTS_ACTIONS: True,
        },
        options={
            CONF_MODE: MODE_MANUAL,
            CONF_ENABLED_EVENTS: {"0": ["short_press"]},
            CONF_BASE_URL: BASE_URL,
        },
    )
    await _setup_with(hass, entry)

    device = dr.async_get(hass).async_get_device(identifiers={(BLEBOX_DOMAIN, odd_id)})
    assert device is not None
    assert device.connections == set()
    assert hass.states.get("event.odd_blebox_button_1") is not None


# --- diagnostics ------------------------------------------------------------


async def test_diagnostics_describe_the_slots_without_leaking_them(
    hass: HomeAssistant,
) -> None:
    """A dump says what every configured slot holds, and redacts what it must.

    A diagnostics dump is meant to be attached to a bug report. Our own URLs
    carry the callback token, and somebody else's action can be pointed at
    anything at all - a cloud endpoint with an API key in the query string is a
    perfectly ordinary thing to find in a wBox action slot - so the two are
    redacted for different reasons and to different degrees.
    """
    from custom_components.blebox_advanced.diagnostics import (
        REDACTED,
        async_get_config_entry_diagnostics,
    )

    ours = _provisioned_slots()[0]
    theirs = _slot(
        1,
        name="notify",
        input=1,
        triggerType=TRIGGER_SHORT_CLICK,
        actionType=ACTION_HTTP_GET,
        param="http://example.invalid/hook?key=hunter2",
    )
    entry = _entry()
    await _setup(hass, entry, _actions_state([ours, theirs]))

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    listed = {action["slot"]: action for action in diagnostics["device_actions"]}

    # Only the two configured slots; the four empty ones say nothing.
    assert set(listed) == {0, 1}
    assert listed[0]["owned_by_integration"] is True
    assert listed[0]["param"].endswith(f"/{REDACTED}/0/short_press")
    # Not ours, so not shown at all rather than merely stripped of our token.
    assert listed[1]["owned_by_integration"] is False
    assert listed[1]["param"] == REDACTED
    assert "hunter2" not in repr(diagnostics)
    assert TOKEN not in repr(diagnostics)

    assert diagnostics["action_slots"] == {
        "available": True,
        "total": 6,
        "free": 4,
        "created_by_this_integration": 1,
        "belonging_to_others": 1,
    }


async def test_diagnostics_say_so_when_the_action_api_is_unavailable(
    hass: HomeAssistant,
) -> None:
    """A device that will not describe its slots is reported as such.

    The action API is undocumented and may vanish with a firmware update. When
    it does, the dump has to distinguish "this device has no slots in use" from
    "we could not ask", because they call for entirely different advice: the
    first is fine, the second means automatic mode cannot work at all.
    """
    from custom_components.blebox_advanced.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    entry = _entry()
    entry.add_to_hass(hass)
    with (
        _reads(),
        patch(
            f"{MANAGER}.async_get_actions_state",
            side_effect=BleBoxConnectionError("no action api"),
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["action_slots"] == {"available": False}
    assert diagnostics["device_actions"] == []
    # The callback mapping is worked out from the options, not from the device,
    # so it is still there - which is the whole point of asking for a dump.
    assert len(diagnostics["callback_mappings"]) == 3


async def test_a_callback_still_lands_when_the_device_row_has_gone(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    """A press is never dropped for want of a device registry entry.

    The registry id is only carried so device triggers can match on it, and it
    is looked up lazily because the row may not exist the first time a callback
    arrives. A user deleting the device from the UI must therefore cost them
    their device-based automations and nothing else: the event entity, and any
    automation listening to it, has to keep working.
    """
    await _setup(hass, _entry())
    registry = dr.async_get(hass)
    device = registry.async_get_device(identifiers={(BLEBOX_DOMAIN, BLEBOX_ID)})
    registry.async_remove_device(device.id)
    await hass.async_block_till_done()

    fired: list[Any] = []
    hass.bus.async_listen(HA_EVENT, lambda event: fired.append(event))

    client = await hass_client_no_auth()
    assert (await client.get(f"/api/{DOMAIN}/{TOKEN}/0/short_press")).status == 200
    await hass.async_block_till_done()

    assert len(fired) == 1
    assert fired[0].data["event_type"] == "short_press"
    assert "device_id" not in fired[0].data
