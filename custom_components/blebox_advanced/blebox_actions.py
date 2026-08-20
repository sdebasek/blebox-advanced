"""Abstraction over the BleBox device HTTP API.

This is the **only** module in the integration that talks to a BleBox device.
Everything else depends on the small, stable surface defined here, so that a
firmware change (or BleBox altering their internal API) is contained to this
file and can be disabled entirely without breaking event reception.

Documented endpoints (see https://technical.blebox.eu/):

    GET  /api/device/state    device identity (id, type, fv, hv, apiLevel)

Undocumented / reverse-engineered endpoints. BleBox states that "only main
functionalities are open for public", and the action CRUD surface is not part
of any published OpenAPI spec. The shapes below were confirmed against
live ``switchBox`` hardware and the device's own built-in wBox UI bundle:

    GET  /api/actions/state   fixed array of action slots, ``itemsLimit`` and a
                              ``fieldsPreferences`` constraint engine
    POST /api/actions/set     upsert exactly **one** action, ``{"action": {...}}``

Two properties of ``/api/actions/set`` matter a great deal and are enforced
here:

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
from dataclasses import dataclass, field
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

# --- Trigger types ----------------------------------------------------------
# 1-5 are documented for buttonBox (`/t/{inputId}/{triggerType}`); 0 is the
# empty-slot marker, and 19/42/43 are device-level triggers we never write.

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


def event_type_for_trigger(
    trigger_type: int, *, invert_edges: bool = False
) -> str | None:
    """Return the Home Assistant event type for a BleBox trigger type."""
    table = _EVENT_TRIGGERS_INVERTED if invert_edges else _EVENT_TRIGGERS
    for event_type, value in table.items():
        if value == trigger_type:
            return event_type
    return None


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
    """One callback the integration wants to exist on the device."""

    input_id: int | None
    trigger_type: int
    url: str
    name: str
    trigger_param: int = 0


@dataclass(slots=True)
class SyncResult:
    """Outcome of reconciling our callbacks with the device's action slots."""

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
        return cls(
            actions=[a for a in actions if isinstance(a, dict)],
            items_limit=int(payload.get("itemsLimit") or len(actions)),
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

    def allowed_trigger_types(self, input_id: int | None) -> list[int]:
        """Trigger types the device accepts for an input."""
        pref = self._preference("triggerType")
        if pref is None:
            return []
        for constraint in pref.get("constraints", []) or []:
            if constraint.get("input") == input_id:
                values = constraint.get("triggerType")
                if isinstance(values, list):
                    return [v for v in values if isinstance(v, int)]
        values = pref.get("values")
        return (
            [v for v in values if isinstance(v, int)]
            if isinstance(values, list)
            else []
        )

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
        """Unconfigured slots, in slot order."""
        return [a for a in self.actions if not is_configured(a)]

    def owned_actions(self) -> list[dict[str, Any]]:
        """Slots created by this integration, in slot order."""
        return [a for a in self.actions if is_owned(a)]

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
      changed, preserving hardware-specific fields.
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
        self._write_lock = asyncio.Lock()

    @property
    def base_url(self) -> str:
        """Base URL of the device."""
        if self._port and self._port != 80:
            return f"http://{self._host}:{self._port}"
        return f"http://{self._host}"

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
        except (ActionsUnsupportedError, BleBoxActionApiError):
            raise
        except (aiohttp.ClientError, TimeoutError) as err:
            raise BleBoxConnectionError(f"POST {path} failed: {err}") from err
        except ValueError:
            # A 2xx with a non-JSON body still means the device accepted it.
            return None

    # -- reads --------------------------------------------------------------

    async def async_get_device_info(self) -> DeviceInfo:
        """Fetch device identity from the documented endpoint."""
        try:
            payload = await self._get("/api/device/state")
        except BleBoxConnectionError:
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
        *,
        state: ActionsState | None = None,
    ) -> None:
        """Set what a physical input does to the relay locally.

        Writes only a slot already holding a native relay action, or an empty
        one. A slot carrying an HTTP action or an unidentified action type is
        never modified, so this cannot clobber either the callbacks or anything
        the user configured that we do not understand.
        """
        state = state or await self.async_get_actions_state()
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

    async def async_check_firmware(self) -> None:
        """Ask the device to look for newer firmware.

        Best-effort: the endpoint is undocumented, and the result is read from
        ``availableFv`` in the device state either way.
        """
        try:
            await self._get("/api/ota/check")
        except BleBoxError as err:
            _LOGGER.debug("Firmware check unavailable: %s", err)

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

    async def async_sync_http_actions(
        self,
        desired: list[DesiredAction],
        *,
        state: ActionsState | None = None,
    ) -> SyncResult:
        """Reconcile the device's slots with ``desired``.

        Slots owned by this integration are created, updated or cleared as
        needed. Every other slot is left untouched. Raises
        :class:`InsufficientSlotsError` *before writing anything* when the new
        callbacks would not fit.
        """
        state = state or await self.async_get_actions_state()
        template = _field_template(state.actions)
        result = SyncResult(slots_total=state.items_limit)

        owned_by_key: dict[tuple[int | None, int], dict[str, Any]] = {}
        surplus: list[dict[str, Any]] = []
        for action in state.owned_actions():
            raw_input = action.get("input")
            key = (
                raw_input if isinstance(raw_input, int) else None,
                int(action.get("triggerType", 0)),
            )
            if key in owned_by_key:
                # A duplicate of one of ours (e.g. a half-finished earlier run).
                surplus.append(action)
            else:
                owned_by_key[key] = action

        free = state.free_slots()
        result.slots_foreign = len(state.foreign_actions())

        # Capacity check first - never leave the device half-provisioned.
        needed = sum(
            1 for d in desired if (d.input_id, d.trigger_type) not in owned_by_key
        )
        if needed > len(free):
            raise InsufficientSlotsError(needed, len(free), state.items_limit)

        writes: list[dict[str, Any]] = []
        for item in desired:
            key = (item.input_id, item.trigger_type)
            existing = owned_by_key.pop(key, None)
            if existing is not None:
                if (
                    existing.get("param") == item.url
                    and existing.get("name") == item.name
                    # Compared too, or changing a periodic action's interval in
                    # the options would never reach the device: the URL and name
                    # stay identical, so the slot would look unchanged.
                    and int(existing.get("triggerParam") or 0) == item.trigger_param
                ):
                    result.unchanged.append(int(existing["id"]))
                    continue
                writes.append(
                    build_action_payload(
                        existing,
                        template,
                        name=item.name,
                        input=item.input_id,
                        triggerType=item.trigger_type,
                        triggerParam=item.trigger_param,
                        actionType=ACTION_HTTP_GET,
                        param=item.url,
                    )
                )
                result.updated.append(int(existing["id"]))
                continue

            slot = free.pop(0)
            writes.append(
                build_action_payload(
                    slot,
                    template,
                    name=item.name,
                    input=item.input_id,
                    triggerType=item.trigger_type,
                    triggerParam=item.trigger_param,
                    actionType=ACTION_HTTP_GET,
                    param=item.url,
                )
            )
            result.created.append(int(slot["id"]))

        # Ours, but no longer wanted.
        for stale in [*owned_by_key.values(), *surplus]:
            writes.append(build_action_payload(stale, template, **_clear_overrides()))
            result.cleared.append(int(stale["id"]))

        for payload in writes:
            await self.async_save_action(payload)

        result.slots_free = len(free) + len(result.cleared)
        return result

    async def async_remove_owned_actions(self) -> list[int]:
        """Clear every slot this integration created, leaving all others alone.

        Returns the slot ids that were cleared.
        """
        state = await self.async_get_actions_state()
        template = _field_template(state.actions)
        cleared: list[int] = []
        for action in state.owned_actions():
            await self.async_save_action(
                build_action_payload(action, template, **_clear_overrides())
            )
            cleared.append(int(action["id"]))
        return cleared
