[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg?style=for-the-badge)](https://hacs.xyz)
[![GitHub Release](https://img.shields.io/github/v/release/sdebasek/blebox-advanced?style=for-the-badge)](https://github.com/sdebasek/blebox-advanced/releases)
[![Validate](https://img.shields.io/github/actions/workflow/status/sdebasek/blebox-advanced/validate.yml?branch=main&style=for-the-badge&label=validate)](https://github.com/sdebasek/blebox-advanced/actions)
[![License](https://img.shields.io/github/license/sdebasek/blebox-advanced?style=for-the-badge)](LICENSE)

# BleBox Advanced

Full local Home Assistant integration for **BleBox** µWiFi / wBox devices,
including **Kontakt-Simon Simon 55 GO** wall switches.

It replaces the official BleBox integration and adds what that one never
exposed: physical button presses as real events, the button backlight, the
BleBox cloud tunnel, overload protection, relay behaviour after a power cut, and
the ability to change what each button does.

## Contents

- [Why not the official integration](#why-not-the-official-integration)
- [Supported devices](#supported-devices)
- [Installation](#installation)
- [Entities](#entities)
- [Configuration](#configuration)
- [Migrating from the official integration](#migrating-from-the-official-integration)
- [Troubleshooting](#troubleshooting)
- [How it works](#how-it-works)
- [Contributing](CONTRIBUTING.md)

## Why not the official integration

The official [BleBox integration](https://www.home-assistant.io/integrations/blebox)
gives you the relay, power, energy and firmware, by polling every five seconds.
It cannot see what the wall switch itself is doing: pressing a button is
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
should work on other hardware, but it has been verified on one product line, not
thirty-seven. For a BleBox device without buttons, the official integration is
the better choice.

## Supported devices

Any BleBox device with physical inputs. Capability is detected at runtime, not
hardcoded: inputs are discovered from the device, entities appear only when the
device reports the underlying setting, and value ranges come from the device's
own constraint metadata.

Verified on a **Simon 55 GO switch** (`switchBox`, firmware `0.1502`, API level
`20220505`). `switchBoxD`, `buttonBox` and `actionBox` hardware should work too.

## Installation

Requires Home Assistant **2025.2.0** or newer. No cloud account, no BleBox app,
nothing exposed to the internet.

### HACS

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=sdebasek&repository=blebox-advanced&category=integration)

Or add it by hand: **HACS → ⋮ → Custom repositories →**
`https://github.com/sdebasek/blebox-advanced`, category **Integration**.

Restart Home Assistant after installing, then add the integration:

[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=blebox_advanced)

HACS does not configure the integration for you; adding it is a separate step.

### Manual

Copy `custom_components/blebox_advanced/` into your Home Assistant
`config/custom_components/` directory and restart.

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
| `select` **Button _n_ … action** | Opt-in. What a button does to the relay locally |

## Configuration

Devices are discovered over zeroconf and DHCP, or you can enter an IP address.
The flow identifies the device, discovers its physical inputs, and asks which
events you want per input.

**See [docs/setup.md](docs/setup.md)** for the full walkthrough: automatic
versus manual configuration, every option, how to use the events in automations,
and how the device's action slots are managed.

## Migrating from the official integration

1. Install this integration and add your device.
2. Delete or ignore the official **BleBox** entry. Running both works but gives
   you two relay switches, two power sensors and two device rows.
3. Update anything referencing the old entity IDs. They differ, because the
   domain is `blebox_advanced`.

The official energy sensor carried no `state_class`, so it produced no long-term
statistics and there is none to migrate.

## Troubleshooting

Start with the **Callback delivery** sensor and the Repairs panel. Between them
they usually name the cause, distinguishing a device that never called from one
Home Assistant rejected.

The most common cause of "setup works but no events arrive" is an isolated IoT
VLAN: the device needs to reach Home Assistant, not just the other way round.

**See [docs/troubleshooting.md](docs/troubleshooting.md)** for the symptom table,
firewall guidance, and how to ask the device what happened to its last call.

## How it works

Button events are pushed by the device over the local network, so they arrive
immediately. Everything else is polled: relay and power every 5 seconds,
settings once a minute.

Some of the device endpoints used are not in any published BleBox specification.
All device communication is isolated in one module so a firmware change is
contained there, and the event receiver does not depend on it.

**See [docs/device-api.md](docs/device-api.md)** for the endpoints, what is
documented versus reverse engineered, and the behaviours established by
experiment.

---

<sub>Keywords: BleBox, wBox, µWiFi, Simon 55 GO, SIMON 55 GO SWITCH, Kontakt-Simon,
switchBox, switchBoxD, buttonBox, actionBox, Home Assistant, HACS, custom
integration, wall switch, button events, local push.</sub>
