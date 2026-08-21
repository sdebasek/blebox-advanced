"""Device configuration switches.

Neither of these is exposed by the official integration. The cloud tunnel one
matters most: BleBox devices hold an outbound tunnel to BleBox's cloud by
default, and turning it off is the difference between a genuinely local device
and one that merely happens to be controlled locally.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import SETTING_RELAYS, SETTING_STATUS_LED, SETTING_TUNNEL
from .coordinator import BleBoxEventsConfigEntry
from .entity import BleBoxDeviceEntity

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

    relays = snapshot.settings.get(SETTING_RELAYS)
    if isinstance(relays, list) and relays:
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


class BleBoxRelaySwitch(BleBoxDeviceEntity, SwitchEntity):
    """The device's relay.

    Polled every 5 seconds, matching what the official integration used. The
    hardware offers no trigger that fires when the relay moves, so there is no
    push path to be had; instead a just-issued command outranks a contradicting
    poll for a short settle window.
    """

    _attr_device_class = SwitchDeviceClass.SWITCH

    def __init__(
        self, entry: BleBoxEventsConfigEntry, index: int, *, multi: bool
    ) -> None:
        """Initialise the switch for one relay."""
        super().__init__(
            entry,
            "relay" if index == 0 else f"relay_{index}",
            translation_key="relay_numbered" if multi else "relay",
            placeholders={"relay": str(index + 1)} if multi else None,
        )
        self._index = index
        self._state: bool | None = None
        self._commanded_at = 0.0
        self._command_lock = asyncio.Lock()

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
        relays = self.device_state.get("relays")
        if not isinstance(relays, list) or self._index >= len(relays):
            return None
        relay = relays[self._index]
        if not isinstance(relay, dict):
            return None
        state = relay.get("state")
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
