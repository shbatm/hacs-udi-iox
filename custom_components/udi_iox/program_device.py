"""Per-IoX-program HA device + entity scaffolding.

Programs outside the legacy ``HA.<platform>/<name>/{status,actions}``
folder convention each get their own device whose entities surface
status / run-state / schedule / enable / run-at-startup / manual run.
Updates flow through pyisyox's :class:`ProgramStatusEvent`; the
dispatcher mutates ``ProgramRecord`` before firing, so wrapper attrs
read fresh on the next render.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.const import Platform
from homeassistant.core import callback
from homeassistant.helpers.entity import DeviceInfo
from pyisyox import Program

from .const import DOMAIN, MANUFACTURER
from .entity import ISYEntity

if TYPE_CHECKING:
    from pyisyox import Controller, ProgramStatusEvent

    from .models import IsyData

# Per-platform unique-id suffixes — kept here so ``models.unique_ids``
# and every platform module agree on the wire format.
PROGRAM_DEVICE_PREFIX = "program"
PROGRAM_BINARY_STATUS_SUFFIX = "_status"
PROGRAM_RUNNING_SENSOR_SUFFIX = "_running"
PROGRAM_LAST_RUN_SENSOR_SUFFIX = "_last_run"
PROGRAM_LAST_FINISH_SENSOR_SUFFIX = "_last_finish"
PROGRAM_NEXT_SCHEDULED_SENSOR_SUFFIX = "_next_scheduled"
PROGRAM_ENABLE_SWITCH_SUFFIX = "_enable"
PROGRAM_RUN_AT_STARTUP_SWITCH_SUFFIX = "_run_at_startup"
PROGRAM_RUN_BUTTON_SUFFIX = "_run"
PROGRAM_RUN_THEN_BUTTON_SUFFIX = "_run_then"
PROGRAM_RUN_ELSE_BUTTON_SUFFIX = "_run_else"
PROGRAM_STOP_BUTTON_SUFFIX = "_stop"


def program_device_uid(uuid: str, program: Program) -> str:
    """Stable identifier for a program's HA device.

    Distinct namespace from node devices (``{uuid}_{address}``) so a
    program and a node that happen to share an address can both have
    devices.
    """
    return f"{uuid}_{PROGRAM_DEVICE_PREFIX}_{program.address}"


def _suggested_area_for_program(controller: Controller, program: Program) -> str | None:
    """Use the program's immediate parent folder name as suggested area.

    Programs always live directly under a folder on the IoX side (the
    parent chain doesn't have intermediate non-folder nodes the way
    nodes can), so a single lookup suffices.

    Returns ``None`` for:
    - root-level programs (no ``parent_address``)
    - programs whose immediate parent IS the synthetic root folder
      (``"My Programs"`` on stock eisy firmware) — surfacing
      ``"My Programs"`` as an Area is noise; treat the program as
      effectively root.
    """
    address = program.parent_address
    if address is None:
        return None
    folder = controller.program_folders.get(address)
    if folder is None:
        return None
    # The synthetic root folder has no parent of its own; user-created
    # folders always have one (they live under root). Skip the synthetic
    # root rather than letting "My Programs" leak through as an Area.
    if folder.parent_address is None:
        return None
    return folder.name or None


def program_device_info(
    controller: Controller, program: Program, host: str
) -> DeviceInfo:
    """Build the per-program :class:`DeviceInfo`.

    Anchored under the controller hub via ``via_device`` so HA renders
    it under the eisy in the device tree.
    """
    uuid = controller.config.uuid
    return DeviceInfo(
        identifiers={(DOMAIN, program_device_uid(uuid, program))},
        manufacturer=MANUFACTURER,
        model="IoX Program",
        name=program.name,
        via_device=(DOMAIN, uuid),
        configuration_url=host,
        suggested_area=_suggested_area_for_program(controller, program),
    )


PROGRAM_DEVICE_ENTITY_SUFFIXES: dict[Platform, tuple[str, ...]] = {
    Platform.BINARY_SENSOR: (PROGRAM_BINARY_STATUS_SUFFIX,),
    Platform.SENSOR: (
        PROGRAM_RUNNING_SENSOR_SUFFIX,
        PROGRAM_LAST_RUN_SENSOR_SUFFIX,
        PROGRAM_LAST_FINISH_SENSOR_SUFFIX,
        PROGRAM_NEXT_SCHEDULED_SENSOR_SUFFIX,
    ),
    Platform.SWITCH: (
        PROGRAM_ENABLE_SWITCH_SUFFIX,
        PROGRAM_RUN_AT_STARTUP_SWITCH_SUFFIX,
    ),
    Platform.BUTTON: (
        PROGRAM_RUN_BUTTON_SUFFIX,
        PROGRAM_RUN_THEN_BUTTON_SUFFIX,
        PROGRAM_RUN_ELSE_BUTTON_SUFFIX,
        PROGRAM_STOP_BUTTON_SUFFIX,
    ),
}


class ISYProgramDeviceEntity(ISYEntity):
    """Shared base: pins ``_node`` to the program and subscribes to its
    status frames (default handler rerenders on every frame)."""

    _node: Program
    _attr_has_entity_name = True

    def __init__(
        self,
        isy_data: IsyData,
        program: Program,
        device_info: DeviceInfo,
        suffix: str,
    ) -> None:
        """``suffix`` picks the per-program unique-id slot;
        subclasses set ``_attr_translation_key`` for the label."""
        super().__init__(
            isy_data,
            program,
            device_info=device_info,
            unique_id=f"{isy_data.uid_base(program)}{suffix}",
        )
        del self._attr_name

    async def async_added_to_hass(self) -> None:
        """Subscribe to program-status frames + WS-health flips.

        Skips ``super()`` deliberately: the per-(addr, control)
        registry doesn't carry program updates — those flow through
        the dedicated ``subscribe_program`` channel.
        """
        events = self._isy_data.controller_events
        program: Program = self._node
        self._unsubscribers.append(
            events.subscribe_program(program.address, self._on_program_status)
        )
        self._unsubscribers.append(events.subscribe_ws_status(self._on_ws_status))

    @callback
    def _on_program_status(self, _event: ProgramStatusEvent) -> None:
        """Refresh HA state on every program-status frame.

        The dispatcher has already mutated ``ProgramRecord`` by the
        time the callback fires, so wrapper attributes read the new
        values on the next render.
        """
        self.async_write_ha_state()


def program_device_unique_ids(
    isy_data: IsyData,
) -> set[tuple[Platform, str]]:
    """Every per-program-device entity's ``(platform, unique_id)`` pair.

    Consumed by :meth:`IsyData.unique_ids` so stale-entity cleanup
    knows what this integration owns.
    """
    pairs: set[tuple[Platform, str]] = set()
    for program in isy_data.program_devices:
        base = isy_data.uid_base(program)
        for platform, suffixes in PROGRAM_DEVICE_ENTITY_SUFFIXES.items():
            for suffix in suffixes:
                pairs.add((platform, f"{base}{suffix}"))
    return pairs
