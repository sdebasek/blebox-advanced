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
you tick here, so a URL you wire up by hand later always has somewhere to land.
An input you select no events for is still registered but **disabled by
default**, which keeps optional inputs out of the way.

That matters because the device reports every input it *could* have, not the
ones actually wired. A switchBox exposes an external input terminal alongside
its touch buttons, and it appears in the list whether or not anything is
connected to it. Leave its events unticked and it stays hidden.

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
- if there are not enough free slots, setup refuses instead of making room. The
  message is thinner than it ought to be: it tells you to free some slots in the
  wBox app, select fewer events, or use manual mode, without naming a single
  number. The figure to compare your ticked events against is on the same
  screen, **Free action slots on the device**, and it counts slots this
  integration already owns, since those get reused. A shortage found later, when
  the callbacks are written again after saving options, does report both numbers,
  but into the Home Assistant log rather than the interface;
- it puts its own callbacks back. Delete one in the wBox app and the next
  metadata poll notices it is missing and writes it again, so it is usually
  back within a minute. That is what makes a factory reset or a restored backup
  repair itself, but it also means the wBox app is not the place to remove a
  callback: untick that event in the options and the integration clears the slot
  itself, or switch to manual mode and it never writes a callback slot again,
  leaving whatever is already on the device exactly as it is;
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

The URLs are shown without a query string, so events fired by them carry no
device state. Append `?p={power_w.0}` to a URL, braces and all, if you want its
events to arrive with the `power_w` attribute described under [using the events
in automations](#using-the-events-in-automations).

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

### Show callback URLs

Displays the current URLs. Treat them as secrets: anyone who can reach Home
Assistant and knows one can fire that event.

## Changing what a button does

Off by default. Enabling it adds a control per button and press type setting the
local relay action, so a button can be repurposed without opening the wBox app:

- **Detached (events only)** clears the local action, so the button no longer
  touches the relay at all and exists purely to fire events into Home Assistant
- **Turn relay on** / **Turn relay off** / **Toggle relay** bind it to the relay

Detached is the interesting one: it is how you turn a wall switch into a
programmable button. The relay stays under Home Assistant's control, and the
button drives whatever automation you point it at.

This is the one place the integration writes something you configured. It only
ever touches a slot holding a native relay action or an empty one. An HTTP
action, or one of the action types the device offers but that are not identified
(`7-10`, `51-53`), is always left alone.

## Using the events in automations

| Event type | BleBox trigger | Meaning |
| --- | --- | --- |
| `short_press` | short click (1) | quick press and release |
| `long_press` | long click (2) | press held down |
| `press` | rising edge (4) | contact made |
| `release` | falling edge (3) | contact broken |

An event can carry the device's state at the instant of the press, as a
`power_w` attribute. The device substitutes that value into the callback URL
before calling it, so the attribute exists only where the URL asks for it:

- **automatic mode** appends `?p={power_w.0}` to every URL it writes, on every
  device that advertises the placeholder, so the attribute is simply there;
- **manual mode** shows URLs with no query string, so a URL pasted as displayed
  delivers no attribute at all. Add `?p={power_w.0}` to the end of it yourself.
  The braces are sent unencoded on purpose, because the device matches them
  literally.

Firmware that does not know the placeholder passes it through verbatim, and a
value still wrapped in braces is discarded rather than read as zero. A missing
`power_w` therefore means "this device did not tell us", never "0 W".

Presses are never inferred by polling the relay, which could not tell a wall
press from a Home Assistant command and could never detect a long press.

There is no `relay_state` attribute. The device offers a placeholder for it, but
it was measured to return a constant regardless of the actual relay state, so
publishing it would have meant handing automations a value that is always the
same. See [the device API notes](device-api.md#url-placeholders).

### The switch keeps up with the wall button

A button the device itself binds to the relay - `OUT ON`, `OUT OFF` or `OUT TOG`
in the wBox app - moves the switch entity the moment the press arrives, instead
of up to five seconds later at the next poll. Nothing in the callback says what
the relay did; the binding does, and the device applies it the same way every
time, so the new state is worked out from the slot the press fires.

The device is then asked to confirm immediately, and its answer wins. Unlike a
command sent from Home Assistant, a prediction is never defended against a
contradicting poll: if the binding was changed in the wBox app since it was last
read, or the press did not reach the relay, the very next poll puts the switch
right. A device whose action API is silent has no binding to read, so its relay
simply keeps waiting for the poll, exactly as before.

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
