"""Tests for callback health, enriched events, relay reporting and button control."""

from __future__ import annotations

import time
from dataclasses import replace
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.blebox_advanced.blebox_actions import (
    ACTION_HTTP_GET,
    ACTION_RELAY_OFF,
    ACTION_RELAY_TOGGLE,
    TRIGGER_LONG_CLICK,
    TRIGGER_SHORT_CLICK,
    ActionsState,
    BleBoxConnectionError,
    InsufficientSlotsError,
    find_native_action,
    relay_state_from,
)
from custom_components.blebox_advanced.const import (
    CONF_ENABLED_EVENTS,
    CONF_MANAGE_BUTTONS,
    DOMAIN,
)
from custom_components.blebox_advanced.coordinator import callback_health

from .test_integration import (
    BLEBOX_ID,
    DEVICE,
    EXTENDED_STATE,
    MANAGER,
    SETTINGS,
    TOKEN,
    _actions_state,
    _entry,
    _setup,
    _slot,
)
from .test_settings_entities import _eid, _reads, _setup_with

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
    """A callback's query parameters become event attributes."""
    await _setup(hass, _entry())
    client = await hass_client_no_auth()

    response = await client.get(f"/api/{DOMAIN}/{TOKEN}/0/short_press?s=1&p=42.5")
    assert response.status == 200
    await hass.async_block_till_done()

    state = hass.states.get(
        er.async_get(hass).async_get_entity_id("event", DOMAIN, f"{BLEBOX_ID}_input_0")
    )
    assert state.attributes["relay_state"] is True
    assert state.attributes["power_w"] == 42.5


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
    """Power and energy come from the state payload already being polled."""
    state = {
        **EXTENDED_STATE,
        "sensors": [{"type": "activePower", "value": 137, "trend": 0, "state": 2}],
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
