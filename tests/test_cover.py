"""Snapshot tests for the udi_iox cover platform.

Cover classification only fires for **plugin** nodes whose nodedef
accepts ``FDUP`` / ``FDDOWN`` / ``FDSTOP`` *without* ``DON`` / ``DOF``
(otherwise pyisyox's classifier picks light or switch). The eisy6
profile bundled inside ``pyisyox.testing`` is a real anonymized
capture of a stock eisy which has no PG3 plugins, so cover-test
fixtures inject a synthetic plugin slot at runtime via
``pyisyox.testing.make_cover_load_result``.

Pin: ``Platform.COVER`` entity creation flowing through the real
``pyisyox.classify`` → ``ControllablePlatform.COVER`` →
``_CONTROLLABLE_TO_HA_PLATFORM`` path.
"""

from __future__ import annotations

import pytest
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pyisyox.testing import (
    make_controller,
    make_cover_load_result,
    make_plugin_cover_node_record,
)
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    SnapshotAssertion,
    snapshot_platform,
)


@pytest.fixture
def platforms() -> list[Platform]:
    return [Platform.COVER]


@pytest.fixture
def populated_controller():
    """Override the default fixture with a controller that carries the
    cover-plugin profile + a single cover node."""
    cover = make_plugin_cover_node_record()
    return make_controller(make_cover_load_result(nodes={cover.address: cover}))


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_cover_entities(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
) -> None:
    """Snapshot every cover entity created by the integration."""
    await snapshot_platform(hass, entity_registry, snapshot, init_integration.entry_id)


# --- Direct entity tests (cover up the non-snapshot logic) ---


async def test_cover_attrs_unknown_status_yields_none() -> None:
    """When the node has no usable status the cover reports neither
    position nor closed-state."""
    from pyisyox import NodePropertyValue
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.cover import ISYCoverEntity
    from custom_components.udi_iox.models import IsyData

    controller = make_controller(make_load_result())
    record = make_node_record(
        "C 1",
        "Roller",
        properties={
            "ST": NodePropertyValue(
                id="ST", value="", formatted="", uom="100", name="Status"
            )
        },
    )
    node = make_node(record, controller)
    isy_data = IsyData()
    isy_data.root = controller
    entity = ISYCoverEntity(isy_data, node=node, device_info=None)

    entity._update_cover_attrs()
    assert entity.is_closed is None
    assert entity.current_cover_position is None


async def test_cover_attrs_byte_range_scales_to_percent() -> None:
    """A UOM-100 (raw byte 0-255) status scales to a 0-100 position."""
    from pyisyox import NodePropertyValue
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.cover import ISYCoverEntity
    from custom_components.udi_iox.models import IsyData

    controller = make_controller(make_load_result())
    record = make_node_record(
        "C 1",
        "Roller",
        properties={
            "ST": NodePropertyValue(
                id="ST", value="128", formatted="50%", uom="100", name="Status"
            )
        },
    )
    node = make_node(record, controller)
    isy_data = IsyData()
    isy_data.root = controller
    entity = ISYCoverEntity(isy_data, node=node, device_info=None)
    entity._update_cover_attrs()

    assert entity.current_cover_position == 50  # 128/255 -> 50
    assert entity.is_closed is False


async def test_cover_attrs_percent_status_clamped() -> None:
    """A UOM-51 percent status is clamped into [0, 100]."""
    from pyisyox import NodePropertyValue
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.cover import ISYCoverEntity
    from custom_components.udi_iox.models import IsyData

    controller = make_controller(make_load_result())
    record = make_node_record(
        "C 1",
        "Roller",
        properties={
            "ST": NodePropertyValue(
                id="ST", value="42", formatted="42%", uom="51", name="Status"
            )
        },
    )
    node = make_node(record, controller)
    isy_data = IsyData()
    isy_data.root = controller
    entity = ISYCoverEntity(isy_data, node=node, device_info=None)
    entity._update_cover_attrs()
    assert entity.current_cover_position == 42
    assert entity.is_closed is False


async def test_cover_open_close_translate_node_command_errors() -> None:
    """Both open and close raise HomeAssistantError on controller rejection."""
    from unittest.mock import AsyncMock, patch

    from homeassistant.exceptions import HomeAssistantError
    from pyisyox import Node, NodeCommandError
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.cover import ISYCoverEntity
    from custom_components.udi_iox.models import IsyData

    controller = make_controller(make_load_result())
    node = make_node(make_node_record("C 1", "Roller"), controller)
    isy_data = IsyData()
    isy_data.root = controller
    entity = ISYCoverEntity(isy_data, node=node, device_info=None)
    with (
        patch.object(
            Node, "send_command", new=AsyncMock(side_effect=NodeCommandError("nope"))
        ),
        pytest.raises(HomeAssistantError, match="Unable to open the cover"),
    ):
        await entity.async_open_cover()
    with (
        patch.object(
            Node, "send_command", new=AsyncMock(side_effect=NodeCommandError("nope"))
        ),
        pytest.raises(HomeAssistantError, match="Unable to close the cover"),
    ):
        await entity.async_close_cover()


async def test_cover_set_position_scales_for_byte_editor() -> None:
    """When the editor is UOM_8_BIT_RANGE with max>100 (classic Insteon
    0-255 byte), HA's 0-100 input scales up to 0-255 before the wire write."""
    from unittest.mock import AsyncMock, patch

    from pyisyox import Node
    from pyisyox.schema.editor import EditorRange
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.cover import ISYCoverEntity
    from custom_components.udi_iox.models import IsyData

    controller = make_controller(make_load_result())
    node = make_node(make_node_record("C 1", "Shade"), controller)
    isy_data = IsyData()
    isy_data.root = controller
    entity = ISYCoverEntity(isy_data, node=node, device_info=None)

    byte_range = EditorRange(uom="100", min=0, max=255)
    set_on_level = AsyncMock()
    with (
        patch.object(entity, "_editor_range_for", return_value=byte_range),
        patch.object(Node, "set_on_level", new=set_on_level),
    ):
        await entity.async_set_cover_position(position=50)

    # 50 (HA percent) -> 128 (rounded 50*255/100) on the wire
    set_on_level.assert_awaited_once_with(128)


async def test_cover_set_position_passes_through_for_percent_editor() -> None:
    """Percent / byte-capped editors get HA's 0-100 input as-is."""
    from unittest.mock import AsyncMock, patch

    from pyisyox import Node
    from pyisyox.schema.editor import EditorRange
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.cover import ISYCoverEntity
    from custom_components.udi_iox.models import IsyData

    controller = make_controller(make_load_result())
    node = make_node(make_node_record("C 1", "Shade"), controller)
    isy_data = IsyData()
    isy_data.root = controller
    entity = ISYCoverEntity(isy_data, node=node, device_info=None)

    percent_range = EditorRange(uom="51", min=0, max=100)
    set_on_level = AsyncMock()
    with (
        patch.object(entity, "_editor_range_for", return_value=percent_range),
        patch.object(Node, "set_on_level", new=set_on_level),
    ):
        await entity.async_set_cover_position(position=42)

    set_on_level.assert_awaited_once_with(42)


async def test_cover_set_position_translates_node_command_error() -> None:
    """A controller rejection on set_on_level becomes HomeAssistantError."""
    from unittest.mock import AsyncMock, patch

    from homeassistant.exceptions import HomeAssistantError
    from pyisyox import Node, NodeCommandError
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.cover import ISYCoverEntity
    from custom_components.udi_iox.models import IsyData

    controller = make_controller(make_load_result())
    node = make_node(make_node_record("C 1", "Shade"), controller)
    isy_data = IsyData()
    isy_data.root = controller
    entity = ISYCoverEntity(isy_data, node=node, device_info=None)
    with (
        patch.object(entity, "_editor_range_for", return_value=None),
        patch.object(
            Node, "set_on_level", new=AsyncMock(side_effect=NodeCommandError("nope"))
        ),
        pytest.raises(HomeAssistantError, match=r"Unable to set cover .* position"),
    ):
        await entity.async_set_cover_position(position=50)


async def test_cover_program_entity() -> None:
    """Program covers: is_closed reads from status program; open/close
    delegate to actions program; both translate failures."""
    from unittest.mock import AsyncMock, patch

    from homeassistant.exceptions import HomeAssistantError
    from pyisyox import Program
    from pyisyox.testing import make_controller, make_load_result, make_program_record

    from custom_components.udi_iox.cover import ISYCoverProgramEntity
    from custom_components.udi_iox.models import IsyData

    controller = make_controller(make_load_result())
    status = Program(
        make_program_record("0001", "Status", status=True), controller._client
    )
    actions = Program(make_program_record("0002", "Actions"), controller._client)
    isy_data = IsyData()
    isy_data.root = controller
    entity = ISYCoverProgramEntity(isy_data, "Garage", status, actions)

    assert entity.is_closed is True  # status=True → closed

    with (
        patch.object(
            Program, "run_then", new=AsyncMock(side_effect=RuntimeError("boom"))
        ),
        pytest.raises(HomeAssistantError, match="Unable to open cover program"),
    ):
        await entity.async_open_cover()
    with (
        patch.object(
            Program, "run_else", new=AsyncMock(side_effect=RuntimeError("boom"))
        ),
        pytest.raises(HomeAssistantError, match="Unable to close cover program"),
    ):
        await entity.async_close_cover()

    # Success paths exercise the no-error branches.
    with patch.object(Program, "run_then", new=AsyncMock()) as run_then:
        await entity.async_open_cover()
    run_then.assert_awaited_once()
    with patch.object(Program, "run_else", new=AsyncMock()) as run_else:
        await entity.async_close_cover()
    run_else.assert_awaited_once()
