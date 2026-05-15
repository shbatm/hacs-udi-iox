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


def _make_program_controller(
    *,
    status: bool = True,
    running: str | None = "idle",
    last_run_time: str | None = "2026-05-13T18:42:11.000Z",
    last_finish_time: str | None = "2026-05-13T18:42:13.000Z",
    next_scheduled_run_time: str | None = "2026-05-14T18:42:00.000Z",
):
    """Build a controller with one rich non-HA-folder program."""
    record = replace(
        make_program_record(
            "0010",
            "Sunset Lights",
            path="Lighting/Sunset Lights",
            status=status,
            enabled=True,
        ),
        run_at_startup=False,
        running=running,
        last_run_time=last_run_time,
        last_finish_time=last_finish_time,
        next_scheduled_run_time=next_scheduled_run_time,
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


@pytest.mark.parametrize(
    ("status", "expected"),
    [(True, True), (False, False)],
)
def test_status_binary_sensor_reads_program_status(
    status: bool, expected: bool
) -> None:
    controller = _make_program_controller(status=status)
    _, program, device_info = _setup_isy_data(controller)
    entity = ISYProgramDeviceStatusBinarySensor(
        isy_data_for(controller), program, device_info
    )
    assert entity.is_on is expected


@pytest.mark.parametrize(
    ("running", "expected"),
    [
        # Older eisy firmware emits human labels.
        ("idle", "idle"),
        ("running then", "running_then"),
        # Modern eisy emits the cookbook <s> byte as two hex digits.
        ("21", "idle"),  # 0x21 = RUN_IDLE | ST_TRUE
        ("22", "running_then"),  # 0x22 = RUN_THEN | ST_TRUE
        # NOT_LOADED — program errored, sensor surfaces unknown.
        ("F0", None),
    ],
)
def test_running_sensor_decodes_program_state(
    running: str, expected: str | None
) -> None:
    controller = _make_program_controller(running=running)
    _, program, device_info = _setup_isy_data(controller)
    entity = ISYProgramRunningSensor(isy_data_for(controller), program, device_info)
    assert entity.native_value == expected


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
    controller = _make_program_controller(last_run_time=None)
    _, program, device_info = _setup_isy_data(controller)
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


# --- suggested_area: derived from IoX program-folder ---------------------


def _add_program_folder(
    controller, address: str, name: str, *, parent_address: str | None
) -> None:
    """Inject a folder-shaped ``ProgramRecord`` into the controller's
    loaded program registry — workaround for the missing
    ``program_folders=`` kwarg on ``make_load_result``
    (pyisyox#153)."""
    from pyisyox.client import ProgramRecord

    controller._loaded.programs[address] = ProgramRecord(
        address=address,
        name=name,
        path=name,
        parent_address=parent_address,
        is_folder=True,
        status=False,
    )


def test_program_device_info_uses_parent_folder_as_suggested_area() -> None:
    """A program inside a user-created folder gets the folder's name as
    ``suggested_area`` — symmetric with the node-side derivation."""
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_program_record,
    )

    program_rec = make_program_record(
        "0010", "Sunset Lights", path="Lighting/Sunset Lights", parent_address="F1"
    )
    controller = make_controller(
        make_load_result(programs={program_rec.address: program_rec})
    )
    # Real eisy hierarchy: synthetic root → user folder → program.
    _add_program_folder(controller, "ROOT", "My Programs", parent_address=None)
    _add_program_folder(controller, "F1", "Lighting", parent_address="ROOT")
    # Guard: if pyisyox restructures `_loaded`, fail loudly here rather
    # than letting suggested_area silently return None below.
    assert controller.program_folders.get("F1") is not None, (
        "program_folders setup failed — _loaded API may have changed"
    )
    program = controller.programs["0010"]

    device_info = program_device_info(controller, program, host="http://localhost")

    assert device_info.get("suggested_area") == "Lighting"


def test_program_device_info_skips_synthetic_root_folder() -> None:
    """A program whose immediate parent IS the synthetic root folder
    (``"My Programs"`` on stock eisy) gets no ``suggested_area`` — the
    root folder is plumbing, not a meaningful Area."""
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_program_record,
    )

    program_rec = make_program_record(
        "0010", "Top-Level Routine", path="Top-Level Routine", parent_address="ROOT"
    )
    controller = make_controller(
        make_load_result(programs={program_rec.address: program_rec})
    )
    _add_program_folder(controller, "ROOT", "My Programs", parent_address=None)
    assert controller.program_folders.get("ROOT") is not None, (
        "program_folders setup failed — _loaded API may have changed"
    )
    program = controller.programs["0010"]

    device_info = program_device_info(controller, program, host="http://localhost")

    assert device_info.get("suggested_area") is None


def test_program_device_info_root_program_has_none() -> None:
    """A program with no ``parent_address`` (root of the program tree)
    leaves ``suggested_area`` unset."""
    from pyisyox.testing import make_controller, make_load_result

    program_rec = make_program_record("0010", "Standalone", path="Standalone")
    controller = make_controller(
        make_load_result(programs={program_rec.address: program_rec})
    )
    program = controller.programs["0010"]

    device_info = program_device_info(controller, program, host="http://localhost")

    assert device_info.get("suggested_area") is None
