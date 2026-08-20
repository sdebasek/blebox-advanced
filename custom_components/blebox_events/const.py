"""Constants for the BleBox Events integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "blebox_events"

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
SCAN_INTERVAL_MINUTES: Final = 15

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

# --- Callback endpoint ------------------------------------------------------

CALLBACK_BASE_PATH: Final = f"/api/{DOMAIN}"
CALLBACK_URL_TEMPLATE: Final = CALLBACK_BASE_PATH + "/{token}/{input_id}/{event_type}"

# --- hass.data keys ---------------------------------------------------------

DATA_TOKENS: Final = "tokens"
DATA_VIEW_REGISTERED: Final = "view_registered"
