# The BleBox device API

[← back to README](../README.md)

BleBox's [technical portal](https://technical.blebox.eu/) publishes OpenAPI
specifications for their devices, but states that "only main functionalities are
open for public". The action and settings CRUD surfaces are not in any published
spec.

All device communication is isolated in a single module,
`custom_components/blebox_advanced/blebox_actions.py`. Nothing else in the
integration talks to a device, so a firmware change is contained to that one
file, and the event receiver does not depend on it at all.

## Endpoints

| Method | Path | Status |
| --- | --- | --- |
| `GET` | `/api/device/state` | documented. Identity: id, type, product, `fv`, `hv`, `apiLevel`, `availableFv` |
| `GET` | `/state/extended` | documented. Relays, power measurement, safety, sensors |
| `GET` | `/api/device/uptime` | documented |
| `GET` | `/s/{relay}/{state}` | documented. Answers with the resulting relay state |
| `GET` | `/api/actions/state` | **undocumented.** Action slots, `itemsLimit`, `fieldsPreferences` |
| `POST` | `/api/actions/set` | **undocumented.** One action per request, `{"action": {...}}` |
| `GET` | `/api/settings/state` | public endpoint, unspecified contents |
| `POST` | `/api/settings/set` | **undocumented.** Partial patch, `{"settings": {...}}` |
| `POST` | `/api/ota/update` | **undocumented.** Starts a firmware update |

The two `set` payload shapes were confirmed against the device's own wBox UI
bundle, which it serves at `/settings.js` and `/main.js` (gzip compressed).

## Behaviours established by experiment

These are not documented anywhere, were determined against live hardware, and
are enforced in code.

### Actions must be round-tripped

`/api/actions/set` takes **one action per request**, not the whole array. The
action object must be sent back with only the edited fields changed.
Hardware-specific fields such as `relay`, `forTime` and `ns` exist on some
revisions, and dropping them makes the device answer HTTP 400.

Empty slots omit fields that configured slots carry, so filling one needs the
field shape of the device as a whole, not of that slot.

`lastCall` is server-managed telemetry and must be stripped before saving.

### Physical inputs are not listed anywhere obvious

`switchBox` hardware reports no `inputs[]` array. The reliable source is the
`fieldsPreferences` entry named `triggerType`: its constraints list one entry per
input, plus one with `input: null` for device-level triggers. The distinct
non-null values give the input count.

### Trigger types

| Value | Meaning |
| --- | --- |
| `0` | unconfigured, the empty-slot marker |
| `1` | short click |
| `2` | long click |
| `3` | falling edge |
| `4` | rising edge |
| `5` | any edge |
| `19` | **periodic timer**, fires every `triggerParam` seconds |
| `42` / `43` | power above / below a threshold |

Type `19` was identified by writing a probe action, watching its `lastCall`
counter cycle, and confirming the period matched `triggerParam` (values of 30,
60 and 10 were all honoured; `0` is floored to 5).

**No trigger fires when the relay changes state.** This is why relay state
reporting can only ever be periodic, and why the integration treats it as
reversed polling rather than push.

### Action types

`1` switch on, `2` switch off, `3` toggle, `50` HTTP GET. The device also offers
`7-10` and `51-53`, which are not identified; the integration never writes them
and never rewrites a slot holding one.

### URL placeholders

The `param` field for an HTTP action supports placeholders the device
substitutes before calling: `{s_state.0}` for relay state and `{power_w.0}` for
power. The braces are sent unencoded because the device matches them literally.
Firmware that does not know a placeholder passes it through verbatim, so a value
still wrapped in braces means "this device cannot tell us", not zero.

### Both set endpoints answer with the resulting state

`/s/{relay}/{state}` returns the relay list, and `/api/settings/set` returns the
full settings object. The integration trusts those answers rather than assuming
a write took effect, which is what makes controls respond immediately and
correctly reflect values the device normalises. Setting the backlight to
`ffffff` and being handed back `fffefa` is a real example.

## Ownership

Actions created by this integration are identified by their URL containing
`/api/blebox_advanced/`, never by their name. A user can rename an action in the
wBox app without the integration losing track of it, and a rotated callback token
or changed Home Assistant URL still resolves to the same slot.

Everything else in the slot table is foreign and is never modified, with one
deliberate exception: the opt-in button behaviour control, which writes only
slots holding a native relay action.

## Credit

The action object shapes were first documented against live hardware by
[Device-Manager-for-BleBox](https://github.com/ThomasKiljanczykDev/Device-Manager-for-BleBox).
