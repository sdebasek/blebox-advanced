"""Abstraction over the BleBox device HTTP API.

This is the **only** module in the integration that talks to a BleBox device.
Everything else depends on the small, stable surface defined here, so that a
firmware change (or BleBox altering their internal API) is contained to this
file and can be disabled entirely without breaking event reception.

Every endpoint this module reaches, documented or not, is listed with its
status and how its shape was established in ``docs/device-api.md``. That table
is the register; keeping a second copy of it here would only give a maintainer
two lists to disagree with each other. Some of what is called is documented
(https://technical.blebox.eu/) and much of it is not: BleBox states that "only
main functionalities are open for public", and the action CRUD surface is part
of no published OpenAPI spec, so those shapes come from live ``switchBox``
hardware and the device's own built-in wBox UI bundle.

The action surface is the reason the module exists, and two properties of
``POST /api/actions/set`` matter a great deal and are enforced here:

* it takes a single action per request, not the whole array; and
* the action object must be **round-tripped** - the device's own object sent
  back with only the edited fields changed. Device-specific fields such as
  ``relay``/``forTime``/``ns`` exist on some hardware revisions and dropping
  them makes the device reject the save with HTTP 400.

Deliberately no Home Assistant imports: this module is plain aiohttp so it can
be unit-tested (and manually poked) without a running Home Assistant.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

# --- Trigger types ----------------------------------------------------------
# 1-5 are documented for buttonBox (`/t/{inputId}/{triggerType}`); 0 is the
# empty-slot marker, and 19/42/43 are device-level triggers we never write.
#
# This is a decoding table for slots read back off a device, not a menu of what
# gets written: the integration only ever writes 1-4. A value with no writer is
# still worth naming, because the alternative is a reviewer meeting a bare 5 or
# 19 in a diagnostics dump with nothing to look it up against.

TRIGGER_UNCONFIGURED = 0
TRIGGER_SHORT_CLICK = 1
TRIGGER_LONG_CLICK = 2
TRIGGER_FALLING_EDGE = 3
TRIGGER_RISING_EDGE = 4
TRIGGER_ANY_EDGE = 5
TRIGGER_PERIODIC = 19
"""Device-level trigger that fires every ``triggerParam`` seconds.

Established by experiment, not documentation: a probe action written with this
trigger fired immediately and then on a fixed cycle matching the value the
device stored in ``triggerParam``. It is a timer, not a state-change trigger -
the hardware offers no way to fire on the relay changing.

Never written by this integration, and defined here for the same reason
:data:`TRIGGER_ANY_EDGE` is: the values are how a slot read back off a device
is decoded. A reviewer looking at a slot reporting trigger 19 needs to know it
is somebody's timer, not an input binding gone wrong. See
``docs/device-api.md`` for the full table.
"""

UNPACED_TRIGGER_PARAM = 0
"""``triggerParam`` for a trigger that carries no parameter.

Every callback this integration writes is edge- or click-triggered, and those
take no parameter. Written explicitly rather than left alone, because
``build_action_payload`` only *defaults* the numeric fields: a recycled slot
arrives carrying whatever its previous occupant had.
"""

# --- Action types -----------------------------------------------------------

ACTION_UNCONFIGURED = 0
ACTION_RELAY_ON = 1
ACTION_RELAY_OFF = 2
ACTION_RELAY_TOGGLE = 3
ACTION_HTTP_GET = 50

NATIVE_RELAY_ACTIONS = frozenset(
    {ACTION_UNCONFIGURED, ACTION_RELAY_ON, ACTION_RELAY_OFF, ACTION_RELAY_TOGGLE}
)
"""Action types this integration understands well enough to rewrite.

Anything else - an HTTP action, or one of the types the device offers but that
are not identified (7-10, 51-53) - is left strictly alone.
"""

# Event type <-> trigger type mapping.
#
# "press"/"release" map onto the electrical edges. Which edge corresponds to a
# finger touching the switch depends on how the input is wired, so the mapping
# is invertible via the `invert_edges` option.
_EVENT_TRIGGERS: dict[str, int] = {
    "short_press": TRIGGER_SHORT_CLICK,
    "long_press": TRIGGER_LONG_CLICK,
    "press": TRIGGER_RISING_EDGE,
    "release": TRIGGER_FALLING_EDGE,
}
_EVENT_TRIGGERS_INVERTED: dict[str, int] = {
    **_EVENT_TRIGGERS,
    "press": TRIGGER_FALLING_EDGE,
    "release": TRIGGER_RISING_EDGE,
}

# Fields the device manages itself and that must never be echoed back on save.
_READ_ONLY_FIELDS = frozenset({"lastCall"})

# Standard numeric fields that empty slots omit but a configured slot needs.
_NUMERIC_DEFAULTS: dict[str, int] = {
    "triggerParam": 0,
    "intervalS": 0,
    "throttleS": 0,
}


def trigger_type_for_event(event_type: str, *, invert_edges: bool = False) -> int:
    """Return the BleBox trigger type backing a Home Assistant event type."""
    table = _EVENT_TRIGGERS_INVERTED if invert_edges else _EVENT_TRIGGERS
    try:
        return table[event_type]
    except KeyError:
        raise ValueError(f"Unknown event type: {event_type}") from None


# --- Errors -----------------------------------------------------------------


class BleBoxError(Exception):
    """Base error for BleBox device communication."""


class BleBoxConnectionError(BleBoxError):
    """The device could not be reached or returned an unusable response."""


class BleBoxActionApiError(BleBoxError):
    """The undocumented action API misbehaved."""


class ActionsUnsupportedError(BleBoxActionApiError):
    """This device or firmware does not expose the action configuration API."""


class InsufficientSlotsError(BleBoxActionApiError):
    """Not enough free action slots to provision every requested callback."""

    def __init__(self, needed: int, available: int, total: int) -> None:
        """Record how many slots were needed against how many were free."""
        self.needed = needed
        self.available = available
        self.total = total
        super().__init__(
            f"Need {needed} free action slot(s), but only "
            f"{available} of {total} are free"
        )


def _device_int(
    value: Any,
    what: str,
    *,
    error: type[BleBoxActionApiError] = BleBoxActionApiError,
) -> int:
    """Coerce a number the device sent, refusing junk as a :class:`BleBoxError`.

    The device is the untrusted side of this conversation: this is a
    reverse-engineered API, and firmware has been seen to omit fields its own
    UI always sends. A bare ``KeyError`` or ``ValueError`` from parsing one
    would sail straight past every ``except BleBoxError`` handler in the
    integration and surface as an unhandled crash, so a value we cannot parse
    fails loudly, but as our own error type.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        raise error(f"Device reported an unusable {what}: {value!r}") from None


def _slot_id(action: dict[str, Any]) -> int:
    """Return an action slot's numeric id, which the device must always report."""
    return _device_int(action.get("id"), "action slot id")


# --- Data model -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    """Identity of a BleBox device, from the documented ``/api/device/state``."""

    device_id: str
    name: str
    device_type: str
    product: str
    firmware_version: str
    hardware_version: str
    api_level: str
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> DeviceInfo:
        """Build from a ``/api/device/state`` (or bare ``/info``) payload."""
        device = payload.get("device", payload)
        device_id = str(device.get("id") or "").strip()
        if not device_id:
            raise BleBoxConnectionError("Device response contained no device id")
        device_type = str(device.get("type") or "")
        return cls(
            device_id=device_id,
            name=str(device.get("deviceName") or device.get("name") or device_type),
            device_type=device_type,
            # `product` is the marketing model name, where the device reports one.
            product=str(device.get("product") or device_type),
            firmware_version=str(device.get("fv") or ""),
            hardware_version=str(device.get("hv") or ""),
            api_level=str(device.get("apiLevel") or ""),
            raw=device,
        )


@dataclass(frozen=True, slots=True)
class DesiredAction:
    """One callback the integration wants to exist on the device.

    Always bound to a physical input: every callback this integration writes
    comes from a button, so there is no device-level variant to allow for.
    """

    input_id: int
    trigger_type: int
    url: str
    name: str


@dataclass(slots=True)
class SyncResult:
    """Outcome of reconciling our callbacks with the device's action slots.

    ``created`` counts callbacks that did not exist before the run, whether
    they landed in an empty slot or recycled one of ours that was no longer
    wanted; ``cleared`` counts slots of ours that the run left empty.
    """

    created: list[int] = field(default_factory=list)
    updated: list[int] = field(default_factory=list)
    unchanged: list[int] = field(default_factory=list)
    cleared: list[int] = field(default_factory=list)
    slots_total: int = 0
    slots_free: int = 0
    slots_foreign: int = 0

    @property
    def changed(self) -> bool:
        """Whether anything was written to the device."""
        return bool(self.created or self.updated or self.cleared)


@dataclass(slots=True)
class ActionsState:
    """Parsed ``GET /api/actions/state`` response."""

    actions: list[dict[str, Any]]
    items_limit: int
    field_preferences: list[dict[str, Any]]

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ActionsState:
        """Build from the raw device payload, tolerating missing extras."""
        actions = payload.get("actions")
        if not isinstance(actions, list):
            raise ActionsUnsupportedError("Device response contained no action list")
        prefs = payload.get("fieldsPreferences")
        raw_limit = payload.get("itemsLimit")
        return cls(
            actions=[a for a in actions if isinstance(a, dict)],
            # A falsy limit means the device did not say; the slot array is
            # fixed-length, so its own length is then the honest answer. A
            # limit we cannot read at all is a device we cannot drive safely.
            items_limit=(
                _device_int(raw_limit, "item limit", error=ActionsUnsupportedError)
                if raw_limit
                else len(actions)
            ),
            field_preferences=[p for p in prefs if isinstance(p, dict)]
            if isinstance(prefs, list)
            else [],
        )

    def _preference(self, name: str) -> dict[str, Any] | None:
        return next((p for p in self.field_preferences if p.get("name") == name), None)

    def input_ids(self) -> list[int]:
        """Physical input indices exposed by the device.

        Derived from the non-null ``input`` values in the ``triggerType``
        constraints. ``switchBox`` hardware does not report an ``inputs[]``
        array in ``/state/extended``, so this is the reliable source.

        Falls back to the inputs referenced by already-configured slots when the
        constraint engine is absent (older firmware).
        """
        pref = self._preference("triggerType")
        inputs: set[int] = set()
        for constraint in (pref or {}).get("constraints", []) or []:
            value = constraint.get("input")
            if isinstance(value, int) and not isinstance(value, bool):
                inputs.add(value)
        if not inputs:
            for action in self.actions:
                value = action.get("input")
                if (
                    isinstance(value, int)
                    and not isinstance(value, bool)
                    and action.get("triggerType") != TRIGGER_UNCONFIGURED
                ):
                    inputs.add(value)
        return sorted(inputs)

    def allowed_action_types(self, trigger_type: int) -> list[int]:
        """Return the action types the device accepts for a trigger type."""
        pref = self._preference("actionType")
        for constraint in (pref or {}).get("constraints", []) or []:
            if constraint.get("triggerType") == trigger_type:
                values = constraint.get("actionType")
                if isinstance(values, list):
                    return [v for v in values if isinstance(v, int)]
        return []

    def param_placeholders(self) -> list[str]:
        """Placeholders the device will substitute inside an action's URL.

        Absent on firmware without a constraint engine, in which case callbacks
        simply carry no device state and nothing breaks.
        """
        pref = self._preference("param")
        values = (pref or {}).get("placeholders")
        return (
            [v for v in values if isinstance(v, str)]
            if isinstance(values, list)
            else []
        )

    def supports_http_action(self, trigger_type: int) -> bool:
        """Whether an HTTP GET action can be bound to this trigger type.

        Firmware without a constraint engine reports nothing; we optimistically
        allow it there and let the device reject the save if it disagrees.
        """
        allowed = self.allowed_action_types(trigger_type)
        return ACTION_HTTP_GET in allowed if allowed else not self.field_preferences

    def free_slots(self) -> list[dict[str, Any]]:
        """Unconfigured slots, in slot order.

        These three accessors partition the slot array: a slot is free, or ours,
        or somebody else's, and never two of those at once. Provisioning adds
        the pools up to decide whether a run fits, so an overlap between any two
        of them would overcount the device's real capacity.
        """
        return [a for a in self.actions if not is_configured(a)]

    def owned_actions(self) -> list[dict[str, Any]]:
        """Slots holding a live callback of this integration, in slot order.

        ``is_configured`` is required as well as the ownership marker, and it is
        not redundant with it: trigger type and action type are separate fields,
        so a slot can carry our URL while its trigger reads as unconfigured.
        Firmware that honours the ``triggerType: 0`` half of a clear and keeps
        the ``actionType``/``param`` half leaves exactly that behind.

        Such a slot used to appear in this list *and* in :meth:`free_slots`,
        which let one physical slot be counted twice towards capacity and be
        handed out twice in one run: the second callback silently destroyed the
        first, and a slot taken from the free list while still sitting in the
        reclaimable list was wiped by the clearing pass that followed. A slot
        with no trigger never fires, so it is simply free.
        """
        return [a for a in self.actions if is_configured(a) and is_owned(a)]

    def foreign_actions(self) -> list[dict[str, Any]]:
        """Return configured slots owned by someone else; these are never touched."""
        return [a for a in self.actions if is_configured(a) and not is_owned(a)]


# --- Ownership --------------------------------------------------------------

OWNERSHIP_MARKER = "/api/blebox_advanced/"
"""Substring identifying a callback created by this integration.

Ownership is derived from the URL rather than the action name so that renaming
an action in the wBox app cannot orphan it, and so a rotated callback token or
a changed Home Assistant URL still resolves to the same slot on reconfigure.
"""


def relay_state_from(payload: Any, relay: int) -> bool | None:
    """Pull one relay's state out of any payload that carries a relay list."""
    if not isinstance(payload, dict):
        return None
    relays = payload.get("relays")
    if not isinstance(relays, list):
        return None
    for item in relays:
        if isinstance(item, dict) and item.get("relay") == relay:
            state = item.get("state")
            return bool(state) if isinstance(state, int) else None
    return None


def is_configured(action: dict[str, Any]) -> bool:
    """Whether a slot holds a real action (trigger type 0 marks an empty slot)."""
    return action.get("triggerType", TRIGGER_UNCONFIGURED) != TRIGGER_UNCONFIGURED


def find_native_action(
    state: ActionsState, input_id: int, trigger_type: int
) -> dict[str, Any] | None:
    """Find the native relay action bound to an input trigger, if any.

    Only slots holding a relay action are eligible: an HTTP action or an
    unidentified type sharing the same trigger is never a candidate for editing.
    """
    for action in state.actions:
        if (
            action.get("input") == input_id
            and action.get("triggerType") == trigger_type
            and is_configured(action)
            and action.get("actionType") in NATIVE_RELAY_ACTIONS
        ):
            return action
    return None


def is_owned(action: dict[str, Any]) -> bool:
    """Whether a slot was created by this integration."""
    if action.get("actionType") != ACTION_HTTP_GET:
        return False
    param = action.get("param")
    return isinstance(param, str) and OWNERSHIP_MARKER in param


# --- Payload construction ---------------------------------------------------


def _field_template(actions: list[dict[str, Any]]) -> dict[str, Any]:
    """Union of the fields this device uses on an action, with neutral defaults.

    Empty slots omit fields that configured slots carry, and some hardware
    revisions add ``relay``/``forTime``/``ns``. Filling an empty slot therefore
    needs the shape of the device as a whole, not of that one slot.
    """
    template: dict[str, Any] = {}
    for action in actions:
        for key, value in action.items():
            if key in _READ_ONLY_FIELDS:
                continue
            if key not in template:
                template[key] = 0 if isinstance(value, (int, float)) else ""
    return template


def build_action_payload(
    slot: dict[str, Any],
    template: dict[str, Any],
    **overrides: Any,
) -> dict[str, Any]:
    """Round-trip a slot back to the device with only ``overrides`` changed."""
    payload: dict[str, Any] = dict(template)
    payload.update({k: v for k, v in slot.items() if k not in _READ_ONLY_FIELDS})
    for key, default in _NUMERIC_DEFAULTS.items():
        payload.setdefault(key, default)
    payload.update(overrides)
    return payload


def _clear_overrides() -> dict[str, Any]:
    """Field values that return a slot to the unconfigured state."""
    return {
        "name": "",
        "triggerType": TRIGGER_UNCONFIGURED,
        "actionType": ACTION_UNCONFIGURED,
        "param": "",
        "triggerParam": 0,
        "intervalS": 0,
        "throttleS": 0,
    }


# --- Manager ----------------------------------------------------------------


class BleBoxActionManager:
    """Read and write BleBox device metadata and physical-input actions.

    All automatic provisioning is conservative by construction:

    * capacity is checked before the first write, so a run either fits or
      changes nothing;
    * only slots this integration owns are ever modified or cleared;
    * every write echoes the device's own object back with the edited fields
      changed, preserving hardware-specific fields;
    * every read-plan-write sequence is serialised against the others, so a
      plan is never carried out against a slot layout that has moved on.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        port: int = 80,
        *,
        timeout: int = 10,
    ) -> None:
        """Initialise the manager for one device."""
        self._session = session
        self._host = host
        self._port = port
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        # Two locks, always taken in this order: `_action_lock` first,
        # `_write_lock` second, never the reverse.
        #
        # `_action_lock` covers a whole read-plan-write sequence. Planning
        # happens against a snapshot of the slot array, and that snapshot goes
        # stale the instant anything else writes: two callers that both read
        # "slot 0 is free" both write slot 0, so the first change is silently
        # destroyed, and worse, a slot a foreign action has taken in the
        # meantime would be clobbered - breaking the one rule this integration
        # must never break.
        #
        # `_write_lock` covers a single POST, and stays a separate object
        # because the sequences holding `_action_lock` call
        # `async_save_action`, which takes `_write_lock` itself. asyncio locks
        # are not reentrant, so one shared lock would deadlock on the first
        # write.
        self._action_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()

    @property
    def base_url(self) -> str:
        """Base URL of the device."""
        if self._port and self._port != 80:
            return f"http://{self._host}:{self._port}"
        return f"http://{self._host}"

    @contextmanager
    def request_timeout(self, seconds: float) -> Iterator[None]:
        """Run the requests made inside this block on a shorter deadline.

        The device timeout is deliberately generous, because an ESP-based device
        on a busy Wi-Fi link is genuinely slow sometimes and giving up on it is
        worse than waiting. There is one caller for whom that is the wrong trade
        - setting a config entry up, where the answer is only needed to put live
        values on entities that already exist - so it asks for a shorter one
        rather than every other caller settling for it.

        Not reentrant, and not safe to hold across a caller that must keep the
        full deadline: it swaps the deadline for the whole manager, and requests
        already in flight keep the one they started with.
        """
        previous = self._timeout
        self._timeout = aiohttp.ClientTimeout(total=seconds)
        try:
            yield
        finally:
            self._timeout = previous

    # -- transport ----------------------------------------------------------

    async def _get(self, path: str) -> Any:
        url = f"{self.base_url}{path}"
        try:
            async with self._session.get(url, timeout=self._timeout) as response:
                if response.status == 404:
                    raise ActionsUnsupportedError(f"{path} not available (HTTP 404)")
                response.raise_for_status()
                return await response.json(content_type=None)
        except ActionsUnsupportedError:
            raise
        except (aiohttp.ClientError, TimeoutError) as err:
            raise BleBoxConnectionError(f"GET {path} failed: {err}") from err
        except ValueError as err:
            raise BleBoxConnectionError(
                f"GET {path} returned invalid JSON: {err}"
            ) from err

    async def _post(self, path: str, payload: dict[str, Any]) -> Any:
        url = f"{self.base_url}{path}"
        try:
            async with self._session.post(
                url, json=payload, timeout=self._timeout
            ) as response:
                if response.status == 404:
                    raise ActionsUnsupportedError(f"{path} not available (HTTP 404)")
                if response.status >= 400:
                    body = (await response.text())[:200]
                    raise BleBoxActionApiError(
                        f"POST {path} rejected with HTTP {response.status}: {body}"
                    )
                return await response.json(content_type=None)
        except BleBoxActionApiError:
            # Ours, raised just above: let it through rather than reclassifying
            # it below. `ActionsUnsupportedError` is a subclass, so naming both
            # here only made the hierarchy look less settled than it is.
            raise
        except (aiohttp.ClientError, TimeoutError) as err:
            raise BleBoxConnectionError(f"POST {path} failed: {err}") from err
        except ValueError:
            # A 2xx with a non-JSON body still means the device accepted it.
            return None

    # -- reads --------------------------------------------------------------

    async def async_get_device_info(self) -> DeviceInfo:
        """Fetch device identity, falling back to the older endpoint.

        The fallback catches ``BleBoxError`` rather than only a connection
        failure, because a device without ``/api/device/state`` says so with
        HTTP 404, which ``_get`` reports as ``ActionsUnsupportedError``. That is
        not a ``BleBoxConnectionError``, so the narrower catch this used to have
        could fire on a timeout or a 5xx but never on the one case the fallback
        exists for: setup simply failed on the older hardware it was written to
        support.
        """
        try:
            payload = await self._get("/api/device/state")
        except BleBoxError:
            # Some older/simpler devices only answer on /info.
            payload = await self._get("/info")
        if not isinstance(payload, dict):
            raise BleBoxConnectionError("Unexpected device state payload")
        return DeviceInfo.from_payload(payload)

    async def async_get_actions_state(self) -> ActionsState:
        """Fetch the action slots (undocumented endpoint)."""
        payload = await self._get("/api/actions/state")
        if not isinstance(payload, dict):
            raise ActionsUnsupportedError("Unexpected actions state payload")
        return ActionsState.from_payload(payload)

    async def async_get_extended_state(self) -> dict[str, Any]:
        """Fetch relay, power and safety state from ``/state/extended``."""
        payload = await self._get("/state/extended")
        if not isinstance(payload, dict):
            raise BleBoxConnectionError("Unexpected extended state payload")
        return payload

    async def async_get_settings(self) -> dict[str, Any]:
        """Fetch the device settings object.

        The endpoint is public but its contents are not specified anywhere;
        which keys exist varies by product (button backlight, status LED, cloud
        tunnel, overload threshold, per-relay restart behaviour).
        """
        payload = await self._get("/api/settings/state")
        if not isinstance(payload, dict):
            raise BleBoxConnectionError("Unexpected settings payload")
        settings = payload.get("settings")
        return settings if isinstance(settings, dict) else {}

    async def async_get_uptime(self) -> int | None:
        """Fetch uptime in seconds, or ``None`` if the device will not say."""
        try:
            payload = await self._get("/api/device/uptime")
        except BleBoxError:
            return None
        if isinstance(payload, dict) and isinstance(payload.get("uptimeS"), int):
            return payload["uptimeS"]
        return None

    async def async_set_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        """Apply a partial settings patch (undocumented endpoint).

        Wrapped as ``{"settings": {...}}``, exactly as the device's own wBox UI
        does - confirmed from the ``settings.js`` bundle it serves. Unlike
        ``/api/actions/set``, only the keys being changed are sent: the device
        merges them, and echoing back read-only sub-objects such as
        ``fieldsPreferences`` or ``factoryCalibration`` risks rejection.

        Returns the settings object the device reports after the write.
        """
        async with self._write_lock:
            payload = await self._post("/api/settings/set", {"settings": patch})
        if isinstance(payload, dict) and isinstance(payload.get("settings"), dict):
            return payload["settings"]
        return {}

    async def async_get_inputs(self) -> list[int]:
        """Discover the device's physical input indices."""
        return (await self.async_get_actions_state()).input_ids()

    async def async_supports_action_configuration(self) -> bool:
        """Whether automatic provisioning can be attempted on this device.

        Never raises: a ``False`` result simply routes the user to manual mode.
        """
        try:
            state = await self.async_get_actions_state()
        except BleBoxError as err:
            _LOGGER.debug("Action configuration unsupported on %s: %s", self._host, err)
            return False
        return bool(state.input_ids()) and state.supports_http_action(
            TRIGGER_SHORT_CLICK
        )

    # -- writes -------------------------------------------------------------

    async def async_set_native_action(
        self,
        input_id: int,
        trigger_type: int,
        action_type: int,
    ) -> None:
        """Set what a physical input does to the relay locally.

        Writes only a slot already holding a native relay action, or an empty
        one. A slot carrying an HTTP action or an unidentified action type is
        never modified, so this cannot clobber either the callbacks or anything
        the user configured that we do not understand.

        The slot array is read here rather than taken from the caller: a
        snapshot fetched outside the lock (a coordinator poll, say) can already
        describe a layout that has moved on, and acting on it would put the new
        action in a slot something else now occupies.
        """
        async with self._action_lock:
            state = await self.async_get_actions_state()
            template = _field_template(state.actions)
            existing = find_native_action(state, input_id, trigger_type)

            if existing is not None:
                overrides = (
                    _clear_overrides()
                    if action_type == ACTION_UNCONFIGURED
                    else {
                        "input": input_id,
                        "triggerType": trigger_type,
                        "actionType": action_type,
                        "param": "",
                    }
                )
                await self.async_save_action(
                    build_action_payload(existing, template, **overrides)
                )
                return

            if action_type == ACTION_UNCONFIGURED:
                return

            free = state.free_slots()
            if not free:
                # Deliberately no reclaiming of stale slots here, unlike
                # `async_sync_http_actions`: the only slots this method could
                # take are ones holding callbacks the user asked for, and
                # dropping an event to satisfy an unrelated request is worse
                # than refusing. One slot was needed and none was free, so the
                # figures reported stay exactly that.
                raise InsufficientSlotsError(1, 0, state.items_limit)
            await self.async_save_action(
                build_action_payload(
                    free[0],
                    template,
                    name=f"HA IN{input_id + 1} local",
                    input=input_id,
                    triggerType=trigger_type,
                    actionType=action_type,
                    param="",
                )
            )

    async def async_get_network(self) -> dict[str, Any]:
        """Fetch WiFi and access point state from ``/api/device/network``."""
        payload = await self._get("/api/device/network")
        if not isinstance(payload, dict):
            raise BleBoxConnectionError("Unexpected network payload")
        return payload

    async def async_set_ap_enabled(self, enabled: bool) -> dict[str, Any]:
        """Turn the device's own access point on or off.

        Mirrors what the device's wBox UI posts: a partial ``network`` patch
        carrying the access point fields only, with the SSID and password round
        tripped so enabling later does not find them blank. The station
        configuration is never included, so this cannot knock the device off
        the network it is joined to.

        Returns the network object the device reports after the write, the same
        way ``async_set_settings`` does, so a caller never has to assume the
        write took effect nor wait for the next poll to find out. Confirmed
        against live hardware: ``/api/device/set`` answers with both ``device``
        and ``network``. An empty result means the device said nothing usable.
        """
        current = await self.async_get_network()
        patch: dict[str, Any] = {"apEnable": bool(enabled)}
        for key in ("apSSID", "apPasswd"):
            if key in current:
                patch[key] = current[key]
        payload = await self._post("/api/device/set", {"network": patch})
        if isinstance(payload, dict) and isinstance(payload.get("network"), dict):
            return payload["network"]
        return {}

    async def async_install_firmware(self) -> None:
        """Start a firmware update.

        The device pulls the image itself and reboots, so this returns as soon
        as it accepts the request. Note it fetches over BleBox's tunnel, so an
        update cannot start with the cloud tunnel switched off.
        """
        await self._post("/api/ota/update", {})

    async def async_set_relay(self, relay: int, on: bool) -> bool | None:
        """Switch a relay, returning the state the device reports afterwards.

        The control endpoint answers with the resulting relay state, so the
        caller never has to assume the command took effect nor wait for the
        next poll to find out. ``None`` means the device said nothing usable.
        """
        payload = await self._get(f"/s/{relay}/{1 if on else 0}")
        return relay_state_from(payload, relay)

    async def async_save_action(self, action: dict[str, Any]) -> None:
        """Persist a single action slot.

        The endpoint takes exactly one action per request, wrapped as
        ``{"action": {...}}``.
        """
        async with self._write_lock:
            await self._post("/api/actions/set", {"action": action})

    async def async_sync_http_actions(self, desired: list[DesiredAction]) -> SyncResult:
        """Reconcile the device's slots with ``desired``.

        Slots owned by this integration are created, updated, recycled or
        cleared as needed. Every other slot is left untouched. Raises
        :class:`InsufficientSlotsError` *before writing anything* when the new
        callbacks would not fit.

        The slot array is read here, inside the lock, rather than taken from
        the caller: a plan made from a snapshot fetched earlier can target a
        slot that has since been filled, and a callback overwriting somebody
        else's action is the one failure this integration cannot have.
        """
        async with self._action_lock:
            return await self._async_sync_locked(desired)

    async def _async_sync_locked(self, desired: list[DesiredAction]) -> SyncResult:
        """Plan and apply one reconciliation, with ``_action_lock`` held."""
        state = await self.async_get_actions_state()
        template = _field_template(state.actions)
        result = SyncResult(slots_total=state.items_limit)

        owned_by_key: dict[tuple[int | None, int], dict[str, Any]] = {}
        surplus: list[dict[str, Any]] = []
        for action in state.owned_actions():
            raw_input = action.get("input")
            key = (
                raw_input if isinstance(raw_input, int) else None,
                _device_int(action.get("triggerType", 0), "trigger type"),
            )
            if key in owned_by_key:
                # A duplicate of one of ours (e.g. a half-finished earlier run).
                surplus.append(action)
            else:
                owned_by_key[key] = action

        free = state.free_slots()
        result.slots_foreign = len(state.foreign_actions())

        # Pair every wanted callback with the slot of ours already holding it
        # before deciding anything, because what is left in `owned_by_key`
        # afterwards is by definition ours and no longer wanted.
        paired: list[tuple[DesiredAction, dict[str, Any] | None]] = []
        for item in desired:
            key = (item.input_id, item.trigger_type)
            paired.append((item, owned_by_key.pop(key, None)))

        # Ours, but no longer wanted: reclaimable capacity. Sorted by slot so
        # that which one gets recycled and which gets cleared is predictable
        # rather than a consequence of dict ordering.
        reclaimable = sorted([*owned_by_key.values(), *surplus], key=_slot_id)

        # Capacity check first - never leave the device half-provisioned. A
        # stale slot of ours counts as available: a device whose slots are full
        # of our own callbacks that nobody wants any more must still be
        # reprovisionable, and refusing there stranded users with no way out
        # short of clearing the slots by hand.
        needed = sum(1 for _, existing in paired if existing is None)
        available = len(free) + len(reclaimable)
        if needed > available:
            raise InsufficientSlotsError(needed, available, state.items_limit)

        writes: list[dict[str, Any]] = []
        for item, existing in paired:
            if existing is not None:
                if (
                    existing.get("param") == item.url
                    and existing.get("name") == item.name
                    # Every callback written here is edge- or click-triggered,
                    # so its trigger parameter is always zero. Compared anyway
                    # so that a non-zero one - left by an older version of this
                    # integration, or by whoever used the slot before - forces
                    # the rewrite that clears it, rather than reading as a slot
                    # with nothing to do.
                    and _device_int(existing.get("triggerParam") or 0, "trigger param")
                    == UNPACED_TRIGGER_PARAM
                ):
                    result.unchanged.append(_slot_id(existing))
                    continue
                writes.append(
                    build_action_payload(
                        existing,
                        template,
                        name=item.name,
                        input=item.input_id,
                        triggerType=item.trigger_type,
                        triggerParam=UNPACED_TRIGGER_PARAM,
                        actionType=ACTION_HTTP_GET,
                        param=item.url,
                    )
                )
                result.updated.append(_slot_id(existing))
                continue

            # Empty slots are spent first; a stale slot of ours is recycled
            # only once they run out, so a run disturbs as few slots as it can.
            if free:
                slot = free.pop(0)
                pacing: dict[str, Any] = {}
            else:
                slot = reclaimable.pop(0)
                # A recycled slot is being repurposed for a different input and
                # trigger, so any pacing the callback that used to live there
                # carried is meaningless now. An empty slot arrives at zero via
                # `_NUMERIC_DEFAULTS`; a recycled one has to be told, or a
                # throttle set on the old action would silently rate-limit the
                # new one.
                pacing = {"intervalS": 0, "throttleS": 0}
            writes.append(
                build_action_payload(
                    slot,
                    template,
                    name=item.name,
                    input=item.input_id,
                    triggerType=item.trigger_type,
                    triggerParam=UNPACED_TRIGGER_PARAM,
                    actionType=ACTION_HTTP_GET,
                    param=item.url,
                    **pacing,
                )
            )
            # Recycling writes the new definition straight over the old one.
            # Clearing first would cost a second round trip and leave the
            # button doing nothing at all in between.
            result.created.append(_slot_id(slot))

        # Ours, no longer wanted, and not recycled above: emptied.
        for stale in reclaimable:
            writes.append(build_action_payload(stale, template, **_clear_overrides()))
            result.cleared.append(_slot_id(stale))

        # The whole plan is checked before the first request leaves, because a
        # run that wrote two things into one slot would break the promise this
        # method exists to keep in the quietest possible way: the second write
        # destroys the first, and `SyncResult` still reports both. The pools are
        # disjoint by construction, so this can only fire if that ever stops
        # being true - in which case refusing the run leaves the device exactly
        # as it was, which is the outcome design rule 2 asks for.
        planned = [_slot_id(payload) for payload in writes]
        if len(set(planned)) != len(planned):
            raise BleBoxActionApiError(
                f"Refusing to write the same action slot twice in one run: {planned}"
            )

        for payload in writes:
            await self.async_save_action(payload)

        result.slots_free = len(free) + len(result.cleared)
        return result

    async def async_remove_owned_actions(self) -> list[int]:
        """Clear every slot this integration created, leaving all others alone.

        Returns the slot ids that were cleared.
        """
        async with self._action_lock:
            state = await self.async_get_actions_state()
            template = _field_template(state.actions)
            # Every slot carrying our marker, rather than `owned_actions()`:
            # removal has to erase our callback URL even from a slot whose
            # trigger the firmware has already zeroed, or the entry goes away
            # and the callback token stays readable in the wBox app.
            #
            # Slot ids are resolved before the first write, so a device that
            # describes a slot without one fails before anything is touched
            # rather than half way through the clearing.
            targets = [
                (_slot_id(action), action)
                for action in state.actions
                if is_owned(action)
            ]
            for _, action in targets:
                await self.async_save_action(
                    build_action_payload(action, template, **_clear_overrides())
                )
            return [slot_id for slot_id, _ in targets]
