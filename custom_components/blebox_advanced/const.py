"""Constants for the BleBox Advanced integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "blebox_advanced"

BLEBOX_DOMAIN: Final = "blebox"
"""Domain of the *official* BleBox integration.

Used for device registry identifiers so that our event entities are attached to
the same device entry as the official integration's relay/power/energy
entities, instead of creating a second, unrelated device.
"""

MANUFACTURER: Final = "BleBox"

DEFAULT_PORT: Final = 80
DEFAULT_TIMEOUT: Final = 10
DEFAULT_DEBOUNCE_MS: Final = 150

SCAN_INTERVAL_SECONDS: Final = 5
"""Poll interval for relay, power and safety state.

Matches what the official integration uses, so the relay switch is as
responsive here as it is there. Input events are pushed and never polled for.
"""

SLOW_REFRESH_EVERY: Final = 12
"""Fetch settings, actions and uptime once every N state polls (5s x 12 = 1min).

These change rarely and cost an extra three requests, so polling them at the
state cadence would be wasteful. A settings write forces a full refresh anyway,
so the slow cycle never delays a change made from Home Assistant.
"""

SETTINGS_SETTLE_S: Final = 5.0
"""How long a just-written settings value outranks a contradicting poll.

A refresh already in flight when a write lands carries the settings from before
it, so accepting it would snap the control back to its old value for up to a
poll interval. Past this window the device is believed again, so a change made
in the wBox app still reaches Home Assistant.

Lives here rather than next to its only two users because the coordinator holds
the written value and ``entity`` imports the coordinator, so the constant cannot
live in ``entity`` without a circular import.
"""

# --- Device settings keys ---------------------------------------------------

SETTING_BACKLIGHT: Final = "buttonsBacklight"
SETTING_STATUS_LED: Final = "statusLed"
SETTING_TUNNEL: Final = "tunnel"
SETTING_POWER_MEASURING: Final = "powerMeasuring"
SETTING_RELAYS: Final = "relays"

DEFAULT_BACKLIGHT_COLOR: Final = "ffffff"

OVERLOAD_OFF: Final = 0
OVERLOAD_MIN: Final = 200
OVERLOAD_MAX: Final = 3680

RESTART_STATE_OFF: Final = "off"
RESTART_STATE_ON: Final = "on"
RESTART_STATE_RESTORE: Final = "restore"

RESTART_STATE_OPTIONS: Final[dict[str, int]] = {
    RESTART_STATE_OFF: 0,
    RESTART_STATE_ON: 1,
    RESTART_STATE_RESTORE: 2,
}
"""Relay behaviour after a power cut.

The device exposes no constraint metadata for this field, so the mapping is
inferred from BleBox's convention rather than reported by the hardware.
"""

# --- Event types -----------------------------------------------------------

EVENT_SHORT_PRESS: Final = "short_press"
EVENT_LONG_PRESS: Final = "long_press"
EVENT_PRESS: Final = "press"
EVENT_RELEASE: Final = "release"

EVENT_TYPES: Final[list[str]] = [
    EVENT_SHORT_PRESS,
    EVENT_LONG_PRESS,
    EVENT_PRESS,
    EVENT_RELEASE,
]

DEFAULT_ENABLED_EVENTS: Final[list[str]] = [EVENT_SHORT_PRESS, EVENT_LONG_PRESS]

# --- Config entry data / options keys --------------------------------------

CONF_BLEBOX_ID: Final = "blebox_id"
CONF_CALLBACK_TOKEN: Final = "callback_token"
CONF_INPUTS: Final = "inputs"
CONF_ENABLED_EVENTS: Final = "enabled_events"
CONF_MODE: Final = "mode"
CONF_BASE_URL: Final = "base_url"
CONF_DEBOUNCE_MS: Final = "debounce_ms"
CONF_INVERT_EDGES: Final = "invert_edges"
CONF_CLEANUP_ON_REMOVE: Final = "cleanup_on_remove"
CONF_DEVICE_NAME: Final = "device_name"
CONF_MODEL: Final = "model"
CONF_PRODUCT: Final = "product"
CONF_HW_VERSION: Final = "hw_version"
CONF_SW_VERSION: Final = "sw_version"
CONF_API_LEVEL: Final = "api_level"
CONF_SUPPORTS_ACTIONS: Final = "supports_actions"

MODE_AUTOMATIC: Final = "automatic"
MODE_MANUAL: Final = "manual"
MODES: Final[list[str]] = [MODE_AUTOMATIC, MODE_MANUAL]

# --- Event bus / dispatcher -------------------------------------------------

HA_EVENT: Final = f"{DOMAIN}_event"
"""Bus event fired for every received callback. Backs the device triggers."""

SIGNAL_INPUT_EVENT: Final = DOMAIN + "_input_event_{}"
"""Dispatcher signal (formatted with the config entry id) driving the entities."""

ATTR_INPUT: Final = "input"
ATTR_BUTTON: Final = "button"
ATTR_EVENT_TYPE: Final = "event_type"
ATTR_BLEBOX_ID: Final = "blebox_id"
ATTR_RELAY_STATE: Final = "relay_state"
ATTR_POWER_W: Final = "power_w"

# --- Callback URL placeholders ---------------------------------------------
# The device substitutes these into an action's URL before calling it, so an
# event can carry the device's state at the instant of the press rather than
# whatever the next poll happens to find.

PLACEHOLDER_RELAY_STATE: Final = "{s_state.0}"
PLACEHOLDER_POWER_W: Final = "{power_w.0}"

QUERY_RELAY_STATE: Final = "s"
QUERY_POWER_W: Final = "p"

# --- Callback endpoint ------------------------------------------------------

CALLBACK_BASE_PATH: Final = f"/api/{DOMAIN}"
CALLBACK_URL_TEMPLATE: Final = CALLBACK_BASE_PATH + "/{token}/{input_id}/{event_type}"
# --- Button behaviour control -----------------------------------------------

CONF_MANAGE_BUTTONS: Final = "manage_buttons"
"""Opt-in: let Home Assistant edit what a physical button does to the relay.

Off by default because it means writing action slots the user configured
themselves, which the integration otherwise refuses to touch.
"""

BUTTON_ACTION_NOTHING: Final = "nothing"
BUTTON_ACTION_ON: Final = "relay_on"
BUTTON_ACTION_OFF: Final = "relay_off"
BUTTON_ACTION_TOGGLE: Final = "toggle"

BUTTON_ACTION_OPTIONS: Final[dict[str, int]] = {
    BUTTON_ACTION_NOTHING: 0,
    BUTTON_ACTION_ON: 1,
    BUTTON_ACTION_OFF: 2,
    BUTTON_ACTION_TOGGLE: 3,
}

MANAGED_BUTTON_EVENTS: Final[list[str]] = [EVENT_SHORT_PRESS, EVENT_LONG_PRESS]


# --- hass.data keys ---------------------------------------------------------

DATA_TOKENS: Final = "tokens"
DATA_VIEW_REGISTERED: Final = "view_registered"
