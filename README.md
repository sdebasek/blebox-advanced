# BleBox Advanced — Home Assistant integration

Full local Home Assistant integration for **BleBox** µWiFi / wBox devices,
including **Kontakt-Simon Simon 55 GO** wall switches.

It replaces the official BleBox integration and adds everything that one never
exposed: physical button presses as real events, the button backlight, the
BleBox cloud tunnel, overload protection, relay behaviour after a power cut, and
the ability to change what each button does.

```
physical button → BleBox input action → local HTTP call → Home Assistant
                → event entity + device trigger → your automation
```

## Why not the official integration

The official [BleBox integration](https://www.home-assistant.io/integrations/blebox)
gives you the relay, power, energy and firmware — by polling, every five
seconds. It cannot see what the wall switch itself is doing: pressing a button is
invisible unless it happens to toggle that relay, and it exposes none of the
device's configuration.

| | Official `blebox` | BleBox Advanced |
| --- | --- | --- |
| Relay, power, energy, firmware | ✅ | ✅ |
| Button presses (short/long/press/release) | ❌ | ✅ pushed by the device |
| Button backlight (RGB) | ❌ | ✅ |
| BleBox cloud tunnel on/off | ❌ | ✅ |
| Overload threshold, restart behaviour | ❌ | ✅ |
| Change what a button does locally | ❌ | ✅ opt-in |
| Delivery diagnostics | ❌ | ✅ |
| Device coverage | ~37 device types | switchBox family, others by capability |

**That last row is the honest trade.** The official integration is written by
BleBox and covers their whole range through `blebox_uniapi`. This one is built
around devices with physical inputs and detects capabilities at runtime, so it
should work on other hardware — but it has been verified on one product line, not
thirty-seven. If you have a BleBox device without buttons, the official
integration is the better choice.

## Supported devices

Verified against a **Simon 55 GO switch** — Kontakt-Simon's BleBox-based wall
switch, sold as *Simon 55 GO* / *SIMON 55 GO SWITCH* (`switchBox`, hardware
`s_KS.swB.1.5.T.p55ST-0.3`, API level `20220505`, five physical buttons).

Capability is **detected, not hardcoded**: inputs are discovered from the
device, entities appear only when the device reports the underlying setting, and
value ranges come from the device's own constraint metadata. So other
`switchBox`, `switchBoxD`, `buttonBox` and `actionBox` hardware should work, as
should any BleBox device exposing physical inputs.

## Entities

| Entity | Purpose |
| --- | --- |
| `event` **Button 1…n** | One per physical input: `short_press`, `long_press`, `press`, `release` |
| `switch` **Relay** | The relay, polled every 5s |
| `sensor` **Active power** | Watts drawn right now |
| `sensor` **Energy this period** | kWh for the device's current measurement period |
| `update` **Firmware** | Version reported by the device, with install |
| `light` **Button backlight** | The illuminated buttons, RGB colour |
| `switch` **BleBox cloud tunnel** | The device's outbound tunnel to BleBox's cloud |
| `switch` **Status LED** | The device's status indicator |
| `number` **Overload threshold** | Power above which the device cuts its own relay; `0` disables |
| `select` **State after power cut** | Relay behaviour on power-up, one per relay |
| `binary_sensor` **Callback delivery** | On when button presses are not reaching Home Assistant |
| `binary_sensor` **Overload protection** | On when the device has tripped, reason preserved |
| `binary_sensor` **Power measurement calibrated** | Diagnostic |
| `sensor` **Uptime**, **Timer remaining** | Diagnostic, disabled by default |
| `select` **Button _n_ … action** | *Opt-in.* What a button does to the relay locally |

## Migrating from the official integration

1. Install this integration and add your device.
2. **Settings → Devices & Services → BleBox → Delete** (or *Ignore* the
   discovery). Running both is supported but gives you two relay switches, two
   power sensors and two device rows.
3. Update anything referencing the old entity IDs. They differ — the domain is
   `blebox_advanced`, not `blebox`.

The official integration's energy sensor carried no `state_class`, so it never
produced long-term statistics; there is no statistics history to migrate. Plain
recorder history for the old entities stays in the database but does not carry
into the new ones.

## Requirements

Home Assistant **2025.2.0** or newer. No cloud account, no BleBox app, nothing
exposed to the internet.

## Installation

### HACS

1. HACS → ⋮ → **Custom repositories**
2. Repository `https://github.com/sdebasek/blebox-advanced`, category **Integration**
3. Install **BleBox Advanced**, then restart Home Assistant
4. **Settings → Devices & Services → Add Integration → BleBox Advanced**

### Manual

Copy `custom_components/blebox_advanced/` into your Home Assistant
`config/custom_components/` directory and restart.

## Setup

Devices are discovered over zeroconf/DHCP, or you can enter an IP address. The
flow identifies the device, discovers its physical inputs, and asks which events
you want per input:

```
Input 1:  [x] Short press  [x] Long press  [ ] Pressed  [ ] Released
Input 2:  [x] Short press  [x] Long press  [ ] Pressed  [ ] Released

Configuration mode:  (•) Automatic   ( ) Manual
```

### Automatic mode

The integration writes the callbacks into the device's own action slots,
conservatively:

* it reads the current configuration first and **never touches an action it did
  not create** — ownership is determined by the callback URL, not by a name you
  might rename;
* capacity is checked **before the first write**, so a run either fits entirely
  or changes nothing;
* reconfiguring **updates** existing actions instead of creating duplicates;
* if slots run out, setup says how many are needed versus free rather than
  making room;
* deleting the integration removes **only** its own actions, and if the device
  is unreachable it leaves them alone and warns rather than guessing.

### Manual mode

The integration shows the exact URLs to paste into the wBox app:

| Input | Event | URL to call |
| --- | --- | --- |
| 1 | `short_press` | `http://homeassistant.local:8123/api/blebox_advanced/<token>/0/short_press` |

In wBox: device → **Actions** → add action → pick the input and trigger (short
click / long click / rising edge / falling edge), action type **Call URL**, and
paste the URL. They are available again any time under **Options → Show callback
URLs**.

The receiver is independent of the automatic programming code. **If a firmware
update breaks automatic configuration, manually configured callbacks keep
working.**

## Button events

| Event type | BleBox trigger | Meaning |
| --- | --- | --- |
| `short_press` | short click (1) | quick press and release |
| `long_press` | long click (2) | press held down |
| `press` | rising edge (4) | contact made |
| `release` | falling edge (3) | contact broken |

`press`/`release` map onto electrical edges, and which edge means "finger down"
depends on how the input is wired. If they arrive the wrong way round, enable
**Swap pressed and released** in the options.

Events carry the device's state at the instant of the press: where the device
advertises the `{s_state.0}` and `{power_w.0}` placeholders, the event gains
`relay_state` and `power_w` attributes — the values when the button was pressed,
not whatever the next poll finds.

Presses are **never** inferred by polling the relay. That cannot tell a wall
press from a Home Assistant command, breaks when the input is detached from the
relay, and can never detect a long press.

### Device trigger (recommended)

**Settings → Automations → Create → Add trigger → Device**, pick the switch:

* Kitchen switch — Button 1 short pressed
* Kitchen switch — Button 1 long pressed

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

## Changing what a button does

Off by default, under **Options → Delivery and cleanup**. Enabling it adds a
control per button and press type setting the local relay action — nothing, on,
off, or toggle — so a button can be repurposed without opening the wBox app.

This edits action slots *you* configured, which the integration otherwise never
touches. It only ever writes a slot holding a native relay action or an empty
one: an HTTP action, or one of the action types the device offers but that are
not identified (`7-10`, `51-53`), is always left alone.

## Knowing when events stop arriving

An HTTP action is fire-and-forget, so a switch that cannot reach Home Assistant
looks exactly like one nobody has pressed. The device records the outcome of each
action's last call, and the integration reads it back:

* **Callback delivery** turns on when callbacks have fired but none got through,
  with `unreachable` / `rejected` / `last_status` attributes;
* a **repair** is raised naming the likely cause — no HTTP response at all means
  a network path problem, an HTTP error means the URL is wrong.

## Security

* The endpoint is unauthenticated by necessity — a BleBox device cannot present
  a Home Assistant token — so **each device gets its own cryptographically
  random token** that forms part of the URL.
* Tokens are compared in constant time; an unknown token gets a bare `404`.
* Only known inputs and event types are accepted, validated against what the
  device reported.
* The token is stored in the config entry, kept out of logs, and redacted from
  diagnostics.
* **Nothing needs to be exposed to the internet.** Home Assistant only has to be
  reachable from the device's own network.

Treat a callback URL like a password: anyone who can reach Home Assistant and
knows one can fire that event.

## Troubleshooting

```yaml
logger:
  default: warning
  logs:
    custom_components.blebox_advanced: debug
```

| Symptom | Cause and fix |
| --- | --- |
| Nothing happens when pressing the button | Check **Callback delivery** and any repair first — they usually name the cause. |
| `404` from the callback URL | Token, input index or event type is wrong. Re-copy from **Options → Show callback URLs**. Input indices are **0-based** while buttons are labelled from 1. |
| `503` from the callback URL | The config entry is unloaded — check the logs. |
| Setup cannot reach the device | Home Assistant needs a route to it. See *Isolated IoT VLANs*. |
| Setup works but no events arrive | The reverse direction: the device cannot reach Home Assistant. See *Isolated IoT VLANs*. |
| Firmware install refuses to start | The image is fetched over BleBox's tunnel, so the **cloud tunnel** switch must be on. |
| "Not enough free action slots" | The device has a fixed number (30 on tested hardware). Free some in wBox, or select fewer events. |
| Automatic mode is not offered | The device did not answer the action API. Manual mode is fully supported. |
| `press` and `release` are swapped | Enable **Swap pressed and released**. |
| Duplicate events | The device retried a call. The suppression window (default 150 ms) absorbs this. |

### Isolated IoT VLANs

The most common reason a working setup produces no events. Two **independent**
directions have to be open:

1. **Home Assistant → device** (`TCP 80`) — setup, polling, automatic configuration.
2. **Device → Home Assistant** (`TCP 8123`, or your HA port) — the callbacks.

Direction 1 usually already works, so it proves nothing about direction 2. A
typical IoT VLAN policy allows *return* traffic only: the device can answer Home
Assistant but can never start a connection of its own — which is exactly what a
callback is. On a UniFi zone-based firewall this shows as the `IoT → Internal`
cell reading **Allow Return** rather than Allow All.

The fix is one narrow allow policy, ordered above the zone's catch-all block:

| Field | Value |
| --- | --- |
| Action | Allow |
| Source Zone | IoT (wherever the device lives) |
| Source | the BleBox device — prefer its MAC/client over an IP DHCP can change |
| Destination Zone | Internal (wherever Home Assistant lives) |
| Destination | the Home Assistant IP |
| Protocol / Port | TCP `8123` |

Then set **Home Assistant URL for the device to call** to a plain IP, e.g.
`http://192.168.1.10:8123`. The auto-detected value may be a `.local` hostname,
and mDNS does not cross VLANs without a repeater.

### Asking the device what happened

The device records the outcome of each action's most recent call, which settles
"the device never called" versus "Home Assistant rejected it":

```bash
curl -s http://<device>/api/actions/state | \
  python3 -c "import json,sys; [print(a['id'], a['name'], a.get('lastCall')) \
    for a in json.load(sys.stdin)['actions'] if a.get('actionType')==50]"
```

| `lastCall` | Meaning |
| --- | --- |
| `{"timeElapsedS": -1}` | never called — the trigger did not fire |
| `status: 0`, non-zero `errorCode` | called, but no connection — routing or firewall |
| `status: 404` | reached Home Assistant, wrong URL |
| `status: 200` | working |

Diagnostics (⋮ → **Download diagnostics**) report model, firmware, detected
inputs, slot usage, whether automatic configuration is supported and the full
callback mapping, with tokens redacted.

## The BleBox device API

BleBox's technical portal states that "only main functionalities are open for
public", and the action and settings CRUD surfaces are in no published spec. All
device communication is isolated in one module, `blebox_actions.py`, so a
firmware change is contained there — and the event receiver does not depend on
it at all.

| Method | Path | Status |
| --- | --- | --- |
| `GET` | `/api/device/state`, `/state/extended`, `/api/device/uptime` | documented |
| `GET` | `/s/{relay}/{state}` | documented; answers with the resulting relay state |
| `GET` | `/api/actions/state` | **undocumented** — action slots, `itemsLimit`, `fieldsPreferences` |
| `POST` | `/api/actions/set` | **undocumented** — one action per request, `{"action": {...}}` |
| `GET` | `/api/settings/state` | public endpoint, unspecified contents |
| `POST` | `/api/settings/set` | **undocumented** — partial patch, `{"settings": {...}}` |
| `POST` | `/api/ota/update` | **undocumented** — start a firmware update |

Behaviours established by experiment and enforced in code:

* `/api/actions/set` takes **one action per request**, and the object must be
  **round-tripped** — the device's own object sent back with only the edited
  fields changed. Hardware-specific fields such as `relay`/`forTime`/`ns` exist
  on some revisions and dropping them makes the device answer HTTP 400.
* Physical inputs come from the non-null `input` values in the
  `fieldsPreferences` `triggerType` constraints; `switchBox` reports no
  `inputs[]` array anywhere.
* Trigger types `1-5` are the physical input clicks and edges, `42`/`43` fire on
  power thresholds, and **`19` is a periodic timer** firing every `triggerParam`
  seconds — determined by writing a probe action and watching its `lastCall`
  counter cycle. **No trigger fires when the relay changes state.**
* Both `set` endpoints answer with the resulting state, which the integration
  trusts rather than assuming a write took effect.

Credit for first documenting the action shapes against live hardware goes to
[Device-Manager-for-BleBox](https://github.com/ThomasKiljanczykDev/Device-Manager-for-BleBox).

## Contributing

```bash
pip install -r requirements-test.txt && pytest
```

The suite covers what must not silently regress: input discovery, action
ownership, that reconciliation never touches a foreign action or half-provisions
a device, and that a stale poll cannot overwrite a just-written value.

## License

MIT

---

<sub>Keywords: BleBox, wBox, µWiFi, Simon 55 GO, SIMON 55 GO SWITCH, Kontakt-Simon,
switchBox, switchBoxD, buttonBox, actionBox, Home Assistant, HACS, custom
integration, wall switch, button events, local push.</sub>
