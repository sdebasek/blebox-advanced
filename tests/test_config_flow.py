"""Config and options flow tests."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.blebox_events.blebox_actions import BleBoxConnectionError
from custom_components.blebox_events.const import (
    CONF_BASE_URL,
    CONF_BLEBOX_ID,
    CONF_CALLBACK_TOKEN,
    CONF_ENABLED_EVENTS,
    CONF_INPUTS,
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


def _device_patches(actions_state=None, actions_error=None):
    """Patch the device endpoints used during a flow."""
    info = patch(f"{MANAGER}.async_get_device_info", return_value=DEVICE)
    if actions_error is not None:
        state = patch(f"{MANAGER}.async_get_actions_state", side_effect=actions_error)
    else:
        state = patch(
            f"{MANAGER}.async_get_actions_state",
            return_value=actions_state or _actions_state(),
        )
    return info, state


async def _start(hass: HomeAssistant, **kwargs):
    """Run the user step up to the event selection form."""
    info, state = _device_patches(**kwargs)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    with info, state:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "192.168.1.100"}
        )
    return result


async def test_user_flow_automatic(hass: HomeAssistant) -> None:
    """The happy path: discover inputs, pick events, configure the device."""
    result = await _start(hass)
    assert result["step_id"] == "events"

    # The patches stay in place: creating the entry also sets it up.
    info, state = _device_patches()
    with info, state, patch(f"{MANAGER}.async_save_action"):
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
        if "short_press" in line and "/api/blebox_events/" in line:
            token = line.split("/api/blebox_events/")[1].split("/")[0]
    assert token is not None
    assert f"{BASE_URL}/api/blebox_events/{token}/0/short_press" in urls
    # Only the selected event is offered.
    assert "long_press" not in urls

    info, state = _device_patches()
    with info, state, patch(f"{MANAGER}.async_save_action") as save:
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

    info, state = _device_patches(actions_error=BleBoxConnectionError("no action api"))
    with info, state:
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_INPUTS] == [0, 1]
    assert result["data"][CONF_SUPPORTS_ACTIONS] is False
    assert result["options"][CONF_MODE] == MODE_MANUAL


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
    assert f"{BASE_URL}/api/blebox_events/" in urls
    assert "/0/short_press" in urls
    assert "/1/short_press" in urls


async def test_options_change_events_and_reprovision(hass: HomeAssistant) -> None:
    """Changing the selection reloads the entry and rewrites the device."""
    entry = _entry(mode=MODE_AUTOMATIC)
    await _setup(hass, entry)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "events"}
    )
    assert result["step_id"] == "events"

    info, state = _device_patches()
    with info, state, patch(f"{MANAGER}.async_save_action") as save:
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

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_ENABLED_EVENTS] == {"0": ["press", "release"], "1": []}

    written = [call.args[0] for call in save.call_args_list]
    # Rising/falling edge actions for input 0 only.
    assert {(a["input"], a["triggerType"]) for a in written} == {(0, 4), (0, 3)}
