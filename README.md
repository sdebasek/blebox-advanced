# BleBox Events

Home Assistant custom integration that adds **physical button/input events** for
BleBox-based devices as first-class `event` entities and device automation
triggers.

The official [BleBox integration](https://www.home-assistant.io/integrations/blebox)
does not expose what the wall switch *itself* is doing: you get the relay, power
and energy, but pressing the button is invisible to Home Assistant unless it
happens to toggle that relay. This integration fills exactly that gap and
nothing else.

```
physical button → BleBox input action → local HTTP call → Home Assistant
                → event entity + device trigger → your automation
```

## What this is not

* **It does not replace or fork the official `blebox` integration.** Keep it
  installed — it stays responsible for relay on/off, power, energy, firmware
  updates and everything else.
* **It does not duplicate any of those entities.**
* **It does not poll the relay to guess at presses.** That approach cannot tell a
  wall press from a Home Assistant command, breaks entirely when the input is
  detached from the relay, and can never detect a long press. The device pushes
  events here instead, so they arrive immediately.
* **It does not use the BleBox cloud.** Everything is local.

## Result

The button events land on the same physical device as the official integration's
entities:

**Kitchen switch**

| From | Entities |
| --- | --- |
| official `blebox` | `switch.kitchen_switch`, `sensor.kitchen_switch_active_power`, `sensor.kitchen_switch_energy_last_hour`, `update.kitchen_switch_firmware` |
| `blebox_events` | `event.kitchen_switch_button_1…n`, plus the additional entities below |

This integration claims the same Device Registry identifier the official one uses
— the BleBox device ID — and additionally advertises the MAC as a connection, so
the association holds whichever integration is set up first, and even if the
official one ever changes how it builds identifiers.

**What you will actually see.** Since Home Assistant 2026, every config entry
gets its own device registry entry, and entries sharing an identifier or a
connection are *linked* rather than merged. So the device list shows two rows
named after your switch — one per integration — and each device page lists the
other under its linked devices. That is the current framework behaviour, not a
misconfiguration: an integration can no longer attach its entities to another
config entry's device row. Both rows carry the same name, model, hardware and
area, and can be assigned to the same area and labels.

Entity IDs are never relied upon, so renaming anything is safe.

## Supported devices

Any BleBox device that exposes physical inputs. Capability is **detected**, not
hardcoded to a model list:

* inputs are discovered from the device itself;
* automatic configuration is only offered when the device actually accepts it;
* everything else falls back to manual mode.

Developed and verified against a **Simon 55 GO switch** (`switchBox`,
hardware `s_KS.swB.1.5.T.p55ST-0.3`, API level `20220505`). Other switchBox,
buttonBox and actionBox hardware should work; devices with no physical inputs
are correctly ignored.

## Event types

| Event type | BleBox trigger | Meaning |
| --- | --- | --- |
| `short_press` | short click (1) | quick press and release |
| `long_press` | long click (2) | press held down |
| `press` | rising edge (4) | contact made |
| `release` | falling edge (3) | contact broken |

`press`/`release` map onto electrical edges, and which edge means "finger down"
depends on how the input is wired. If they arrive the wrong way round, enable
**Swap pressed and released** in the integration options.

Every input's entity always advertises all four types, so a URL you wire up by
hand later always has somewhere to land.

## Additional entities

Beyond the button events, the device exposes settings and state that the official
integration does not surface. Every one of these is **additive** — nothing here
duplicates the relay, power, energy or firmware entities, so no statistics or
history are affected.

| Entity | What it does |
| --- | --- |
| `light` **Button backlight** | The illuminated buttons, with RGB colour |
| `switch` **BleBox cloud tunnel** | The device's outbound tunnel to BleBox's cloud |
| `switch` **Status LED** | The device's status indicator |
| `number` **Overload threshold** | Power above which the device cuts its own relay; `0` disables it |
| `select` **State after power cut** | Relay behaviour on power-up: off, on, or restore previous |
| `binary_sensor` **Overload protection** | On when the device has tripped, with the reason preserved |
| `binary_sensor` **Power measurement calibrated** | Diagnostic |
| `sensor` **Uptime** | Time since the device booted — *disabled by default* |
| `sensor` **Timer remaining** | Countdown on a timed relay operation — *disabled by default* |

Entities are created only when the device actually reports the underlying
setting, so a BleBox device without a backlight simply gets no backlight entity.
The accepted overload range is read from the device's own constraint metadata
rather than hardcoded.

The two `sensor` entities are **disabled by default** — they change on every
poll, so leaving them on would fill the recorder for values most setups never
look at. Enable either from the device page if you want it.

**The cloud tunnel is worth a look.** BleBox devices hold an outbound tunnel to
BleBox's cloud by default (`tunnel.enabled: 1`). Turning it off is the difference
between a genuinely local device and one that merely happens to be controlled
locally. Note that the tunnel is also how the device pulls OTA firmware, so the
official integration's update entity may stop working with it disabled.

These are polled — they are state, not events — on a one-minute interval. That is
unrelated to the button events, which are always pushed and never polled for.

Writes go through `/api/settings/set`, which is **as undocumented as the actions
API**. It is isolated in the same module and can be ignored entirely: every
read-only sensor above keeps working regardless.

## Installation

### HACS

1. HACS → ⋮ → **Custom repositories**
2. Repository: `https://github.com/sdebasek/blebox-events`, category **Integration**
3. Install **BleBox Events**, then restart Home Assistant
4. **Settings → Devices & Services → Add Integration → BleBox Events**

### Manual

Copy `custom_components/blebox_events/` into your Home Assistant `config/custom_components/` directory and restart.

Requires Home Assistant **2025.2.0** or newer.

## Setup

Devices are discovered over zeroconf/DHCP, or you can enter an IP address. The
flow then identifies the device, discovers its physical inputs, and asks which
events you want per input:

```
Input 1:  [x] Short press  [x] Long press  [ ] Pressed  [ ] Released
Input 2:  [x] Short press  [x] Long press  [ ] Pressed  [ ] Released

Configuration mode:  (•) Automatic   ( ) Manual
```

### Automatic mode

The integration writes the callbacks into the device's own action slots. It is
deliberately conservative:

* it reads the current configuration first and **never touches an action it did
  not create** — ownership is determined by the callback URL, not by a name you
  might rename;
* capacity is checked **before the first write**, so a run either fits entirely
  or changes nothing at all;
* reconfiguring **updates** the existing actions instead of creating duplicates;
* if there are not enough free slots, setup tells you how many are needed versus
  free rather than making room;
* deleting the integration removes **only** its own actions, and if the device
  cannot be reached it leaves them alone and logs a warning instead of guessing.

### Manual mode

The integration shows the exact URLs to configure in the wBox app:

| Input | Event | URL to call |
| --- | --- | --- |
| 1 | `short_press` | `http://homeassistant.local:8123/api/blebox_events/<token>/0/short_press` |
| 1 | `long_press` | `http://homeassistant.local:8123/api/blebox_events/<token>/0/long_press` |

In the wBox app: device → **Actions** → add action → pick the input and trigger
(short click / long click / rising edge / falling edge), action type **Call URL**,
and paste the URL.

You can see these again at any time under **Options → Show callback URLs**.

The event receiver is completely independent of the automatic programming code.
**If a firmware update breaks automatic configuration, manually configured
callbacks keep working.**

## Using the events

### Device trigger (recommended)

**Settings → Automations → Create → Add trigger → Device**, pick the switch:

* Kitchen switch — Button 1 short pressed
* Kitchen switch — Button 1 long pressed
* Kitchen switch — Button 1 pressed
* Kitchen switch — Button 1 released

### Event entity

```yaml
triggers:
  - trigger: state
    entity_id: event.kitchen_switch_button_1
    attribute: event_type
    to: long_press
actions:
  - action: light.turn_on
    target:
      entity_id: light.kitchen
```

## Security

* The endpoint is unauthenticated by necessity — a BleBox device cannot present
  a Home Assistant token — so **each device gets its own cryptographically
  random token** that forms part of the URL.
* Tokens are compared in constant time; an unknown token gets a bare `404`, so
  the endpoint reveals nothing about which tokens exist.
* Only known inputs and known event types are accepted, validated against what
  the device actually reported.
* The token is stored in the config entry, kept out of logs, and redacted from
  diagnostics.
* **Nothing needs to be exposed to the internet.** Home Assistant only has to be
  reachable from the device's own network. If the device sits on a separate VLAN,
  set the Home Assistant URL explicitly during setup.

Treat a callback URL like a password: anyone who can reach Home Assistant and
knows one can fire that event.

## Troubleshooting

Turn on debug logging first:

```yaml
logger:
  default: warning
  logs:
    custom_components.blebox_events: debug
```

Every accepted callback is logged, as is every rejection and why.

| Symptom | Cause and fix |
| --- | --- |
| Nothing happens when pressing the button | Check the device can reach Home Assistant. From the device's network: `curl -v "<callback URL>"` — it should return `OK`. |
| `404` from the callback URL | The token, input index or event type is wrong. Copy the URL again from **Options → Show callback URLs**. Input indices in URLs are **0-based** while buttons are labelled from 1. |
| `503` from the callback URL | The config entry is unloaded — check Home Assistant logs for the reason. |
| Setup says it cannot reach the device | Home Assistant needs a route to the device. See *Isolated IoT VLANs* below. |
| Setup works but no events ever arrive | Almost always the reverse direction: the device cannot reach Home Assistant. See *Isolated IoT VLANs* below. |
| "Not enough free action slots" | The device has a fixed number of action slots (30 on tested hardware). Free some in wBox, select fewer events, or use manual mode. |
| Automatic mode is not offered | The device did not answer the action API, or does not accept HTTP actions. Use manual mode — it is fully supported. |
| `press` and `release` are swapped | Enable **Swap pressed and released** in the options. |
| Duplicate events | The device retried a call. The duplicate-suppression window (default 150 ms) absorbs this; raise it in the options if needed, or set it to 0 to disable. |
| Two device rows for one switch | Expected on Home Assistant 2026+ — see *Result* above. Each config entry owns a row and they are linked, not merged. Worth checking only that both rows show the same model and hardware version; if they do not, they are not the same device. |

### Isolated IoT VLANs

This is the most common reason a working setup produces no events, so it is worth
understanding before blaming the integration.

Two **independent** directions have to be open:

1. **Home Assistant → device** (`TCP 80`) — setup, metadata polling and automatic
   configuration.
2. **Device → Home Assistant** (`TCP 8123`, or your HA port) — the callbacks.

Direction 1 usually already works, because the official BleBox integration polls
the device. That proves nothing about direction 2. A typical IoT VLAN policy
allows *return* traffic only, meaning the device can answer Home Assistant but can
never start a connection of its own — which is precisely what a callback is. On a
UniFi zone-based firewall this shows up as the `IoT → Internal` cell reading
**Allow Return** rather than Allow All.

The fix is one narrow allow policy, ordered above the zone's catch-all block:

| Field | Value |
| --- | --- |
| Action | Allow |
| Source Zone | IoT (wherever the device lives) |
| Source | the BleBox device — prefer its MAC/client over an IP that DHCP can change |
| Destination Zone | Internal (wherever Home Assistant lives) |
| Destination | the Home Assistant IP |
| Protocol | TCP |
| Destination Port | `8123` |

Then set **Home Assistant URL for the device to call** to a plain IP, e.g.
`http://192.168.1.10:8123`. The auto-detected value may be a `.local` hostname,
and mDNS does not cross VLANs without a repeater, so the device would never
resolve it.

The firewall rule's own hit counter is the quickest confirmation that the switch
is really calling in.

#### Asking the device what happened

The device records the outcome of each action's most recent call, which settles
"the device never called" versus "Home Assistant rejected it" without guesswork:

```bash
curl -s http://<device>/api/actions/state | \
  python3 -c "import json,sys; [print(a['id'], a['name'], a.get('lastCall')) \
    for a in json.load(sys.stdin)['actions'] if a.get('actionType')==50]"
```

| `lastCall` | Meaning |
| --- | --- |
| `{"timeElapsedS": -1}` | never called — the trigger did not fire, so check the action's input and trigger type |
| `status: 0`, non-zero `errorCode` | called, but no connection — routing or firewall between the device and Home Assistant |
| `status: 404` | reached Home Assistant, wrong URL — re-copy it from **Options → Show callback URLs** |
| `status: 200` | working |

To confirm the device's HTTP client itself is healthy, point one action at
`http://example.com/` temporarily; a `status: 200` there alongside `status: 0`
for Home Assistant isolates the problem to the network path.

Diagnostics (⋮ → **Download diagnostics** on the integration) report the model,
firmware, detected inputs, slot usage, whether automatic configuration is
supported and the full callback mapping, with tokens redacted.

## The BleBox action API

Automatic configuration relies on an API that BleBox does not publish — their
technical portal states that "only main functionalities are open for public",
and the action CRUD surface is not in any OpenAPI spec.

All of it is isolated in a single module, `blebox_actions.py`, behind a small
abstraction (`BleBoxActionManager`). Nothing else in the integration talks to
the device, so a firmware change is contained to that one file — and the event
receiver does not depend on it at all.

| Method | Path | Status |
| --- | --- | --- |
| `GET` | `/api/device/state` | documented |
| `GET` | `/api/actions/state` | **undocumented** — action slots, `itemsLimit`, and a `fieldsPreferences` constraint engine |
| `POST` | `/api/actions/set` | **undocumented** — upserts exactly one action as `{"action": {...}}` |

Two observed behaviours matter and are enforced in code:

* `/api/actions/set` takes **one action per request**, not the whole array; and
* the action object must be **round-tripped** — the device's own object sent back
  with only the edited fields changed. Hardware-specific fields such as
  `relay`/`forTime`/`ns` exist on some revisions and dropping them makes the
  device reject the save with HTTP 400.

Physical inputs are derived from the non-null `input` values in the
`fieldsPreferences` `triggerType` constraints, because `switchBox` hardware does
not report an `inputs[]` array anywhere else.

Credit for documenting these shapes against live hardware goes to
[Device-Manager-for-BleBox](https://github.com/ThomasKiljanczykDev/Device-Manager-for-BleBox)
(`docs/action-shape.md`).

## Contributing

`pip install -r requirements-test.txt && pytest` runs the test suite, which
covers the parts that must not silently regress: input discovery, ownership
detection, and that reconciliation never touches a foreign action or
half-provisions a device.

## License

MIT
