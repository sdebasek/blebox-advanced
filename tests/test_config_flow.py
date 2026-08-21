"""Config and options flow tests."""

from __future__ import annotations

from contextlib import ExitStack
from ipaddress import ip_address
from typing import Any
from unittest.mock import patch

import pytest
import voluptuous as vol
from homeassistant.config_entries import SOURCE_DHCP, SOURCE_USER, SOURCE_ZEROCONF
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.helpers.network import NoURLAvailableError
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from custom_components.blebox_advanced.blebox_actions import (
    ActionsState,
    BleBoxConnectionError,
)
from custom_components.blebox_advanced.const import (
    CONF_BASE_URL,
    CONF_BLEBOX_ID,
    CONF_CALLBACK_TOKEN,
    CONF_CLEANUP_ON_REMOVE,
    CONF_DEBOUNCE_MS,
    CONF_ENABLED_EVENTS,
    CONF_INPUTS,
    CONF_INVERT_EDGES,
    CONF_MANAGE_BUTTONS,
    CONF_MODE,
    CONF_SUPPORTS_ACTIONS,
    DOMAIN,
    MODE_AUTOMATIC,
    MODE_MANUAL,
)

from .test_integration import (
    BASE_URL,
    BLEBOX_ID,
    DEVICE,
    MANAGER,
    _actions_state,
    _entry,
    _setup,
    _slot,
)

API = "custom_components.blebox_advanced.api"


def _device_patches(actions_state=None, actions_error=None) -> ExitStack:
    """Patch every device read a flow (and the setup that follows) performs."""
    from .test_integration import EXTENDED_STATE, NETWORK, SETTINGS, UPTIME_S

    stack = ExitStack()
    stack.enter_context(patch(f"{MANAGER}.async_get_device_info", return_value=DEVICE))
    if actions_error is not None:
        stack.enter_context(
            patch(f"{MANAGER}.async_get_actions_state", side_effect=actions_error)
        )
    else:
        stack.enter_context(
            patch(
                f"{MANAGER}.async_get_actions_state",
                return_value=actions_state or _actions_state(),
            )
        )
    stack.enter_context(
        patch(f"{MANAGER}.async_get_settings", return_value=dict(SETTINGS))
    )
    stack.enter_context(
        patch(f"{MANAGER}.async_get_extended_state", return_value=dict(EXTENDED_STATE))
    )
    stack.enter_context(patch(f"{MANAGER}.async_get_uptime", return_value=UPTIME_S))
    stack.enter_context(
        patch(f"{MANAGER}.async_get_network", return_value=dict(NETWORK))
    )
    return stack


def _zeroconf(host: str = "192.168.1.100") -> ZeroconfServiceInfo:
    """Build the `_bbxsrv._tcp` advertisement a BleBox device broadcasts."""
    address = ip_address(host)
    return ZeroconfServiceInfo(
        ip_address=address,
        ip_addresses=[address],
        port=80,
        hostname=f"simongoswitch-{BLEBOX_ID}.local.",
        type="_bbxsrv._tcp.local.",
        name=f"simongoswitch-{BLEBOX_ID}._bbxsrv._tcp.local.",
        properties={},
    )


def _dhcp(host: str = "192.168.1.100", mac: str = BLEBOX_ID) -> DhcpServiceInfo:
    """Build the DHCP lease a BleBox device produces when it joins the network.

    Home Assistant normalises the MAC to lowercase without separators before it
    reaches a config flow, which is exactly the form a BleBox device id takes.
    """
    return DhcpServiceInfo(ip=host, hostname=f"simongoswitch-{mac}", macaddress=mac)


def _no_inputs_state() -> ActionsState:
    """Return the action state a device with no physical inputs reports.

    A wLightBox answers the action API perfectly well but constrains every
    trigger to the null input, because it has no buttons to bind one to.
    """
    return ActionsState.from_payload(
        {
            "actions": [_slot(index) for index in range(6)],
            "itemsLimit": 6,
            "fieldsPreferences": [
                {
                    "name": "triggerType",
                    "values": [19],
                    "dependsOn": "input",
                    "constraints": [{"input": None, "triggerType": [19]}],
                }
            ],
        }
    )


def _form_defaults(result: dict[str, Any]) -> dict[str, Any]:
    """Read back the values a shown form offers as its defaults."""
    return {
        str(marker.schema): marker.default()
        for marker in result["data_schema"].schema
        if isinstance(marker, vol.Optional)
    }


async def _discover(hass: HomeAssistant, source: str, info: Any, **kwargs):
    """Start a discovery flow with every device read patched."""
    with _device_patches(**kwargs):
        return await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": source}, data=info
        )


async def _start(hass: HomeAssistant, **kwargs):
    """Run the user step up to the event selection form."""
    mocks = _device_patches(**kwargs)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    with mocks:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "192.168.1.100"}
        )
    return result


async def test_user_flow_automatic(hass: HomeAssistant) -> None:
    """The happy path: discover inputs, pick events, configure the device."""
    result = await _start(hass)
    assert result["step_id"] == "events"

    # The patches stay in place: creating the entry also sets it up.
    mocks = _device_patches()
    with mocks, patch(f"{MANAGER}.async_save_action"):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                "input_0": ["short_press", "long_press"],
                "input_1": ["short_press"],
                CONF_MODE: MODE_AUTOMATIC,
                CONF_BASE_URL: BASE_URL,
            },
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Simon GO Switch"
    assert result["data"][CONF_BLEBOX_ID] == BLEBOX_ID
    assert result["data"][CONF_INPUTS] == [0, 1]
    assert result["data"][CONF_SUPPORTS_ACTIONS] is True
    assert len(result["data"][CONF_CALLBACK_TOKEN]) == 32
    assert result["options"][CONF_MODE] == MODE_AUTOMATIC
    assert result["options"][CONF_ENABLED_EVENTS] == {
        "0": ["short_press", "long_press"],
        "1": ["short_press"],
    }


async def test_manual_flow_shows_the_urls(hass: HomeAssistant) -> None:
    """Manual mode presents the URLs to paste into wBox before finishing."""
    result = await _start(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "input_0": ["short_press"],
            "input_1": [],
            CONF_MODE: MODE_MANUAL,
            CONF_BASE_URL: BASE_URL,
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "manual"

    urls = result["description_placeholders"]["urls"]
    token = None
    for line in urls.splitlines():
        if "short_press" in line and "/api/blebox_advanced/" in line:
            token = line.split("/api/blebox_advanced/")[1].split("/")[0]
    assert token is not None
    assert f"{BASE_URL}/api/blebox_advanced/{token}/0/short_press" in urls
    # Only the selected event is offered.
    assert "long_press" not in urls

    mocks = _device_patches()
    with mocks, patch(f"{MANAGER}.async_save_action") as save:
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"][CONF_MODE] == MODE_MANUAL
    # Manual mode must never write to the device.
    assert save.call_count == 0


async def test_cannot_connect(hass: HomeAssistant) -> None:
    """An unreachable device is reported on the form, not crashed on."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with patch(
        f"{MANAGER}.async_get_device_info", side_effect=BleBoxConnectionError("nope")
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "192.168.1.100"}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_already_configured(hass: HomeAssistant) -> None:
    """The same device cannot be added twice."""
    existing = _entry()
    existing.add_to_hass(hass)

    result = await _start(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_insufficient_slots_is_reported(hass: HomeAssistant) -> None:
    """A device with no room says so instead of silently doing nothing."""
    full = [
        _slot(i, name=f"user {i}", input=0, triggerType=1, actionType=1)
        for i in range(6)
    ]
    result = await _start(hass, actions_state=_actions_state(full))

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "input_0": ["short_press", "long_press"],
            "input_1": [],
            CONF_MODE: MODE_AUTOMATIC,
            CONF_BASE_URL: BASE_URL,
        },
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "insufficient_slots"}


async def test_no_events_selected_is_rejected(hass: HomeAssistant) -> None:
    """At least one event has to be chosen."""
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "input_0": [],
            "input_1": [],
            CONF_MODE: MODE_MANUAL,
            CONF_BASE_URL: BASE_URL,
        },
    )
    assert result["errors"] == {"base": "no_events"}


async def test_falls_back_to_manual_input_count(hass: HomeAssistant) -> None:
    """A device that will not report its inputs can still be set up by hand.

    This is the path that keeps the integration usable if BleBox ever removes
    the undocumented action API.
    """
    result = await _start(hass, actions_error=BleBoxConnectionError("no action api"))
    assert result["step_id"] == "inputs"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_INPUTS: 2}
    )
    assert result["step_id"] == "events"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "input_0": ["short_press"],
            "input_1": ["long_press"],
            CONF_MODE: MODE_MANUAL,
            CONF_BASE_URL: BASE_URL,
        },
    )
    assert result["step_id"] == "manual"

    mocks = _device_patches(actions_error=BleBoxConnectionError("no action api"))
    with mocks:
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_INPUTS] == [0, 1]
    assert result["data"][CONF_SUPPORTS_ACTIONS] is False
    assert result["options"][CONF_MODE] == MODE_MANUAL


# --- discovery --------------------------------------------------------------


async def test_zeroconf_discovery_creates_an_entry(hass: HomeAssistant) -> None:
    """A device found over mDNS can be added without typing an address.

    The manifest asks Home Assistant to watch `_bbxsrv._tcp`, which is how most
    users will meet this integration: the device turns up on its own and the
    only thing left to decide is which events to use.
    """
    result = await _discover(hass, SOURCE_ZEROCONF, _zeroconf())
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "discovery_confirm"
    # The device is named in the confirmation, not just its address, so a user
    # with several BleBox devices can tell which one turned up.
    assert result["description_placeholders"]["name"] == "Simon GO Switch"
    assert result["description_placeholders"]["host"] == "192.168.1.100"
    assert result["description_placeholders"]["inputs"] == "2"

    # The card in the discovery list is titled from the same identification.
    progress = hass.config_entries.flow.async_progress()
    assert progress[0]["context"]["title_placeholders"] == {"name": "Simon GO Switch"}

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "events"

    with _device_patches(), patch(f"{MANAGER}.async_save_action"):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                "input_0": ["short_press"],
                "input_1": [],
                CONF_MODE: MODE_AUTOMATIC,
                CONF_BASE_URL: BASE_URL,
            },
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Simon GO Switch"
    assert result["data"][CONF_HOST] == "192.168.1.100"
    assert result["data"][CONF_BLEBOX_ID] == BLEBOX_ID
    # Discovery has to identify the device itself; it is what makes a second
    # advertisement from the same switch recognisable as a duplicate.
    assert result["result"].unique_id == BLEBOX_ID


async def test_dhcp_discovery_creates_an_entry(hass: HomeAssistant) -> None:
    """A device taking a DHCP lease is offered the same way mDNS is.

    Both hostname patterns in the manifest exist because BleBox devices do not
    all advertise over mDNS reliably, so the lease is the second chance.
    """
    result = await _discover(hass, SOURCE_DHCP, _dhcp())
    assert result["step_id"] == "discovery_confirm"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "events"

    with _device_patches(), patch(f"{MANAGER}.async_save_action"):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                "input_0": ["short_press"],
                "input_1": ["short_press"],
                CONF_MODE: MODE_MANUAL,
                CONF_BASE_URL: BASE_URL,
            },
        )
        assert result["step_id"] == "manual"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_INPUTS] == [0, 1]


async def test_dhcp_discovery_of_a_known_device_never_touches_the_network(
    hass: HomeAssistant,
) -> None:
    """A renewed lease for a configured device is settled from the MAC alone.

    A BleBox device id *is* its MAC without separators, so an already
    configured device is recognisable without a single request. That matters:
    every device on the network renewing its lease would otherwise make Home
    Assistant probe a switch it already owns, and a sleeping one would stall
    the flow before it could abort.
    """
    existing = _entry()
    existing.add_to_hass(hass)

    with patch(f"{MANAGER}.async_get_device_info") as probe:
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_DHCP},
            data=_dhcp(host="192.168.1.111"),
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert probe.call_count == 0
    # A lease is also how a device that moved is followed to its new address.
    assert existing.data[CONF_HOST] == "192.168.1.111"


async def test_dhcp_discovery_without_a_mac_falls_back_to_asking_the_device(
    hass: HomeAssistant,
) -> None:
    """A lease carrying no MAC is still worth probing.

    The MAC is only a shortcut to the device id. Without one there is nothing
    to compare, so the device has to be asked - the same route zeroconf takes.
    """
    result = await _discover(hass, SOURCE_DHCP, _dhcp(mac=""))

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "discovery_confirm"


async def test_zeroconf_discovery_of_a_configured_device_aborts(
    hass: HomeAssistant,
) -> None:
    """The same switch advertising again updates its address instead of doubling.

    mDNS carries no MAC, so this one has to ask the device who it is before it
    can tell. Adding it twice would give the user two sets of event entities
    and two sets of callbacks racing for the device's action slots.
    """
    existing = _entry()
    existing.add_to_hass(hass)

    result = await _discover(hass, SOURCE_ZEROCONF, _zeroconf("192.168.1.222"))

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert existing.data[CONF_HOST] == "192.168.1.222"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_discovery_of_a_device_that_does_not_answer_aborts(
    hass: HomeAssistant,
) -> None:
    """An advertisement from something unreachable is dropped, not shown.

    Anything at all may answer a hostname pattern. Offering a device that
    cannot be identified would put a card in the user's discovery list that can
    only ever fail.
    """
    with patch(
        f"{MANAGER}.async_get_device_info", side_effect=BleBoxConnectionError("silent")
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_ZEROCONF}, data=_zeroconf()
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "cannot_connect"


async def test_discovery_of_a_device_without_inputs_aborts(
    hass: HomeAssistant,
) -> None:
    """A BleBox device with no buttons is none of this integration's business.

    Plenty of them have none at all - a wLightBox, a shutterBox - and those are
    fully covered by the official integration. Offering them here would invite
    the user to set up an integration that can only produce empty entities.
    """
    result = await _discover(
        hass, SOURCE_ZEROCONF, _zeroconf(), actions_state=_no_inputs_state()
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_inputs"


async def test_discovery_can_still_be_confirmed_by_hand(hass: HomeAssistant) -> None:
    """Confirming a discovered device is a plain yes, with nothing to fill in."""
    result = await _discover(hass, SOURCE_ZEROCONF, _zeroconf())
    assert result["step_id"] == "discovery_confirm"
    # A confirm-only step carries no schema, which is what makes Home Assistant
    # render it as a single button rather than an empty form.
    assert result["data_schema"] is None


# --- the manual input count -------------------------------------------------


async def test_the_input_count_form_is_offered_within_the_devices_limits(
    hass: HomeAssistant,
) -> None:
    """The fallback form asks for a count and turns it into that many inputs.

    Only reached when the device refuses to report its inputs, which is the
    situation the whole manual path exists for.
    """
    result = await _start(hass, actions_error=BleBoxConnectionError("no action api"))
    assert result["step_id"] == "inputs"
    assert result["description_placeholders"]["name"] == "Simon GO Switch"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_INPUTS: 4}
    )
    assert result["step_id"] == "events"
    # Four inputs means four selectable rows, not four of something else.
    assert {"input_0", "input_1", "input_2", "input_3"} <= set(_form_defaults(result))


# --- no reachable Home Assistant URL ----------------------------------------


async def test_setup_without_a_home_assistant_url_is_reported(
    hass: HomeAssistant,
) -> None:
    """A device needs a URL to call, so the flow refuses to finish without one.

    Home Assistant cannot always work one out, and there is nothing sensible to
    guess: a callback pointed at the wrong address fails silently, which is the
    single hardest thing to debug about this integration.
    """
    result = await _start(hass)

    with patch(f"{API}.get_url", side_effect=NoURLAvailableError):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                "input_0": ["short_press"],
                "input_1": [],
                CONF_MODE: MODE_MANUAL,
                CONF_BASE_URL: "",
            },
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_BASE_URL: "no_url"}
    assert not hass.config_entries.async_entries(DOMAIN)

    # Supplying one by hand is the way out, and it is accepted.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "input_0": ["short_press"],
            "input_1": [],
            CONF_MODE: MODE_MANUAL,
            CONF_BASE_URL: BASE_URL,
        },
    )
    assert result["step_id"] == "manual"


async def test_options_without_a_home_assistant_url_are_rejected(
    hass: HomeAssistant,
) -> None:
    """Clearing the URL in the options hits the same wall setup does."""
    entry = _entry()
    await _setup(hass, entry)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "events"}
    )

    with patch(f"{API}.get_url", side_effect=NoURLAvailableError):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                "input_0": ["short_press"],
                "input_1": [],
                CONF_MODE: MODE_MANUAL,
                CONF_BASE_URL: "   ",
            },
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_BASE_URL: "no_url"}
    assert entry.options[CONF_BASE_URL] == BASE_URL


async def test_options_show_callback_urls(hass: HomeAssistant) -> None:
    """The URLs stay retrievable after setup."""
    entry = _entry()
    await _setup(hass, entry)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "urls"}
    )
    urls = result["description_placeholders"]["urls"]
    assert f"{BASE_URL}/api/blebox_advanced/" in urls
    assert "/0/short_press" in urls
    assert "/1/short_press" in urls


async def test_options_change_events_and_reprovision(hass: HomeAssistant) -> None:
    """Changing the selection reloads the entry and rewrites the device.

    The section now saves and returns to the menu rather than ending the flow,
    and the reload is the part that had to survive that: the options are written
    to the entry directly, so if they were ever written some way that did not
    notify Home Assistant, the device would keep the callbacks the user just
    changed and nothing would say so.
    """
    entry = _entry(mode=MODE_AUTOMATIC)
    await _setup(hass, entry)
    coordinator = entry.runtime_data.coordinator

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "events"}
    )
    assert result["step_id"] == "events"

    mocks = _device_patches()
    with mocks, patch(f"{MANAGER}.async_save_action") as save:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                "input_0": ["press", "release"],
                "input_1": [],
                CONF_MODE: MODE_AUTOMATIC,
                CONF_BASE_URL: BASE_URL,
            },
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.MENU
    assert entry.options[CONF_ENABLED_EVENTS] == {"0": ["press", "release"], "1": []}
    # A reload leaves a new coordinator behind, so a different one means the
    # entry really was re-provisioned rather than merely re-rendered.
    assert entry.runtime_data.coordinator is not coordinator

    written = [call.args[0] for call in save.call_args_list]
    # Rising/falling edge actions for input 0 only.
    assert {(a["input"], a["triggerType"]) for a in written} == {(0, 4), (0, 3)}


async def test_options_rejecting_a_selection_keeps_what_was_typed(
    hass: HomeAssistant,
) -> None:
    """A refused submission is handed back, not silently reverted.

    Re-rendering the form from the stored options would throw away every change
    the user made alongside the one that was wrong, which on a device with many
    inputs means retyping the lot.
    """
    entry = _entry()
    await _setup(hass, entry)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "events"}
    )
    # Input 1 starts with an event selected, so clearing it is a real edit.
    assert _form_defaults(result)["input_1"] == ["short_press"]

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "input_0": [],
            "input_1": [],
            CONF_MODE: MODE_MANUAL,
            CONF_BASE_URL: BASE_URL,
        },
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "no_events"}
    assert _form_defaults(result)["input_1"] == []
    # Nothing was written, so the device keeps working as it did.
    assert entry.options[CONF_ENABLED_EVENTS] == {
        "0": ["short_press", "long_press"],
        "1": ["short_press"],
    }


async def test_options_advanced_reaches_the_running_entry(hass: HomeAssistant) -> None:
    """Delivery tuning is saved, applied and does not disturb the other options.

    Each option group writes on top of the stored options rather than replacing
    them, so saving one must not clear another; and the values only mean
    anything once the reloaded entry is actually using them.
    """
    entry = _entry(mode=MODE_MANUAL)
    await _setup(hass, entry)
    assert entry.runtime_data.debounce == 0.0

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "advanced"}
    )
    assert result["step_id"] == "advanced"

    with _device_patches(), patch(f"{MANAGER}.async_save_action"):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                CONF_DEBOUNCE_MS: 500,
                CONF_INVERT_EDGES: True,
                CONF_CLEANUP_ON_REMOVE: False,
                CONF_MANAGE_BUTTONS: True,
            },
        )
        await hass.async_block_till_done()

    # Back at the menu, with the save already made: the flow does not end here,
    # so nothing but this write could have persisted it.
    assert result["type"] is FlowResultType.MENU
    assert entry.options[CONF_DEBOUNCE_MS] == 500
    assert entry.options[CONF_INVERT_EDGES] is True
    assert entry.options[CONF_CLEANUP_ON_REMOVE] is False
    assert entry.options[CONF_MANAGE_BUTTONS] is True
    # The event selection belongs to another group and has to survive.
    assert entry.options[CONF_ENABLED_EVENTS] == {
        "0": ["short_press", "long_press"],
        "1": ["short_press"],
    }

    data = entry.runtime_data
    assert data.debounce == 0.5
    assert data.invert_edges is True
    assert data.manage_buttons is True


async def test_options_urls_page_changes_nothing(hass: HomeAssistant) -> None:
    """Looking up the URLs is read-only, right down to not reloading the entry.

    It is the page a user opens while debugging a switch that stopped working,
    so it must not be the thing that restarts the entry and rewrites the
    device's action slots underneath them. Its button is now a way back to the
    menu, and closing the dialog from there must not reload the entry either.
    """
    entry = _entry()
    await _setup(hass, entry)
    before = dict(entry.options)
    coordinator = entry.runtime_data.coordinator

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "urls"}
    )

    with patch(f"{MANAGER}.async_save_action") as save:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {}
        )
        await hass.async_block_till_done()
        assert result["type"] is FlowResultType.MENU

        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "done"}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert dict(entry.options) == before
    assert save.call_count == 0
    # A reload leaves a new coordinator behind, so the same one means none.
    assert entry.runtime_data.coordinator is coordinator


async def test_the_urls_page_says_when_nothing_is_selected(
    hass: HomeAssistant,
) -> None:
    """An empty selection reads as such instead of as an empty table.

    A bare table header is indistinguishable from a page that failed to render,
    which is a bad thing to show someone who came here because their switch is
    not working.
    """
    entry = _entry(**{CONF_ENABLED_EVENTS: {"0": [], "1": []}})
    await _setup(hass, entry)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "urls"}
    )

    assert result["description_placeholders"]["urls"] == "_No events selected._"


# --- the options menu -------------------------------------------------------


async def test_several_sections_can_be_used_in_one_dialog(hass: HomeAssistant) -> None:
    """The options are a settings dialog, not a one-shot wizard.

    Regression: every section ended the flow, which closed the whole dialog. A
    user who wanted to change two things had to open Configure again for the
    second, and reported it as not being able to go back from a section.

    Ending the flow is also the only moment Home Assistant applies a flow's
    options, so a section that stops doing it has to persist its own changes;
    both saves below are made while the dialog is still open, and both have to
    be there afterwards.
    """
    entry = _entry(mode=MODE_MANUAL)
    await _setup(hass, entry)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "advanced"}
    )
    with _device_patches(), patch(f"{MANAGER}.async_save_action"):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                CONF_DEBOUNCE_MS: 500,
                CONF_INVERT_EDGES: False,
                CONF_CLEANUP_ON_REMOVE: True,
                CONF_MANAGE_BUTTONS: False,
            },
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.MENU

    # Straight on to a second section, without reopening Configure.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "events"}
    )
    assert result["step_id"] == "events"

    with _device_patches(), patch(f"{MANAGER}.async_save_action"):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                "input_0": ["press"],
                "input_1": [],
                CONF_MODE: MODE_MANUAL,
                CONF_BASE_URL: BASE_URL,
            },
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.MENU

    assert entry.options[CONF_DEBOUNCE_MS] == 500
    assert entry.options[CONF_ENABLED_EVENTS] == {"0": ["press"], "1": []}

    # And there is a way out that closes the dialog rather than abandoning it.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "done"}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_DEBOUNCE_MS] == 500
    assert entry.options[CONF_ENABLED_EVENTS] == {"0": ["press"], "1": []}


async def test_the_urls_are_only_offered_in_manual_mode(hass: HomeAssistant) -> None:
    """The URL list is the point of manual mode and noise in automatic mode.

    In automatic mode the integration writes those URLs into the device itself,
    so there is nothing for the user to do with them; in manual mode they are
    what gets pasted into the wBox app. Leaving the entry out of the menu is
    also what stops it being navigated to, since Home Assistant only accepts a
    choice the menu actually offered.
    """
    entry = _entry(mode=MODE_MANUAL)
    await _setup(hass, entry)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert "urls" in result["menu_options"]

    # Switched from inside the same dialog, which is the case a menu built once
    # would get wrong: it is rebuilt on every visit, so the entry goes the
    # moment it stops applying.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "events"}
    )
    with _device_patches(), patch(f"{MANAGER}.async_save_action"):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                "input_0": ["short_press"],
                "input_1": [],
                CONF_MODE: MODE_AUTOMATIC,
                CONF_BASE_URL: BASE_URL,
            },
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.MENU
    assert entry.options[CONF_MODE] == MODE_AUTOMATIC
    assert "urls" not in result["menu_options"]
    assert "events" in result["menu_options"]

    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "urls"}
        )


async def test_the_urls_page_gives_up_if_the_mode_changed_under_it(
    hass: HomeAssistant,
) -> None:
    """A menu drawn in manual mode must not show URLs after a switch away.

    The menu is what normally keeps the page out of automatic mode, but a menu
    is a snapshot: the events section of this very dialog can switch the mode,
    and so can a second dialog open on the same entry. Landing on the page
    anyway puts the user back at the menu, which by then no longer offers it.
    """
    entry = _entry(mode=MODE_MANUAL)
    await _setup(hass, entry)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert "urls" in result["menu_options"]

    # The mode changes while this dialog sits on its menu.
    with _device_patches(), patch(f"{MANAGER}.async_save_action"):
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, CONF_MODE: MODE_AUTOMATIC}
        )
        await hass.async_block_till_done()

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "urls"}
    )

    assert result["type"] is FlowResultType.MENU
    assert "urls" not in result["menu_options"]
