# hacs-udi-iox

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg?style=for-the-badge)](https://github.com/hacs/integration)

Home Assistant custom component for **Universal Devices eisy / Polisy** controllers running **IoX 6.0+**.

## Status

Alpha. Tracks `pyisyox` 6.x. Feature-equivalent to `hacs-isy994` for IoX-6+ hardware (authentication, classifier-driven entities, services, variables, SSDP discovery, Repair-card lifecycle UX). Targets the Bronze [integration quality scale](https://developers.home-assistant.io/docs/core/integration-quality-scale/) tier — see [`custom_components/udi_iox/quality_scale.yaml`](custom_components/udi_iox/quality_scale.yaml).

## Scope

- HA domain: `udi_iox`
- Library: [`pyisyox`](https://github.com/automicus/pyisyox) 6.x (eisy/Polisy on IoX 6+)
- Hardware: eisy, Polisy. **Not** ISY-994.

If you have ISY-994 hardware, use the existing [`hacs-isy994`](https://github.com/shbatm/hacs-isy994) (which stays on `pyisy` 3.x) or HA Core's first-party `isy994` integration. The two integrations register distinct domains and coexist on the same HA instance.

## Why a separate repo

`hacs-isy994` is a stable beta-testing channel for the upstream `isy994` integration. The eisy / IoX-6+ rewrite is a clean break: different library (`pyisyox` v6 with JWT/portal auth, WebSocket-only, classifier-driven entity routing, ergonomic Node wrappers). Forcing existing `hacs-isy994` users onto that rewrite would regress their working ISY-994 setups, so this is a new repo with a new domain.

## Installation

This is a HACS Custom Repository:

1. HACS → Integrations → ⋮ → Custom repositories
2. Add `https://github.com/shbatm/hacs-udi-iox`, category Integration
3. Install, restart HA, then add the integration via Settings → Devices & Services.

## Removal

1. Settings → Devices & Services → Universal Devices IoX → ⋮ → Delete (removes the config entry, its devices, and its entities).
2. Optionally remove the repository in HACS (Integrations → Universal Devices IoX → ⋮ → Remove) and restart HA.

## Supported devices

The integration surfaces every device on your controller — Insteon, Z-Wave, Zigbee, Matter, and any [PG3 node-server plugin](https://www.universal-devices.com/polyglot/) you have installed. HA platform routing is driven by `pyisyox`'s classifier reading each device's nodedef, not a hard-coded table.

If you bridge a device through a PG3 plugin **and** Home Assistant has a first-party integration for that same device (Sonos, Hue, Rachio, Roku, Plex, Ecobee, Shelly, WLED, etc.), install HA's native integration directly. It will almost always be the better source of truth — local-push state, manufacturer-aware quirks, more device-specific features. The two coexist fine: this integration still exposes the device's IoX-side state (useful if your IoX programs reference the device), and the native integration handles the device itself.

See [`docs/supported-devices.md`](docs/supported-devices.md) for the full list of PG3 plugins with HA-native equivalents, the cases where the IoX path is the right call, and roadmap notes for the platforms still to come.

## Roadmap

Open issues and milestones are tracked in [GitHub Issues](https://github.com/shbatm/hacs-udi-iox/issues).
