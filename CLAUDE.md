# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

`hacs-udi-iox` is a Home Assistant **HACS custom component** for **Universal Devices eisy / Polisy** controllers running **IoX 6.0+**. It registers under HA domain `udi_iox` and consumes [`pyisyox`](https://github.com/automicus/pyisyox) **6.x**.

This repo was forked from `hacs-isy994` to host the IoX-6+ rewrite without breaking ISY-994 users on the existing repo. The two integrations register distinct HA domains and coexist.

| Repo | Library | Hardware | Domain |
|---|---|---|---|
| `hacs-isy994` (sibling) | `pyisy` 3.x | ISY-994 | `isy994` |
| `hacs-udi-iox` (this) | `pyisyox` 6.x | eisy / Polisy on IoX 6+ | `udi_iox` |

**Critical rule**: do not regress `hacs-isy994` from this repo. Pyisyox 6.x and pyisy 3.x are independent libraries with different public surfaces.

## Implementation status

Pre-alpha. Phase 0 (repo bootstrap) is the only completed phase. See `../hacs-udi-iox_fork_plan.md` (sibling workspace doc) for the seven-phase migration plan:

- **Phase 0** — repo bootstrap (rename, manifest, branding) — done
- **Phase 1** — pyisyox 6.x import surface migration — pending
- **Phase 2** — auth-mode picker (Portal/JWT vs Local/basic) — pending
- **Phase 3** — classifier-driven helpers.py — pending
- **Phase 4** — ergonomic Node wrappers in platform handlers — pending
- **Phase 5** — lifecycle-event repair flow — pending
- **Phase 6** — services + variables rewire — pending
- **Phase 7** — testing path — pending

## DevContainer

Mirrors `hacs-isy994`'s setup:

- Auto-installs Home Assistant + test fixtures
- Mounts `../pyisyox` and installs editable for live co-development
- Symlinks `custom_components/udi_iox` into `.homeassistant/custom_components/`

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

`prek` (the user's preferred pre-commit runner) is available inside the HA core devcontainer; the host machine does not have it.

## pyisyox 6.x public surface

The library re-exports its full public API from the package root:

```python
from pyisyox import (
    Controller, ControllerNotConnectedError,
    PortalAuth, LocalAuth, Auth, AuthError,
    Node, Group, Folder,
    Event, EventDispatcher, EventListener,
    NodeLifecycleAction, NodeLifecycleEvent, NodeLifecycleListener,
    StatusListener, WebSocketEventStream,
    classify, ClassificationResult, ControllablePlatform, Reading, ReadingPlatform,
    ISYConnectionError, ISYInvalidAuthError, ISYStreamDataError,
)
from pyisyox.schema.profile import Profile
from pyisyox.helpers.session import build_sslcontext, TLSVersionError
```

Legacy modules **gone in v6**: `pyisyox.connection`, `pyisyox.helpers.events`, `pyisyox.helpers.models`, `pyisyox.node_servers`, `pyisyox.networking`, `pyisyox.programs`, `pyisyox.variables`, `pyisyox.nodes.nodebase`. The classes `ISY`, `ISYConnectionInfo`, `Connection`, `EntityStatus`, `NodeChangedEvent`, `NodeProperty`, `Program`, `ProgramDetail`, `Variable`, `NetworkCommand`, `Protocol`, `NodeBase` no longer exist.

`pyisyox.constants` still exports the `CMD_*` and `PROP_*` ids referenced from `const.py`.

See `../pyisyox/CLAUDE.md` and `../pyisyox/docs/connection-flow.md` for upstream architecture.

## Code style

Same as `hacs-isy994`: ruff (line 88, py3.10+), codespell, yamllint, prettier.
