# CLAUDE.md

Guidance for Claude Code (claude.ai/code) when working in this repo.

## Overview

`hacs-udi-iox` is a Home Assistant **HACS custom component** for **Universal Devices eisy / Polisy** controllers running **IoX 6.0+**. It registers under HA domain `udi_iox` and consumes [`pyisyox`](https://github.com/automicus/pyisyox) **6.x**.

This repo was forked from `hacs-isy994` to host the IoX-6+ rewrite without breaking ISY-994 users on the existing repo. The two integrations register distinct HA domains and coexist on the same HA instance.

| Repo | Library | Hardware | Domain |
|---|---|---|---|
| `hacs-isy994` (sibling) | `pyisy` 3.x | ISY-994 | `isy994` |
| `hacs-udi-iox` (this) | `pyisyox` 6.x | eisy / Polisy on IoX 6+ | `udi_iox` |

**Critical rule**: do not regress `hacs-isy994` from this repo. `pyisyox` 6.x and `pyisy` 3.x are independent libraries with different public surfaces.

## Status

Alpha. The seven-phase migration is complete and `main` is feature-equivalent to `hacs-isy994` for IoX-6+ hardware: authentication picker, classifier-driven entity wiring, Repair-card lifecycle UX, services + variables, typed `Program` / `NetworkResource` / `Variable` wrappers, WS-health surface, SSDP discovery gated to IoX 6+. Test suite drives a real `pyisyox.Controller` (via the `tests/builders.py` factories backed by the bundled anonymized eisy6 profile); every entity platform has snapshot coverage. Open issues are tracked at <https://github.com/shbatm/hacs-udi-iox/issues>.

## DevContainer

- Auto-installs Home Assistant + test fixtures.
- Mounts `../pyisyox` and installs it editable for live co-development.
- Symlinks `custom_components/udi_iox` into `.homeassistant/custom_components/`.

```bash
# inside the container
source /opt/venv/bin/activate
pytest tests/
hass -c /workspaces/hacs-udi-iox/.homeassistant
```

## Linting

```bash
pre-commit run --all-files
ruff check custom_components/udi_iox --fix
ruff format custom_components/udi_iox
```

## pyisyox 6.x public surface

The library re-exports its full public API from the package root:

```python
from pyisyox import (
    Controller, ControllerNotConnectedError,
    PortalAuth, LocalAuth, Auth, AuthError,
    Node, Group, Folder, NetworkResource, Program, ProgramFolder, Variable,
    Event, EventDispatcher, EventListener,
    NodeLifecycleAction, NodeLifecycleEvent, NodeLifecycleListener,
    ProgramCommand, StatusListener, WebSocketEventStream,
    classify, ClassificationResult, ControllablePlatform, Reading, ReadingPlatform,
    ISYConnectionError, ISYInvalidAuthError, ISYStreamDataError,
)
from pyisyox.schema.profile import Profile
from pyisyox.helpers.session import build_sslcontext, TLSVersionError
```

Legacy modules **gone in v6**: `pyisyox.connection`, `pyisyox.helpers.events`, `pyisyox.helpers.models`, `pyisyox.node_servers`, `pyisyox.networking`, `pyisyox.programs`, `pyisyox.variables`, `pyisyox.nodes.nodebase`. The classes `ISY`, `ISYConnectionInfo`, `Connection`, `EntityStatus`, `NodeChangedEvent`, `NodeProperty`, `ProgramDetail`, `NetworkCommand`, `Protocol`, `NodeBase` no longer exist.

`pyisyox.constants` still exports the `CMD_*` and `PROP_*` ids referenced from `const.py`.

## Code style

ruff (line 88, py3.10+), codespell, yamllint, prettier, mypy, pylint. Pre-commit hooks gate every commit.
