"""The BleBox Advanced integration.

Adds physical button/input events for BleBox-based devices to Home Assistant.
The official ``blebox`` integration keeps full ownership of relay, power and
energy entities - this integration only supplies the input events it does not
cover, and attaches them to the very same device.
"""

from __future__ import annotations

import logging

from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import async_register_token, async_setup_callback_view, async_unregister_token
from .blebox_actions import BleBoxActionManager, BleBoxError
from .const import (
    CONF_BASE_URL,
    CONF_BLEBOX_ID,
    CONF_CALLBACK_TOKEN,
    CONF_CLEANUP_ON_REMOVE,
    CONF_DEBOUNCE_MS,
    CONF_INPUTS,
    CONF_INVERT_EDGES,
    CONF_MANAGE_BUTTONS,
    CONF_MODE,
    CONF_SUPPORTS_ACTIONS,
    DEFAULT_DEBOUNCE_MS,
    DEFAULT_PORT,
    DOMAIN,
    MODE_MANUAL,
)
from .coordinator import (
    BleBoxEventsConfigEntry,
    BleBoxEventsCoordinator,
    BleBoxEventsData,
    async_apply_provisioning,
    enabled_events_from_entry,
    issue_keys,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.EVENT,
    Platform.LIGHT,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.UPDATE,
]


async def async_setup_entry(
    hass: HomeAssistant, entry: BleBoxEventsConfigEntry
) -> bool:
    """Set up one BleBox device's event support."""
    manager = BleBoxActionManager(
        async_get_clientsession(hass),
        entry.data[CONF_HOST],
        entry.data.get(CONF_PORT, DEFAULT_PORT),
    )
    coordinator = BleBoxEventsCoordinator(hass, entry, manager)
    options = entry.options

    data = BleBoxEventsData(
        manager=manager,
        coordinator=coordinator,
        blebox_id=entry.data[CONF_BLEBOX_ID],
        device_name=entry.title,
        token=entry.data[CONF_CALLBACK_TOKEN],
        inputs=[int(index) for index in entry.data.get(CONF_INPUTS, [])],
        enabled_events=enabled_events_from_entry(entry),
        mode=options.get(CONF_MODE, MODE_MANUAL),
        debounce=max(0, int(options.get(CONF_DEBOUNCE_MS, DEFAULT_DEBOUNCE_MS))) / 1000,
        invert_edges=bool(options.get(CONF_INVERT_EDGES, False)),
        base_url=options.get(CONF_BASE_URL) or None,
        manage_buttons=bool(options.get(CONF_MANAGE_BUTTONS, False)),
        options_snapshot=dict(options),
    )
    data.provisioning.supported = bool(entry.data.get(CONF_SUPPORTS_ACTIONS))
    entry.runtime_data = data

    async_setup_callback_view(hass)
    async_register_token(hass, data.token, entry.entry_id)

    # Setup deliberately does not depend on the device answering. A device that
    # is asleep, moved or on a temporarily unreachable VLAN must not remove the
    # event entities, and manually configured callbacks keep arriving.
    await coordinator.async_refresh()

    snapshot = coordinator.data
    await async_apply_provisioning(
        hass, entry, state=snapshot.actions if snapshot else None
    )

    if snapshot is None:
        # Nothing has ever been observed on this device - it is not answering
        # now and it has never been remembered - so every polled platform below
        # creates nothing, and with no entity listening the coordinator would
        # stop polling and never notice the device coming back.
        entry.async_on_unload(coordinator.async_keep_polling_without_entities())

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: BleBoxEventsConfigEntry
) -> bool:
    """Unload a config entry.

    Device-side actions are intentionally left in place: unloading also happens
    on every reload, and a reload must never disturb device configuration.
    Cleanup belongs to :func:`async_remove_entry`.

    Repair issues are left alone here for the same reason. Clearing them would
    make a genuine, still-true warning disappear from the repairs dashboard on
    every reload and come back a moment later; the coordinator already clears
    one the instant the device reports its callbacks arriving again.
    """
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        async_unregister_token(hass, entry.data[CONF_CALLBACK_TOKEN])
    return unloaded


async def async_remove_entry(
    hass: HomeAssistant, entry: BleBoxEventsConfigEntry
) -> None:
    """Remove this integration's own actions from the device, and only those.

    Anything the user configured themselves is left alone. If the device cannot
    be reached, the actions are left in place and the user is told - silently
    guessing at device configuration would be worse than a stale action.

    Whatever happens on the device, the entry's repair issues go: an entry that
    no longer exists can neither raise nor resolve them, so one left behind is a
    permanent warning about a device Home Assistant has forgotten, with no way
    to dismiss it.
    """
    # Before anything that can return early, for exactly that reason.
    for key in issue_keys(entry.entry_id):
        ir.async_delete_issue(hass, DOMAIN, key)

    if not entry.options.get(CONF_CLEANUP_ON_REMOVE, True):
        return

    manager = BleBoxActionManager(
        async_get_clientsession(hass),
        entry.data[CONF_HOST],
        entry.data.get(CONF_PORT, DEFAULT_PORT),
    )
    try:
        cleared = await manager.async_remove_owned_actions()
    except BleBoxError as err:
        _LOGGER.warning(
            "Could not remove callback actions from %s (%s). They are still on "
            "the device and can be deleted in the wBox app",
            entry.title,
            err,
        )
        return

    if cleared:
        _LOGGER.info("Removed %s callback action(s) from %s", len(cleared), entry.title)


async def _async_options_updated(
    hass: HomeAssistant, entry: BleBoxEventsConfigEntry
) -> None:
    """Reload the entry so option changes are re-provisioned and re-applied.

    Home Assistant fires this for *any* change to the entry, and the coordinator
    also writes the device's remembered capability shape into ``entry.data``.
    Reloading for that would restart the entry and re-provision the device
    because a poll noticed the device had gained a setting, so what actually
    changed is checked first.
    """
    data = getattr(entry, "runtime_data", None)
    if data is not None and dict(entry.options) == data.options_snapshot:
        return
    await hass.config_entries.async_reload(entry.entry_id)
