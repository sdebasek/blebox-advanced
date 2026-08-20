"""Shared device-registry wiring and entity base.

Everything except the event entities is polled state read from the device's
settings and ``/state/extended``. Those entities share this base; the event
entities deliberately do not, because they are push-driven and must keep
recording presses even while the device is unreachable for polling.
"""

from __future__ import annotations

from typing import Any

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    BLEBOX_DOMAIN,
    CONF_HW_VERSION,
    CONF_MODEL,
    CONF_SW_VERSION,
    MANUFACTURER,
)
from .coordinator import (
    BleBoxEventsConfigEntry,
    BleBoxEventsCoordinator,
    BleBoxEventsData,
)

MAC_LENGTH = 12
HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


def mac_connection(blebox_id: str) -> str | None:
    """Return the device id as a MAC connection, if it looks like one.

    BleBox device ids are the MAC address without separators. Verified rather
    than assumed, so a device that numbers itself some other way does not end up
    advertising a nonsense connection.
    """
    if len(blebox_id) != MAC_LENGTH or any(c not in HEX_DIGITS for c in blebox_id):
        return None
    return dr.format_mac(blebox_id)


def build_device_info(
    entry: BleBoxEventsConfigEntry, data: BleBoxEventsData
) -> DeviceInfo:
    """Return device info that links to the official BleBox device.

    Home Assistant gives every config entry its own device registry entry and
    links entries sharing an identifier or a connection. Claiming the BleBox
    device id under the *official* integration's domain — exactly the identifier
    ``blebox`` uses — is what associates our entities with its relay, power and
    energy entities, whichever integration is set up first. The MAC connection
    is advertised too, so the link survives if the official integration ever
    changes how it builds identifiers.
    """
    device_info = DeviceInfo(
        identifiers={(BLEBOX_DOMAIN, data.blebox_id)},
        manufacturer=MANUFACTURER,
        name=entry.title,
        model=entry.data.get(CONF_MODEL) or None,
        sw_version=entry.data.get(CONF_SW_VERSION) or None,
        hw_version=entry.data.get(CONF_HW_VERSION) or None,
        configuration_url=data.manager.base_url,
    )
    if (mac := mac_connection(data.blebox_id)) is not None:
        device_info["connections"] = {(dr.CONNECTION_NETWORK_MAC, mac)}
    return device_info


class BleBoxDeviceEntity(CoordinatorEntity[BleBoxEventsCoordinator]):
    """Base for entities backed by polled device settings or state."""

    _attr_has_entity_name = True

    def __init__(self, entry: BleBoxEventsConfigEntry, key: str) -> None:
        """Initialise the entity for one device capability."""
        data = entry.runtime_data
        super().__init__(data.coordinator)
        self._entry = entry
        self._data = data
        self._attr_unique_id = f"{data.blebox_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = build_device_info(entry, data)

    @property
    def settings(self) -> dict[str, Any]:
        """The device's settings object as last seen."""
        snapshot = self.coordinator.data
        return snapshot.settings if snapshot else {}

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

    async def async_patch_settings(self, patch: dict[str, Any]) -> None:
        """Apply a settings patch and refresh so the UI reflects the device."""
        await self._data.manager.async_set_settings(patch)
        await self.coordinator.async_request_refresh()
