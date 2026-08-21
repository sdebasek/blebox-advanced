"""The relay, and the configuration switches around it.

Three kinds of switch live here: one per relay, which this integration
publishes because it replaces the official one rather than adding to it; the
plain on/off device settings; and the device's own WiFi access point.

Of the settings the cloud tunnel matters most: BleBox devices hold an outbound
tunnel to BleBox's cloud by default, and turning it off is the difference
between a genuinely local device and one that merely happens to be controlled
locally.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .blebox_actions import (
    ACTION_RELAY_OFF,
    ACTION_RELAY_ON,
    ACTION_RELAY_TOGGLE,
    find_native_action,
    trigger_type_for_event,
)
from .const import (
    SETTING_STATUS_LED,
    SETTING_TUNNEL,
    SIGNAL_INPUT_EVENT,
)
from .coordinator import BleBoxEventsConfigEntry, relay_list
from .entity import BleBoxDeviceEntity, BleBoxRelayEntity

_LOGGER = logging.getLogger(__name__)

COMMAND_SETTLE_S = 5.0
"""How long a just-commanded state outranks a contradicting observation.

A poll or state report already in flight when a command lands carries the state
from *before* it, so accepting it would leave the entity showing the opposite of
reality until the next poll. Within this window a disagreeing observation is
treated as stale; past it, the device is believed - so a relay changed at the
wall, or a command that silently failed, still corrects itself.
"""

SWITCHES: list[tuple[str, str]] = [
    ("cloud_tunnel", SETTING_TUNNEL),
    ("status_led", SETTING_STATUS_LED),
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BleBoxEventsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up whichever configuration switches this device reports."""
    snapshot = entry.runtime_data.coordinator.data
    if not snapshot:
        return
    entities: list[BleBoxDeviceEntity] = [
        BleBoxSettingSwitch(entry, key, setting)
        for key, setting in SWITCHES
        if isinstance(snapshot.settings.get(setting), dict)
    ]

    if "apEnable" in snapshot.network:
        entities.append(BleBoxAccessPointSwitch(entry))

    relays = relay_list(snapshot.settings)
    entities.extend(
        BleBoxRelaySwitch(entry, index, multi=len(relays) > 1)
        for index in range(len(relays))
    )

    async_add_entities(entities)


class BleBoxSettingSwitch(BleBoxDeviceEntity, SwitchEntity):
    """A device setting that is simply on or off."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry: BleBoxEventsConfigEntry, key: str, setting: str) -> None:
        """Initialise the switch for one settings key."""
        super().__init__(entry, key)
        self._setting = setting

    @property
    def is_on(self) -> bool:
        """Whether the setting is enabled."""
        return bool(self.setting(self._setting, "enabled"))

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable the setting."""
        await self.async_patch_settings({self._setting: {"enabled": 1}})

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable the setting."""
        await self.async_patch_settings({self._setting: {"enabled": 0}})


class BleBoxRelaySwitch(BleBoxRelayEntity, SwitchEntity):
    """The device's relay.

    Polled every 5 seconds, matching what the official integration used. The
    hardware offers no trigger that fires when the relay moves, so there is no
    push path for the relay itself; instead a just-issued command outranks a
    contradicting poll for a short settle window.

    A press on a button the device binds to this relay is the one thing that
    does arrive instantly, as a callback. That says nothing about the relay
    directly, but the binding it fires is known, so the resulting state is
    predicted from it rather than waited for.
    """

    _attr_device_class = SwitchDeviceClass.SWITCH

    def __init__(
        self, entry: BleBoxEventsConfigEntry, index: int, *, multi: bool
    ) -> None:
        """Initialise the switch for one relay."""
        super().__init__(
            entry, "relay", index, multi=multi, numbered_key="relay_numbered"
        )
        self._state: bool | None = None
        self._commanded_at = 0.0
        self._command_lock = asyncio.Lock()

    async def async_added_to_hass(self) -> None:
        """Subscribe to polls, and to the presses the device pushes.

        The base class wires up the coordinator; the dispatcher subscription is
        what lets a button wired to this relay move it without waiting up to a
        poll interval for anyone to notice.
        """
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_INPUT_EVENT.format(self._entry.entry_id),
                self._async_handle_input_event,
            )
        )

    @callback
    def _async_handle_input_event(
        self, input_id: int, event_type: str, hints: dict[str, Any] | None = None
    ) -> None:
        """Show what a press on a bound button just did to this relay.

        Nothing the callback carries is used. The one placeholder the hardware
        offers for relay state was measured to substitute a constant, so the
        press tells us *that* something happened and the device's own action
        slot tells us *what* - see ``docs/device-api.md`` under "URL
        placeholders".
        """
        predicted = self._predicted_state(input_id, event_type)
        if predicted is None:
            return

        # A prediction deliberately does not arm the settle window a command
        # arms, because the two are not the same kind of claim. A command is
        # something we did and the device confirmed, so it may outrank a poll
        # that was already in flight and still describes the world before it. A
        # prediction is an inference from the binding the device last reported,
        # and it is wrong whenever that reading is stale, whenever the slot was
        # edited in the wBox app since, or whenever the press never reached the
        # relay. Defending a wrong prediction for five seconds would turn a
        # 5-second lag into 5 seconds of confidently showing the opposite, so
        # the very next poll gets to overrule it instead.
        #
        # Any window an earlier command left open is cleared for the same
        # reason: the relay has since moved for a reason that command knows
        # nothing about, so that command no longer describes reality either.
        self._state = predicted
        self._commanded_at = 0.0
        self.async_write_ha_state()

        # Ask the device to confirm within one round trip rather than at the
        # next scheduled poll. Tied to the config entry so unloading cancels it.
        self._entry.async_create_task(
            self.hass, self.coordinator.async_request_refresh()
        )

    def _predicted_state(self, input_id: int, event_type: str) -> bool | None:
        """Return the state the action bound to this press will have left behind.

        ``None`` means "do nothing", which covers both "this press cannot move
        this relay" and "it can, but the result is not knowable".
        """
        snapshot = self.coordinator.data
        if snapshot is None or snapshot.actions is None:
            # Nothing has read the slot table yet, so there is no binding to
            # reason from. An offline device that has never answered lands here.
            return None

        # `invert_edges` matters for the same reason provisioning honours it: it
        # decides which electrical edge our callback for `press` was written
        # against, and that edge is what the device fired. Reading it the other
        # way round on an inverted input would look up the wrong slot and either
        # predict nothing or predict a neighbouring binding's action. Short and
        # long clicks are not edges, so they map identically either way.
        try:
            trigger = trigger_type_for_event(
                event_type, invert_edges=self._data.invert_edges
            )
        except ValueError:
            # The endpoint only dispatches event types it has validated, so this
            # guards the two lists drifting apart rather than device input. An
            # exception raised inside a dispatcher callback would be logged as a
            # bug in the integration, which this is not worth being.
            return None

        action = find_native_action(snapshot.actions, input_id, trigger)
        if action is None or not self._targets_this_relay(action):
            return None

        action_type = action.get("actionType")
        if action_type == ACTION_RELAY_ON:
            return True
        if action_type == ACTION_RELAY_OFF:
            return False
        if action_type == ACTION_RELAY_TOGGLE:
            # Inverting a state we do not have is a coin flip, and being wrong
            # here costs more than the lag it would save.
            return None if self.is_on is None else not self.is_on
        # ACTION_UNCONFIGURED, or one of the types the device offers that are
        # not identified: the press does something, but not something this
        # entity can claim to know the outcome of.
        return None

    def _targets_this_relay(self, action: dict[str, Any]) -> bool:
        """Whether an action slot drives this relay rather than a sibling.

        Some hardware revisions carry a ``relay`` index on the slot - every
        captured payload from the Simon 55 GO does, always ``0`` - and on a
        multi-relay device that index is the only thing separating a button
        bound to relay 1 from one bound to relay 2.

        A slot that does not say is read as relay 0. Firmware that omits the
        field has one relay to talk about, and on a device that somehow both
        omits it and has several, relay 0 predicting alone is the conservative
        reading: at worst one switch is briefly optimistic, where treating the
        slot as targeting all of them would move every switch on the device.
        """
        relay = action.get("relay")
        return (relay if isinstance(relay, int) else 0) == self._index

    @callback
    def _handle_coordinator_update(self) -> None:
        """Fold a fresh poll into what we believe."""
        self._async_observe(self._polled_state())
        super()._handle_coordinator_update()

    @callback
    def _async_observe(self, value: bool | None) -> None:
        """Accept an observed state unless it contradicts a recent command."""
        if value is None:
            return
        if (
            self._state is not None
            and value != self._state
            and time.monotonic() - self._commanded_at < COMMAND_SETTLE_S
        ):
            _LOGGER.debug(
                "%s: ignoring stale relay state %s, commanded %s",
                self._data.device_name,
                value,
                self._state,
            )
            return
        self._state = value
        self._commanded_at = 0.0

    def _polled_state(self) -> bool | None:
        """Return this relay's state as of the last poll."""
        state = self.relay_field(self.device_state, "state")
        return bool(state) if isinstance(state, int) else None

    @property
    def is_on(self) -> bool | None:
        """Whether the relay is closed."""
        return self._state if self._state is not None else self._polled_state()

    async def _async_set(self, on: bool) -> None:
        """Command the relay, trusting the device's own answer.

        Serialised per entity so that toggling faster than the round-trip can
        take cannot land the two commands out of order.
        """
        async with self._command_lock:
            with self.write_errors():
                confirmed = await self._data.manager.async_set_relay(self._index, on)
            self._state = on if confirmed is None else confirmed
            self._commanded_at = time.monotonic()
            self.async_write_ha_state()
        await self.coordinator.async_request_refresh()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Close the relay."""
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Open the relay."""
        await self._async_set(False)


class BleBoxAccessPointSwitch(BleBoxDeviceEntity, SwitchEntity):
    """The device's own WiFi access point.

    BleBox devices keep an access point running after setup, often unprotected,
    which is one more way onto the network than most installations want.
    """

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry: BleBoxEventsConfigEntry) -> None:
        """Initialise the access point switch."""
        super().__init__(entry, "access_point")

    def _network(self) -> dict[str, Any]:
        """Return the device's network state, preferring one we just wrote."""
        return self.coordinator.network

    @property
    def is_on(self) -> bool:
        """Whether the device is broadcasting its access point."""
        return bool(self._network().get("apEnable"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the access point name and whether it is protected."""
        network = self._network()
        return {
            "ssid": network.get("apSSID"),
            "protected": bool(network.get("apPasswd")),
        }

    async def _async_set(self, enabled: bool) -> None:
        """Command the access point, showing the result without waiting for a poll.

        The device answers the write with its whole network object, so that
        answer is used directly and the state is published straight away.

        Without this the toggle sprang back. Nothing here wrote a state, and the
        network object is only re-read on the coordinator's slow cycle, so Home
        Assistant went on reporting the old value until a full refresh landed
        and the frontend reverted the toggle in the meantime.

        The wrap covers the read too: the SSID and password are round-tripped
        from ``/api/device/network``, so a device that cannot be reached fails
        before anything is written.
        """
        with self.write_errors():
            returned = await self._data.manager.async_set_ap_enabled(enabled)
        self.coordinator.async_network_written(
            returned if returned else {**self._network(), "apEnable": enabled}
        )
        self.async_write_ha_state()
        self.coordinator.async_request_full_refresh()
        await self.coordinator.async_request_refresh()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Start broadcasting the access point."""
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Stop broadcasting the access point."""
        await self._async_set(False)
