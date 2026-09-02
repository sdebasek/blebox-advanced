# Security policy

## Supported versions

The latest release only. This is a single-maintainer project, so fixes go out as
a new release rather than as a patch to an older one.

## Reporting a vulnerability

Use GitHub's [private vulnerability reporting][advisory] on this repository.
That opens a draft advisory only you and the maintainer can read, which is the
right place for anything you would not want in a public issue.

[advisory]: https://github.com/sdebasek/blebox-advanced/security/advisories/new

Please do not open a public issue for a vulnerability first. Anything that turns
out not to be one is very welcome as a normal issue afterwards.

Expect a first reply within a week. A single maintainer cannot promise faster
than that honestly.

## What the integration exposes

Worth knowing before you report, because two of these look alarming and are
deliberate.

**The callback endpoint is unauthenticated.** `/api/blebox_advanced/<token>/…`
is registered with `requires_auth = False`, because the device is a wall switch
with no way to hold a Home Assistant bearer token. The URL's own token is the
credential: 128 bits from `secrets.token_hex(16)`, compared with
`hmac.compare_digest` so a wrong guess takes the same time as a right one. Only
the event type named in the URL can be fired, and only for the input named in
it. It reads nothing and writes nothing on the device.

**Callback URLs are secrets.** Anyone who can reach Home Assistant and knows one
can fire that event, and therefore anything it triggers. They are redacted from
diagnostics, never logged, and shown in the options only in manual mode where
you have to paste them yourself.

**Device traffic is plain HTTP on the local network.** BleBox hardware offers no
TLS, so the token crosses the LAN in the clear. A network position that can read
it can already talk to the switch directly. Keeping such devices on their own
VLAN is worth doing for other reasons and is covered in
[docs/troubleshooting.md](docs/troubleshooting.md).

**The device's own access point is a separate risk.** BleBox devices ship with
their setup access point on, often unprotected. This integration surfaces it as
a switch so you can see and turn it off; it does not turn it off for you.

## Out of scope

Vulnerabilities in Home Assistant core, HACS, or BleBox firmware. Report those
to the projects that own them. A firmware issue this integration makes reachable
in a new way is in scope, so say so if that is what you have found.
