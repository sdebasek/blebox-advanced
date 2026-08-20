"""Runtime state and the (slow) device metadata coordinator.

Input events are **pushed** by the device — see :mod:`api`. Nothing here polls
for them, and a press is never inferred from relay state, which would be both
laggy and wrong (the relay also moves when Home Assistant switches it, inputs
can be detached from the relay entirely, and a long press cannot be
distinguished this way at all).

What the coordinator does do, on a deliberately lazy interval:

* refresh device identity so firmware/hardware changes show up; and
* in automatic mode, notice that our callbacks have disappeared from the
  device (edited away in the wBox app, factory reset, restored backup) and put
  them back.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import async_default_base_url, build_desired_actions, build_state_action
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
    CONF_ENABLED_EVENTS,
    DOMAIN,
    MODE_AUTOMATIC,
    SCAN_INTERVAL_SECONDS,
)

_LOGGER = logging.getLogger(__name__)


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
            # No HTTP response at all — routing or firewall.
            health.unreachable += 1
    return health


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
    push_relay_state: bool = False
    push_interval_s: int = 0
    ha_device_id: str | None = None
    last_event: dict[tuple[int, str], float] = field(default_factory=dict)
    provisioning: ProvisioningStatus = field(default_factory=ProvisioningStatus)


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
        # Fetched up front so the URLs can carry whatever placeholders this
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
    if data.push_relay_state and data.push_interval_s:
        desired.append(
            build_state_action(
                data.token,
                base_url,
                data.push_interval_s,
                placeholders=placeholders,
            )
        )
    return await data.manager.async_sync_http_actions(desired, state=state)


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
        status.error = str(err)
        status.result = None
        _LOGGER.error(
            "%s: %s. Free some action slots in the wBox app, or switch this "
            "integration to manual mode",
            data.device_name,
            err,
        )
    except BleBoxError as err:
        status.error = str(err)
        status.result = None
        _LOGGER.warning(
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

    async def _async_update_data(self) -> DeviceSnapshot:
        """Fetch device identity, then reconcile callbacks if needed."""
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
        settings: dict[str, Any] = {}
        try:
            settings = await self.manager.async_get_settings()
        except BleBoxError as err:
            _LOGGER.debug("Settings unavailable on %s: %s", info.name, err)

        state: dict[str, Any] = {}
        try:
            state = await self.manager.async_get_extended_state()
        except BleBoxError as err:
            _LOGGER.debug("Extended state unavailable on %s: %s", info.name, err)

        uptime = await self.manager.async_get_uptime()

        await self._async_heal(actions)
        health = callback_health(actions)
        self._async_update_issues(health)
        return DeviceSnapshot(
            info=info,
            actions=actions,
            settings=settings,
            state=state,
            uptime_s=uptime,
            health=health,
        )

    @callback
    def _async_update_issues(self, health: CallbackHealth) -> None:
        """Raise or clear repairs describing why callbacks are not arriving.

        These are the two failures that otherwise present identically — nothing
        happens when you press the switch — and they need opposite fixes.
        """
        entry_id = self.config_entry.entry_id
        name = self.config_entry.title

        unreachable = health.problem and health.unreachable > 0
        rejected = health.problem and health.rejected > 0 and not unreachable

        for key, active, placeholders in (
            (
                f"callbacks_unreachable_{entry_id}",
                unreachable,
                {"name": name, "host": self.manager.base_url},
            ),
            (
                f"callbacks_rejected_{entry_id}",
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
        """Restore our callbacks if the device no longer has them."""
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
            return

        _LOGGER.info(
            "%s: %s configured callback(s) missing from the device, restoring",
            data.device_name,
            len(missing),
        )
        await async_apply_provisioning(self.hass, entry, state=actions)
