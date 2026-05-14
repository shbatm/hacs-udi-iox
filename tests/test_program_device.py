"""Direct unit tests for the per-program-device entity fan-out.

The snapshot suites already pin the registry shape (entity ids, names,
icons, translation keys) for the fixture's ``Sunset Lights`` program.
These tests cover the runtime behaviour of each entity class against a
real :class:`pyisyox.Program` wrapper.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from pyisyox.testing import (
    make_controller,
    make_load_result,
    make_program_record,
)

from custom_components.udi_iox.binary_sensor import (
    ISYProgramDeviceStatusBinarySensor,
)
from custom_components.udi_iox.button import (
    ISYProgramRunButton,
    ISYProgramRunElseButton,
    ISYProgramRunIfButton,
    ISYProgramRunThenButton,
    ISYProgramStopButton,
)
from custom_components.udi_iox.helpers import _categorize_program_devices
from custom_components.udi_iox.program_device import program_device_info
from custom_components.udi_iox.sensor import (
    ISYProgramLastFinishSensor,
    ISYProgramLastRunSensor,
    ISYProgramNextScheduledSensor,
    ISYProgramRunningSensor,
)
from custom_components.udi_iox.switch import (
    ISYProgramEnableSwitch,
    ISYProgramRunAtStartupSwitch,
)
from tests.conftest import isy_data_for


def _make_program_controller():
    """Build a controller with one rich non-HA-folder program."""
    record = replace(
        make_program_record(
            "0010",
            "Sunset Lights",
            path="Lighting/Sunset Lights",
            status=True,
            enabled=True,
        ),
        run_at_startup=False,
        running="idle",
        last_run_time="2026-05-13T18:42:11.000Z",
        last_finish_time="2026-05-13T18:42:13.000Z",
        next_scheduled_run_time="2026-05-14T18:42:00.000Z",
    )
    return make_controller(make_load_result(programs={record.address: record}))


def _setup_isy_data(controller):
    isy_data = isy_data_for(controller)
    _categorize_program_devices(isy_data, controller.programs, program_prefix="HA.")
    program = isy_data.program_devices[0]
    device_info = program_device_info(controller, program, host="http://localhost")
    return isy_data, program, device_info


def test_categorize_skips_legacy_ha_programs() -> None:
    """Programs in HA.<platform>/<name>/{status,actions} are left alone."""
    legacy = make_program_record("0001", "Status", path="HA.switch/Movie Mode/status")
    rich = make_program_record("0010", "Sunset Lights", path="Lighting/Sunset Lights")
    controller = make_controller(
        make_load_result(programs={legacy.address: legacy, rich.address: rich})
    )
    isy_data = isy_data_for(controller)
    _categorize_program_devices(isy_data, controller.programs, program_prefix="HA.")
    assert [p.address for p in isy_data.program_devices] == ["0010"]


def test_status_binary_sensor_reads_program_status() -> None:
    controller = _make_program_controller()
    _, program, device_info = _setup_isy_data(controller)
    entity = ISYProgramDeviceStatusBinarySensor(
        isy_data_for(controller), program, device_info
    )
    assert entity.is_on is True
    program._record.status = False
    assert entity.is_on is False


def test_running_sensor_decodes_text_label_unchanged() -> None:
    """Older eisy firmware emits human labels (``"idle"`` / ``"running then"``)
    for the running field. The sensor passes those through, lower-snake-cased
    so the enum-options match (``"running then"`` → ``"running_then"``)."""
    controller = _make_program_controller()
    _, program, device_info = _setup_isy_data(controller)
    entity = ISYProgramRunningSensor(isy_data_for(controller), program, device_info)
    assert entity.native_value == "idle"


def test_running_sensor_decodes_hex_status_byte() -> None:
    """Modern eisy firmware emits the cookbook ``<s>`` byte as a hex
    string. ``"21"`` = ``0x21`` = ``RUN_IDLE | ST_TRUE`` → ``"idle"``."""
    controller = _make_program_controller()
    _, program, device_info = _setup_isy_data(controller)
    program._record.running = "21"  # 0x21 = RUN_IDLE | ST_TRUE
    entity = ISYProgramRunningSensor(isy_data_for(controller), program, device_info)
    assert entity.native_value == "idle"


def test_running_sensor_decodes_running_then_byte() -> None:
    """``"22"`` = ``0x22`` = ``RUN_THEN | ST_TRUE`` → ``"running_then"``."""
    controller = _make_program_controller()
    _, program, device_info = _setup_isy_data(controller)
    program._record.running = "22"
    entity = ISYProgramRunningSensor(isy_data_for(controller), program, device_info)
    assert entity.native_value == "running_then"


def test_running_sensor_returns_none_for_not_loaded() -> None:
    """``"F0"`` = NOT_LOADED — program errored, no run state. Sensor
    returns None so HA renders ``unknown`` instead of an arbitrary label."""
    controller = _make_program_controller()
    _, program, device_info = _setup_isy_data(controller)
    program._record.running = "F0"
    entity = ISYProgramRunningSensor(isy_data_for(controller), program, device_info)
    assert entity.native_value is None


def test_timestamp_sensors_parse_iso_8601() -> None:
    """The three timestamp sensors return tz-aware ``datetime`` values."""
    controller = _make_program_controller()
    _, program, device_info = _setup_isy_data(controller)
    last_run = ISYProgramLastRunSensor(isy_data_for(controller), program, device_info)
    last_finish = ISYProgramLastFinishSensor(
        isy_data_for(controller), program, device_info
    )
    next_scheduled = ISYProgramNextScheduledSensor(
        isy_data_for(controller), program, device_info
    )
    assert last_run.native_value == datetime(2026, 5, 13, 18, 42, 11, tzinfo=UTC)
    assert last_finish.native_value == datetime(2026, 5, 13, 18, 42, 13, tzinfo=UTC)
    assert next_scheduled.native_value == datetime(2026, 5, 14, 18, 42, 0, tzinfo=UTC)


def test_timestamp_sensor_returns_none_for_missing_field() -> None:
    """An empty timestamp string round-trips to ``None``."""
    controller = _make_program_controller()
    _, program, device_info = _setup_isy_data(controller)
    program._record.last_run_time = None
    entity = ISYProgramLastRunSensor(isy_data_for(controller), program, device_info)
    assert entity.native_value is None


def test_enable_switch_calls_enable_disable() -> None:
    controller = _make_program_controller()
    _, program, device_info = _setup_isy_data(controller)
    program.enable = AsyncMock()  # type: ignore[method-assign]
    program.disable = AsyncMock()  # type: ignore[method-assign]
    switch = ISYProgramEnableSwitch(isy_data_for(controller), program, device_info)
    assert switch.is_on is True

    asyncio.run(switch.async_turn_off())
    program.disable.assert_awaited_once_with()
    asyncio.run(switch.async_turn_on())
    program.enable.assert_awaited_once_with()


def test_run_at_startup_switch_calls_matching_pyisyox_verbs() -> None:
    controller = _make_program_controller()
    _, program, device_info = _setup_isy_data(controller)
    program.enable_run_at_startup = AsyncMock()  # type: ignore[method-assign]
    program.disable_run_at_startup = AsyncMock()  # type: ignore[method-assign]
    switch = ISYProgramRunAtStartupSwitch(
        isy_data_for(controller), program, device_info
    )
    assert switch.is_on is False

    asyncio.run(switch.async_turn_on())
    program.enable_run_at_startup.assert_awaited_once_with()
    asyncio.run(switch.async_turn_off())
    program.disable_run_at_startup.assert_awaited_once_with()


@pytest.mark.parametrize(
    ("button_cls", "verb"),
    [
        (ISYProgramRunButton, "run"),
        (ISYProgramRunThenButton, "run_then"),
        (ISYProgramRunElseButton, "run_else"),
        (ISYProgramRunIfButton, "run_if"),
        (ISYProgramStopButton, "stop"),
    ],
)
def test_program_buttons_invoke_matching_verb(button_cls, verb) -> None:
    """Each button calls the matching :class:`pyisyox.Program` verb."""
    controller = _make_program_controller()
    _, program, device_info = _setup_isy_data(controller)
    setattr(program, verb, AsyncMock())
    button = button_cls(isy_data_for(controller), program, device_info)

    asyncio.run(button.async_press())
    getattr(program, verb).assert_awaited_once_with()
