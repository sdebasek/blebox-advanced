"""End-to-end tests against a real Home Assistant instance.

These cover the acceptance criteria: pressing the wall switch reaches Home
Assistant as an event, the entity lands on the *official* BleBox device, the
events show up as device triggers, and the endpoint refuses anything it does
not recognise.
"""

from __future__ import annotations

import asyncio
import dataclasses
from contextlib import ExitStack
from datetime import timedelta
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.components.device_automation import DeviceAutomationType
from homeassistant.config_entries import RELOAD_AFTER_UPDATE_DELAY
from homeassistant.const import CONF_HOST, CONF_PORT, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    async_get_device_automations,
)

from custom_components.blebox_advanced.blebox_actions import (
    ACTION_HTTP_GET,
    TRIGGER_LONG_CLICK,
    TRIGGER_SHORT_CLICK,
    ActionsState,
    BleBoxConnectionError,
    DeviceInfo,
)
from custom_components.blebox_advanced.const import (
    BLEBOX_DOMAIN,
    CONF_BASE_URL,
    CONF_BLEBOX_ID,
    CONF_CALLBACK_TOKEN,
    CONF_DEBOUNCE_MS,
    CONF_DEVICE_CACHE,
    CONF_ENABLED_EVENTS,
    CONF_HW_VERSION,
    CONF_INPUTS,
    CONF_MODE,
    CONF_MODEL,
    CONF_SUPPORTS_ACTIONS,
    CONF_SW_VERSION,
    DOMAIN,
    HA_EVENT,
    MODE_AUTOMATIC,
    MODE_MANUAL,
    RESTART_STATE_RESTORE,
    SCAN_INTERVAL_SECONDS,
    SETUP_REFRESH_TIMEOUT_S,
)

BLEBOX_ID = "ae0bfbf927ba"
TOKEN = "0123456789abcdef0123456789abcdef"
BASE_URL = "http://192.168.10.50:8123"

DEVICE = DeviceInfo(
    device_id=BLEBOX_ID,
    name="Simon GO Switch",
    device_type="switchBox",
    product="SimonGOSwitch",
    firmware_version="0.1502",
    hardware_version="s_KS.swB.1.5.T.p55ST-0.3",
    api_level="20220114",
)

# Captured from the live Simon 55 GO.
SETTINGS = {
    "deviceName": "Simon GO Switch",
    "tunnel": {"enabled": 1, "logEnabled": 0},
    "statusLed": {"enabled": 0},
    "buttonsBacklight": {"enabled": 1, "color": "ffffff"},
    "relays": [{"stateAfterRestart": 2, "defaultForTime": 0, "iconSet": 38}],
    "switch": {},
    "powerMeasuring": {
        "enabled": 1,
        "safetyValue": {
            "activePower": 0,
            "fieldsPreferences": [
                {
                    "name": "activePower",
                    "minValue": 200,
                    "maxValue": 3680,
                    "specialValues": {"off": 0},
                }
            ],
        },
        "factoryCalibration": {"isCalibrated": 0},
    },
}

EXTENDED_STATE = {
    "relays": [
        {
            "relay": 0,
            "state": 1,
            "stateAfterRestart": 2,
            "defaultForTime": 0,
            "forTimeLeftS": 0,
            "forTimeEndState": 1,
            "iconSet": 38,
        }
    ],
    "switch": {"safety": {"eventReason": 0, "triggered": []}},
    "powerMeasuring": {
        "enabled": 1,
        "powerConsumption": [{"periodS": 3291, "value": 0.0}],
    },
    "sensors": [{"type": "activePower", "value": 0, "trend": 0, "state": 2}],
}

UPTIME_S = 3994

NETWORK = {
    "ip": "192.168.1.100",
    "ssid": "IoT",
    "mac": "ae:0b:fb:f9:27:ba",
    "apEnable": True,
    "apSSID": "SimonGOSwitch-ae0bfbf927ba",
    "apPasswd": "",
}

MANAGER = "custom_components.blebox_advanced.blebox_actions.BleBoxActionManager"


def _slot(slot_id: int, **overrides: Any) -> dict[str, Any]:
    return {
        "id": slot_id,
        "name": "",
        "input": 0,
        "triggerType": 0,
        "actionType": 0,
        "lastCall": {"timeElapsedS": -1},
        "param": "",
        "relay": 0,
        "forTime": 0,
        "ns": 0,
        **overrides,
    }


def _actions_state(slots: list[dict[str, Any]] | None = None) -> ActionsState:
    """Return the action state a two-input device with six slots reports."""
    slots = list(slots or [])
    slots.extend(_slot(i) for i in range(len(slots), 6))
    return ActionsState.from_payload(
        {
            "actions": slots,
            "itemsLimit": 6,
            "fieldsPreferences": [
                {
                    "name": "triggerType",
                    "values": [1, 2, 3, 4, 5],
                    "dependsOn": "input",
                    "constraints": [
                        {"input": None, "triggerType": [19]},
                        {"input": 0, "triggerType": [1, 2, 3, 4, 5]},
                        {"input": 1, "triggerType": [1, 2, 3, 4, 5]},
                    ],
                },
                {
                    "name": "actionType",
                    "dependsOn": "triggerType",
                    "constraints": [
                        {"triggerType": t, "actionType": [1, 2, 3, 50]}
                        for t in (1, 2, 3, 4, 5)
                    ],
                },
            ],
        }
    )


def _entry(
    mode: str = MODE_MANUAL, debounce: int = 0, **extra_options: object
) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Simon GO Switch",
        unique_id=BLEBOX_ID,
        data={
            CONF_HOST: "192.168.1.100",
            CONF_PORT: 80,
            CONF_BLEBOX_ID: BLEBOX_ID,
            CONF_CALLBACK_TOKEN: TOKEN,
            CONF_INPUTS: [0, 1],
            CONF_SUPPORTS_ACTIONS: True,
            CONF_MODEL: "switchBox",
            CONF_SW_VERSION: "0.1502",
            CONF_HW_VERSION: "s_KS.swB.1.5.T.p55ST-0.3",
        },
        options={
            CONF_MODE: mode,
            CONF_ENABLED_EVENTS: {
                "0": ["short_press", "long_press"],
                "1": ["short_press"],
            },
            CONF_BASE_URL: BASE_URL,
            CONF_DEBOUNCE_MS: debounce,
            **extra_options,
        },
    )


async def _setup(
    hass: HomeAssistant, entry: MockConfigEntry, state: ActionsState | None = None
):
    """Set up an entry with the device mocked out. Returns the save mock."""
    entry.add_to_hass(hass)
    with (
        patch(f"{MANAGER}.async_get_device_info", return_value=DEVICE),
        patch(
            f"{MANAGER}.async_get_actions_state", return_value=state or _actions_state()
        ),
        patch(f"{MANAGER}.async_get_settings", return_value=dict(SETTINGS)),
        patch(f"{MANAGER}.async_get_extended_state", return_value=dict(EXTENDED_STATE)),
        patch(f"{MANAGER}.async_get_uptime", return_value=UPTIME_S),
        patch(f"{MANAGER}.async_get_network", return_value=dict(NETWORK)),
        patch(f"{MANAGER}.async_save_action") as save,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return save


def _device_reads(
    settings: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
    uptime: int | None = UPTIME_S,
) -> ExitStack:
    """Patch every device read the coordinator performs, for a later refresh."""
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
        patch(f"{MANAGER}.async_get_network", return_value=dict(NETWORK))
    )
    return stack


def _unreachable() -> ExitStack:
    """Patch every device read to fail, as a device that is not answering does."""
    stack = ExitStack()
    for method in (
        "async_get_device_info",
        "async_get_actions_state",
        "async_get_settings",
        "async_get_extended_state",
        "async_get_network",
    ):
        stack.enter_context(
            patch(f"{MANAGER}.{method}", side_effect=BleBoxConnectionError("down"))
        )
    # The API layer swallows this one and answers None, exactly as it does for a
    # device that simply will not say.
    stack.enter_context(patch(f"{MANAGER}.async_get_uptime", return_value=None))
    return stack


def _entity_id(hass: HomeAssistant, input_id: int) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(
        "event", DOMAIN, f"{BLEBOX_ID}_input_{input_id}"
    )
    assert entity_id is not None
    return entity_id


def _registered(hass: HomeAssistant, domain: str, key: str) -> str | None:
    """Resolve an entity id by unique id, or None if it was never registered."""
    return er.async_get(hass).async_get_entity_id(domain, DOMAIN, f"{BLEBOX_ID}_{key}")


async def test_press_reaches_home_assistant(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    """A callback from the device updates the matching event entity."""
    await _setup(hass, _entry())

    entity_id = _entity_id(hass, 0)
    assert hass.states.get(entity_id).state == "unknown"

    client = await hass_client_no_auth()
    response = await client.get(f"/api/{DOMAIN}/{TOKEN}/0/short_press")
    assert response.status == 200
    await hass.async_block_till_done()

    state = hass.states.get(entity_id)
    assert state.attributes["event_type"] == "short_press"
    assert state.attributes["input"] == 0
    assert state.attributes["button"] == 1
    assert state.state != "unknown"

    # A second input is independent.
    assert hass.states.get(_entity_id(hass, 1)).state == "unknown"


async def test_entity_created_for_every_input(hass: HomeAssistant) -> None:
    """One entity per discovered input, advertising all four event types."""
    await _setup(hass, _entry())

    # Named after the device, so it reads well next to the official entities.
    assert _entity_id(hass, 0) == "event.simon_go_switch_button_1"
    assert _entity_id(hass, 1) == "event.simon_go_switch_button_2"

    state = hass.states.get(_entity_id(hass, 0))
    assert state.attributes["event_types"] == [
        "short_press",
        "long_press",
        "press",
        "release",
    ]
    assert _entity_id(hass, 1)


async def test_endpoint_rejects_unknown_input_and_token(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    """Only known devices, inputs and event types are accepted."""
    await _setup(hass, _entry())
    client = await hass_client_no_auth()

    assert (await client.get(f"/api/{DOMAIN}/wrong-token/0/short_press")).status == 404
    assert (await client.get(f"/api/{DOMAIN}/{TOKEN}/7/short_press")).status == 404
    assert (await client.get(f"/api/{DOMAIN}/{TOKEN}/0/triple_press")).status == 404
    assert (await client.get(f"/api/{DOMAIN}/{TOKEN}/abc/short_press")).status == 404


async def test_duplicate_callbacks_are_suppressed(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    """A retried call does not produce a second event."""
    await _setup(hass, _entry(debounce=500))
    client = await hass_client_no_auth()

    fired: list[Any] = []
    hass.bus.async_listen(HA_EVENT, lambda event: fired.append(event))

    assert (await client.get(f"/api/{DOMAIN}/{TOKEN}/0/short_press")).status == 200
    assert (await client.get(f"/api/{DOMAIN}/{TOKEN}/0/short_press")).status == 200
    await hass.async_block_till_done()

    assert len(fired) == 1

    # A different event on the same input is not suppressed.
    assert (await client.get(f"/api/{DOMAIN}/{TOKEN}/0/long_press")).status == 200
    await hass.async_block_till_done()
    assert len(fired) == 2


async def test_entities_link_to_the_official_blebox_device(hass: HomeAssistant) -> None:
    """Our entities land on the same logical device as the official integration.

    Home Assistant gives each config entry its own device registry entry and
    links entries sharing an identifier or connection (the frontend's
    `list_linked_devices`), so "one device" means linked, not literally one row.
    """
    device_registry = dr.async_get(hass)
    if not hasattr(device_registry, "async_get_devices"):
        pytest.skip("linked devices need Home Assistant 2026.1 or newer")
    blebox_entry = MockConfigEntry(domain=BLEBOX_DOMAIN, unique_id=BLEBOX_ID)
    blebox_entry.add_to_hass(hass)
    official = device_registry.async_get_or_create(
        config_entry_id=blebox_entry.entry_id,
        identifiers={(BLEBOX_DOMAIN, BLEBOX_ID)},
        manufacturer="BleBox",
        name="Simon GO Switch",
        model="switchBox",
    )

    await _setup(hass, _entry())

    entity = er.async_get(hass).async_get(_entity_id(hass, 0))
    ours = device_registry.async_get(entity.device_id)

    # The identifier the official integration uses is what links the two.
    assert (BLEBOX_DOMAIN, BLEBOX_ID) in ours.identifiers
    assert (BLEBOX_DOMAIN, BLEBOX_ID) in official.identifiers
    assert (dr.CONNECTION_NETWORK_MAC, "ae:0b:fb:f9:27:ba") in ours.connections

    linked = device_registry.async_get_devices(
        identifiers=ours.identifiers, connections=ours.connections
    )
    assert official.id in {device.id for device in linked}

    # Our own row is fully described rather than a nameless stub.
    assert ours.name == "Simon GO Switch"
    assert ours.manufacturer == "BleBox"
    assert ours.model == "switchBox"


async def test_linking_works_when_set_up_before_the_official_integration(
    hass: HomeAssistant,
) -> None:
    """Order does not matter: the official integration links to us just as well."""
    device_registry = dr.async_get(hass)
    if not hasattr(device_registry, "async_get_devices"):
        pytest.skip("linked devices need Home Assistant 2026.1 or newer")
    await _setup(hass, _entry())

    blebox_entry = MockConfigEntry(domain=BLEBOX_DOMAIN, unique_id=BLEBOX_ID)
    blebox_entry.add_to_hass(hass)
    official = device_registry.async_get_or_create(
        config_entry_id=blebox_entry.entry_id,
        identifiers={(BLEBOX_DOMAIN, BLEBOX_ID)},
        name="Simon GO Switch",
    )

    ours = device_registry.async_get(
        er.async_get(hass).async_get(_entity_id(hass, 0)).device_id
    )
    linked = device_registry.async_get_devices(
        identifiers=official.identifiers, connections=official.connections
    )
    assert ours.id in {device.id for device in linked}


async def test_device_triggers_are_exposed(hass: HomeAssistant) -> None:
    """Every input/event pair appears in the automation editor."""
    entry = _entry()
    await _setup(hass, entry)

    device = dr.async_get(hass).async_get_device(
        identifiers={(BLEBOX_DOMAIN, BLEBOX_ID)}
    )
    assert device is not None

    triggers = await async_get_device_automations(
        hass, DeviceAutomationType.TRIGGER, device.id
    )
    ours = [trigger for trigger in triggers if trigger["domain"] == DOMAIN]

    assert len(ours) == 8  # 2 inputs x 4 event types
    assert {(t["type"], t["subtype"]) for t in ours} == {
        (event_type, button)
        for button in ("1", "2")
        for event_type in ("short_press", "long_press", "press", "release")
    }


async def test_device_trigger_fires_automation(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    """A device trigger runs when the matching button event arrives."""
    from homeassistant.setup import async_setup_component

    await _setup(hass, _entry())
    device = dr.async_get(hass).async_get_device(
        identifiers={(BLEBOX_DOMAIN, BLEBOX_ID)}
    )

    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": {
                "triggers": {
                    "trigger": "device",
                    "domain": DOMAIN,
                    "device_id": device.id,
                    "type": "long_press",
                    "subtype": "1",
                },
                "actions": {"event": "blebox_advanced_test_fired"},
            }
        },
    )
    await hass.async_block_till_done()

    fired: list[Any] = []
    hass.bus.async_listen(
        "blebox_advanced_test_fired", lambda event: fired.append(event)
    )

    client = await hass_client_no_auth()
    # Wrong event type on the right button, then the right one.
    await client.get(f"/api/{DOMAIN}/{TOKEN}/0/short_press")
    await hass.async_block_till_done()
    assert not fired

    await client.get(f"/api/{DOMAIN}/{TOKEN}/0/long_press")
    await hass.async_block_till_done()
    assert len(fired) == 1


async def test_automatic_mode_provisions_only_its_own_actions(
    hass: HomeAssistant,
) -> None:
    """Automatic setup writes our callbacks and leaves a user action alone."""
    user_action = _slot(
        0, name="my toggle", input=0, triggerType=TRIGGER_SHORT_CLICK, actionType=1
    )
    save = await _setup(
        hass, _entry(mode=MODE_AUTOMATIC), _actions_state([user_action])
    )

    written = [call.args[0] for call in save.call_args_list]
    assert len(written) == 3  # short+long on input 0, short on input 1
    assert 0 not in {action["id"] for action in written}

    by_key = {(a["input"], a["triggerType"]): a for a in written}
    assert set(by_key) == {
        (0, TRIGGER_SHORT_CLICK),
        (0, TRIGGER_LONG_CLICK),
        (1, TRIGGER_SHORT_CLICK),
    }
    first = by_key[(0, TRIGGER_SHORT_CLICK)]
    assert first["actionType"] == ACTION_HTTP_GET
    assert first["param"] == f"{BASE_URL}/api/{DOMAIN}/{TOKEN}/0/short_press"
    assert "lastCall" not in first


async def test_manual_mode_writes_nothing_to_the_device(hass: HomeAssistant) -> None:
    """Manual mode never modifies device configuration."""
    save = await _setup(hass, _entry(mode=MODE_MANUAL))
    assert save.call_count == 0


async def test_setup_survives_an_unreachable_device(hass: HomeAssistant) -> None:
    """Entities still exist when the device cannot be reached at startup."""
    entry = _entry()
    entry.add_to_hass(hass)
    with _unreachable():
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert hass.states.get(_entity_id(hass, 0)) is not None

        # Unloaded inside the patch: the retry timer would otherwise fire
        # against a real socket during teardown.
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


# --- what setup is prepared to wait for -------------------------------------
#
# Setting an entry up must survive an unreachable device (above), which means
# every read it makes can only end by timing out. Each one of those is ten
# seconds during which this entry has no entities at all, so what setup waits
# for is a design decision rather than an ordering accident.

SETUP_MUST_NOT_BLOCK_S = 5
"""How long a test lets setup run before calling it blocked.

Generous, because it is only ever reached when something is wrong: the point of
the deadline is that a regression fails the test instead of hanging the suite.
"""

ORDINARY_DEADLINE_S = 10
"""The deadline `BleBoxActionManager` gives every request of its own accord."""


async def test_setup_does_not_wait_for_the_devices_action_slots(
    hass: HomeAssistant,
) -> None:
    """Provisioning is not allowed to hold the platforms up.

    Regression: an unreachable device cost setup two full timeouts in a row,
    because provisioning ran inline and, with no action slots in the failed
    poll to work from, went and asked the device for them itself. Nothing in
    platform setup needs that answer - healing does not start until provisioning
    has been attempted, and manual callbacks never needed it - so twenty seconds
    passed before this entry had a single entity.
    """
    answered = asyncio.Event()

    async def _answers_only_when_released(*_args: Any, **_kwargs: Any) -> ActionsState:
        await answered.wait()
        raise BleBoxConnectionError("down")

    entry = _entry(mode=MODE_AUTOMATIC)
    entry.add_to_hass(hass)
    with (
        _unreachable(),
        patch(f"{MANAGER}.async_get_actions_state", _answers_only_when_released),
        patch(f"{MANAGER}.async_save_action"),
    ):
        async with asyncio.timeout(SETUP_MUST_NOT_BLOCK_S):
            assert await hass.config_entries.async_setup(entry.entry_id)

        # Reached with the device still not having answered, so these exist
        # despite it: that is the whole claim.
        assert hass.states.get(_entity_id(hass, 0)) is not None

        answered.set()
        await hass.async_block_till_done()
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_a_remembered_device_is_given_a_shorter_first_deadline(
    hass: HomeAssistant,
) -> None:
    """A device whose shape is known is not waited out for the full timeout.

    Regression: the first poll always ran on the ordinary ten-second deadline,
    so restarting Home Assistant while the switch was unreachable - the exact
    case `CONF_DEVICE_CACHE` exists for - left the entry with no entities for
    ten seconds, to establish something the entry already knew. The poll is only
    being asked for values here, and the ordinary poll five seconds later
    fetches those anyway.
    """
    entry = _entry()
    await _setup(hass, entry)
    assert entry.data[CONF_DEVICE_CACHE], "nothing was remembered while it answered"

    deadlines: list[float | None] = []

    async def _record_the_deadline(manager: Any, *_args: Any, **_kwargs: Any) -> None:
        # Which deadline is in force is the manager's business and it offers no
        # way to ask, but it is exactly what this test is about.
        deadlines.append(manager._timeout.total)
        raise BleBoxConnectionError("down")

    with (
        _unreachable(),
        patch(f"{MANAGER}.async_get_extended_state", _record_the_deadline),
    ):
        # A reload is a restart for this purpose: a new coordinator, seeded from
        # what the entry remembers before it polls anything.
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()

        assert deadlines == [SETUP_REFRESH_TIMEOUT_S]
        # And only for that one poll: everything after it is a device read like
        # any other, and giving up early on those would be a regression of its
        # own.
        assert entry.runtime_data.manager._timeout.total == ORDINARY_DEADLINE_S

        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_a_device_nothing_is_known_about_is_worth_waiting_for(
    hass: HomeAssistant,
) -> None:
    """The first poll of an unknown device keeps the full deadline.

    Deliberate asymmetry with the test above. Nothing has ever observed what
    this device has, so this poll is the only thing that can create its polled
    entities at all: giving up on it early would trade ten seconds of setup for
    a device that comes up with no entities and stays that way until the user
    reloads it by hand.
    """
    deadlines: list[float | None] = []

    async def _record_the_deadline(manager: Any, *_args: Any, **_kwargs: Any) -> None:
        deadlines.append(manager._timeout.total)
        raise BleBoxConnectionError("down")

    entry = _entry()
    entry.add_to_hass(hass)
    with (
        _unreachable(),
        patch(f"{MANAGER}.async_get_extended_state", _record_the_deadline),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert deadlines == [ORDINARY_DEADLINE_S]

        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_unload_stops_accepting_callbacks(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    """Tokens stop working once the entry is unloaded, and no actions are removed."""
    entry = _entry(mode=MODE_AUTOMATIC)
    await _setup(hass, entry)
    client = await hass_client_no_auth()
    assert (await client.get(f"/api/{DOMAIN}/{TOKEN}/0/short_press")).status == 200

    with patch(f"{MANAGER}.async_save_action") as save:
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        assert save.call_count == 0

    assert (await client.get(f"/api/{DOMAIN}/{TOKEN}/0/short_press")).status == 404


# --- an unreachable device keeps the entities it has already shown ----------

POLLED_ENTITIES: list[tuple[str, str]] = [
    ("switch", "relay"),
    ("switch", "cloud_tunnel"),
    ("switch", "status_led"),
    ("switch", "access_point"),
    ("light", "buttons_backlight"),
    ("number", "overload_threshold"),
    ("select", "state_after_restart"),
    ("binary_sensor", "callback_delivery"),
    ("binary_sensor", "safety_triggered"),
    ("binary_sensor", "power_calibrated"),
    ("sensor", "active_power"),
    ("sensor", "power_consumption"),
    ("update", "firmware"),
]
"""Everything the fixture device produces that is enabled by default.

Uptime and countdown are deliberately absent: both are registered disabled, so
they never reach the state machine and cannot be checked there.
"""


async def test_entities_survive_a_restart_while_the_device_is_offline(
    hass: HomeAssistant,
) -> None:
    """A device that answered once keeps its entities when offline at startup.

    Regression: every polled platform decides what to create by inspecting live
    device data, so restarting Home Assistant while the switch was unreachable
    created the two pushed event entities and nothing else. Platform setup does
    not run again either, so the other fourteen stayed missing - with the user's
    automations and dashboards pointing at entities that no longer existed -
    until the entry was reloaded by hand.
    """
    entry = _entry()
    await _setup(hass, entry)
    persisted = dict(entry.data)
    assert persisted[CONF_DEVICE_CACHE], "nothing was remembered while it answered"

    # A restart is the entry coming back from `.storage` into a registry and a
    # state machine that hold nothing for it yet. Unloading it in place is not
    # the same thing at all: the entity registry keeps its rows, and Home
    # Assistant then answers for a registered entity nobody created with an
    # unavailable state of its own - which is precisely what this has to tell
    # apart from an entity that really came back.
    with patch(f"{MANAGER}.async_remove_owned_actions", return_value=[]):
        await hass.config_entries.async_remove(entry.entry_id)
        await hass.async_block_till_done()
    assert not hass.states.async_all()

    restarted = MockConfigEntry(
        domain=DOMAIN,
        title=entry.title,
        unique_id=BLEBOX_ID,
        data=persisted,
        options=dict(entry.options),
    )
    restarted.add_to_hass(hass)

    with _unreachable():
        assert await hass.config_entries.async_setup(restarted.entry_id)
        await hass.async_block_till_done()

        for domain, key in POLLED_ENTITIES:
            entity_id = _registered(hass, domain, key)
            assert entity_id is not None, f"{domain}.{key} was not created"
            assert hass.states.get(entity_id).state == STATE_UNAVAILABLE, (
                f"{domain}.{key} reports a remembered value as though it were live"
            )

        # The pushed entities never depended on the device answering.
        assert hass.states.get(_entity_id(hass, 0)) is not None

    # The point of creating them at all: they come back by themselves as soon as
    # the device answers, instead of waiting for the user to reload the entry.
    with _device_reads():
        await restarted.runtime_data.coordinator.async_refresh()
        await hass.async_block_till_done()

    assert hass.states.get(_registered(hass, "switch", "relay")).state == "on"
    assert (
        hass.states.get(_registered(hass, "update", "firmware")).attributes[
            "installed_version"
        ]
        == "0.1502"
    )


async def test_first_ever_setup_of_an_offline_device_still_pushes_events(
    hass: HomeAssistant,
) -> None:
    """A device that has never answered gets its event entities and nothing else.

    The polled entities genuinely cannot be created here, and that is
    unavoidable: nothing has ever observed what this device has, so there is no
    shape to fall back on and inventing one would be a guess that outlived the
    device it was guessed for. The event entities are pushed, so they must come
    up regardless - a manually configured callback keeps arriving either way.
    """
    entry = _entry()
    entry.add_to_hass(hass)
    with _unreachable():
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert hass.states.get(_entity_id(hass, 0)) is not None
        assert hass.states.get(_entity_id(hass, 1)) is not None
        assert _registered(hass, "switch", "relay") is None
        assert CONF_DEVICE_CACHE not in entry.data

        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_an_unchanged_shape_does_not_rewrite_the_config_entry(
    hass: HomeAssistant,
) -> None:
    """What the device reports is only persisted when its capabilities move.

    The remembered payloads carry live values - relay state, power, uptime - so
    storing them whenever one of those changed would rewrite the config entry,
    and with it Home Assistant's `.storage`, every few seconds for as long as
    the integration ran.
    """
    entry = _entry()
    await _setup(hass, entry)
    remembered = entry.data[CONF_DEVICE_CACHE]
    coordinator = entry.runtime_data.coordinator

    # Same capabilities, different readings: the relay has moved, the meter has
    # counted and the device has been up a minute longer.
    moved = {
        **EXTENDED_STATE,
        "relays": [{**EXTENDED_STATE["relays"][0], "state": 0}],
        "sensors": [{"type": "activePower", "value": 137, "trend": 1, "state": 2}],
    }
    with (
        _device_reads(state=moved, uptime=UPTIME_S + 60),
        patch.object(
            hass.config_entries,
            "async_update_entry",
            wraps=hass.config_entries.async_update_entry,
        ) as update,
    ):
        for _ in range(2):
            # Forced, because an ordinary poll fetches only the relay state and
            # would never reach the decision under test.
            coordinator.async_request_full_refresh()
            await coordinator.async_refresh()
        await hass.async_block_till_done()

    assert update.call_count == 0
    assert entry.data[CONF_DEVICE_CACHE] == remembered


async def test_a_half_answered_poll_does_not_forget_the_shape(
    hass: HomeAssistant,
) -> None:
    """A read that failed must not be remembered as a device without that feature.

    Settings, network state and uptime are best-effort reads: a device that
    answers its identity but times out on one of those reports exactly what a
    device that genuinely has none of it reports. Remembering that would leave
    the next offline start missing entities the device really does have.

    Settings and network defend themselves by carrying the last payload
    forward, so what is remembered is still what the device last said about
    itself. Uptime cannot do that - it only ever counts up, so a carried-forward
    one would be a lie - and it is therefore still what decides whether a poll
    is worth remembering at all.
    """
    entry = _entry()
    await _setup(hass, entry)
    remembered = entry.data[CONF_DEVICE_CACHE]
    coordinator = entry.runtime_data.coordinator

    with (
        _device_reads(),
        patch(
            f"{MANAGER}.async_get_settings",
            side_effect=BleBoxConnectionError("timed out"),
        ),
    ):
        coordinator.async_request_full_refresh()
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    assert entry.data[CONF_DEVICE_CACHE] == remembered
    assert entry.data[CONF_DEVICE_CACHE]["settings"]["relays"]

    # A device that has reported an uptime and now will not is the one case
    # that still has to hold the shape back, rather than remember a device
    # without an uptime sensor.
    with _device_reads(uptime=None):
        coordinator.async_request_full_refresh()
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    assert entry.data[CONF_DEVICE_CACHE] == remembered


async def test_a_changed_shape_is_remembered_without_reloading(
    hass: HomeAssistant,
) -> None:
    """A device whose capabilities really change has the new shape remembered.

    Firmware updates add and remove settings. Without this the entry would keep
    handing platform setup a shape the device grew out of, for as long as it
    stayed unreachable.
    """
    entry = _entry()
    await _setup(hass, entry)
    coordinator = entry.runtime_data.coordinator
    assert "statusLed" in entry.data[CONF_DEVICE_CACHE]["settings"]

    without_led = {key: value for key, value in SETTINGS.items() if key != "statusLed"}
    with _device_reads(settings=without_led):
        coordinator.async_request_full_refresh()
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    assert "statusLed" not in entry.data[CONF_DEVICE_CACHE]["settings"]
    # Writing to an entry fires its update listener, which reloads on an options
    # change. Remembering a shape must not be mistaken for one: a reload would
    # restart the entry, and re-provision the device, because a poll noticed
    # something. A new coordinator object is what a reload leaves behind.
    assert entry.runtime_data.coordinator is coordinator


async def test_new_firmware_is_remembered_for_the_next_offline_start(
    hass: HomeAssistant,
) -> None:
    """A firmware update reaches the remembered identity, not just the live one.

    The signature recorded only whether an identity existed, so a firmware
    version change never rewrote the cache. Starting up unreachable then seeded
    the device page from before the update, which is the one field someone
    checks to confirm an update worked.
    """
    entry = _entry()
    await _setup(hass, entry)
    assert entry.data[CONF_DEVICE_CACHE]["info"]["fv"] == DEVICE.firmware_version

    updated = dataclasses.replace(DEVICE, firmware_version="0.1600")
    with (
        _device_reads(),
        patch(f"{MANAGER}.async_get_device_info", return_value=updated),
    ):
        entry.runtime_data.coordinator.async_request_full_refresh()
        await entry.runtime_data.coordinator.async_refresh()
        await hass.async_block_till_done()

    assert entry.data[CONF_DEVICE_CACHE]["info"]["fv"] == "0.1600"


async def test_removing_the_entry_clears_its_repair_issues(
    hass: HomeAssistant,
) -> None:
    """Repairs raised for a device do not outlive the entry that raised them.

    Nothing deleted them on removal, so deleting the integration left a warning
    in the repairs dashboard about a device Home Assistant no longer knew
    anything about, and no way at all to dismiss it.
    """
    unreachable = _slot(
        0,
        name="HA IN1 short_press",
        input=0,
        triggerType=TRIGGER_SHORT_CLICK,
        actionType=ACTION_HTTP_GET,
        param=f"{BASE_URL}/api/{DOMAIN}/{TOKEN}/0/short_press",
    )
    unreachable["lastCall"] = {"timeElapsedS": 5, "response": {"status": 0}}
    entry = _entry()
    await _setup(hass, entry, _actions_state([unreachable]))

    issues = ir.async_get(hass)
    unreachable_key = f"callbacks_unreachable_{entry.entry_id}"
    rejected_key = f"callbacks_rejected_{entry.entry_id}"
    assert issues.async_get_issue(DOMAIN, unreachable_key) is not None

    # The other issue is what the same poll raises on a device that reaches Home
    # Assistant and is refused; raised directly so both are proved cleared.
    ir.async_create_issue(
        hass,
        DOMAIN,
        rejected_key,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="callbacks_rejected",
        translation_placeholders={"name": entry.title, "status": "404"},
    )

    with patch(f"{MANAGER}.async_remove_owned_actions", return_value=[]):
        await hass.config_entries.async_remove(entry.entry_id)
        await hass.async_block_till_done()

    assert issues.async_get_issue(DOMAIN, unreachable_key) is None
    assert issues.async_get_issue(DOMAIN, rejected_key) is None


async def test_diagnostics_redact_the_token(hass: HomeAssistant) -> None:
    """A diagnostics dump never contains the callback token."""
    from custom_components.blebox_advanced.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    entry = _entry()
    await _setup(hass, entry)

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert TOKEN not in repr(diagnostics)
    assert diagnostics["inputs"]["detected"] == [0, 1]
    assert len(diagnostics["callback_mappings"]) == 3


# --- a best-effort read that failed is not a device that answered emptily ----


async def test_a_failed_settings_read_keeps_the_settings_it_had(
    hass: HomeAssistant,
) -> None:
    """A settings read that times out leaves the settings-backed entities alone.

    Regression: settings are a best-effort read, so a failure was swallowed and
    the empty payload left behind went into the snapshot as though the device
    had answered with it. The poll still counted as a success, so nothing went
    unavailable and the entities simply inverted: Home Assistant recorded that
    as a genuine state change, so the switch the README singles out as the
    security relevant one reported itself turned off and any automation watching
    the cloud tunnel fired for nothing.
    """
    entry = _entry()
    await _setup(hass, entry)
    coordinator = entry.runtime_data.coordinator

    tunnel = _registered(hass, "switch", "cloud_tunnel")
    backlight = _registered(hass, "light", "buttons_backlight")
    overload = _registered(hass, "number", "overload_threshold")
    restart = _registered(hass, "select", "state_after_restart")
    assert hass.states.get(tunnel).state == STATE_ON

    with (
        _device_reads(),
        patch(
            f"{MANAGER}.async_get_settings",
            side_effect=BleBoxConnectionError("timed out"),
        ),
    ):
        coordinator.async_request_full_refresh()
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    # Still believed, because the device did answer everything else - and still
    # reporting what it last said about itself rather than the absence of it.
    assert coordinator.last_update_success
    assert hass.states.get(tunnel).state == STATE_ON
    assert hass.states.get(backlight).state == STATE_ON
    # The colour matters as much as the state here: with the payload blanked,
    # turning the backlight on wrote the default colour over the user's own.
    assert hass.states.get(backlight).attributes["rgb_color"] == (255, 255, 255)
    assert hass.states.get(overload).state == "0.0"
    assert hass.states.get(restart).state == RESTART_STATE_RESTORE

    # Every relay-only poll until the next slow cycle carries the same snapshot
    # forward, which is what stretched a single failed read to a whole minute.
    with _device_reads():
        await coordinator.async_refresh()
        await hass.async_block_till_done()
    assert hass.states.get(tunnel).state == STATE_ON

    # A device that really does answer with an empty object is still believed:
    # carrying values forward must not outlive the failure it covers for.
    with _device_reads(), patch(f"{MANAGER}.async_get_settings", return_value={}):
        coordinator.async_request_full_refresh()
        await coordinator.async_refresh()
        await hass.async_block_till_done()
    assert hass.states.get(tunnel).state == "off"


async def test_a_failed_network_read_keeps_the_access_point_it_had(
    hass: HomeAssistant,
) -> None:
    """The same for the network read, which the access point switch is built on.

    A blanked network payload turned the access point switch off and emptied the
    SSID it publishes, so the device looked as though it had stopped
    broadcasting - the opposite of the state the entity exists to warn about.
    """
    entry = _entry()
    await _setup(hass, entry)
    coordinator = entry.runtime_data.coordinator

    access_point = _registered(hass, "switch", "access_point")
    assert hass.states.get(access_point).state == STATE_ON

    with (
        _device_reads(),
        patch(
            f"{MANAGER}.async_get_network",
            side_effect=BleBoxConnectionError("timed out"),
        ),
    ):
        coordinator.async_request_full_refresh()
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    assert coordinator.last_update_success
    state = hass.states.get(access_point)
    assert state.state == STATE_ON
    assert state.attributes["ssid"] == NETWORK["apSSID"]


async def test_a_device_without_a_network_endpoint_is_still_remembered(
    hass: HomeAssistant,
) -> None:
    """Firmware without ``/api/device/network`` still has its shape remembered.

    The remembered shape is the only thing that gives a device its entities back
    after a restart while it is unreachable. It used to be written only when
    settings, network *and* uptime had all answered on the same poll, so a
    device whose firmware simply does not have that endpoint was never
    remembered at all and fell into the offline-start trap on every restart it
    was unlucky with.
    """
    entry = _entry()
    entry.add_to_hass(hass)
    with (
        _device_reads(),
        patch(
            f"{MANAGER}.async_get_network",
            side_effect=BleBoxConnectionError("no such endpoint"),
        ),
        patch(f"{MANAGER}.async_save_action"),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    cache = entry.data.get(CONF_DEVICE_CACHE)
    assert cache, "a device that answered everything it has was not remembered"
    assert cache["settings"]["relays"]
    # Nothing was invented for the endpoint it does not have, so the access
    # point switch is absent here exactly as it is on a live poll.
    assert cache["network"] == {}
    assert _registered(hass, "switch", "access_point") is None

    # What all of that is for: the entities come back on an offline restart.
    persisted = dict(entry.data)
    with patch(f"{MANAGER}.async_remove_owned_actions", return_value=[]):
        await hass.config_entries.async_remove(entry.entry_id)
        await hass.async_block_till_done()

    restarted = MockConfigEntry(
        domain=DOMAIN,
        title=entry.title,
        unique_id=BLEBOX_ID,
        data=persisted,
        options=dict(entry.options),
    )
    restarted.add_to_hass(hass)
    with _unreachable():
        assert await hass.config_entries.async_setup(restarted.entry_id)
        await hass.async_block_till_done()

        assert _registered(hass, "switch", "relay") is not None
        assert _registered(hass, "switch", "cloud_tunnel") is not None

        # Unloaded inside the patch: a poll would otherwise reach a real socket.
        await hass.config_entries.async_unload(restarted.entry_id)
        await hass.async_block_till_done()


async def test_a_device_that_has_never_answered_comes_back_by_itself(
    hass: HomeAssistant,
) -> None:
    """An entry set up while the device was down still recovers on its own.

    Regression: setup deliberately succeeds when the device is unreachable, so
    Home Assistant never retries it, and with nothing remembered every polled
    platform creates nothing. `DataUpdateCoordinator` arms its interval only
    while something is listening to it, so the entry stopped polling altogether:
    the device could come back and nothing would notice, no automatic callback
    would ever be healed, and only a manual reload got the entry out of it.
    """
    entry = _entry()
    entry.add_to_hass(hass)
    with _unreachable():
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert _registered(hass, "switch", "relay") is None
        # Kept across the reload below, which replaces the runtime data.
        coordinator = entry.runtime_data.coordinator

        # A poll that failed is announced just as a successful one is, and must
        # not be taken for the device answering: there is still nothing to build
        # anything from, so nothing should happen but another poll.
        async_fire_time_changed(
            hass, dt_util.utcnow() + timedelta(seconds=SCAN_INTERVAL_SECONDS + 1)
        )
        await hass.async_block_till_done()
        assert not coordinator.last_update_success
        assert _registered(hass, "switch", "relay") is None

        # The announcement a poll makes when it fails after a good one, which is
        # how entities go unavailable. It carries no snapshot either.
        coordinator.async_update_listeners()
        await hass.async_block_till_done()
        assert _registered(hass, "switch", "relay") is None

    with _device_reads(), patch(f"{MANAGER}.async_save_action"):
        async_fire_time_changed(
            hass, dt_util.utcnow() + timedelta(seconds=2 * SCAN_INTERVAL_SECONDS + 2)
        )
        await hass.async_block_till_done()

        # It polled at all, which is what an entry with no entities stopped
        # doing, and then acted on what the poll finally told it.
        assert coordinator.last_update_success
        relay = _registered(hass, "switch", "relay")
        assert relay is not None, "the device answered and nothing was created"
        assert hass.states.get(relay).state == STATE_ON
        # The pushed entities were there all along and survive the recovery.
        assert hass.states.get(_entity_id(hass, 0)) is not None

        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_enabling_events_for_an_input_reloads_the_entry_once(
    hass: HomeAssistant,
) -> None:
    """Ticking events for an unused input reloads the entry once, not twice.

    An input with nothing selected is registered disabled, and
    ``entity_registry_enabled_default`` is read only at that first registration,
    so platform setup has to clear the disable by hand for the option to mean
    anything. Doing that the obvious way makes Home Assistant schedule a reload
    of the config entry thirty seconds later, which tears down every entity and
    re-runs provisioning to arrive at exactly what the reload the user's own
    change already triggered had just finished building.
    """
    entry = _entry(**{CONF_ENABLED_EVENTS: {"0": ["short_press"], "1": []}})
    await _setup(hass, entry)
    registry = er.async_get(hass)
    spare = _entity_id(hass, 1)
    assert registry.async_get(spare).disabled_by is er.RegistryEntryDisabler.INTEGRATION

    with (
        _device_reads(),
        patch(f"{MANAGER}.async_save_action"),
        patch.object(
            hass.config_entries,
            "async_reload",
            wraps=hass.config_entries.async_reload,
        ) as reload,
    ):
        hass.config_entries.async_update_entry(
            entry,
            options={
                **entry.options,
                CONF_ENABLED_EVENTS: {"0": ["short_press"], "1": ["long_press"]},
            },
        )
        await hass.async_block_till_done()

        # The option took effect on the one reload the change itself asked for.
        assert reload.call_count == 1
        assert registry.async_get(spare).disabled_by is None
        assert hass.states.get(spare) is not None

        async_fire_time_changed(
            hass, dt_util.utcnow() + timedelta(seconds=RELOAD_AFTER_UPDATE_DELAY + 1)
        )
        await hass.async_block_till_done()
        assert reload.call_count == 1, "the entry was reloaded a second time"

        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
