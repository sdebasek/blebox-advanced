# Setup

[← back to README](../README.md)

## Adding a device

Devices are discovered over zeroconf and DHCP, so your switch may already be
waiting under **Settings → Devices & Services**. Otherwise add the integration
manually and enter its IP address.

The flow then:

1. connects to the device and reads its identity (model, firmware, API level);
2. discovers how many physical inputs it has;
3. asks which events you want from each input;
4. either configures the device for you, or shows you the URLs to paste in.

```
Input 1:  [x] Short press  [x] Long press  [ ] Pressed  [ ] Released
Input 2:  [x] Short press  [x] Long press  [ ] Pressed  [ ] Released

Configuration mode:  (•) Automatic   ( ) Manual
```

Every input gets an entity advertising all four event types regardless of what
you tick here. The selection controls which callbacks are written to the device,
so a URL you wire up by hand later always has somewhere to land.

### Home Assistant URL

The flow pre-fills the URL it thinks the device should call. **Check it.** The
detected value is often a `.local` hostname, and mDNS does not cross VLANs, so a
device on a separate network will never resolve it. A plain IP is safest:

```
http://192.168.1.10:8123
```

## Automatic mode

The integration writes the callbacks into the device's own action slots. It is
deliberately conservative, because those slots may already hold configuration
you built yourself:

- it reads the current configuration first and **never touches an action it did
  not create**. Ownership is determined by the callback URL, not by a name you
  might rename in the wBox app;
- capacity is checked **before the first write**, so a run either fits entirely
  or changes nothing at all;
- reconfiguring **updates** the existing actions rather than creating
  duplicates;
- if there are not enough free slots, setup tells you how many are needed versus
  how many are free instead of making room;
- deleting the integration removes **only** its own actions. If the device
  cannot be reached, they are left alone and a warning is logged rather than
  guessing at device configuration.

Automatic mode is only offered when the device actually answers the action API
and accepts HTTP actions. Everything degrades to manual mode.

## Manual mode

The integration shows the exact URLs to configure:

| Input | Event | URL to call |
| --- | --- | --- |
| 1 | `short_press` | `http://192.168.1.10:8123/api/blebox_advanced/<token>/0/short_press` |
| 1 | `long_press` | `http://192.168.1.10:8123/api/blebox_advanced/<token>/0/long_press` |

In the wBox app: device → **Actions** → add action → pick the input and trigger
(short click, long click, rising edge, falling edge), action type **Call URL**,
and paste the URL.

You can see them again at any time under **Options → Show callback URLs**.

Note that input indices in URLs are **0-based** while buttons are labelled from
1, so button 1 is `/0/`.

The event receiver is completely independent of the automatic programming code.
**If a firmware update ever breaks automatic configuration, manually configured
callbacks keep working.**

## Options

Reachable via **Settings → Devices & Services → BleBox Advanced → Configure**.

### Button events and configuration mode

Change which events each input sends, switch between automatic and manual, or
correct the Home Assistant URL. Saving re-provisions the device.

### Delivery and cleanup

| Option | Default | What it does |
| --- | --- | --- |
| Duplicate suppression window | 150 ms | Ignores an identical event repeated within the window, absorbing device retries. Set to 0 to disable. |
| Swap pressed and released | off | Use if `press` and `release` arrive the wrong way round for how the input is wired. |
| Remove actions when deleting | on | Only actions created by this integration are ever removed. |
| Let Home Assistant change what buttons do | off | See below. |
| Report relay state from the device | off | See below. |
| State report interval | 30 s | Only used when the above is on. |

### Show callback URLs

Displays the current URLs. Treat them as secrets: anyone who can reach Home
Assistant and knows one can fire that event.

## Changing what a button does

Off by default. Enabling it adds a control per button and press type setting the
local relay action, so a button can be repurposed without opening the wBox app:

- **Nothing** clears the action
- **Turn relay on** / **Turn relay off** / **Toggle relay**

This is the one place the integration writes something you configured. It only
ever touches a slot holding a native relay action or an empty one. An HTTP
action, or one of the action types the device offers but that are not identified
(`7-10`, `51-53`), is always left alone.

## Reporting relay state from the device

Off by default, and worth understanding before you turn it on.

The hardware has **no trigger that fires when the relay changes**. The only
device-level trigger is a periodic timer, so this is polling with the direction
reversed: the device calls Home Assistant every N seconds with its current
state. It costs one action slot and is no fresher than the interval you pick.

The relay switch already polls every 5 seconds without it, so enable this only
if you specifically want the device driving the update.

## Using the events in automations

| Event type | BleBox trigger | Meaning |
| --- | --- | --- |
| `short_press` | short click (1) | quick press and release |
| `long_press` | long click (2) | press held down |
| `press` | rising edge (4) | contact made |
| `release` | falling edge (3) | contact broken |

Events carry the device's state at the instant of the press, so they gain
`relay_state` and `power_w` attributes where the device supports it. Presses are
never inferred by polling the relay, which could not tell a wall press from a
Home Assistant command and could never detect a long press.

### Device trigger

**Settings → Automations → Create → Add trigger → Device**, pick the switch, and
choose e.g. *Button 1 long pressed*.

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
