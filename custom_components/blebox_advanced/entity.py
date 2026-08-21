"""Shared device-registry wiring and entity base.

Everything except the event entities is polled state read from the device's
settings and ``/state/extended``. Those entities share this base; the event
entities deliberately do not, because they are push-driven and must keep
recording presses even while the device is unreachable for polling.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Any

from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.typing import UNDEFINED
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .blebox_actions import BleBoxError, InsufficientSlotsError
from .const import (
    BLEBOX_DOMAIN,
    CONF_HW_VERSION,
    CONF_MODEL,
    CONF_SW_VERSION,
    DOMAIN,
    MANUFACTURER,
)
from .coordinator import (
    BleBoxEventsConfigEntry,
    BleBoxEventsCoordinator,
    BleBoxEventsData,
)

MAC_LENGTH = 12
HEX_DIGITS = frozenset("0123456789abcdefABCDEF")

ERROR_WRITE_FAILED = "device_write_failed"
"""Translation key for a device that could not be reached or refused a write."""

ERROR_NO_FREE_SLOTS = "no_free_action_slots"
"""Translation key for a button rebind that found no free action slot."""


@contextmanager
def device_write_errors(device_name: str) -> Iterator[None]:
    """Re-raise a failed device write as something Home Assistant can present.

    Home Assistant's contract is that a service call reports failure by raising
    :class:`HomeAssistantError`; anything else reaches the user as an unhandled
    traceback in the log plus a red toast that says nothing about which device
    failed or why. A BleBox device is on the far side of a LAN, so a write
    failing is ordinary - unplugged, asleep, on a VLAN that stopped routing -
    and must read as a device problem rather than as a bug in the integration.

    ``InsufficientSlotsError`` is caught first because it is a ``BleBoxError``
    subclass, and it earns its own message: nothing is wrong with the device or
    the network, the user simply has to free an action slot in the wBox app.
    """
    try:
        yield
    except InsufficientSlotsError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key=ERROR_NO_FREE_SLOTS,
            translation_placeholders={"device": device_name, "error": str(err)},
        ) from err
    except BleBoxError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key=ERROR_WRITE_FAILED,
            translation_placeholders={"device": device_name, "error": str(err)},
        ) from err


def mac_connection(blebox_id: str) -> str | None:
    """Return the device id as a MAC connection, if it looks like one.

    BleBox device ids are the MAC address without separators. Verified rather
    than assumed, so a device that numbers itself some other way does not end up
    advertising a nonsense connection.
    """
    if len(blebox_id) != MAC_LENGTH or any(c not in HEX_DIGITS for c in blebox_id):
        return None
    return dr.format_mac(blebox_id)


def deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Merge a settings patch into a settings object, nested dicts included."""
    merged = dict(base)
    for key, value in patch.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def device_identity(
    entry: BleBoxEventsConfigEntry, data: BleBoxEventsData
) -> dict[str, str | None]:
    """Return the model, firmware and hardware version the device reports now.

    Keyed to match both :class:`DeviceInfo` and
    ``device_registry.async_update_device``, because the device page has to be
    told the same three values in two different ways: once when an entity is
    added, and again whenever a poll finds them changed.

    The coordinator re-reads identity on every slow cycle, so its snapshot is
    the live answer and wins. ``entry.data`` is only what the config flow wrote
    at setup and nothing ever updates it, which is why it is the fallback rather
    than the source: it is all a device that has never answered has, and the
    offline-start path depends on those values still producing valid device
    info.

    ``device_type`` and not ``product`` on purpose. The config flow stored the
    device type as the model, so reading the marketing name here instead would
    silently rename the model on every existing device page.
    """
    snapshot = data.coordinator.data
    info = snapshot.info if snapshot else None
    return {
        "model": (info.device_type if info else "")
        or entry.data.get(CONF_MODEL)
        or None,
        "sw_version": (info.firmware_version if info else "")
        or entry.data.get(CONF_SW_VERSION)
        or None,
        "hw_version": (info.hardware_version if info else "")
        or entry.data.get(CONF_HW_VERSION)
        or None,
    }


def build_device_info(
    entry: BleBoxEventsConfigEntry, data: BleBoxEventsData
) -> DeviceInfo:
    """Return device info that links to the official BleBox device.

    Home Assistant gives every config entry its own device registry entry and
    links entries sharing an identifier or a connection. Claiming the BleBox
    device id under the *official* integration's domain - exactly the identifier
    ``blebox`` uses - is what associates our entities with its relay, power and
    energy entities, whichever integration is set up first. The MAC connection
    is advertised too, so the link survives if the official integration ever
    changes how it builds identifiers.
    """
    identity = device_identity(entry, data)
    device_info = DeviceInfo(
        identifiers={(BLEBOX_DOMAIN, data.blebox_id)},
        manufacturer=MANUFACTURER,
        name=entry.title,
        model=identity["model"],
        sw_version=identity["sw_version"],
        hw_version=identity["hw_version"],
        configuration_url=data.manager.base_url,
    )
    if (mac := mac_connection(data.blebox_id)) is not None:
        device_info["connections"] = {(dr.CONNECTION_NETWORK_MAC, mac)}
    return device_info


class BleBoxDeviceEntity(CoordinatorEntity[BleBoxEventsCoordinator]):
    """Base for entities backed by polled device settings or state."""

    _attr_has_entity_name = True

    def __init__(
        self,
        entry: BleBoxEventsConfigEntry,
        key: str,
        *,
        translation_key: str | None = None,
        placeholders: dict[str, str] | None = None,
    ) -> None:
        """Initialise the entity for one device capability.

        ``key`` fixes the unique id and must never change for an existing
        entity; ``translation_key`` can differ so that, say, a second relay gets
        a numbered name without disturbing the first one's identity.
        """
        data = entry.runtime_data
        super().__init__(data.coordinator)
        self._entry = entry
        self._data = data
        self._attr_unique_id = f"{data.blebox_id}_{key}"
        self._attr_translation_key = translation_key or key
        if placeholders:
            self._attr_translation_placeholders = placeholders
        self._attr_device_info = build_device_info(entry, data)
        # What the device page was last told, so an unchanged identity costs a
        # dict comparison rather than a registry lookup on every poll.
        self._identity = device_identity(entry, data)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Write the new state, taking any identity change to the device page."""
        self._async_apply_device_identity()
        super()._handle_coordinator_update()

    @callback
    def _async_apply_device_identity(self) -> None:
        """Push a changed firmware, hardware or model version into the registry.

        Home Assistant reads ``device_info`` once, when the entity is added, so
        without this the device page keeps showing whatever the device reported
        at setup for as long as the entry stays loaded - and firmware version is
        exactly the field a user checks to confirm an update worked, on an
        integration that can start that update itself. Sourcing the values live
        is not enough on its own: nothing re-reads them until a reload.

        The registry row is addressed by id rather than looked up by identifier.
        Our identifier is the *official* integration's (see
        :func:`build_device_info`), and looking one up by identifier finds that
        integration's row instead of ours when both are configured for the same
        device.
        """
        identity = device_identity(self._entry, self._data)
        # Nothing new to say, or no device page to say it to. The identity is
        # recorded as applied only once it really has been, so an entity that
        # somehow has no device row yet tries again on the next poll.
        if identity == self._identity or self.device_entry is None:
            return
        self._identity = identity
        # UNDEFINED rather than None for anything unreported, so firmware that
        # stops sending ``hv`` leaves the hardware version the page already
        # shows alone instead of blanking it.
        dr.async_get(self.hass).async_update_device(
            self.device_entry.id,
            model=identity["model"] or UNDEFINED,
            sw_version=identity["sw_version"] or UNDEFINED,
            hw_version=identity["hw_version"] or UNDEFINED,
        )

    @property
    def settings(self) -> dict[str, Any]:
        """The device's settings, preferring a value any entity just wrote.

        Deliberately the coordinator's copy and not a per-entity one: settings
        are written in whole groups, so two entities editing different fields of
        the same group have to read each other's writes.
        """
        return self.coordinator.settings

    @property
    def device_state(self) -> dict[str, Any]:
        """The device's ``/state/extended`` payload as last seen."""
        snapshot = self.coordinator.data
        return snapshot.state if snapshot else {}

    def setting(self, *path: str) -> Any:
        """Read a nested settings value, or ``None`` if absent."""
        value: Any = self.settings
        for key in path:
            if not isinstance(value, dict):
                return None
            value = value.get(key)
        return value

    def write_errors(self) -> AbstractContextManager[None]:
        """Context manager turning device failures into readable service errors.

        Named after what it does to errors rather than to the write, because a
        write is not the only thing it wraps: a rebind reads the device's slot
        layout first, and that read failing has to reach the user the same way.
        """
        return device_write_errors(self._data.device_name)

    async def async_patch_settings(self, patch: dict[str, Any]) -> None:
        """Apply a settings patch, showing the result without waiting for a poll.

        The device echoes its full settings back from a write, so that answer is
        used directly: it reflects any value the device normalised, and it makes
        the control respond immediately instead of after the refresh debounce.
        The result is handed to the coordinator so the device's other entities
        see it too, and expires there once a poll can be trusted to carry it.

        Every settings-backed control writes through here - the two setting
        switches, the restart-behaviour selects, the backlight and the overload
        threshold - so this is also where a device that refused the write turns
        into a message the user can act on.
        """
        with self.write_errors():
            returned = await self._data.manager.async_set_settings(patch)
        self.coordinator.async_settings_written(
            returned if returned else deep_merge(self.settings, patch)
        )
        self.async_write_ha_state()
        self.coordinator.async_request_full_refresh()
        await self.coordinator.async_request_refresh()
