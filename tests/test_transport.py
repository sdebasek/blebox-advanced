"""Wire-level tests for the BleBox transport.

Everything else in the suite stubs `_get`/`_post`, or the methods above them,
so the code that actually builds a request, reads a status code and parses a
body never runs. That is precisely the layer a firmware change breaks, and the
device is the untrusted side of the conversation: it answers 404 for endpoints
it does not have, serves JSON labelled `text/html`, and says nothing useful at
all when it is unhappy.

So these tests run a real aiohttp server on loopback and point a real
`aiohttp.ClientSession` at it. No Home Assistant is involved: the module under
test deliberately has no Home Assistant imports, and these tests keep that
property, which is why they use `aiohttp.test_utils` rather than Home
Assistant's `aioclient_mock`.

The payload shapes are the ones captured from a live Simon 55 GO, the same
fixtures `test_integration.py` and `test_blebox_actions.py` use.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, NamedTuple

import aiohttp
import pytest
from aiohttp import test_utils, web

from custom_components.blebox_advanced.blebox_actions import (
    ACTION_RELAY_TOGGLE,
    ACTION_UNCONFIGURED,
    TRIGGER_SHORT_CLICK,
    ActionsUnsupportedError,
    BleBoxActionApiError,
    BleBoxActionManager,
    BleBoxConnectionError,
)

# --- Captured device payloads -----------------------------------------------

DEVICE_STATE = {
    "device": {
        "id": "ae0bfbf927ba",
        "deviceName": "Simon GO Switch",
        "type": "switchBox",
        "product": "SimonGOSwitch",
        "fv": "0.1502",
        "hv": "s_KS.swB.1.5.T.p55ST-0.3",
        "apiLevel": "20220114",
    }
}

# `/info` on simpler devices answers with the device object unwrapped.
INFO = dict(DEVICE_STATE["device"])

SETTINGS = {
    "deviceName": "Simon GO Switch",
    "tunnel": {"enabled": 1, "logEnabled": 0},
    "statusLed": {"enabled": 0},
    "buttonsBacklight": {"enabled": 1, "color": "ffffff"},
    "relays": [{"stateAfterRestart": 2, "defaultForTime": 0, "iconSet": 38}],
    "switch": {},
}

EXTENDED_STATE = {
    "relays": [
        {
            "relay": 0,
            "state": 1,
            "stateAfterRestart": 2,
            "defaultForTime": 0,
            "forTimeLeftS": 0,
            "forTimeEndState": 1,
            "iconSet": 38,
        }
    ],
    "switch": {"safety": {"eventReason": 0, "triggered": []}},
    "sensors": [{"type": "activePower", "value": 0, "trend": 0, "state": 2}],
}

NETWORK = {
    "ip": "192.168.1.100",
    "ssid": "IoT",
    "mac": "ae:0b:fb:f9:27:ba",
    "apEnable": True,
    "apSSID": "SimonGOSwitch-ae0bfbf927ba",
    # Captured empty on the test unit; a real value here makes it visible that
    # the round-trip is what stops enabling the AP later from finding it blank.
    "apPasswd": "hunter2",
}

UPTIME = {"uptimeS": 3994}


def _slot(slot_id: int, **overrides: Any) -> dict[str, Any]:
    """Build one action slot, shaped as the device reports it."""
    return {
        "id": slot_id,
        "name": "",
        "input": 0,
        "triggerType": 0,
        "actionType": 0,
        "lastCall": {"timeElapsedS": -1},
        "param": "",
        "relay": 0,
        "forTime": 0,
        "ns": 0,
        **overrides,
    }


ACTIONS_STATE = {
    "actions": [_slot(i) for i in range(6)],
    "itemsLimit": 6,
    "fieldsPreferences": [
        {
            "name": "triggerType",
            "values": [1, 2, 3, 4, 5],
            "dependsOn": "input",
            "constraints": [
                {"input": None, "triggerType": [19]},
                {"input": 0, "triggerType": [1, 2, 3, 4, 5]},
                {"input": 1, "triggerType": [1, 2, 3, 4, 5]},
            ],
        },
        {
            "name": "actionType",
            "dependsOn": "triggerType",
            "constraints": [
                {"triggerType": t, "actionType": [1, 2, 3, 50]} for t in (1, 2, 3, 4, 5)
            ],
        },
    ],
}


# --- Test server -------------------------------------------------------------

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


class Received(NamedTuple):
    """One request as it arrived at the device."""

    method: str
    path: str
    body: str
    content_type: str


class DeviceServer:
    """An aiohttp application answering the way a BleBox device answers.

    Only the registered paths exist. Everything else answers 404, which is what
    the hardware does for an endpoint its firmware does not have, and is the
    single most important response for this integration to get right: the whole
    manual-mode fallback hangs off telling "no such endpoint" apart from "the
    device is unreachable".
    """

    def __init__(self, routes: dict[str, Handler] | None = None) -> None:
        """Serve `routes`, recording every request that arrives."""
        self.routes = routes or {}
        self.received: list[Received] = []
        self.app = web.Application()
        self.app.router.add_route("*", "/{tail:.*}", self._dispatch)

    async def _dispatch(self, request: web.Request) -> web.StreamResponse:
        self.received.append(
            Received(
                request.method,
                request.path,
                await request.text(),
                request.headers.get("Content-Type", ""),
            )
        )
        handler = self.routes.get(request.path)
        if handler is None:
            return web.Response(status=404, text="Not found")
        return await handler(request)

    @property
    def last(self) -> Received:
        """Return the most recent request, for asserting on what was sent."""
        return self.received[-1]

    def sent_json(self) -> Any:
        """Return the body of the most recent request, decoded."""
        return json.loads(self.last.body)


def serves_json(payload: Any, *, content_type: str = "application/json") -> Handler:
    """Build a handler answering 200 with `payload` encoded as JSON."""

    async def handler(_request: web.Request) -> web.Response:
        return web.Response(text=json.dumps(payload), content_type=content_type)

    return handler


def serves_text(
    body: str, *, status: int = 200, content_type: str = "text/html"
) -> Handler:
    """Build a handler answering `status` with a body that is not JSON."""

    async def handler(_request: web.Request) -> web.Response:
        return web.Response(status=status, text=body, content_type=content_type)

    return handler


@asynccontextmanager
async def connected(
    device: DeviceServer, **kwargs: Any
) -> AsyncIterator[BleBoxActionManager]:
    """Run `device` on a loopback port with a manager pointed at it."""
    server = test_utils.TestServer(device.app)
    await server.start_server()
    session = aiohttp.ClientSession()
    try:
        yield BleBoxActionManager(session, server.host, server.port, **kwargs)
    finally:
        await session.close()
        await server.close()


@pytest.fixture(autouse=True)
def _loopback_sockets(socket_enabled: None) -> None:
    """Let these tests bind a real server.

    The Home Assistant pytest plugin blocks sockets for every test in the
    process, which is exactly why the rest of the suite cannot reach this code.
    """
    return


# --- URL building ------------------------------------------------------------


def test_the_default_port_is_left_out_of_the_url() -> None:
    """Port 80 is omitted, anything else is spelled out.

    BleBox devices serve on port 80, and some firmware compares the Host header
    against its own name, so an explicit `:80` is worth not sending.
    """
    manager = BleBoxActionManager(None, "192.0.2.10")  # type: ignore[arg-type]
    assert manager.base_url == "http://192.0.2.10"

    moved = BleBoxActionManager(None, "192.0.2.10", 8080)  # type: ignore[arg-type]
    assert moved.base_url == "http://192.0.2.10:8080"


# --- GET ---------------------------------------------------------------------


async def test_a_missing_endpoint_is_reported_as_unsupported_not_unreachable() -> None:
    """A 404 must raise `ActionsUnsupportedError`, never the connection error.

    The two mean opposite things to the caller: unsupported routes the user to
    manual mode for good, unreachable is a transient failure worth retrying.
    Older firmware has no action API at all and says so with a 404.
    """
    async with connected(DeviceServer()) as manager:
        with pytest.raises(ActionsUnsupportedError):
            await manager.async_get_actions_state()


async def test_a_server_error_is_a_connection_error() -> None:
    """A 5xx is the device failing, not the endpoint being absent."""
    device = DeviceServer(
        {"/api/actions/state": serves_text("Internal Error", status=500)}
    )
    async with connected(device) as manager:
        with pytest.raises(BleBoxConnectionError) as err:
            await manager.async_get_actions_state()
    assert not isinstance(err.value, ActionsUnsupportedError)


async def test_a_body_that_is_not_json_is_a_connection_error() -> None:
    """A 200 carrying a captive-portal page or a stub is not a usable answer."""
    device = DeviceServer({"/api/actions/state": serves_text("<html>hello</html>")})
    async with connected(device) as manager:
        with pytest.raises(BleBoxConnectionError) as err:
            await manager.async_get_actions_state()
    assert "invalid JSON" in str(err.value)


async def test_json_is_parsed_whatever_the_device_labels_it() -> None:
    """A JSON body served as `text/html` is still parsed.

    BleBox firmware mislabels its content type on several endpoints, which is
    why the parse passes `content_type=None`. aiohttp would otherwise refuse
    the body outright and every read would look like a broken device.
    """
    device = DeviceServer(
        {"/api/device/state": serves_json(DEVICE_STATE, content_type="text/html")}
    )
    async with connected(device) as manager:
        info = await manager.async_get_device_info()
    assert info.device_id == "ae0bfbf927ba"


async def test_an_unreachable_device_is_a_connection_error() -> None:
    """Nothing listening on the other end surfaces as our own error type.

    A raw `aiohttp.ClientError` would sail past every `except BleBoxError`
    handler in the integration and surface as an unhandled crash.
    """
    device = DeviceServer()
    server = test_utils.TestServer(device.app)
    await server.start_server()
    host, port = server.host, server.port
    await server.close()  # the device has just been unplugged

    async with aiohttp.ClientSession() as session:
        manager = BleBoxActionManager(session, host, port)
        with pytest.raises(BleBoxConnectionError):
            await manager.async_get_actions_state()


async def test_a_device_that_never_answers_is_a_connection_error() -> None:
    """A request that runs out of time is a connection failure, not a crash.

    Sleeping BleBox devices accept the connection and then say nothing, so the
    timeout path is a routine occurrence rather than an edge case.
    """
    release = asyncio.Event()

    async def never_answers(_request: web.Request) -> web.Response:
        await release.wait()
        return web.Response(text="{}")

    device = DeviceServer({"/api/actions/state": never_answers})
    # The timeout reaches aiohttp as `ClientTimeout(total=...)`, which is happy
    # with a fraction of a second; a whole one would just be dead test time.
    async with connected(device, timeout=0.1) as manager:
        try:
            with pytest.raises(BleBoxConnectionError):
                await manager.async_get_actions_state()
        finally:
            # Let the handler finish, so shutting the server down does not wait
            # on it.
            release.set()


async def test_a_shorter_deadline_lasts_only_as_long_as_the_block() -> None:
    """`request_timeout` narrows the deadline, then hands it back.

    Setting a config entry up uses this for its first poll: it needs an answer
    from an entity's point of view rather than a device's, and the entities are
    there either way. Every other caller has to keep the generous deadline,
    because an ESP-based device on a busy link really is slow sometimes and
    giving up on one is worse than waiting for it.
    """
    slow = 0.15

    async def answers_eventually(_request: web.Request) -> web.Response:
        await asyncio.sleep(slow)
        return web.Response(text=json.dumps(EXTENDED_STATE))

    device = DeviceServer({"/state/extended": answers_eventually})
    async with connected(device, timeout=5) as manager:
        with manager.request_timeout(slow / 10), pytest.raises(BleBoxConnectionError):
            await manager.async_get_extended_state()

        assert await manager.async_get_extended_state() == EXTENDED_STATE


# --- POST --------------------------------------------------------------------


async def test_a_write_to_a_missing_endpoint_is_reported_as_unsupported() -> None:
    """A 404 on a write is a firmware without the action API, not a failure."""
    async with connected(DeviceServer()) as manager:
        with pytest.raises(ActionsUnsupportedError):
            await manager.async_save_action(_slot(0))


async def test_a_rejected_write_carries_the_devices_own_complaint() -> None:
    """A 4xx surfaces the response body, truncated.

    The device answers 400 when a saved action is missing a field its hardware
    revision expects, and the body is the only clue as to which one. It is
    capped because some firmware answers with an entire HTML page, and the
    whole thing would end up in the log.
    """
    complaint = "Bad request: missing field 'relay'. " + "x" * 400
    device = DeviceServer({"/api/actions/set": serves_text(complaint, status=400)})

    async with connected(device) as manager:
        with pytest.raises(BleBoxActionApiError) as err:
            await manager.async_save_action(_slot(0))

    message = str(err.value)
    assert not isinstance(err.value, ActionsUnsupportedError)
    assert "HTTP 400" in message
    assert "missing field 'relay'" in message
    assert message.endswith(complaint[:200])


async def test_a_write_the_device_accepted_without_json_is_not_a_failure() -> None:
    """A 2xx with an unparsable body means it worked and said nothing.

    Several BleBox write endpoints answer with a bare `OK`, or with an empty
    body. Treating that as an error would report a successful provisioning run
    as a failure and, worse, invite the caller to retry it.
    """
    device = DeviceServer({"/api/actions/set": serves_text("OK")})
    async with connected(device) as manager:
        assert await manager.async_save_action(_slot(0)) is None

    device = DeviceServer({"/api/actions/set": serves_text("")})
    async with connected(device) as manager:
        assert await manager.async_save_action(_slot(0)) is None


async def test_a_write_to_an_unreachable_device_is_a_connection_error() -> None:
    """A write that never arrives is our own error type, like a read is.

    A raw `aiohttp.ClientError` from a POST would sail past every
    `except BleBoxError` handler in the integration.
    """
    device = DeviceServer()
    server = test_utils.TestServer(device.app)
    await server.start_server()
    host, port = server.host, server.port
    await server.close()  # the device has just been unplugged

    async with aiohttp.ClientSession() as session:
        manager = BleBoxActionManager(session, host, port)
        with pytest.raises(BleBoxConnectionError):
            await manager.async_save_action(_slot(0))


async def test_an_action_goes_on_the_wire_wrapped_in_its_own_key() -> None:
    """The endpoint takes one action per request, wrapped as `{"action": ...}`.

    Sending the whole slot array, or the bare action, is rejected. The content
    type matters too: form-encoded fields would not be read at all.
    """
    device = DeviceServer({"/api/actions/set": serves_json({})})
    action = _slot(2, name="HA IN1 short_press", triggerType=1, actionType=50)

    async with connected(device) as manager:
        await manager.async_save_action(action)

    assert device.last.method == "POST"
    assert device.last.path == "/api/actions/set"
    assert device.last.content_type == "application/json"
    assert device.sent_json() == {"action": action}


async def test_a_settings_patch_goes_on_the_wire_wrapped_in_its_own_key() -> None:
    """Settings are posted as `{"settings": ...}`, carrying only what changed.

    Unlike an action, a settings write is a partial patch: echoing back
    read-only sub-objects such as `factoryCalibration` risks rejection.
    """
    device = DeviceServer({"/api/settings/set": serves_json({"settings": SETTINGS})})
    async with connected(device) as manager:
        await manager.async_set_settings({"statusLed": {"enabled": 1}})

    assert device.last.path == "/api/settings/set"
    assert device.sent_json() == {"settings": {"statusLed": {"enabled": 1}}}


async def test_a_network_patch_goes_on_the_wire_wrapped_in_its_own_key() -> None:
    """Network changes are posted to `/api/device/set` under `network`."""
    device = DeviceServer(
        {
            "/api/device/network": serves_json(NETWORK),
            "/api/device/set": serves_json({}),
        }
    )
    async with connected(device) as manager:
        await manager.async_set_ap_enabled(True)

    assert device.last.path == "/api/device/set"
    assert set(device.sent_json()) == {"network"}


# --- Device identity ---------------------------------------------------------


async def test_device_identity_comes_from_the_documented_endpoint() -> None:
    """`/api/device/state` is parsed into the identity the config entry uses."""
    device = DeviceServer({"/api/device/state": serves_json(DEVICE_STATE)})
    async with connected(device) as manager:
        info = await manager.async_get_device_info()

    assert info.device_id == "ae0bfbf927ba"
    assert info.name == "Simon GO Switch"
    assert info.device_type == "switchBox"
    assert info.product == "SimonGOSwitch"
    assert info.firmware_version == "0.1502"
    assert info.hardware_version == "s_KS.swB.1.5.T.p55ST-0.3"
    assert info.api_level == "20220114"


async def test_device_identity_falls_back_to_info() -> None:
    """A device that cannot serve `/api/device/state` is asked for `/info`.

    Simpler and older BleBox products only implement `/info`, and it answers
    with the device object unwrapped rather than under a `device` key.
    """
    device = DeviceServer(
        {
            "/api/device/state": serves_text("Internal Error", status=500),
            "/info": serves_json(INFO),
        }
    )
    async with connected(device) as manager:
        info = await manager.async_get_device_info()

    assert info.device_id == "ae0bfbf927ba"
    assert [request.path for request in device.received] == [
        "/api/device/state",
        "/info",
    ]


async def test_device_identity_falls_back_to_info_on_a_404() -> None:
    """A 404 is how a device says it has no `/api/device/state`, so it must fall back.

    Regression: the fallback caught only `BleBoxConnectionError`, but `_get`
    reports a 404 as `ActionsUnsupportedError`, which is not one. It could fire
    on a timeout or a 5xx and never on the single case it was written for, so
    setting up the older hardware it exists to support failed outright.
    """
    device = DeviceServer({"/info": serves_json(INFO)})
    async with connected(device) as manager:
        info = await manager.async_get_device_info()

    assert info.device_id == "ae0bfbf927ba"
    # Unregistered paths answer 404 here, exactly as firmware without the
    # endpoint does, so this pins the 404 route specifically.
    assert [request.path for request in device.received] == [
        "/api/device/state",
        "/info",
    ]


async def test_a_device_that_will_not_identify_itself_is_refused() -> None:
    """No device id means no stable unique id, so the read has to fail.

    Both shapes are covered: an answer that is not an object at all, and one
    that is but carries no id. Either would otherwise produce a config entry
    that cannot be matched to the hardware again.
    """
    device = DeviceServer({"/api/device/state": serves_json(["switchBox"])})
    async with connected(device) as manager:
        with pytest.raises(BleBoxConnectionError):
            await manager.async_get_device_info()

    device = DeviceServer(
        {"/api/device/state": serves_json({"device": {"type": "switchBox"}})}
    )
    async with connected(device) as manager:
        with pytest.raises(BleBoxConnectionError):
            await manager.async_get_device_info()


# --- Reads -------------------------------------------------------------------


async def test_action_slots_are_read_off_the_wire() -> None:
    """The slot array, its limit and the constraint engine all survive a read."""
    device = DeviceServer({"/api/actions/state": serves_json(ACTIONS_STATE)})
    async with connected(device) as manager:
        state = await manager.async_get_actions_state()
        inputs = await manager.async_get_inputs()

    assert len(state.actions) == 6
    assert state.items_limit == 6
    assert state.input_ids() == [0, 1]
    assert inputs == [0, 1]


async def test_an_actions_payload_we_cannot_read_is_unsupported() -> None:
    """A device that answers but describes no slots cannot be driven.

    Both shapes reach here in the field: firmware that answers the endpoint
    with something other than an object, and firmware that answers with an
    object holding no action list. Neither is a slot array we can plan against.
    """
    device = DeviceServer({"/api/actions/state": serves_json([])})
    async with connected(device) as manager:
        with pytest.raises(ActionsUnsupportedError):
            await manager.async_get_actions_state()

    device = DeviceServer({"/api/actions/state": serves_json({"itemsLimit": 6})})
    async with connected(device) as manager:
        with pytest.raises(ActionsUnsupportedError):
            await manager.async_get_actions_state()


async def test_extended_state_is_read_off_the_wire() -> None:
    """`/state/extended` carries relay, safety and sensor state in one read."""
    device = DeviceServer({"/state/extended": serves_json(EXTENDED_STATE)})
    async with connected(device) as manager:
        state = await manager.async_get_extended_state()
    assert state["relays"][0]["state"] == 1

    device = DeviceServer({"/state/extended": serves_json([EXTENDED_STATE])})
    async with connected(device) as manager:
        with pytest.raises(BleBoxConnectionError):
            await manager.async_get_extended_state()


async def test_settings_are_unwrapped_from_their_envelope() -> None:
    """The settings object is lifted out of the `settings` key the device uses."""
    device = DeviceServer({"/api/settings/state": serves_json({"settings": SETTINGS})})
    async with connected(device) as manager:
        settings = await manager.async_get_settings()

    assert settings["deviceName"] == "Simon GO Switch"
    assert settings["statusLed"] == {"enabled": 0}


async def test_settings_are_empty_when_the_device_answers_something_else() -> None:
    """A device with no settings to report yields `{}`, not a failure.

    Which keys exist varies by product and none of them are specified anywhere,
    so an answer without a `settings` object is a device with nothing to
    configure rather than a broken one.
    """
    device = DeviceServer({"/api/settings/state": serves_json(DEVICE_STATE)})
    async with connected(device) as manager:
        assert await manager.async_get_settings() == {}

    device = DeviceServer({"/api/settings/state": serves_json(["settings"])})
    async with connected(device) as manager:
        with pytest.raises(BleBoxConnectionError):
            await manager.async_get_settings()


async def test_a_settings_write_returns_what_the_device_echoed() -> None:
    """The device's own answer is the truth about what was stored.

    It merges the patch itself, so what it echoes back can differ from what was
    asked for, and that is what the entities must show.
    """
    stored = {**SETTINGS, "statusLed": {"enabled": 1}}
    device = DeviceServer({"/api/settings/set": serves_json({"settings": stored})})
    async with connected(device) as manager:
        assert await manager.async_set_settings({"statusLed": {"enabled": 1}}) == stored


async def test_a_settings_write_with_an_unusable_answer_returns_nothing() -> None:
    """An answer without a settings object leaves us with nothing to report.

    The write itself is not treated as failed: the device accepted it, it just
    did not say what it now holds, and the next poll will find out.
    """
    device = DeviceServer({"/api/settings/set": serves_json({"result": "ok"})})
    async with connected(device) as manager:
        assert await manager.async_set_settings({"statusLed": {"enabled": 1}}) == {}


async def test_uptime_is_read_when_the_device_offers_it() -> None:
    """Uptime comes back as whole seconds."""
    device = DeviceServer({"/api/device/uptime": serves_json(UPTIME)})
    async with connected(device) as manager:
        assert await manager.async_get_uptime() == 3994


async def test_uptime_is_none_rather_than_an_error() -> None:
    """A device without the endpoint, or with a shape we cannot read, says nothing.

    Uptime is a diagnostic sensor: it must never be able to fail a coordinator
    refresh and take every other entity unavailable with it.
    """
    async with connected(DeviceServer()) as manager:
        assert await manager.async_get_uptime() is None

    device = DeviceServer({"/api/device/uptime": serves_json({"uptimeS": "3994"})})
    async with connected(device) as manager:
        assert await manager.async_get_uptime() is None


async def test_network_state_is_read_off_the_wire() -> None:
    """`/api/device/network` reports the joined WiFi and the device's own AP."""
    device = DeviceServer({"/api/device/network": serves_json(NETWORK)})
    async with connected(device) as manager:
        network = await manager.async_get_network()
    assert network["apEnable"] is True

    device = DeviceServer({"/api/device/network": serves_json([NETWORK])})
    async with connected(device) as manager:
        with pytest.raises(BleBoxConnectionError):
            await manager.async_get_network()


# --- Capability probing ------------------------------------------------------


async def test_action_support_is_detected_from_a_real_read() -> None:
    """A device listing inputs and HTTP actions supports automatic mode."""
    device = DeviceServer({"/api/actions/state": serves_json(ACTIONS_STATE)})
    async with connected(device) as manager:
        assert await manager.async_supports_action_configuration() is True


async def test_action_support_probing_never_raises() -> None:
    """Any device error means "no", because this decides which mode to offer.

    It runs during the config flow, where an exception would abort setup
    instead of routing the user to manual mode. Both a missing endpoint and a
    failing one have to come back as `False`.
    """
    async with connected(DeviceServer()) as manager:
        assert await manager.async_supports_action_configuration() is False

    device = DeviceServer(
        {"/api/actions/state": serves_text("Internal Error", status=500)}
    )
    async with connected(device) as manager:
        assert await manager.async_supports_action_configuration() is False

    device = DeviceServer({"/api/actions/state": serves_text("<html></html>")})
    async with connected(device) as manager:
        assert await manager.async_supports_action_configuration() is False


# --- Control -----------------------------------------------------------------


async def test_switching_a_relay_reads_the_resulting_state_back() -> None:
    """The control endpoint's own answer says what the relay actually did.

    That saves the caller both assuming the command took effect and waiting for
    the next poll to find out.
    """
    device = DeviceServer({"/s/0/1": serves_json(EXTENDED_STATE)})
    async with connected(device) as manager:
        assert await manager.async_set_relay(0, True) is True
    assert device.last.method == "GET"
    assert device.last.path == "/s/0/1"

    off = {"relays": [{**EXTENDED_STATE["relays"][0], "state": 0}]}
    device = DeviceServer({"/s/0/0": serves_json(off)})
    async with connected(device) as manager:
        assert await manager.async_set_relay(0, False) is False


async def test_an_answer_without_a_relay_list_means_we_do_not_know() -> None:
    """A relay state we cannot read is `None`, never a guess.

    Firmware that answers with something other than a relay list would
    otherwise be read as "off", showing a switch that is really on as off until
    the next poll corrects it.
    """
    device = DeviceServer({"/s/1/1": serves_json({"state": 1})})
    async with connected(device) as manager:
        assert await manager.async_set_relay(1, True) is None

    # A relay list that does not mention the relay we asked about.
    device = DeviceServer({"/s/1/1": serves_json(EXTENDED_STATE)})
    async with connected(device) as manager:
        assert await manager.async_set_relay(1, True) is None


async def test_clearing_an_input_that_has_no_action_writes_nothing() -> None:
    """Unbinding a button the device never bound is a no-op, not a write.

    Every write to the undocumented action API is a risk, so a request that
    would change nothing must not reach the device at all.
    """
    device = DeviceServer({"/api/actions/state": serves_json(ACTIONS_STATE)})
    async with connected(device) as manager:
        await manager.async_set_native_action(
            0, TRIGGER_SHORT_CLICK, ACTION_UNCONFIGURED
        )

    assert [request.method for request in device.received] == ["GET"]


async def test_a_native_action_is_written_into_a_free_slot() -> None:
    """Binding a button locally reads the slots, then writes exactly one.

    This is the whole read-plan-write sequence over real HTTP, so the request
    the device receives is the one the hardware would.
    """
    device = DeviceServer(
        {
            "/api/actions/state": serves_json(ACTIONS_STATE),
            "/api/actions/set": serves_json({}),
        }
    )
    async with connected(device) as manager:
        await manager.async_set_native_action(
            1, TRIGGER_SHORT_CLICK, ACTION_RELAY_TOGGLE
        )

    written = device.sent_json()["action"]
    assert written["id"] == 0
    assert written["input"] == 1
    assert written["triggerType"] == TRIGGER_SHORT_CLICK
    assert written["actionType"] == ACTION_RELAY_TOGGLE
    # Read-only telemetry is never echoed back; hardware-specific fields are.
    assert "lastCall" not in written
    assert written["relay"] == 0 and written["forTime"] == 0 and written["ns"] == 0


# --- Firmware ----------------------------------------------------------------


async def test_a_firmware_install_is_posted_with_an_empty_body() -> None:
    """The device pulls the image itself, so the request carries nothing."""
    device = DeviceServer({"/api/ota/update": serves_json({})})
    async with connected(device) as manager:
        await manager.async_install_firmware()

    assert device.last.method == "POST"
    assert device.last.path == "/api/ota/update"
    assert device.sent_json() == {}


# --- Access point ------------------------------------------------------------


async def test_the_ap_toggle_never_touches_the_station_configuration() -> None:
    """Turning the AP off must not disturb the WiFi the device is joined to.

    This is the worst failure this integration could cause: a patch carrying
    station fields could strand the device on a network it can no longer reach,
    leaving a factory reset as the only way back. Pinned at the wire level
    because what matters is the bytes that leave the machine.
    """
    device = DeviceServer(
        {
            "/api/device/network": serves_json(NETWORK),
            "/api/device/set": serves_json({}),
        }
    )
    async with connected(device) as manager:
        await manager.async_set_ap_enabled(False)

    patch = device.sent_json()["network"]
    assert patch == {
        "apEnable": False,
        # Round-tripped, or enabling the AP later would find them blank.
        "apSSID": "SimonGOSwitch-ae0bfbf927ba",
        "apPasswd": "hunter2",
    }
    assert "ssid" not in patch
    assert "ip" not in patch
    assert "mac" not in patch


async def test_the_ap_toggle_copes_with_a_device_that_reports_no_ap_fields() -> None:
    """A device that does not report an SSID is sent only the enable flag.

    Inventing values here would be worse than omitting them: writing a blank
    SSID could leave the access point unjoinable.
    """
    device = DeviceServer(
        {
            "/api/device/network": serves_json({"ip": "192.168.1.100", "ssid": "IoT"}),
            "/api/device/set": serves_json({}),
        }
    )
    async with connected(device) as manager:
        await manager.async_set_ap_enabled(True)

    assert device.sent_json() == {"network": {"apEnable": True}}
