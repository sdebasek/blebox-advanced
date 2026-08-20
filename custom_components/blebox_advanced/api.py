"""The Home Assistant side of the BleBox callback contract.

This module owns the local HTTP endpoint that BleBox devices call when a
physical input is used, and - because it defines the URL shape - the helper
that builds those URLs for provisioning and for manual setup.

    physical button -> BleBox input action -> HTTP GET to this endpoint
        -> dispatcher signal (event entities) + bus event (device triggers)

The receiver is deliberately independent of the undocumented action API in
:mod:`blebox_actions`: a URL pasted by hand into the wBox app keeps working
even if automatic provisioning breaks after a firmware update.

Security notes:

* the endpoint is unauthenticated by necessity (the device cannot present a
  Home Assistant token), so each config entry carries its own cryptographically
  random token which forms part of the URL;
* tokens are compared in constant time and an unknown token is answered with a
  bare 404, so the endpoint reveals nothing about which tokens exist;
* input index and event type are validated against what the device actually
  reported, so the endpoint only ever accepts known devices and events.
"""

from __future__ import annotations

import hmac
import logging
import time
from collections.abc import Mapping, Sequence
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.const import CONF_DEVICE_ID
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.network import NoURLAvailableError, get_url

from .blebox_actions import DesiredAction, trigger_type_for_event
from .const import (
    ATTR_BLEBOX_ID,
    ATTR_BUTTON,
    ATTR_EVENT_TYPE,
    ATTR_INPUT,
    ATTR_POWER_W,
    ATTR_RELAY_STATE,
    BLEBOX_DOMAIN,
    CALLBACK_BASE_PATH,
    CALLBACK_URL_TEMPLATE,
    DATA_TOKENS,
    DATA_VIEW_REGISTERED,
    DOMAIN,
    EVENT_TYPES,
    HA_EVENT,
    PLACEHOLDER_POWER_W,
    PLACEHOLDER_RELAY_STATE,
    QUERY_POWER_W,
    QUERY_RELAY_STATE,
    SIGNAL_INPUT_EVENT,
)

if TYPE_CHECKING:
    from .coordinator import BleBoxEventsConfigEntry, BleBoxEventsData

_LOGGER = logging.getLogger(__name__)


# --- URL construction -------------------------------------------------------


def callback_url(
    base_url: str,
    token: str,
    input_id: int,
    event_type: str,
    *,
    placeholders: Sequence[str] = (),
) -> str:
    """Build the URL a device should call for one input/event combination.

    Any placeholder the device advertises is appended as a query parameter, so
    the callback carries the device's own state at the instant of the press
    instead of whatever the next poll happens to find. The braces are left
    unencoded on purpose: the device matches them literally and substitutes
    before calling.
    """
    url = base_url.rstrip("/") + CALLBACK_URL_TEMPLATE.format(
        token=token, input_id=input_id, event_type=event_type
    )
    return url + _placeholder_query(placeholders)


def _placeholder_query(placeholders: Sequence[str]) -> str:
    """Query string carrying whichever placeholders the device advertises."""
    extras = [
        f"{query}={placeholder}"
        for placeholder, query in (
            (PLACEHOLDER_RELAY_STATE, QUERY_RELAY_STATE),
            (PLACEHOLDER_POWER_W, QUERY_POWER_W),
        )
        if placeholder in placeholders
    ]
    return f"?{'&'.join(extras)}" if extras else ""


def action_name(input_id: int, event_type: str) -> str:
    """Human-readable label written to the device's action slot.

    Kept short because the wBox UI shows it in a narrow column. Ownership is
    never inferred from this string - see ``blebox_actions.is_owned``.
    """
    return f"HA IN{input_id + 1} {event_type}"


def build_desired_actions(
    enabled_events: dict[int, list[str]],
    token: str,
    base_url: str,
    *,
    invert_edges: bool = False,
    placeholders: Sequence[str] = (),
) -> list[DesiredAction]:
    """Translate the user's per-input selection into device action definitions."""
    desired: list[DesiredAction] = []
    for input_id in sorted(enabled_events):
        for event_type in EVENT_TYPES:
            if event_type not in enabled_events[input_id]:
                continue
            desired.append(
                DesiredAction(
                    input_id=input_id,
                    trigger_type=trigger_type_for_event(
                        event_type, invert_edges=invert_edges
                    ),
                    url=callback_url(
                        base_url,
                        token,
                        input_id,
                        event_type,
                        placeholders=placeholders,
                    ),
                    name=action_name(input_id, event_type),
                )
            )
    return desired


def async_default_base_url(hass: HomeAssistant) -> str | None:
    """Best guess at the URL a LAN device should use to reach Home Assistant.

    Prefers the internal URL and tolerates a plain IP without TLS, which is the
    normal shape for a device on an IoT VLAN. Returns ``None`` when Home
    Assistant cannot work one out, in which case the user must supply it.
    """
    for internal in (True, False):
        try:
            return get_url(
                hass,
                allow_internal=internal,
                allow_external=not internal,
                allow_ip=True,
                require_ssl=False,
                require_standard_port=False,
            )
        except NoURLAvailableError:
            continue
    return None


# --- Token registry ---------------------------------------------------------


@callback
def _async_tokens(hass: HomeAssistant) -> dict[str, str]:
    """Return the token -> config entry id registry."""
    return hass.data.setdefault(DOMAIN, {}).setdefault(DATA_TOKENS, {})


@callback
def async_register_token(hass: HomeAssistant, token: str, entry_id: str) -> None:
    """Make a config entry's callback token live."""
    _async_tokens(hass)[token] = entry_id


@callback
def async_unregister_token(hass: HomeAssistant, token: str) -> None:
    """Stop accepting callbacks for a token."""
    _async_tokens(hass).pop(token, None)


@callback
def _async_resolve_token(hass: HomeAssistant, token: str) -> str | None:
    """Resolve a token to a config entry id, comparing in constant time."""
    match: str | None = None
    for known, entry_id in _async_tokens(hass).items():
        # Compare every candidate so timing does not leak which prefix matched.
        if hmac.compare_digest(known, token):
            match = entry_id
    return match


# --- The endpoint -----------------------------------------------------------


class BleBoxEventsCallbackView(HomeAssistantView):
    """Receive physical input events pushed by BleBox devices."""

    url = CALLBACK_URL_TEMPLATE
    name = f"api:{DOMAIN}"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        """Hold a reference to hass; views are not otherwise given one."""
        self.hass = hass

    async def get(
        self, request: web.Request, token: str, input_id: str, event_type: str
    ) -> web.Response:
        """Handle the GET the device performs for an HTTP action."""
        return self._handle(request.query, token, input_id, event_type)

    async def post(
        self, request: web.Request, token: str, input_id: str, event_type: str
    ) -> web.Response:
        """Accept POST as well, for hand-written or proxied callbacks."""
        return self._handle(request.query, token, input_id, event_type)

    @callback
    def _handle(
        self, query: Mapping[str, str], token: str, input_id: str, event_type: str
    ) -> web.Response:
        """Validate and dispatch one callback.

        Synchronous and allocation-light on purpose: it runs in the event loop
        and must not delay the device's request.
        """
        hass = self.hass
        entry_id = _async_resolve_token(hass, token)
        if entry_id is None:
            _LOGGER.debug("Rejected callback with unknown token")
            return web.Response(status=HTTPStatus.NOT_FOUND)

        entry: BleBoxEventsConfigEntry | None = hass.config_entries.async_get_entry(
            entry_id
        )
        data: BleBoxEventsData | None = (
            getattr(entry, "runtime_data", None) if entry else None
        )
        if data is None:
            _LOGGER.debug(
                "Callback for %s arrived while the entry was unloaded", entry_id
            )
            return web.Response(status=HTTPStatus.SERVICE_UNAVAILABLE)

        try:
            index = int(input_id)
        except ValueError:
            _LOGGER.warning("Callback with non-numeric input %r ignored", input_id)
            return web.Response(status=HTTPStatus.NOT_FOUND)

        if index not in data.inputs:
            _LOGGER.warning(
                "Callback for unknown input %s on %s ignored (known inputs: %s)",
                index,
                data.device_name,
                data.inputs,
            )
            return web.Response(status=HTTPStatus.NOT_FOUND)

        if event_type not in EVENT_TYPES:
            _LOGGER.warning("Callback with unknown event type %r ignored", event_type)
            return web.Response(status=HTTPStatus.NOT_FOUND)

        # Absorb duplicates from device-side retries without swallowing a real
        # second press. Answering 200 stops the device retrying again.
        now = time.monotonic()
        key = (index, event_type)
        if data.debounce > 0:
            previous = data.last_event.get(key)
            if previous is not None and now - previous < data.debounce:
                _LOGGER.debug(
                    "Ignored duplicate %s on input %s of %s (%.3fs apart)",
                    event_type,
                    index,
                    data.device_name,
                    now - previous,
                )
                return web.Response(status=HTTPStatus.OK, text="duplicate")
        data.last_event[key] = now

        hints = _parse_state_hints(query)
        _LOGGER.debug("%s: input %s %s %s", data.device_name, index, event_type, hints)

        async_dispatcher_send(
            hass, SIGNAL_INPUT_EVENT.format(entry_id), index, event_type, hints
        )

        event_data: dict[str, Any] = {
            ATTR_INPUT: index,
            ATTR_BUTTON: index + 1,
            ATTR_EVENT_TYPE: event_type,
            ATTR_BLEBOX_ID: data.blebox_id,
            **hints,
        }
        device_id = _async_ha_device_id(hass, data)
        if device_id is not None:
            event_data[CONF_DEVICE_ID] = device_id
        hass.bus.async_fire(HA_EVENT, event_data)

        return web.Response(status=HTTPStatus.OK, text="OK")


def _numeric(raw: str | None) -> int | float | None:
    """Parse a substituted placeholder value, ignoring unsubstituted ones.

    Firmware that does not know a placeholder passes it through verbatim, so a
    value still wrapped in braces means "this device cannot tell us", not zero.
    """
    if not raw or raw.startswith("{"):
        return None
    try:
        return int(raw)
    except ValueError:
        try:
            return float(raw)
        except ValueError:
            return None


@callback
def _parse_state_hints(query: Mapping[str, str]) -> dict[str, Any]:
    """Extract the device state the callback carried, if any."""
    hints: dict[str, Any] = {}
    if (state := _numeric(query.get(QUERY_RELAY_STATE))) is not None:
        hints[ATTR_RELAY_STATE] = bool(state)
    if (power := _numeric(query.get(QUERY_POWER_W))) is not None:
        hints[ATTR_POWER_W] = power
    return hints


@callback
def _async_ha_device_id(hass: HomeAssistant, data: BleBoxEventsData) -> str | None:
    """Resolve (and cache) the device registry id backing this entry.

    Looked up lazily because the device entry may not exist yet the first time
    a callback arrives, and its id never changes once it does.
    """
    if data.ha_device_id is not None:
        return data.ha_device_id
    device = dr.async_get(hass).async_get_device(
        identifiers={(BLEBOX_DOMAIN, data.blebox_id)}
    )
    if device is not None:
        data.ha_device_id = device.id
    return data.ha_device_id


@callback
def async_setup_callback_view(hass: HomeAssistant) -> None:
    """Register the callback endpoints once for the whole integration."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    if domain_data.get(DATA_VIEW_REGISTERED):
        return
    hass.http.register_view(BleBoxEventsCallbackView(hass))
    domain_data[DATA_VIEW_REGISTERED] = True
    _LOGGER.debug("Registered callback endpoints under %s", CALLBACK_BASE_PATH)
