# hacs-udi-iox

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg?style=for-the-badge)](https://github.com/hacs/integration)

Home Assistant custom component for **Universal Devices eisy / Polisy** controllers running **IoX 6.0+**.

## Status

Pre-alpha. Tracks `pyisyox` 6.0.0a1.

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

## Roadmap

Open issues and milestones are tracked in [GitHub Issues](https://github.com/shbatm/hacs-udi-iox/issues).
