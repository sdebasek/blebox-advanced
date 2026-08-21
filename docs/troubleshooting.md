# Troubleshooting

[← back to README](../README.md)

## Start here

Two things usually name the cause before you have to dig:

1. the **Callback delivery** binary sensor, which turns on when callbacks have
   fired but none are getting through, with `unreachable`, `rejected` and
   `last_status` attributes;
2. the **Repairs** panel, which raises an issue distinguishing a network path
   problem from a wrong URL.

Then turn on debug logging, which records every accepted callback and every
rejection with its reason:

```yaml
logger:
  default: warning
  logs:
    custom_components.blebox_advanced: debug
```

## Symptoms

| Symptom | Cause and fix |
| --- | --- |
| Nothing happens when pressing the button | Check Callback delivery and Repairs first. Then see [Isolated IoT VLANs](#isolated-iot-vlans). |
| `404` from the callback URL | Token, input index or event type is wrong. Re-copy from **Options → Show callback URLs**, which is offered in manual mode, the only mode where you paste URLs yourself. Input indices are 0-based; button 1 is `/0/`. |
| `503` from the callback URL | The config entry is unloaded. Check the Home Assistant log for why. |
| Setup cannot reach the device | Home Assistant needs a route to it. See [Isolated IoT VLANs](#isolated-iot-vlans). |
| Setup works but no events arrive | Almost always the reverse direction: the device cannot reach Home Assistant. |
| Firmware install refuses to start | The image is fetched over BleBox's tunnel, so the **cloud tunnel** switch must be on. |
| "Not enough free action slots" | The device has a fixed number, 30 on a Simon GO reporting `switchBox` firmware `0.1502`, and nothing is ever cleared to make room. Free some in wBox, select fewer events, or use manual mode. The message names no numbers; the free count is on the setup screen itself, and a shortage found while saving options is logged with both figures. |
| An action I deleted in wBox came back | Automatic mode rewrites its own callbacks when it finds them missing, within about a minute. Untick that input's events in the options to have it clear the slot properly, or switch to manual mode so it stops writing slots at all. |
| Events arrive without a `power_w` attribute | Only URLs carrying the device's placeholder deliver it. Automatic mode adds it; a manually pasted URL needs `?p={power_w.0}` appended by hand. Some firmware does not substitute it at all, in which case the attribute stays absent. |
| Automatic mode is not offered | The device did not answer the action API, or does not accept HTTP actions. Manual mode is fully supported. |
| `press` and `release` are swapped | Enable **Swap pressed and released** in the options. Which electrical edge means "finger down" depends on wiring. |
| Duplicate events | The device retried a call. The suppression window (default 150 ms) absorbs this; raise it, or set it to 0 to disable. |
| Two device rows for one switch | Expected if the official integration is also installed. Since Home Assistant 2026 each config entry owns a device row, and rows sharing an identifier are linked rather than merged. |
| Relay switch shows the wrong state | Should self-correct within a poll. If it persists, enable debug logging: every ignored stale value is logged with what it contradicted. |

## Isolated IoT VLANs

This is the single most common reason a working setup produces no events.

Two **independent** directions have to be open:

1. **Home Assistant → device** (`TCP 80`) for setup, polling and automatic
   configuration;
2. **Device → Home Assistant** (`TCP 8123`, or your port) for the callbacks.

Direction 1 usually already works, because polling the device is how the
integration reads state. That proves nothing about direction 2. A typical IoT
VLAN policy allows *return* traffic only: the device can answer Home Assistant
but can never start a connection of its own, which is exactly what a callback
is.

On a UniFi zone-based firewall this shows up as the `IoT → Internal` cell
reading **Allow Return** rather than Allow All.

The fix is one narrow allow policy, ordered above the zone's catch-all block:

| Field | Value |
| --- | --- |
| Action | Allow |
| Source Zone | IoT, wherever the device lives |
| Source | the BleBox device. Prefer its MAC or client entry over an IP that DHCP can change |
| Destination Zone | Internal, wherever Home Assistant lives |
| Destination | the Home Assistant IP |
| Protocol | TCP |
| Destination Port | `8123` |

Then set **Home Assistant URL for the device to call** to a plain IP. The
auto-detected value may be a `.local` hostname, and mDNS does not cross VLANs
without a repeater, so the device would never resolve it.

The firewall rule's own hit counter is the quickest confirmation that the switch
is really calling in.

## Asking the device what happened

The device records the outcome of each action's most recent call. This settles
"the device never called" versus "Home Assistant rejected it" without guesswork:

```bash
curl -s http://<device>/api/actions/state | \
  python3 -c "import json,sys; [print(a['id'], a['name'], a.get('lastCall')) \
    for a in json.load(sys.stdin)['actions'] if a.get('actionType')==50]"
```

| `lastCall` | Meaning |
| --- | --- |
| `{"timeElapsedS": -1}` | Never called. The trigger did not fire, so check the action's input and trigger type. |
| `status: 0` with a non-zero `errorCode` | Called, but no connection. Routing or firewall between the device and Home Assistant. |
| `status: 404` | Reached Home Assistant, wrong URL. Re-copy it from the options. |
| `status: 200` | Working. |

To confirm the device's HTTP client is healthy at all, point one action at
`http://example.com/` temporarily. A `200` there alongside `status: 0` for Home
Assistant isolates the problem to the network path rather than the device.

## Diagnostics

⋮ → **Download diagnostics** on the integration reports the model, firmware,
detected inputs, action slot usage, whether automatic configuration is
supported, and the full callback mapping. Callback tokens are redacted, so the
dump is safe to attach to a bug report.
