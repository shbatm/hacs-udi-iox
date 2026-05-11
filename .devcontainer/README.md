Devcontainer: running tests for the udi_iox custom component

This devcontainer is configured to let maintainers run the integration's tests against Home Assistant's pytest fixtures, and develop alongside `pyisyox`.

Quick steps after opening the repository in the Dev Container:

1. The container build will run the setup script automatically (via `postCreateCommand`).
   - The script uses the venv at `/opt/venv` (prebuilt during image build) and installs a pre-release of `homeassistant` from PyPI (including testing extras).
   - Wheels are cached in `.wheels/` and the pip cache in `.cache/pip/` so subsequent rebuilds are fast.
   - If `pyisyox` is available at `../pyisyox`, it will be installed in editable mode automatically, and its `requirements-dev.txt` (ruff, mypy, pylint, pre-commit, codespell) is installed into `/opt/venv` so the toolchain matches the host.
   - The integration's runtime requirements (read from `custom_components/udi_iox/manifest.json` via `jq`) are then installed.

2. Open a terminal in the container and activate the venv:

```bash
cd /workspaces/hacs-udi-iox
source /opt/venv/bin/activate
```

3. Run the tests for this integration:

```bash
pytest tests/
```

Co-Development with pyisyox
----------------------------

This devcontainer automatically mounts the `pyisyox` directory (if available) at `/workspaces/pyisyox` and installs it in editable mode during setup.

**Directory structure required:**
```
parent/
├── hacs-udi-iox/
└── pyisyox/
```

When both repositories are present:
- `pyisyox` is installed with `pip install -e /workspaces/pyisyox`
- Changes to `pyisyox` are immediately reflected in the integration
- You can edit both codebases simultaneously

If `pyisyox` is not available, the setup script falls back to installing the version pinned in `custom_components/udi_iox/manifest.json`.

Notes and troubleshooting
--------------------------
- If the container build fails due to missing system dependencies while building wheels (cryptography, etc.), rebuild the container after adding the required package to `.devcontainer/Dockerfile` (for example `libssl-dev` and `cargo` are already included).
- To pin a specific Home Assistant version, edit `.devcontainer/scripts/setup_ha_test_env.sh` and replace the `pip install --pre "homeassistant[tests]"` line with a pinned version, for example:

```bash
python -m pip install --pre 'homeassistant[tests]==2026.5.0'
```

VS Code Tasks
--------------

The workspace includes VS Code tasks in `.vscode/tasks.json`:

- **Start Home Assistant (devcontainer)**: runs `hass` from `/opt/venv` against the `.homeassistant` config directory in the workspace. HA binds to its default port (8123) inside the container; `devcontainer.json` maps that to host port 9123.
- **Start Home Assistant (with project venv)**: same, but prefers a workspace-local `.venv` if one exists (e.g. when developing without the prebuilt `/opt/venv`).
- **Stop Home Assistant (kill)**: convenience task that stops any running Home Assistant process.
- **Run pytest**: runs the test suite under `tests/`.
- **Run Home Assistant on port 9123**: legacy task that invokes `scripts/develop` (no port override; HA uses its default 8123 inside the container and is reached at `http://localhost:9123` on the host via the same port mapping).

The `setup_homeassistant_runtime.sh` post-create step also creates the symlink `.homeassistant/custom_components` → `../custom_components` so HA picks up the integration directly from the working tree.

Usage (inside the devcontainer):

1. Open the Command Palette (Ctrl+Shift+P) and run "Tasks: Run Task" → one of the Start tasks.
2. The Home Assistant process will run in the Terminal panel in the foreground (shows HA logs).
3. To stop it, press Ctrl+C in the task terminal or run the "Stop Home Assistant (kill)" task.

Access Home Assistant:
- From the host machine: http://localhost:9123
- The devcontainer maps container port 8123 to host port 9123.

The `.homeassistant/` directory is in `.gitignore` so runtime state/config doesn't get committed.
