"""Firmware updates.

The device reports the version it is running and the newest one it knows about,
and can be told to fetch and install it. It pulls the image over BleBox's own
tunnel, so an update cannot start while the cloud tunnel switch is off.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.update import (
    UpdateDeviceClass,
    UpdateEntity,
    UpdateEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .blebox_actions import BleBoxError
from .const import DOMAIN, SETTING_TUNNEL
from .coordinator import BleBoxEventsConfigEntry
from .entity import BleBoxDeviceEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BleBoxEventsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the firmware update entity."""
    snapshot = entry.runtime_data.coordinator.data
    if not snapshot or snapshot.info is None:
        return
    async_add_entities([BleBoxFirmwareUpdate(entry)])


class BleBoxFirmwareUpdate(BleBoxDeviceEntity, UpdateEntity):
    """Firmware version reported by the device."""

    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_supported_features = UpdateEntityFeature.INSTALL

    def __init__(self, entry: BleBoxEventsConfigEntry) -> None:
        """Initialise the update entity."""
        super().__init__(entry, "firmware")

    @property
    def installed_version(self) -> str | None:
        """Firmware currently running."""
        snapshot = self.coordinator.data
        return snapshot.info.firmware_version if snapshot and snapshot.info else None

    @property
    def latest_version(self) -> str | None:
        """Newest firmware the device knows about.

        The device reports ``availableFv`` as null when it has nothing newer, in
        which case the installed version is the latest.
        """
        snapshot = self.coordinator.data
        if not snapshot or snapshot.info is None:
            return None
        available = snapshot.info.raw.get("availableFv")
        return str(available) if available else self.installed_version

    async def async_install(
        self, version: str | None, backup: bool, **kwargs: Any
    ) -> None:
        """Tell the device to fetch and install the newer firmware."""
        if not self.setting(SETTING_TUNNEL, "enabled"):
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="update_needs_tunnel"
            )
        try:
            await self._data.manager.async_install_firmware()
        except BleBoxError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="update_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        # The device reboots into the new image; the next poll picks it up.
        self.coordinator.async_request_full_refresh()
        await self.coordinator.async_request_refresh()
