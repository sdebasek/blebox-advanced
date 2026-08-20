"""Config and options flows for BleBox Events.

Setup path: discover (or type an IP) -> identify the device -> discover its
physical inputs -> pick which events to react to -> configure the device
automatically, or show the URLs to paste into the wBox app.

Automatic configuration is only ever offered when the device actually answers
the (undocumented) action API *and* accepts HTTP actions. Everything degrades
to manual mode, including input discovery, which the user can supply by hand if
the device will not report it.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .api import async_default_base_url, build_desired_actions, callback_url
from .blebox_actions import (
    TRIGGER_SHORT_CLICK,
    BleBoxActionManager,
    BleBoxError,
    DeviceInfo,
)
from .const import (
    CONF_API_LEVEL,
    CONF_BASE_URL,
    CONF_BLEBOX_ID,
    CONF_CALLBACK_TOKEN,
    CONF_CLEANUP_ON_REMOVE,
    CONF_DEBOUNCE_MS,
    CONF_ENABLED_EVENTS,
    CONF_HW_VERSION,
    CONF_INPUTS,
    CONF_INVERT_EDGES,
    CONF_MODE,
    CONF_MODEL,
    CONF_PRODUCT,
    CONF_SUPPORTS_ACTIONS,
    CONF_SW_VERSION,
    DEFAULT_DEBOUNCE_MS,
    DEFAULT_ENABLED_EVENTS,
    DEFAULT_PORT,
    DOMAIN,
    EVENT_TYPES,
    MODE_AUTOMATIC,
    MODE_MANUAL,
    MODES,
)
from .coordinator import parse_enabled_events

_LOGGER = logging.getLogger(__name__)

MAX_MANUAL_INPUTS = 16

_EVENT_SELECTOR = SelectSelector(
    SelectSelectorConfig(
        options=EVENT_TYPES,
        multiple=True,
        mode=SelectSelectorMode.LIST,
        translation_key="event_type",
    )
)


def _input_field(input_id: int) -> str:
    """Form field name carrying one input's event selection."""
    return f"input_{input_id}"


def _mode_selector(allow_automatic: bool) -> SelectSelector:
    """Selector for the configuration mode, hiding automatic when unsupported."""
    options = list(MODES) if allow_automatic else [MODE_MANUAL]
    return SelectSelector(
        SelectSelectorConfig(
            options=options,
            mode=SelectSelectorMode.LIST,
            translation_key="configuration_mode",
        )
    )


def _events_schema(
    inputs: list[int],
    current: dict[int, list[str]],
    *,
    allow_automatic: bool,
    mode: str,
    base_url: str,
) -> vol.Schema:
    """Build the per-input event selection form."""
    fields: dict[Any, Any] = {}
    for input_id in inputs:
        fields[
            vol.Optional(
                _input_field(input_id),
                default=current.get(input_id, list(DEFAULT_ENABLED_EVENTS)),
            )
        ] = _EVENT_SELECTOR
    fields[vol.Required(CONF_MODE, default=mode)] = _mode_selector(allow_automatic)
    fields[vol.Optional(CONF_BASE_URL, default=base_url)] = TextSelector()
    return vol.Schema(fields)


def _collect_enabled(
    user_input: dict[str, Any], inputs: list[int]
) -> dict[int, list[str]]:
    """Pull the per-input selection back out of submitted form data."""
    enabled: dict[int, list[str]] = {}
    for input_id in inputs:
        selected = user_input.get(_input_field(input_id)) or []
        enabled[input_id] = [event for event in EVENT_TYPES if event in selected]
    return enabled


def _urls_markdown(enabled: dict[int, list[str]], token: str, base_url: str) -> str:
    """Render the callback URLs as a Markdown table for the user to copy."""
    rows = [
        "| Input | Event | URL to call |",
        "| --- | --- | --- |",
    ]
    for input_id in sorted(enabled):
        for event_type in EVENT_TYPES:
            if event_type not in enabled[input_id]:
                continue
            rows.append(
                f"| {input_id + 1} | `{event_type}` | "
                f"`{callback_url(base_url, token, input_id, event_type)}` |"
            )
    if len(rows) == 2:
        return "_No events selected._"
    return "\n".join(rows)


class BleBoxEventsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle setting up a BleBox device's input events."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialise per-flow state."""
        self._host: str = ""
        self._port: int = DEFAULT_PORT
        self._device: DeviceInfo | None = None
        self._inputs: list[int] = []
        self._supports_actions = False
        self._slots_total = 0
        self._slots_available = 0
        self._token = secrets.token_hex(16)
        self._enabled: dict[int, list[str]] = {}
        self._mode = MODE_MANUAL
        self._base_url = ""

    # -- entry points -------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle a user-initiated setup."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._host = user_input[CONF_HOST]
            self._port = user_input.get(CONF_PORT, DEFAULT_PORT)
            error = await self._async_probe()
            if error is None:
                await self.async_set_unique_id(self._device.device_id)
                self._abort_if_unique_id_configured(
                    updates={CONF_HOST: self._host, CONF_PORT: self._port}
                )
                if self._inputs:
                    return await self.async_step_events()
                return await self.async_step_inputs()
            errors["base"] = error

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST, default=self._host or vol.UNDEFINED): str,
                    vol.Optional(CONF_PORT, default=self._port): int,
                }
            ),
            errors=errors,
        )

    async def async_step_zeroconf(
        self, discovery_info: ZeroconfServiceInfo
    ) -> ConfigFlowResult:
        """Handle a device found over zeroconf."""
        return await self._async_handle_discovery(str(discovery_info.host))

    async def async_step_dhcp(
        self, discovery_info: DhcpServiceInfo
    ) -> ConfigFlowResult:
        """Handle a device found over DHCP."""
        # The BleBox device id is its MAC without separators, so an already
        # configured device can be recognised without touching the network.
        if mac := discovery_info.macaddress:
            await self.async_set_unique_id(mac.replace(":", "").lower())
            self._abort_if_unique_id_configured(updates={CONF_HOST: discovery_info.ip})
        return await self._async_handle_discovery(discovery_info.ip)

    async def _async_handle_discovery(self, host: str) -> ConfigFlowResult:
        """Probe a discovered device and confirm with the user."""
        self._host = host
        self._port = DEFAULT_PORT
        if await self._async_probe() is not None:
            return self.async_abort(reason="cannot_connect")

        await self.async_set_unique_id(self._device.device_id)
        self._abort_if_unique_id_configured(updates={CONF_HOST: host})

        if not self._inputs:
            # Plenty of BleBox devices have no physical inputs at all; those are
            # fully covered by the official integration and are not our business.
            return self.async_abort(reason="no_inputs")

        self.context["title_placeholders"] = {"name": self._device.name}
        return await self.async_step_discovery_confirm()

    async def async_step_discovery_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask the user to confirm adding a discovered device."""
        if user_input is not None:
            return await self.async_step_events()
        self._set_confirm_only()
        return self.async_show_form(
            step_id="discovery_confirm",
            description_placeholders=self._device_placeholders(),
        )

    # -- shared steps -------------------------------------------------------

    async def async_step_inputs(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask how many physical inputs the device has.

        Only reached when the device refuses to report them, which is exactly
        the situation manual mode exists for.
        """
        if user_input is not None:
            count = int(user_input[CONF_INPUTS])
            self._inputs = list(range(count))
            return await self.async_step_events()

        return self.async_show_form(
            step_id="inputs",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_INPUTS, default=1): NumberSelector(
                        NumberSelectorConfig(
                            min=1,
                            max=MAX_MANUAL_INPUTS,
                            step=1,
                            mode=NumberSelectorMode.BOX,
                        )
                    )
                }
            ),
            description_placeholders=self._device_placeholders(),
        )

    async def async_step_events(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick the events to react to and how the device should be configured."""
        errors: dict[str, str] = {}
        default_url = self._base_url or async_default_base_url(self.hass) or ""

        if user_input is not None:
            self._enabled = _collect_enabled(user_input, self._inputs)
            self._mode = user_input.get(CONF_MODE, MODE_MANUAL)
            self._base_url = (user_input.get(CONF_BASE_URL) or "").strip()
            base_url = self._base_url or async_default_base_url(self.hass) or ""

            if not base_url:
                errors[CONF_BASE_URL] = "no_url"
            elif not any(self._enabled.values()):
                errors["base"] = "no_events"
            elif self._mode == MODE_AUTOMATIC:
                desired = build_desired_actions(self._enabled, self._token, base_url)
                if len(desired) > self._slots_available:
                    errors["base"] = "insufficient_slots"

            if not errors:
                if self._mode == MODE_MANUAL:
                    return await self.async_step_manual()
                return self._async_create()

            default_url = self._base_url or default_url

        return self.async_show_form(
            step_id="events",
            data_schema=_events_schema(
                self._inputs,
                self._enabled,
                allow_automatic=self._supports_actions,
                mode=self._mode if self._supports_actions else MODE_MANUAL,
                base_url=default_url,
            ),
            errors=errors,
            description_placeholders={
                **self._device_placeholders(),
                "needed": str(sum(len(v) for v in self._enabled.values())),
                "available": str(self._slots_available),
            },
        )

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the URLs to configure by hand in the wBox app."""
        if user_input is not None:
            return self._async_create()

        base_url = self._base_url or async_default_base_url(self.hass) or ""
        return self.async_show_form(
            step_id="manual",
            data_schema=vol.Schema({}),
            description_placeholders={
                "urls": _urls_markdown(self._enabled, self._token, base_url)
            },
        )

    # -- helpers ------------------------------------------------------------

    async def _async_probe(self) -> str | None:
        """Identify the device and its capabilities. Returns an error key."""
        manager = BleBoxActionManager(
            async_get_clientsession(self.hass), self._host, self._port
        )
        try:
            self._device = await manager.async_get_device_info()
        except BleBoxError as err:
            _LOGGER.debug("Could not identify device at %s: %s", self._host, err)
            return "cannot_connect"

        self._inputs = []
        self._supports_actions = False
        try:
            state = await manager.async_get_actions_state()
        except BleBoxError as err:
            _LOGGER.debug(
                "Action API unavailable on %s (%s); manual mode only", self._host, err
            )
            return None

        self._inputs = state.input_ids()
        self._slots_total = state.items_limit
        # Slots we already own are reusable, so they count as available.
        self._slots_available = len(state.free_slots()) + len(state.owned_actions())
        self._supports_actions = bool(self._inputs) and state.supports_http_action(
            TRIGGER_SHORT_CLICK
        )
        if self._supports_actions:
            self._mode = MODE_AUTOMATIC
        return None

    @callback
    def _device_placeholders(self) -> dict[str, str]:
        """Describe the device for step descriptions."""
        device = self._device
        return {
            "name": device.name if device else self._host,
            "model": device.product if device else "",
            "host": self._host,
            "firmware": device.firmware_version if device else "",
            "inputs": str(len(self._inputs)),
            "slots": str(self._slots_available),
        }

    @callback
    def _async_create(self) -> ConfigFlowResult:
        """Create the config entry."""
        device = self._device
        assert device is not None
        return self.async_create_entry(
            title=device.name,
            data={
                CONF_HOST: self._host,
                CONF_PORT: self._port,
                CONF_BLEBOX_ID: device.device_id,
                CONF_CALLBACK_TOKEN: self._token,
                CONF_INPUTS: self._inputs,
                CONF_MODEL: device.device_type,
                CONF_PRODUCT: device.product,
                CONF_SW_VERSION: device.firmware_version,
                CONF_HW_VERSION: device.hardware_version,
                CONF_API_LEVEL: device.api_level,
                CONF_SUPPORTS_ACTIONS: self._supports_actions,
            },
            options={
                CONF_MODE: self._mode,
                CONF_ENABLED_EVENTS: {
                    str(key): value for key, value in self._enabled.items()
                },
                CONF_BASE_URL: self._base_url,
                CONF_DEBOUNCE_MS: DEFAULT_DEBOUNCE_MS,
                CONF_INVERT_EDGES: False,
                CONF_CLEANUP_ON_REMOVE: True,
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> BleBoxEventsOptionsFlow:
        """Return the options flow."""
        return BleBoxEventsOptionsFlow()


class BleBoxEventsOptionsFlow(OptionsFlow):
    """Change which events are used, how they are delivered, and show URLs."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer the available option groups."""
        return self.async_show_menu(
            step_id="init", menu_options=["events", "advanced", "urls"]
        )

    async def async_step_events(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the per-input event selection and configuration mode."""
        entry = self.config_entry
        inputs = [int(index) for index in entry.data.get(CONF_INPUTS, [])]
        supports_actions = bool(entry.data.get(CONF_SUPPORTS_ACTIONS))
        current = parse_enabled_events(entry.options.get(CONF_ENABLED_EVENTS))
        errors: dict[str, str] = {}

        if user_input is not None:
            enabled = _collect_enabled(user_input, inputs)
            mode = user_input.get(CONF_MODE, MODE_MANUAL)
            base_url = (user_input.get(CONF_BASE_URL) or "").strip()

            if not (base_url or async_default_base_url(self.hass)):
                errors[CONF_BASE_URL] = "no_url"
            elif not any(enabled.values()):
                errors["base"] = "no_events"

            if not errors:
                return self._async_save(
                    {
                        CONF_MODE: mode,
                        CONF_ENABLED_EVENTS: {
                            str(key): value for key, value in enabled.items()
                        },
                        CONF_BASE_URL: base_url,
                    }
                )
            current = enabled

        return self.async_show_form(
            step_id="events",
            data_schema=_events_schema(
                inputs,
                current,
                allow_automatic=supports_actions,
                mode=entry.options.get(CONF_MODE, MODE_MANUAL),
                base_url=entry.options.get(CONF_BASE_URL)
                or async_default_base_url(self.hass)
                or "",
            ),
            errors=errors,
        )

    async def async_step_advanced(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Tune delivery behaviour and removal cleanup."""
        options = self.config_entry.options
        if user_input is not None:
            return self._async_save(
                {
                    CONF_DEBOUNCE_MS: int(user_input[CONF_DEBOUNCE_MS]),
                    CONF_INVERT_EDGES: bool(user_input[CONF_INVERT_EDGES]),
                    CONF_CLEANUP_ON_REMOVE: bool(user_input[CONF_CLEANUP_ON_REMOVE]),
                }
            )

        return self.async_show_form(
            step_id="advanced",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_DEBOUNCE_MS,
                        default=options.get(CONF_DEBOUNCE_MS, DEFAULT_DEBOUNCE_MS),
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=0, max=2000, step=10, mode=NumberSelectorMode.BOX
                        )
                    ),
                    vol.Required(
                        CONF_INVERT_EDGES,
                        default=options.get(CONF_INVERT_EDGES, False),
                    ): BooleanSelector(),
                    vol.Required(
                        CONF_CLEANUP_ON_REMOVE,
                        default=options.get(CONF_CLEANUP_ON_REMOVE, True),
                    ): BooleanSelector(),
                }
            ),
        )

    async def async_step_urls(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the callback URLs currently in effect."""
        if user_input is not None:
            return self.async_create_entry(data=dict(self.config_entry.options))

        entry = self.config_entry
        base_url = (
            entry.options.get(CONF_BASE_URL) or async_default_base_url(self.hass) or ""
        )
        return self.async_show_form(
            step_id="urls",
            data_schema=vol.Schema({}),
            description_placeholders={
                "urls": _urls_markdown(
                    parse_enabled_events(entry.options.get(CONF_ENABLED_EVENTS)),
                    entry.data[CONF_CALLBACK_TOKEN],
                    base_url,
                )
            },
        )

    @callback
    def _async_save(self, changes: dict[str, Any]) -> ConfigFlowResult:
        """Persist changed options on top of the existing ones."""
        return self.async_create_entry(data={**self.config_entry.options, **changes})
