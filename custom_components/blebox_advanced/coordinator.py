"""Runtime state and the device coordinator.

One interval, two cadences. Every ``SCAN_INTERVAL_SECONDS`` the coordinator
reads ``/state/extended``; that payload is the sole data source for the relay,
power, energy, countdown and safety entities, so this is its primary job. Once
``SLOW_REFRESH_SECONDS`` have elapsed since the last one, the next poll also
reads device identity, settings, network state, uptime and the action slot
table, which change rarely and would be wasteful at the fast cadence. A write
from Home Assistant forces the next cycle to be a full one, so a change made
here never waits for the slow cadence.

The full cycle is also where automatic mode notices that our callbacks have
disappeared from the device (edited away in the wBox app, factory reset,
restored backup) and puts them back, and where the device's capability shape is
remembered so an unreachable device still produces its entities at startup.

Input events are the one thing never polled for: they are **pushed** by the
device, see :mod:`api`. A press is never inferred from relay state, which would
be both laggy and wrong - the relay also moves when Home Assistant switches it,
inputs can be detached from the relay entirely, and a long press cannot be
distinguished this way at all.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import async_default_base_url, build_desired_actions
from .blebox_actions import (
    ActionsState,
    BleBoxActionManager,
    BleBoxError,
    DeviceInfo,
    InsufficientSlotsError,
    SyncResult,
    is_owned,
)
from .const import (
    CONF_DEVICE_CACHE,
    CONF_ENABLED_EVENTS,
    DOMAIN,
    HEAL_BACKOFF_MAX_CYCLES,
    MODE_AUTOMATIC,
    SCAN_INTERVAL_SECONDS,
    SETTING_POWER_MEASURING,
    SETTING_RELAYS,
    SLOW_REFRESH_SECONDS,
    WRITE_SETTLE_S,
)

_LOGGER = logging.getLogger(__name__)

# Keys inside the remembered payloads (see `CONF_DEVICE_CACHE`). They are a
# storage format: renaming one silently discards what existing entries hold.
CACHE_INFO = "info"
CACHE_SETTINGS = "settings"
CACHE_STATE = "state"
CACHE_NETWORK = "network"
CACHE_UPTIME = "uptime_s"


WRITTEN_SETTINGS = "settings"
WRITTEN_NETWORK = "network"


@dataclass(slots=True)
class _WrittenPayload:
    """A payload the device echoed back after Home Assistant wrote to it.

    Both `/api/settings/set` and `/api/device/set` answer with the resulting
    object, so a control can show what the device actually stored rather than
    what was asked for, without waiting for the next poll. A refresh already in
    flight when the write landed still carries the state from before it, which
    is what this outranks.
    """

    payload: dict[str, Any]
    written_at: float

    def expired(self) -> bool:
        """Whether a poll can now be trusted to carry this write."""
        return time.monotonic() - self.written_at >= WRITE_SETTLE_S


@dataclass(slots=True)
class ProvisioningStatus:
    """Outcome of the most recent automatic provisioning attempt."""

    supported: bool = False
    attempted: bool = False
    error: str | None = None
    result: SyncResult | None = None


@dataclass(slots=True)
class CallbackHealth:
    """What the device reports about its own attempts to call Home Assistant.

    Each action records the outcome of its most recent call, which is the only
    delivery feedback this transport offers: an HTTP action is fire-and-forget,
    so without this a switch that cannot reach Home Assistant is indistinguish-
    able from one nobody has pressed.
    """

    configured: int = 0
    delivered: int = 0
    unreachable: int = 0
    rejected: int = 0
    last_status: int | None = None

    @property
    def ever_called(self) -> bool:
        """Whether any callback has fired at all since it was configured."""
        return bool(self.delivered or self.unreachable or self.rejected)

    @property
    def problem(self) -> bool:
        """Whether the most recent evidence says callbacks are not arriving."""
        return self.ever_called and self.delivered == 0


@dataclass(slots=True)
class DeviceSnapshot:
    """What the coordinator last saw on the device."""

    info: DeviceInfo | None = None
    actions: ActionsState | None = None
    settings: dict[str, Any] = field(default_factory=dict)
    state: dict[str, Any] = field(default_factory=dict)
    uptime_s: int | None = None
    network: dict[str, Any] = field(default_factory=dict)
    health: CallbackHealth = field(default_factory=CallbackHealth)


def callback_health(actions: ActionsState | None) -> CallbackHealth:
    """Summarise the delivery outcome recorded against our own actions."""
    health = CallbackHealth()
    if actions is None:
        return health
    for action in actions.owned_actions():
        health.configured += 1
        last_call = action.get("lastCall") or {}
        if last_call.get("timeElapsedS", -1) == -1:
            continue  # never fired, so it says nothing either way
        status = (last_call.get("response") or {}).get("status")
        if isinstance(status, int) and status:
            health.last_status = status
        if isinstance(status, int) and 200 <= status < 400:
            health.delivered += 1
        elif status:
            # Reached Home Assistant, which answered with an error: a wrong URL
            # rather than a broken network path.
            health.rejected += 1
        else:
            # No HTTP response at all - routing or firewall.
            health.unreachable += 1
    return health


def _as_dict(value: Any) -> dict[str, Any]:
    """Return a remembered payload if it still looks like one, else an empty one."""
    return value if isinstance(value, dict) else {}


def _keys(value: Any) -> tuple[str, ...]:
    """Return the keys of a payload object, which is what platforms look for."""
    return tuple(sorted(value)) if isinstance(value, dict) else ()


def relay_list(payload: dict[str, Any]) -> list[Any]:
    """Return the relays a settings or state payload describes, or none.

    Both payloads carry the relay array under the same key, and every platform
    that builds per-relay entities has to ask the same question of one of them.
    Firmware has been seen to answer with something other than a list, so the
    check belongs in one place rather than in each caller's own spelling of it.
    """
    relays = payload.get(SETTING_RELAYS)
    return relays if isinstance(relays, list) else []


def _measured_values(state: dict[str, Any]) -> tuple[str, ...]:
    """Return which measurements the device reports, one sensor each.

    A measurement is identified by the ``type`` its entry carries rather than by
    a key, so unlike everything else in the signature this has to look at a
    value.
    """
    types = {
        sensor["type"]
        for sensor in state.get("sensors") or []
        if isinstance(sensor, dict) and isinstance(sensor.get("type"), str)
    }
    measuring = state.get("powerMeasuring")
    if isinstance(measuring, dict) and measuring.get("powerConsumption"):
        types.add("powerConsumption")
    return tuple(sorted(types))


def _identity(info: DeviceInfo | None) -> tuple[str, ...] | None:
    """Return the identity fields a device page shows, or None if it never said."""
    if info is None:
        return None
    return (
        info.device_type,
        info.product,
        info.firmware_version,
        info.hardware_version,
        info.api_level,
    )


def timed_relays(state: dict[str, Any]) -> tuple[int, ...]:
    """Return the relays reporting a countdown, one sensor each.

    Public because the sensor platform decides its countdown entities from
    exactly this predicate. It used to hold its own copy, and the two agreeing
    is load-bearing: :func:`capability_signature` calls this one, so a drift
    between them would stop the remembered shape being rewritten and give a
    device restarted while offline the wrong countdown sensors.
    """
    return tuple(
        index
        for index, relay in enumerate(relay_list(state))
        if isinstance(relay, dict) and "forTimeLeftS" in relay
    )


def capability_signature(snapshot: DeviceSnapshot) -> tuple[Any, ...]:
    """Summarise which entities a snapshot would produce, and nothing else.

    This is what decides whether the remembered payloads are worth rewriting.
    It follows the checks the platforms make but stays deliberately blind to
    values: relay state, power and uptime move constantly, and a signature that
    noticed them would rewrite the config entry on every poll.

    Being a little too coarse is the safe direction. A shape change this misses
    costs nothing until the device is next unreachable at startup, whereas a
    signature that changed too eagerly would hammer ``.storage``.
    """
    settings = snapshot.settings
    state = snapshot.state
    return (
        _keys(settings),
        # Nested, because two entities are gated on sub-objects of this one.
        _keys(settings.get(SETTING_POWER_MEASURING)),
        len(relay_list(settings)),
        _keys(snapshot.network),
        _keys(state),
        _keys(state.get("switch")),
        _measured_values(state),
        timed_relays(state),
        snapshot.uptime_s is not None,
        # The identity is compared by value, not merely by presence. It decides
        # no entity, so it is not a capability, but it is what a device page
        # shows; remembering only "an identity existed" left an offline start
        # seeding the firmware version from before the last update, which is
        # exactly the field someone checks to confirm an update worked. These
        # move about as often as the firmware does, so they cost no writes.
        _identity(snapshot.info),
    )


def device_payload(info: DeviceInfo) -> dict[str, Any]:
    """Return an identity payload that :meth:`DeviceInfo.from_payload` accepts.

    Rebuilt from the parsed fields, with ``raw`` underneath so extras such as
    ``availableFv`` survive, rather than stored as ``raw`` alone: ``raw`` is
    whatever this firmware happened to send, and a payload that would not parse
    back costs the device its firmware entity on the next offline start.
    """
    return {
        **info.raw,
        "id": info.device_id,
        "deviceName": info.name,
        "type": info.device_type,
        "product": info.product,
        "fv": info.firmware_version,
        "hv": info.hardware_version,
        "apiLevel": info.api_level,
    }


def cached_snapshot(cached: Any) -> DeviceSnapshot | None:
    """Rebuild the shape a device last had from what its config entry remembers.

    The values come along with the shape, but are never presented as live: a
    coordinator seeded from here has not refreshed yet, and
    ``last_update_success`` only becomes true once the device itself answers.

    Action slots are deliberately not remembered. They describe what the device
    is doing right now rather than what it has, no entity is gated on them, and
    a stale slot layout is exactly the thing that must never be acted on.
    """
    if not isinstance(cached, dict):
        return None

    info: DeviceInfo | None = None
    payload = cached.get(CACHE_INFO)
    if isinstance(payload, dict):
        try:
            info = DeviceInfo.from_payload(payload)
        except BleBoxError as err:
            _LOGGER.debug("Ignoring an unusable remembered device identity: %s", err)

    uptime = cached.get(CACHE_UPTIME)
    return DeviceSnapshot(
        info=info,
        settings=_as_dict(cached.get(CACHE_SETTINGS)),
        state=_as_dict(cached.get(CACHE_STATE)),
        network=_as_dict(cached.get(CACHE_NETWORK)),
        uptime_s=uptime if isinstance(uptime, int) else None,
    )


def issue_keys(entry_id: str) -> tuple[str, str]:
    """Return the repair issue ids one entry can raise.

    Shared with entry removal, which has to clear them: an issue outlives the
    entry that raised it otherwise, leaving a warning about a device Home
    Assistant no longer knows anything about and no way to dismiss it.
    """
    return (f"callbacks_unreachable_{entry_id}", f"callbacks_rejected_{entry_id}")


@dataclass(slots=True)
class BleBoxEventsData:
    """Runtime data for one configured device."""

    manager: BleBoxActionManager
    coordinator: BleBoxEventsCoordinator
    blebox_id: str
    device_name: str
    token: str
    inputs: list[int]
    enabled_events: dict[int, list[str]]
    mode: str
    debounce: float
    invert_edges: bool
    base_url: str | None
    manage_buttons: bool = False
    ha_device_id: str | None = None
    last_event: dict[tuple[int, str], float] = field(default_factory=dict)
    provisioning: ProvisioningStatus = field(default_factory=ProvisioningStatus)
    options_snapshot: dict[str, Any] = field(default_factory=dict)
    """The options this entry was set up with, so a reload can be told apart.

    Every update to a config entry fires its update listener, whatever changed -
    including the coordinator remembering the device's capability shape in
    ``entry.data``. Reloading for that would tear the entry down and
    re-provision the device merely because a poll noticed something new.
    """


type BleBoxEventsConfigEntry = ConfigEntry[BleBoxEventsData]


def parse_enabled_events(raw: object) -> dict[int, list[str]]:
    """Normalise the stored per-input selection into ``{input: [event, ...]}``.

    Config entry options round-trip through JSON, so the input indices come
    back as strings.
    """
    if not isinstance(raw, dict):
        return {}
    parsed: dict[int, list[str]] = {}
    for key, value in raw.items():
        try:
            input_id = int(key)
        except (TypeError, ValueError):
            continue
        if isinstance(value, list):
            parsed[input_id] = [str(item) for item in value]
    return parsed


def enabled_events_from_entry(entry: ConfigEntry) -> dict[int, list[str]]:
    """Read the per-input selection from an entry's options."""
    return parse_enabled_events(entry.options.get(CONF_ENABLED_EVENTS))


async def async_provision_entry(
    hass: HomeAssistant,
    entry: BleBoxEventsConfigEntry,
    *,
    state: ActionsState | None = None,
) -> SyncResult:
    """Write this entry's callbacks to the device, preserving everything else.

    ``state`` supplies the URL placeholder list only. Which substitutions a
    device advertises is static metadata, so a copy from a previous read is
    harmless there; slot allocation deliberately does not use it, because the
    writer re-reads the slots itself under its own lock and a stale slot list
    would let two writes claim the same slot.

    Raises :class:`InsufficientSlotsError` (without having written anything) if
    the callbacks would not fit, and :class:`BleBoxError` on transport failure.
    """
    data = entry.runtime_data
    base_url = data.base_url or async_default_base_url(hass)
    if not base_url:
        raise BleBoxError(
            "No Home Assistant URL is available for the device to call; set one "
            "in the integration options"
        )
    if state is None:
        # Fetched only so the URLs can carry whatever placeholders this
        # particular device advertises.
        state = await data.manager.async_get_actions_state()
    placeholders = state.param_placeholders()
    desired = build_desired_actions(
        data.enabled_events,
        data.token,
        base_url,
        invert_edges=data.invert_edges,
        placeholders=placeholders,
    )
    return await data.manager.async_sync_http_actions(desired)


async def async_apply_provisioning(
    hass: HomeAssistant,
    entry: BleBoxEventsConfigEntry,
    *,
    state: ActionsState | None = None,
) -> None:
    """Provision in automatic mode, recording rather than raising failures.

    Automatic device programming rides on an undocumented API, so it must never
    be able to stop the event receiver from working: a manual callback keeps
    firing regardless of what happens here.

    ``state`` is passed through purely to save re-reading the URL placeholder
    list; nothing decides what to write from it.
    """
    data = entry.runtime_data
    status = data.provisioning
    if data.mode != MODE_AUTOMATIC:
        status.attempted = False
        status.error = None
        return

    status.attempted = True
    try:
        result = await async_provision_entry(hass, entry, state=state)
    except InsufficientSlotsError as err:
        # Only the first of a run of identical failures is logged as one. This
        # is retried on a timer, and a message asking the user to go and free a
        # slot loses all its force repeated once a minute for as long as Home
        # Assistant runs - it buries itself along with everything else in the
        # log. A different message, or the same one after a spell of working,
        # is a fresh problem and says so.
        repeated = status.error == str(err)
        status.error = str(err)
        status.result = None
        _LOGGER.log(
            logging.DEBUG if repeated else logging.ERROR,
            "%s: %s. Free some action slots in the wBox app, or switch this "
            "integration to manual mode",
            data.device_name,
            err,
        )
    except BleBoxError as err:
        repeated = status.error == str(err)
        status.error = str(err)
        status.result = None
        _LOGGER.log(
            logging.DEBUG if repeated else logging.WARNING,
            "%s: automatic action configuration failed (%s). Existing device "
            "configuration was left untouched; manual callbacks still work",
            data.device_name,
            err,
        )
    else:
        status.error = None
        status.result = result
        if result.changed:
            _LOGGER.info(
                "%s: action slots created=%s updated=%s cleared=%s (left %s "
                "unrelated action(s) untouched)",
                data.device_name,
                result.created,
                result.updated,
                result.cleared,
                result.slots_foreign,
            )


class BleBoxEventsCoordinator(DataUpdateCoordinator[DeviceSnapshot]):
    """Refresh device metadata and keep automatic callbacks in place."""

    config_entry: BleBoxEventsConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: BleBoxEventsConfigEntry,
        manager: BleBoxActionManager,
    ) -> None:
        """Initialise the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.title}",
            update_interval=timedelta(seconds=SCAN_INTERVAL_SECONDS),
        )
        self.manager = manager
        # When the slow fetches last landed, so their cadence is elapsed time
        # rather than a count of refreshes. `None` means "not yet", which makes
        # the first refresh a full one.
        self._last_full: float | None = None
        self._force_full = False
        self._written: dict[str, _WrittenPayload] = {}
        self._cached_signature: tuple[Any, ...] | None = None

        # What the last repair attempt was for, and how it has been going. See
        # `_async_heal_due`.
        self._heal_attempt: tuple[Any, ...] | None = None
        self._heal_failures = 0
        self._heal_skipped = 0

        # Seeded before the first refresh, because platform setup decides what
        # to create by inspecting `coordinator.data` and would otherwise create
        # nothing at all for a device that is not answering right now.
        # `DataUpdateCoordinator` is content for `data` to exist before a
        # refresh, and `last_update_success` is what keeps those entities
        # unavailable until the device really answers.
        #
        # A device that has never answered has nothing remembered, and there is
        # no way around that: nothing has ever observed what it has. Only the
        # pushed event entities, which never consult the coordinator, come up in
        # that case - exactly as they always did.
        if (seed := cached_snapshot(entry.data.get(CONF_DEVICE_CACHE))) is not None:
            self.data = seed
            self._cached_signature = capability_signature(seed)

    @callback
    def async_request_full_refresh(self) -> None:
        """Ask for settings and actions on the next refresh, not just state."""
        self._force_full = True

    @callback
    def async_keep_polling_without_entities(self) -> CALLBACK_TYPE:
        """Poll on with nothing listening, and reload once the device answers.

        A device that has never answered has nothing remembered, so every polled
        platform creates nothing and no ``CoordinatorEntity`` is ever added.
        ``DataUpdateCoordinator`` arms its interval only while something is
        listening, so without this the entry stays inert for good: it never
        polls, so automatic callbacks are never healed and the entities never
        arrive however long the device has been back, until somebody reloads the
        entry by hand. Setup deliberately succeeds while the device is down, so
        Home Assistant will not retry it either.

        Registering a listener is deliberately the whole mechanism. A timer of
        our own would poll a device that *does* have entities a second time on
        every interval; a listener shares the single interval the coordinator
        already keeps, and it is only ever registered when there are no entities
        to keep it.

        Polling alone would still not produce the entities, because platform
        setup has already run and nothing runs it again - so the first answer
        reloads the entry. Setup refreshes before it forwards the platforms, so
        by then they see a live snapshot and create everything the device turned
        out to have. Stopping first makes this a one-shot: the reloaded entry
        either has entities of its own or lands right back here.

        Returns the callback that stops it, for the caller to hand to the config
        entry so that an unload takes it down with everything else.
        """
        remove: CALLBACK_TYPE | None = None

        @callback
        def _async_stop() -> None:
            """Stop watching, at most once: removing a listener twice raises."""
            nonlocal remove
            if remove is None:
                return
            unsubscribe, remove = remove, None
            unsubscribe()

        @callback
        def _async_answered() -> None:
            """Reload the entry as soon as there is something to build from."""
            # Listeners are told about a *failed* refresh too, which is what
            # keeps entities unavailable, so the snapshot is what to check.
            if self.data is None:
                return
            _async_stop()
            self.hass.config_entries.async_schedule_reload(self.config_entry.entry_id)

        remove = self.async_add_listener(_async_answered)
        return _async_stop

    @property
    def settings(self) -> dict[str, Any]:
        """The device's settings, preferring a value just written from here.

        Held by the coordinator rather than by the writing entity because whole
        groups of settings are written as one object: the relay list carries
        every relay, so an entity keeping its own copy would round-trip a
        sibling's pre-write value and revert a change the sibling just made.
        """
        return self._written_or(
            WRITTEN_SETTINGS, self.data.settings if self.data else {}
        )

    @property
    def network(self) -> dict[str, Any]:
        """The device's network state, preferring a value just written from here.

        The access point switch is the only reader today, but it goes through
        the coordinator for the same reason settings do: the device echoes the
        whole network object back from a write, and a poll already in flight
        when that write lands still describes the state from before it.
        """
        return self._written_or(WRITTEN_NETWORK, self.data.network if self.data else {})

    def _written_or(self, name: str, polled: dict[str, Any]) -> dict[str, Any]:
        """Return a payload written from here if one is still fresh, else the poll."""
        written = self._written.get(name)
        return written.payload if written is not None else polled

    @callback
    def async_settings_written(self, settings: dict[str, Any]) -> None:
        """Record the settings a write just produced, for every entity to see."""
        self._written[WRITTEN_SETTINGS] = _WrittenPayload(settings, time.monotonic())

    @callback
    def async_network_written(self, network: dict[str, Any]) -> None:
        """Record the network state a write just produced."""
        self._written[WRITTEN_NETWORK] = _WrittenPayload(network, time.monotonic())

    @callback
    def _async_expire_written(self) -> None:
        """Forget written payloads once a poll can be trusted to include them."""
        for name, written in list(self._written.items()):
            if written.expired():
                del self._written[name]
            else:
                _LOGGER.debug(
                    "%s: keeping just-written %s over an in-flight poll",
                    self.config_entry.title,
                    name,
                )

    async def _async_update_data(self) -> DeviceSnapshot:
        """Fetch relay state every cycle, everything else occasionally."""
        previous = self.data or DeviceSnapshot()
        # Taken once, and taken before the requests, so a cycle's cost does not
        # push the next one out and the cadence stays on the poll grid.
        now = time.monotonic()
        full = (
            self._force_full
            or self._last_full is None
            or now - self._last_full >= SLOW_REFRESH_SECONDS
        )

        try:
            state = await self.manager.async_get_extended_state()
        except BleBoxError as err:
            # Nothing has been consumed at this point, deliberately: a single
            # timed-out poll must not cancel a full refresh an entity asked for
            # after writing a setting, nor push the slow cycle out by one.
            raise UpdateFailed(f"Could not reach the device: {err}") from err

        if not full:
            self._async_expire_written()
            return replace(previous, state=state)

        try:
            info = await self.manager.async_get_device_info()
        except BleBoxError as err:
            raise UpdateFailed(f"Could not reach the device: {err}") from err

        actions: ActionsState | None = None
        try:
            actions = await self.manager.async_get_actions_state()
        except BleBoxError as err:
            _LOGGER.debug("Action state unavailable on %s: %s", info.name, err)

        # Best-effort: a device that answers its identity but not these still
        # delivers events, so a failure here must not fail the whole update.
        #
        # What a failure must not do either is publish an empty payload as live
        # state. The update still succeeds, so every entity stays *available*
        # and simply reports the wrong thing: the cloud tunnel, the backlight
        # and the access point all read as off, the overload threshold and the
        # restart select go unknown, and the access point blanks its SSID. Home
        # Assistant records each of those as a genuine state change, and the
        # relay-only polls in between carry it forward until the next slow
        # cycle, so an automation watching the cloud tunnel fires on nothing at
        # all. The payload the device last gave is still the best description of
        # it, so that is what a failed read carries forward - only ever on
        # failure, so a device that really did answer with an empty object is
        # still believed.
        settings: dict[str, Any]
        try:
            settings = await self.manager.async_get_settings()
        except BleBoxError as err:
            settings = previous.settings
            _LOGGER.debug("Settings unavailable on %s: %s", info.name, err)

        network: dict[str, Any]
        try:
            network = await self.manager.async_get_network()
        except BleBoxError as err:
            network = previous.network
            _LOGGER.debug("Network state unavailable on %s: %s", info.name, err)

        uptime = await self.manager.async_get_uptime()

        await self._async_heal(actions)
        health = callback_health(actions)
        self._async_update_issues(health)

        # Only now that the slow fetches have actually landed: anything raising
        # above leaves the request pending so the next poll picks it up, and
        # leaves the cadence measured from the last cycle that really happened.
        self._force_full = False
        self._last_full = now
        self._async_expire_written()
        snapshot = DeviceSnapshot(
            info=info,
            actions=actions,
            settings=settings,
            state=state,
            uptime_s=uptime,
            network=network,
            health=health,
        )

        # A fetch that failed is not evidence that a capability went away, but
        # it no longer has to block this either: the best-effort reads above
        # carry the last payload forward, so the snapshot describes the same
        # capabilities the last successful read did. Demanding that all of them
        # succeed instead meant firmware without ``/api/device/network`` never
        # had its shape remembered at all, and a device that has never been
        # remembered comes up with no entities at all when it is unreachable at
        # startup. Uptime is the one read still worth guarding: it is
        # best-effort inside the API layer itself and answers ``None`` either
        # way, so a device that has reported one before has to keep reporting it
        # to count as having answered.
        if uptime is not None or previous.uptime_s is None:
            self._async_remember_capabilities(snapshot)
        return snapshot

    @callback
    def _async_remember_capabilities(self, snapshot: DeviceSnapshot) -> None:
        """Store this device's shape, unless it is the shape already stored.

        Compared by signature and not by payload, because the payloads carry
        values that move on every poll: writing whenever one of those changed
        would rewrite the config entry, and with it Home Assistant's
        ``.storage``, every few seconds for as long as the integration ran.
        """
        signature = capability_signature(snapshot)
        if signature == self._cached_signature:
            return
        self._cached_signature = signature
        _LOGGER.debug(
            "%s: remembering the device's capability shape",
            self.config_entry.title,
        )
        self.hass.config_entries.async_update_entry(
            self.config_entry,
            data={
                **self.config_entry.data,
                CONF_DEVICE_CACHE: {
                    CACHE_INFO: (
                        device_payload(snapshot.info) if snapshot.info else None
                    ),
                    CACHE_SETTINGS: snapshot.settings,
                    CACHE_STATE: snapshot.state,
                    CACHE_NETWORK: snapshot.network,
                    CACHE_UPTIME: snapshot.uptime_s,
                },
            },
        )

    @callback
    def _async_update_issues(self, health: CallbackHealth) -> None:
        """Raise or clear repairs describing why callbacks are not arriving.

        These are the two failures that otherwise present identically - nothing
        happens when you press the switch - and they need opposite fixes.
        """
        name = self.config_entry.title
        unreachable_key, rejected_key = issue_keys(self.config_entry.entry_id)

        unreachable = health.problem and health.unreachable > 0
        rejected = health.problem and health.rejected > 0 and not unreachable

        for key, active, placeholders in (
            (
                unreachable_key,
                unreachable,
                {"name": name, "host": self.manager.base_url},
            ),
            (
                rejected_key,
                rejected,
                {"name": name, "status": str(health.last_status)},
            ),
        ):
            if active:
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    key,
                    is_fixable=False,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key=key.rsplit("_", 1)[0],
                    translation_placeholders=placeholders,
                )
            else:
                ir.async_delete_issue(self.hass, DOMAIN, key)

    async def _async_heal(self, actions: ActionsState | None) -> None:
        """Restore our callbacks if the device no longer has them.

        The polled ``actions`` decide *whether* to provision and supply the URL
        placeholder list; the writer re-reads the slots itself before touching
        any of them.
        """
        entry = self.config_entry
        data = getattr(entry, "runtime_data", None)
        if data is None or data.mode != MODE_AUTOMATIC or actions is None:
            return
        if not data.provisioning.attempted:
            # First refresh runs before initial provisioning; setting the
            # callbacks up is that step's job, not a drift repair.
            return

        base_url = data.base_url or async_default_base_url(self.hass)
        if not base_url:
            return
        desired = build_desired_actions(
            data.enabled_events,
            data.token,
            base_url,
            invert_edges=data.invert_edges,
            placeholders=actions.param_placeholders(),
        )
        present = {
            (action.get("input"), action.get("triggerType"), action.get("param"))
            for action in actions.actions
            if is_owned(action)
        }
        missing = [
            item
            for item in desired
            if (item.input_id, item.trigger_type, item.url) not in present
        ]
        if not missing:
            # Nothing to repair, so nothing to hold against the next repair: a
            # problem arising later is a new one and gets tried at once rather
            # than at the tail of a backoff earned by an older failure.
            self._heal_attempt = None
            self._heal_failures = 0
            return

        # Keyed on what this attempt would do rather than on the failure it hit:
        # the callbacks that are absent, and the slot layout they have to fit
        # into. The user freeing a slot in the wBox app - the fix the error
        # message asks for - changes that key.
        attempt = (
            tuple((item.input_id, item.trigger_type, item.url) for item in missing),
            tuple(
                (
                    action.get("triggerType"),
                    action.get("actionType"),
                    action.get("param"),
                )
                for action in actions.actions
            ),
        )
        if not self._async_heal_due(attempt):
            return

        _LOGGER.info(
            "%s: %s configured callback(s) missing from the device, restoring",
            data.device_name,
            len(missing),
        )
        await async_apply_provisioning(self.hass, entry, state=actions)
        if data.provisioning.error is None:
            self._heal_failures = 0
        else:
            # Bounded, because it is an exponent below and an entry can stay
            # loaded for months.
            self._heal_failures = min(self._heal_failures + 1, HEAL_BACKOFF_MAX_CYCLES)

    @callback
    def _async_heal_due(self, attempt: tuple[Any, ...]) -> bool:
        """Whether to make this repair attempt now, given how the last ones went.

        A repair that cannot succeed - most often a device with no free action
        slot left, which only the user can clear - would otherwise be retried
        every cycle for as long as the entry is loaded, costing a request and an
        error log line a minute and fixing nothing. So consecutive failures at
        the same attempt back off exponentially, up to `HEAL_BACKOFF_MAX_CYCLES`.

        The backoff never outlives the situation it was earned in: an attempt
        that differs from the last one, in either the callbacks it would write
        or the slot layout it would write them into, starts from zero.
        """
        if attempt != self._heal_attempt:
            self._heal_attempt = attempt
            self._heal_failures = 0
            self._heal_skipped = 0
            return True
        if not self._heal_failures:
            return True

        due_after = min(2 ** (self._heal_failures - 1), HEAL_BACKOFF_MAX_CYCLES)
        if self._heal_skipped < due_after:
            self._heal_skipped += 1
            return False
        self._heal_skipped = 0
        return True
