# Universal Devices IoX Integration Context

This repository contains a Home Assistant custom component for **Universal Devices eisy** controllers running **IoX 6.0+**. (Polisy is end-of-life; auto-discovery is removed and the README "Legacy Hardware Scope" section documents the manual-config path that still works on the same wire.)

## Project Overview

- **Domain:** `udi_iox`
- **Primary Library:** [`pyisyox`](https://github.com/shbatm/pyisyox) v6.x (WebSocket-only, JWT/Portal auth).
- **Architecture:**
    - **Clean Break:** This is a rewrite/fork of `hacs-isy994` specifically for IoX 6+ hardware. It is designed to coexist with the legacy `isy994` integration.
    - **Classifier-Driven:** Entity routing is driven by `pyisyox`'s classifier reading device nodedefs rather than hard-coded tables.
    - **Platform Coverage:** Supports binary sensors, buttons, climate, covers, fans, lights, locks, numbers, selects, sensors, and switches.
    - **Discovery:** Supports SSDP and DHCP discovery for eisy hardware (Polisy auto-discovery removed in PR #58).

## Building and Running

### Development Environment
The project includes a DevContainer configuration that auto-installs Home Assistant and necessary fixtures.

- **Start Home Assistant:**
  ```bash
  hass -c .homeassistant
  ```

### Testing
The test suite uses `pytest` and includes snapshot testing for entity platforms. It drives a real `pyisyox.Controller` using anonymized profile factories.

- **Run all tests:**
  ```bash
  pytest tests/
  ```

### Linting and Formatting
Strict linting is enforced via `ruff` and `pre-commit`.

- **Check and Fix:**
  ```bash
  ruff check . --fix
  ```
- **Format Code:**
  ```bash
  ruff format .
  ```
- **Run all pre-commit hooks:**
  ```bash
  pre-commit run --all-files
  ```

## Development Conventions

- **Target Version:** Python 3.12+ (managed via `pyproject.toml`).
- **Code Style:** Ruff (line length 88). Standard Home Assistant coding patterns.
- **Sub-devices:** Use `DeviceInfo` with `via_device` to represent sub-components (e.g., individual outlets on a power strip).
- **Action Feedback:** Raise `HomeAssistantError` (from `homeassistant.exceptions`) for failures in actions (e.g., `async_turn_on`) to provide UI feedback.
- **Concurrency:** Use `asyncio.gather` for independent API calls during update cycles to minimize latency.
- **Entity Identification:** Avoid `device_id` where `entity_id` is sufficient.
- **No Legacy Regressions:** Ensure changes do not negatively impact the architectural clean break from `hacs-isy994`.

## Key Files

- `custom_components/udi_iox/`: Core integration source code.
    - `entity.py`: Base entity classes and common logic.
    - `config_flow.py`: Implementation of the UI-based configuration flow.
    - `controller_events.py`: Logic for handling WebSocket events from the IoX controller.
- `pyisyox.testing` (upstream): Factory methods for building mock controller states for tests — imported as `from pyisyox.testing import make_controller, make_load_result, ...`.
- `pyproject.toml`: Configuration for `ruff`, `pytest`, and build metadata.
- `CLAUDE.md`: Internal documentation for Claude Code (claude.ai/code) with specific library surface details.
