# hacs-udi-iox

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg?style=for-the-badge)](https://github.com/hacs/integration)

Home Assistant custom component for **Universal Devices eisy / Polisy** controllers running **IoX 6.0+**.

## Legacy Hardware Scope

If you have ISY-994 hardware, use the existing Home Assistant core [`isy994` integration](https://www.home-assistant.io/integrations/isy994/) (which stays on `pyisy` 3.x). The two integrations register distinct domains and coexist on the same HA instance.

## Why a separate repo

`hacs-isy994` was a stable beta-testing channel for the upstream `isy994` integration which served it's purpose, but I no longer want to maintain legacy hardware support for Home Assistant's core `isy994` and try to maintain both new features and legacy support in another repo. The eisy / IoX-6+ rewrite is a clean break: different library (`pyisyox` v6 with JWT/portal auth, WebSocket-only, classifier-driven entity routing, ergonomic Node wrappers). Forcing existing `hacs-isy994` users onto that rewrite would regress their working ISY-994 setups, so this is a new repo with a new domain.

## Installation

> **IF YOU ARE USING THE CORE ISY994 INTEGRATION:** While they can co-exist side-by-side, you may wish to remove that integration before adding this one. For the large portion of entities that overlap, they will try to get the same `entity_id` and you will end up with everything suffixed with `domain.*_2`. To keep your dashboards intact as much as possible, remove the `isy994` integration, restart HA once (needed for installing this repo anyways) and then add this integration to restore the entities. Any `entity_id` that you manually renamed on the old integration will need to be renamed again.

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
