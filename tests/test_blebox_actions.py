"""Tests for the BleBox action API abstraction.

These cover the promises the README makes about automatic configuration being
conservative: foreign actions are never touched, a run that will not fit changes
nothing, reconfiguring updates rather than duplicates, and hardware-specific
fields survive a round-trip.

They deliberately avoid Home Assistant so they can run anywhere.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from custom_components.blebox_advanced.blebox_actions import (
    ACTION_HTTP_GET,
    TRIGGER_LONG_CLICK,
    TRIGGER_SHORT_CLICK,
    TRIGGER_UNCONFIGURED,
    ActionsState,
    BleBoxActionManager,
    BleBoxConnectionError,
    DesiredAction,
    DeviceInfo,
    InsufficientSlotsError,
    event_type_for_trigger,
    is_configured,
    is_owned,
    trigger_type_for_event,
)

# The constraint engine as captured from a live Simon 55 GO (5 inputs).
FIELD_PREFERENCES: list[dict[str, Any]] = [
    {
        "name": "triggerType",
        "values": [1, 2, 3, 4, 5, 19, 42, 43],
        "dependsOn": "input",
        "constraints": [
            {"input": None, "triggerType": [19, 42, 43]},
            *({"input": i, "triggerType": [1, 2, 3, 4, 5]} for i in range(5)),
        ],
    },
    {
        "name": "actionType",
        "dependsOn": "triggerType",
        "constraints": [
            {"triggerType": t, "actionType": [1, 2, 3, 7, 8, 9, 10, 50, 51, 52, 53]}
            for t in (1, 2, 3, 4, 5)
        ],
    },
]

HA_URL = "http://192.168.10.50:8123"
TOKEN = "0123456789abcdef0123456789abcdef"


def owned_url(input_id: int, event_type: str) -> str:
    """A callback URL as this integration would generate it."""
    return f"{HA_URL}/api/blebox_advanced/{TOKEN}/{input_id}/{event_type}"


def empty_slot(slot_id: int) -> dict[str, Any]:
    """An unconfigured slot, shaped as the device reports it."""
    return {
        "id": slot_id,
        "name": "",
        "input": 0,
        "triggerType": TRIGGER_UNCONFIGURED,
        "actionType": 0,
        "lastCall": {"timeElapsedS": -1},
        "param": "",
        # Present on hv 1.5 hardware; dropping these makes the device 400.
        "relay": 0,
        "forTime": 0,
        "ns": 0,
    }


def configured_slot(
    slot_id: int,
    input_id: int,
    trigger_type: int,
    action_type: int,
    param: str,
    name: str = "",
) -> dict[str, Any]:
    """A configured slot."""
    return {
        **empty_slot(slot_id),
        "name": name,
        "input": input_id,
        "triggerType": trigger_type,
        "actionType": action_type,
        "triggerParam": 0,
        "intervalS": 0,
        "throttleS": 0,
        "param": param,
    }


def make_state(actions: list[dict[str, Any]], total: int = 8) -> ActionsState:
    """Build an ActionsState padded out to `total` fixed slots."""
    slots = list(actions)
    slots.extend(empty_slot(i) for i in range(len(slots), total))
    return ActionsState.from_payload(
        {
            "actions": slots,
            "itemsLimit": total,
            "fieldsPreferences": FIELD_PREFERENCES,
        }
    )


class RecordingManager(BleBoxActionManager):
    """Manager that captures writes instead of performing them."""

    def __init__(self, state: ActionsState) -> None:
        super().__init__(session=None, host="192.0.2.10")  # type: ignore[arg-type]
        self._state = state
        self.writes: list[dict[str, Any]] = []

    async def async_get_actions_state(self) -> ActionsState:
        return copy.deepcopy(self._state)

    async def async_save_action(self, action: dict[str, Any]) -> None:
        self.writes.append(copy.deepcopy(action))


# --- Discovery --------------------------------------------------------------


def test_inputs_derived_from_constraint_engine() -> None:
    """Inputs come from the fieldsPreferences constraints, ignoring `None`."""
    assert make_state([]).input_ids() == [0, 1, 2, 3, 4]


def test_inputs_fall_back_to_configured_slots() -> None:
    """Firmware without a constraint engine still yields inputs in use."""
    state = ActionsState.from_payload(
        {
            "actions": [
                configured_slot(0, 2, TRIGGER_SHORT_CLICK, 1, ""),
                empty_slot(1),
            ],
            "itemsLimit": 2,
        }
    )
    assert state.input_ids() == [2]


def test_http_action_support_detection() -> None:
    """HTTP actions are allowed when the device lists action type 50."""
    assert make_state([]).supports_http_action(TRIGGER_SHORT_CLICK) is True

    without = ActionsState.from_payload(
        {
            "actions": [empty_slot(0)],
            "itemsLimit": 1,
            "fieldsPreferences": [
                {
                    "name": "actionType",
                    "dependsOn": "triggerType",
                    "constraints": [{"triggerType": 1, "actionType": [1, 2, 3]}],
                }
            ],
        }
    )
    assert without.supports_http_action(TRIGGER_SHORT_CLICK) is False


def test_device_info_requires_an_id() -> None:
    """A response without a device id is not usable as an identity."""
    info = DeviceInfo.from_payload(
        {
            "device": {
                "id": "ae0bfbf927ba",
                "deviceName": "Simon GO Switch",
                "type": "switchBox",
                "fv": "0.1502",
                "hv": "s_KS.swB.1.5.T.p55ST-0.3",
                "apiLevel": "20220114",
            }
        }
    )
    assert info.device_id == "ae0bfbf927ba"
    assert info.name == "Simon GO Switch"

    with pytest.raises(BleBoxConnectionError):
        DeviceInfo.from_payload({"device": {"type": "switchBox"}})


# --- Ownership --------------------------------------------------------------


def test_ownership_is_derived_from_the_url() -> None:
    """Ours is an HTTP action pointing at our endpoint; renaming cannot orphan it."""
    ours = configured_slot(
        0,
        0,
        TRIGGER_SHORT_CLICK,
        ACTION_HTTP_GET,
        owned_url(0, "short_press"),
        "renamed",
    )
    assert is_owned(ours)

    other_webhook = configured_slot(
        1, 0, TRIGGER_SHORT_CLICK, ACTION_HTTP_GET, f"{HA_URL}/api/webhook/abc"
    )
    assert not is_owned(other_webhook)
    assert is_configured(other_webhook)

    native_relay = configured_slot(2, 1, TRIGGER_SHORT_CLICK, 1, "")
    assert not is_owned(native_relay)
    assert not is_configured(empty_slot(3))


def test_event_and_trigger_mapping_round_trips() -> None:
    """Event types map onto trigger types, invertibly for the edges."""
    assert trigger_type_for_event("short_press") == TRIGGER_SHORT_CLICK
    assert trigger_type_for_event("long_press") == TRIGGER_LONG_CLICK
    assert trigger_type_for_event("press") == 4
    assert trigger_type_for_event("release") == 3

    assert trigger_type_for_event("press", invert_edges=True) == 3
    assert event_type_for_trigger(3, invert_edges=True) == "press"

    with pytest.raises(ValueError):
        trigger_type_for_event("triple_press")


# --- Reconciliation ---------------------------------------------------------


async def test_creates_actions_in_free_slots_only() -> None:
    """New callbacks land in empty slots and carry the right fields."""
    manager = RecordingManager(make_state([]))
    result = await manager.async_sync_http_actions(
        [
            DesiredAction(
                0,
                TRIGGER_SHORT_CLICK,
                owned_url(0, "short_press"),
                "HA IN1 short_press",
            ),
            DesiredAction(
                0, TRIGGER_LONG_CLICK, owned_url(0, "long_press"), "HA IN1 long_press"
            ),
        ]
    )

    assert len(manager.writes) == 2
    assert result.created == [0, 1]
    assert not result.updated and not result.cleared

    first = manager.writes[0]
    assert first["input"] == 0
    assert first["triggerType"] == TRIGGER_SHORT_CLICK
    assert first["actionType"] == ACTION_HTTP_GET
    assert first["param"] == owned_url(0, "short_press")
    # Read-only telemetry must not be echoed back.
    assert "lastCall" not in first
    # Hardware-specific fields must survive the round-trip.
    assert first["relay"] == 0 and first["forTime"] == 0 and first["ns"] == 0
    # Empty slots omit these; a configured action needs them.
    assert first["triggerParam"] == 0
    assert first["intervalS"] == 0
    assert first["throttleS"] == 0


async def test_never_touches_foreign_actions() -> None:
    """User-configured actions are left exactly as they were."""
    user_relay = configured_slot(0, 0, TRIGGER_SHORT_CLICK, 1, "", "my relay toggle")
    user_webhook = configured_slot(
        1, 1, TRIGGER_LONG_CLICK, ACTION_HTTP_GET, f"{HA_URL}/api/webhook/private"
    )
    manager = RecordingManager(make_state([user_relay, user_webhook]))

    result = await manager.async_sync_http_actions(
        [
            DesiredAction(
                2,
                TRIGGER_SHORT_CLICK,
                owned_url(2, "short_press"),
                "HA IN3 short_press",
            )
        ]
    )

    written_slots = {write["id"] for write in manager.writes}
    assert written_slots == {2}
    assert 0 not in written_slots and 1 not in written_slots
    assert result.slots_foreign == 2


async def test_reconfigure_updates_instead_of_duplicating() -> None:
    """An existing callback of ours is rewritten in place, not added again."""
    existing = configured_slot(
        0,
        0,
        TRIGGER_SHORT_CLICK,
        ACTION_HTTP_GET,
        "http://old-host:8123/api/blebox_advanced/oldtoken/0/short_press",
        "HA IN1 short_press",
    )
    manager = RecordingManager(make_state([existing]))

    result = await manager.async_sync_http_actions(
        [
            DesiredAction(
                0,
                TRIGGER_SHORT_CLICK,
                owned_url(0, "short_press"),
                "HA IN1 short_press",
            )
        ]
    )

    assert result.updated == [0]
    assert not result.created
    assert len(manager.writes) == 1
    assert manager.writes[0]["id"] == 0
    assert manager.writes[0]["param"] == owned_url(0, "short_press")


async def test_unchanged_actions_are_not_rewritten() -> None:
    """A device already in the desired state is not written to at all."""
    url = owned_url(0, "short_press")
    existing = configured_slot(
        0, 0, TRIGGER_SHORT_CLICK, ACTION_HTTP_GET, url, "HA IN1 short_press"
    )
    manager = RecordingManager(make_state([existing]))

    result = await manager.async_sync_http_actions(
        [DesiredAction(0, TRIGGER_SHORT_CLICK, url, "HA IN1 short_press")]
    )

    assert result.unchanged == [0]
    assert manager.writes == []
    assert result.changed is False


async def test_deselected_events_clear_only_our_slots() -> None:
    """Dropping an event clears our slot and leaves everything else alone."""
    ours = configured_slot(
        0,
        0,
        TRIGGER_LONG_CLICK,
        ACTION_HTTP_GET,
        owned_url(0, "long_press"),
        "HA IN1 long_press",
    )
    theirs = configured_slot(1, 1, TRIGGER_SHORT_CLICK, 1, "", "user action")
    manager = RecordingManager(make_state([ours, theirs]))

    result = await manager.async_sync_http_actions([])

    assert result.cleared == [0]
    assert len(manager.writes) == 1
    cleared = manager.writes[0]
    assert cleared["id"] == 0
    assert cleared["triggerType"] == TRIGGER_UNCONFIGURED
    assert cleared["actionType"] == 0
    assert cleared["param"] == ""
    assert cleared["name"] == ""


async def test_insufficient_slots_writes_nothing() -> None:
    """Capacity is checked up front, so the device is never half-provisioned."""
    filled = [
        configured_slot(i, 0, TRIGGER_SHORT_CLICK, 1, "", f"user {i}") for i in range(3)
    ]
    manager = RecordingManager(make_state(filled, total=4))

    desired = [
        DesiredAction(0, TRIGGER_SHORT_CLICK, owned_url(0, "short_press"), "a"),
        DesiredAction(1, TRIGGER_SHORT_CLICK, owned_url(1, "short_press"), "b"),
        DesiredAction(2, TRIGGER_SHORT_CLICK, owned_url(2, "short_press"), "c"),
    ]
    with pytest.raises(InsufficientSlotsError) as err:
        await manager.async_sync_http_actions(desired)

    assert manager.writes == []
    assert err.value.needed == 3
    assert err.value.available == 1
    assert err.value.total == 4


async def test_duplicate_owned_slots_are_collapsed() -> None:
    """A leftover duplicate of ours is cleared rather than left firing twice."""
    url = owned_url(0, "short_press")
    first = configured_slot(
        0, 0, TRIGGER_SHORT_CLICK, ACTION_HTTP_GET, url, "HA IN1 short_press"
    )
    duplicate = configured_slot(
        1, 0, TRIGGER_SHORT_CLICK, ACTION_HTTP_GET, url, "HA IN1 short_press"
    )
    manager = RecordingManager(make_state([first, duplicate]))

    result = await manager.async_sync_http_actions(
        [DesiredAction(0, TRIGGER_SHORT_CLICK, url, "HA IN1 short_press")]
    )

    assert result.unchanged == [0]
    assert result.cleared == [1]


async def test_remove_owned_actions_leaves_others_intact() -> None:
    """Deleting the integration clears only what it created."""
    ours = configured_slot(
        0,
        0,
        TRIGGER_SHORT_CLICK,
        ACTION_HTTP_GET,
        owned_url(0, "short_press"),
        "HA IN1",
    )
    theirs = configured_slot(1, 1, TRIGGER_LONG_CLICK, 1, "", "user action")
    manager = RecordingManager(make_state([ours, theirs]))

    cleared = await manager.async_remove_owned_actions()

    assert cleared == [0]
    assert [write["id"] for write in manager.writes] == [0]
    assert manager.writes[0]["triggerType"] == TRIGGER_UNCONFIGURED


async def test_changing_a_periodic_interval_reaches_the_device() -> None:
    """A periodic action whose only change is its interval must be rewritten.

    Regression: URL and name stay identical when only the interval changes, so
    the slot looked unchanged and the new interval never left Home Assistant.
    """
    url = f"{HA_URL}/api/blebox_advanced/{TOKEN}/state?s={{s_state.0}}"
    existing = {
        **empty_slot(0),
        "name": "HA state report",
        "triggerType": 19,
        "actionType": ACTION_HTTP_GET,
        "triggerParam": 5,
        "param": url,
    }
    del existing["input"]  # device-level actions carry no input
    manager = RecordingManager(make_state([existing]))

    result = await manager.async_sync_http_actions(
        [DesiredAction(None, 19, url, "HA state report", trigger_param=30)]
    )

    assert result.updated == [0]
    assert manager.writes[0]["triggerParam"] == 30

    # Same interval, nothing to do.
    manager = RecordingManager(make_state([existing]))
    result = await manager.async_sync_http_actions(
        [DesiredAction(None, 19, url, "HA state report", trigger_param=5)]
    )
    assert result.unchanged == [0]
    assert manager.writes == []


async def test_ap_toggle_round_trips_and_leaves_the_station_alone() -> None:
    """Turning the access point off must not disturb the WiFi it is joined to.

    The patch carries only the access point fields, mirroring what the device's
    own UI posts. Including the station configuration could strand the device on
    a network it can no longer reach.
    """
    calls: list[tuple[str, str, dict | None]] = []

    class Fake(BleBoxActionManager):
        def __init__(self) -> None:
            super().__init__(session=None, host="192.0.2.10")  # type: ignore[arg-type]

        async def _get(self, path: str):
            calls.append(("GET", path, None))
            return {
                "ssid": "home-wifi",
                "mac": "ae:0b:fb:f9:27:ba",
                "apEnable": True,
                "apSSID": "SimonGOSwitch-ae0bfbf927ba",
                "apPasswd": "hunter2",
            }

        async def _post(self, path: str, payload: dict):
            calls.append(("POST", path, payload))
            return {}

    await Fake().async_set_ap_enabled(False)

    method, path, payload = calls[-1]
    assert (method, path) == ("POST", "/api/device/set")
    assert payload == {
        "network": {
            "apEnable": False,
            "apSSID": "SimonGOSwitch-ae0bfbf927ba",
            "apPasswd": "hunter2",
        }
    }
    assert "ssid" not in payload["network"]
