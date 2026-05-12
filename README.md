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

## Roadmap

Open issues and milestones are tracked in [GitHub Issues](https://github.com/shbatm/hacs-udi-iox/issues).
