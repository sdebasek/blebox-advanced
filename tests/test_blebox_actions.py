"""Tests for the BleBox action API abstraction.

These cover the promises the README makes about automatic configuration being
conservative: foreign actions are never touched, a run that will not fit changes
nothing, reconfiguring updates rather than duplicates, and hardware-specific
fields survive a round-trip.

They deliberately avoid Home Assistant so they can run anywhere.
"""

from __future__ import annotations

import asyncio
import copy
from typing import Any

import pytest

from custom_components.blebox_advanced.blebox_actions import (
    ACTION_HTTP_GET,
    ACTION_RELAY_TOGGLE,
    TRIGGER_LONG_CLICK,
    TRIGGER_SHORT_CLICK,
    TRIGGER_UNCONFIGURED,
    ActionsState,
    ActionsUnsupportedError,
    BleBoxActionApiError,
    BleBoxActionManager,
    BleBoxConnectionError,
    BleBoxError,
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
    """Build a callback URL exactly as this integration would generate it."""
    return f"{HA_URL}/api/blebox_advanced/{TOKEN}/{input_id}/{event_type}"


def empty_slot(slot_id: int) -> dict[str, Any]:
    """Build an unconfigured slot, shaped as the device reports it."""
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
    """Build a slot holding an action, filled the way the device fills one."""
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


def owned_slot(
    slot_id: int, input_id: int, trigger_type: int, event_type: str
) -> dict[str, Any]:
    """Build a slot holding one of this integration's own callbacks."""
    return configured_slot(
        slot_id,
        input_id,
        trigger_type,
        ACTION_HTTP_GET,
        owned_url(input_id, event_type),
        f"HA IN{input_id + 1} {event_type}",
    )


def half_cleared_owned_slot(slot_id: int, input_id: int) -> dict[str, Any]:
    """Build a slot of ours that the device reports as having no trigger.

    Trigger type and action type are separate fields, so firmware that honours
    the ``triggerType: 0`` half of a clear and keeps the ``actionType``/``param``
    half leaves this behind: a slot the device considers empty which still
    carries our callback URL. It is the one shape that used to satisfy both
    "free" and "ours".
    """
    slot = owned_slot(slot_id, input_id, TRIGGER_SHORT_CLICK, "short_press")
    slot["triggerType"] = TRIGGER_UNCONFIGURED
    return slot


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
        """Plan against `state`, which no write is ever allowed to change."""
        super().__init__(session=None, host="192.0.2.10")  # type: ignore[arg-type]
        self._state = state
        self.writes: list[dict[str, Any]] = []

    async def async_get_actions_state(self) -> ActionsState:
        """Return the fixed slot array, copied so a caller cannot mutate it."""
        return copy.deepcopy(self._state)

    async def async_save_action(self, action: dict[str, Any]) -> None:
        """Record a write instead of sending it, leaving the slots untouched."""
        self.writes.append(copy.deepcopy(action))


class FakeDevice(BleBoxActionManager):
    """Manager wired to an in-memory device instead of to HTTP.

    Unlike :class:`RecordingManager` this stubs the transport rather than the
    methods above it, so the manager's own locking still runs; it applies every
    write to the slot array, so a later read sees earlier writes; and it gives
    up the event loop on each request the way a real round trip does. All three
    are needed before a read-plan-write race can happen at all.
    """

    def __init__(self, state: ActionsState) -> None:
        """Start from the slots of `state` and let writes change them."""
        super().__init__(session=None, host="192.0.2.10")  # type: ignore[arg-type]
        self.slots = copy.deepcopy(state.actions)
        self.items_limit = state.items_limit
        self.writes: list[dict[str, Any]] = []
        self.overwritten: list[dict[str, Any]] = []

    def slot(self, slot_id: int) -> dict[str, Any]:
        """Return the slot the device currently holds under `slot_id`."""
        return next(slot for slot in self.slots if slot["id"] == slot_id)

    async def _get(self, path: str) -> Any:
        assert path == "/api/actions/state"
        await asyncio.sleep(0)
        return {
            "actions": copy.deepcopy(self.slots),
            "itemsLimit": self.items_limit,
            "fieldsPreferences": FIELD_PREFERENCES,
        }

    async def _post(self, path: str, payload: dict[str, Any]) -> Any:
        assert path == "/api/actions/set"
        action = copy.deepcopy(payload["action"])
        await asyncio.sleep(0)
        target = self.slot(action["id"])
        # Kept so a test can ask what each write landed on top of.
        self.overwritten.append(copy.deepcopy(target))
        self.writes.append(action)
        target.update(action)
        return {}


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


# --- Serialisation ----------------------------------------------------------


async def test_two_native_writes_do_not_land_in_the_same_slot() -> None:
    """Rebinding two buttons at once must not plan both into the same slot.

    Regression, reproduced on hardware: read-plan-write was not serialised, so
    both calls read the same free-slot list, both chose slot 0, and the second
    write destroyed the first. Neither change appeared on the device.
    """
    device = FakeDevice(make_state([]))

    await asyncio.gather(
        device.async_set_native_action(0, TRIGGER_SHORT_CLICK, ACTION_RELAY_TOGGLE),
        device.async_set_native_action(1, TRIGGER_SHORT_CLICK, ACTION_RELAY_TOGGLE),
    )

    assert len(device.writes) == 2
    assert {write["id"] for write in device.writes} == {0, 1}
    assert {device.slot(0)["input"], device.slot(1)["input"]} == {0, 1}
    for slot_id in (0, 1):
        assert device.slot(slot_id)["actionType"] == ACTION_RELAY_TOGGLE

    # Two distinct locks on purpose: `async_save_action` takes `_write_lock`
    # from inside a sequence already holding `_action_lock`, and asyncio locks
    # are not reentrant, so one shared lock would deadlock on the first write.
    assert device._action_lock is not device._write_lock


async def test_sync_and_native_writes_do_not_interleave() -> None:
    """A provisioning run and a button rebind must not share a slot.

    Regression: both sequences read the slot array before either had written,
    so a callback and a local relay action were planned into the same slot and
    whichever was written first was immediately overwritten.
    """
    device = FakeDevice(make_state([]))

    await asyncio.gather(
        device.async_sync_http_actions(
            [
                DesiredAction(
                    0,
                    TRIGGER_SHORT_CLICK,
                    owned_url(0, "short_press"),
                    "HA IN1 short_press",
                ),
                DesiredAction(
                    1,
                    TRIGGER_SHORT_CLICK,
                    owned_url(1, "short_press"),
                    "HA IN2 short_press",
                ),
            ]
        ),
        device.async_set_native_action(2, TRIGGER_SHORT_CLICK, ACTION_RELAY_TOGGLE),
    )

    written = [write["id"] for write in device.writes]
    assert len(written) == 3
    # Every write got a slot of its own, so nothing was overwritten.
    assert len(set(written)) == 3
    params = {slot["param"] for slot in device.slots}
    assert owned_url(0, "short_press") in params
    assert owned_url(1, "short_press") in params
    assert any(
        slot["actionType"] == ACTION_RELAY_TOGGLE and slot["input"] == 2
        for slot in device.slots
    )


async def test_a_stale_plan_can_never_write_into_a_foreign_slot() -> None:
    """A callback must never be written over a slot something else has taken.

    Regression: the free-slot list was captured outside any lock, so a slot
    that a local relay action (which this integration does not own) had taken
    in the meantime was still treated as empty and overwritten. That is the one
    thing automatic configuration must never do.
    """
    device = FakeDevice(make_state([]))

    await asyncio.gather(
        device.async_set_native_action(0, TRIGGER_SHORT_CLICK, ACTION_RELAY_TOGGLE),
        device.async_sync_http_actions(
            [
                DesiredAction(
                    1,
                    TRIGGER_SHORT_CLICK,
                    owned_url(1, "short_press"),
                    "HA IN2 short_press",
                )
            ]
        ),
    )

    for before, write in zip(device.overwritten, device.writes, strict=True):
        if not is_owned(write):
            continue
        assert not is_configured(before) or is_owned(before), (
            f"callback written over slot {before['id']}, which held action type "
            f"{before['actionType']} that we do not own"
        )


# --- Reclaiming our own slots -----------------------------------------------


async def test_reprovisioning_reclaims_our_own_stale_slots() -> None:
    """A device full of our own unwanted callbacks must still reprovision.

    Regression: capacity counted empty slots only, so a device whose every slot
    held a callback of ours that the same run was about to clear refused the
    run outright, leaving no way forward short of clearing slots by hand.
    """
    stale = [owned_slot(i, i, TRIGGER_SHORT_CLICK, "short_press") for i in range(4)]
    manager = RecordingManager(make_state(stale, total=4))

    result = await manager.async_sync_http_actions(
        [
            DesiredAction(
                i,
                TRIGGER_LONG_CLICK,
                owned_url(i, "long_press"),
                f"HA IN{i + 1} long_press",
            )
            for i in range(4)
        ]
    )

    assert result.created == [0, 1, 2, 3]
    assert result.cleared == []
    assert len(manager.writes) == 4
    assert [write["triggerType"] for write in manager.writes] == [
        TRIGGER_LONG_CLICK
    ] * 4
    assert result.slots_free == 0


async def test_insufficient_slots_counts_what_can_be_reclaimed() -> None:
    """A run that still does not fit refuses, reporting the true capacity.

    Regression: the shortfall counted empty slots only, understating what the
    device could offer, and it must keep the "fits entirely or changes nothing"
    promise now that reclaiming exists.
    """
    user_relay = configured_slot(0, 0, TRIGGER_SHORT_CLICK, 1, "", "user relay")
    user_webhook = configured_slot(
        1, 1, TRIGGER_SHORT_CLICK, ACTION_HTTP_GET, f"{HA_URL}/api/webhook/private"
    )
    ours = [
        owned_slot(2, 2, TRIGGER_SHORT_CLICK, "short_press"),
        owned_slot(3, 3, TRIGGER_SHORT_CLICK, "short_press"),
    ]
    manager = RecordingManager(make_state([user_relay, user_webhook, *ours], total=4))

    desired = [
        DesiredAction(
            i,
            TRIGGER_LONG_CLICK,
            owned_url(i, "long_press"),
            f"HA IN{i + 1} long_press",
        )
        for i in range(3)
    ]
    with pytest.raises(InsufficientSlotsError) as err:
        await manager.async_sync_http_actions(desired)

    assert manager.writes == []
    assert err.value.needed == 3
    # The two slots of ours are reclaimable; the user's two never are.
    assert err.value.available == 2
    assert err.value.total == 4


async def test_empty_slots_are_spent_before_recycling_ours() -> None:
    """Recycling is a last resort, and a stale slot left over is still cleared.

    Regression risk in the reclaim fix: a run must not churn slots it has no
    need to touch, and a stale callback of ours that goes unused must not be
    left firing at Home Assistant.
    """
    stale = [
        owned_slot(0, 0, TRIGGER_SHORT_CLICK, "short_press"),
        owned_slot(1, 1, TRIGGER_SHORT_CLICK, "short_press"),
    ]
    manager = RecordingManager(make_state(stale, total=3))  # slot 2 is empty

    result = await manager.async_sync_http_actions(
        [
            DesiredAction(
                3, TRIGGER_LONG_CLICK, owned_url(3, "long_press"), "HA IN4 long_press"
            ),
            DesiredAction(
                4, TRIGGER_LONG_CLICK, owned_url(4, "long_press"), "HA IN5 long_press"
            ),
        ]
    )

    # The empty slot goes first, then the lowest-numbered stale slot of ours.
    assert result.created == [2, 0]
    assert result.cleared == [1]
    assert result.slots_free == 1
    assert [write["id"] for write in manager.writes] == [2, 0, 1]


async def test_a_recycled_slot_is_repurposed_in_one_write() -> None:
    """A reclaimed slot takes its new definition directly, never a blanking first.

    Regression risk in the reclaim fix: clearing a stale slot and then refilling
    it costs a second round trip over an undocumented API and leaves the button
    doing nothing in between.

    The pacing fields are pinned too. A slot arriving from the free list starts
    at zero, but a recycled one carries whatever the callback that used to live
    there had, so a throttle set on the old action in the wBox app would
    silently rate-limit an unrelated button.
    """
    stale = owned_slot(0, 0, TRIGGER_SHORT_CLICK, "short_press")
    stale["throttleS"] = 30
    stale["intervalS"] = 15
    theirs = configured_slot(1, 1, TRIGGER_LONG_CLICK, 1, "", "user action")
    manager = RecordingManager(make_state([stale, theirs], total=2))

    url = owned_url(0, "long_press")
    result = await manager.async_sync_http_actions(
        [DesiredAction(0, TRIGGER_LONG_CLICK, url, "HA IN1 long_press")]
    )

    assert len(manager.writes) == 1
    write = manager.writes[0]
    assert write["id"] == 0
    assert write["param"] == url
    assert write["triggerType"] == TRIGGER_LONG_CLICK
    assert write["actionType"] == ACTION_HTTP_GET
    assert write["throttleS"] == 0
    assert write["intervalS"] == 0
    assert result.created == [0]
    assert result.cleared == []
    assert result.slots_free == 0


# --- Slots that read as both free and ours ----------------------------------


async def test_a_half_cleared_slot_is_spent_once_not_filled_and_wiped() -> None:
    """A slot that reads as both empty and ours is used once and left alone.

    Regression: `free_slots()` selected on trigger type while `owned_actions()`
    selected on action type plus our URL marker, and neither excluded the other.
    A half-cleared slot of ours therefore sat in both lists, so the run took it
    from the free list, wrote the callback into it, and then cleared the very
    same slot on the way out because it was still queued as stale. The device
    was left with an empty slot while `SyncResult` reported a creation, and the
    user's button silently never fired.
    """
    manager = RecordingManager(make_state([half_cleared_owned_slot(0, 0)], total=1))

    url = owned_url(0, "long_press")
    result = await manager.async_sync_http_actions(
        [DesiredAction(0, TRIGGER_LONG_CLICK, url, "HA IN1 long_press")]
    )

    assert [write["id"] for write in manager.writes] == [0]
    assert manager.writes[0]["param"] == url
    assert manager.writes[0]["triggerType"] == TRIGGER_LONG_CLICK
    assert result.created == [0]
    assert result.cleared == []


async def test_a_half_cleared_slot_is_counted_once_towards_capacity() -> None:
    """One physical slot counts once, and a run that needs two still refuses.

    Regression: counted as free *and* as reclaimable, a single half-cleared slot
    of ours made a two-callback run look like it fitted on a device with one
    usable slot. Both callbacks were then planned into that slot and the second
    write destroyed the first, which is exactly the "either fits entirely or
    changes nothing" promise the capacity check exists to keep.
    """
    theirs = configured_slot(1, 1, TRIGGER_SHORT_CLICK, 1, "", "user action")
    manager = RecordingManager(
        make_state([half_cleared_owned_slot(0, 0), theirs], total=2)
    )

    desired = [
        DesiredAction(
            0, TRIGGER_LONG_CLICK, owned_url(0, "long_press"), "HA IN1 long_press"
        ),
        DesiredAction(
            1, TRIGGER_LONG_CLICK, owned_url(1, "long_press"), "HA IN2 long_press"
        ),
    ]
    with pytest.raises(InsufficientSlotsError) as err:
        await manager.async_sync_http_actions(desired)

    assert manager.writes == []
    assert err.value.needed == 2
    # The half-cleared slot is usable, once. The user's slot never is.
    assert err.value.available == 1
    assert err.value.total == 2


async def test_a_plan_that_double_books_a_slot_writes_nothing() -> None:
    """Two writes planned into one slot abort the run before the first request.

    The slot pools are disjoint by construction, so this drives the check with a
    state whose pools deliberately overlap the way they used to. What it pins is
    the failure mode: a run that would write a slot twice must leave the device
    exactly as it was rather than create a callback and wipe it moments later.
    """

    class OverlappingState(ActionsState):
        """Slot pools that overlap, as they did before they were made disjoint."""

        def free_slots(self) -> list[dict[str, Any]]:
            """Offer every slot as free, including the ones we already own."""
            return list(self.actions)

    base = make_state([owned_slot(0, 0, TRIGGER_SHORT_CLICK, "short_press")], total=1)
    manager = RecordingManager(
        OverlappingState(base.actions, base.items_limit, base.field_preferences)
    )

    with pytest.raises(BleBoxActionApiError) as err:
        await manager.async_sync_http_actions(
            [
                DesiredAction(
                    0,
                    TRIGGER_LONG_CLICK,
                    owned_url(0, "long_press"),
                    "HA IN1 long_press",
                )
            ]
        )

    assert manager.writes == []
    assert "twice" in str(err.value)


async def test_removal_erases_our_url_from_a_half_cleared_slot() -> None:
    """Deleting the integration takes our URL out of a dormant slot as well.

    A half-cleared slot no longer counts as one of ours for provisioning, since
    a slot with no trigger never fires and is simply free. Removal is the one
    place that still has to visit it: leaving it be would keep the callback
    token readable in the wBox app after the entry it belonged to was gone.
    """
    theirs = configured_slot(1, 1, TRIGGER_LONG_CLICK, 1, "", "user action")
    manager = RecordingManager(
        make_state([half_cleared_owned_slot(0, 0), theirs], total=2)
    )

    cleared = await manager.async_remove_owned_actions()

    assert cleared == [0]
    assert [write["id"] for write in manager.writes] == [0]
    assert manager.writes[0]["param"] == ""


# --- Malformed device payloads ----------------------------------------------


async def test_an_action_without_an_id_fails_as_a_blebox_error() -> None:
    """A slot the device sent without an id must not escape as a KeyError.

    Regression: `int(action["id"])` raised a bare KeyError, which sails past
    every `except BleBoxError` handler in the integration and surfaces as an
    unhandled crash instead of a provisioning failure.
    """
    url = owned_url(0, "short_press")
    broken = configured_slot(
        0, 0, TRIGGER_SHORT_CLICK, ACTION_HTTP_GET, url, "HA IN1 short_press"
    )
    del broken["id"]

    manager = RecordingManager(make_state([broken]))
    with pytest.raises(BleBoxActionApiError) as err:
        await manager.async_sync_http_actions(
            [DesiredAction(0, TRIGGER_SHORT_CLICK, url, "HA IN1 short_press")]
        )
    assert isinstance(err.value, BleBoxError)
    assert manager.writes == []

    manager = RecordingManager(make_state([broken]))
    with pytest.raises(BleBoxActionApiError):
        await manager.async_remove_owned_actions()
    # Ids are resolved before the first write, so nothing was half-cleared.
    assert manager.writes == []


def test_a_non_numeric_items_limit_fails_as_a_blebox_error() -> None:
    """Junk in `itemsLimit` must not escape as a bare ValueError.

    Regression: `int(payload["itemsLimit"])` raised ValueError straight out of
    parsing, past every `except BleBoxError` handler in the integration.
    """
    with pytest.raises(ActionsUnsupportedError) as err:
        ActionsState.from_payload({"actions": [empty_slot(0)], "itemsLimit": "lots"})
    assert isinstance(err.value, BleBoxError)

    # An absent limit is not junk: the slot array is fixed-length, so its own
    # length remains the honest answer.
    assert ActionsState.from_payload({"actions": [empty_slot(0)]}).items_limit == 1


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
