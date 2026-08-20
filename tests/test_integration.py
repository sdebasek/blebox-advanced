"""End-to-end tests against a real Home Assistant instance.

These cover the acceptance criteria: pressing the wall switch reaches Home
Assistant as an event, the entity lands on the *official* BleBox device, the
events show up as device triggers, and the endpoint refuses anything it does
not recognise.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from homeassistant.components.device_automation import DeviceAutomationType
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_get_device_automations,
)

from custom_components.blebox_events.blebox_actions import (
    ACTION_HTTP_GET,
    TRIGGER_LONG_CLICK,
    TRIGGER_SHORT_CLICK,
    ActionsState,
    DeviceInfo,
)
from custom_components.blebox_events.const import (
    BLEBOX_DOMAIN,
    CONF_BASE_URL,
    CONF_BLEBOX_ID,
    CONF_CALLBACK_TOKEN,
    CONF_DEBOUNCE_MS,
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

MANAGER = "custom_components.blebox_events.blebox_actions.BleBoxActionManager"


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
    """A two-input device with six action slots."""
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


def _entry(mode: str = MODE_MANUAL, debounce: int = 0) -> MockConfigEntry:
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
        patch(f"{MANAGER}.async_save_action") as save,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return save


def _entity_id(hass: HomeAssistant, input_id: int) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(
        "event", DOMAIN, f"{BLEBOX_ID}_input_{input_id}"
    )
    assert entity_id is not None
    return entity_id


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
                "actions": {"event": "blebox_events_test_fired"},
            }
        },
    )
    await hass.async_block_till_done()

    fired: list[Any] = []
    hass.bus.async_listen("blebox_events_test_fired", lambda event: fired.append(event))

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
    from custom_components.blebox_events.blebox_actions import BleBoxConnectionError

    entry = _entry()
    entry.add_to_hass(hass)
    with (
        patch(
            f"{MANAGER}.async_get_device_info",
            side_effect=BleBoxConnectionError("down"),
        ),
        patch(
            f"{MANAGER}.async_get_actions_state",
            side_effect=BleBoxConnectionError("down"),
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert hass.states.get(_entity_id(hass, 0)) is not None


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


async def test_diagnostics_redact_the_token(hass: HomeAssistant) -> None:
    """A diagnostics dump never contains the callback token."""
    from custom_components.blebox_events.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    entry = _entry()
    await _setup(hass, entry)

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert TOKEN not in repr(diagnostics)
    assert diagnostics["inputs"]["detected"] == [0, 1]
    assert len(diagnostics["callback_mappings"]) == 3
