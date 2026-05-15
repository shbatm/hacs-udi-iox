"""Snapshot tests for the udi_iox button platform."""

from __future__ import annotations

import pytest
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    SnapshotAssertion,
    snapshot_platform,
)

from tests.conftest import isy_data_for


@pytest.fixture
def platforms() -> list[Platform]:
    return [Platform.BUTTON]


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_button_entities(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
) -> None:
    """Snapshot every button entity created by the integration."""
    await snapshot_platform(hass, entity_registry, snapshot, init_integration.entry_id)


def test_command_button_disabled_by_default_for_maintenance_verbs() -> None:
    """``WDU`` ("Write Changes") plus the Insteon "fast on/off" and
    momentary paddle verbs (``DFON``/``DFOF``/``BRT``/``DIM``/``FDUP``/
    ``FDDOWN``/``FDSTOP``) are created disabled by default, while
    everyday verbs are enabled."""
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.button import ISYNodeCommandButtonEntity

    controller = make_controller(make_load_result())
    node = make_node(make_node_record("AA AA AA 1", "Lamp"), controller)
    isy_data = isy_data_for(controller)

    def _button(command_id: str) -> ISYNodeCommandButtonEntity:
        return ISYNodeCommandButtonEntity(
            isy_data,
            node,
            command_id=command_id,
            name=command_id.title(),
            unique_id=f"x_{command_id}",
            device_info=None,  # type: ignore[arg-type]
        )

    for verb in ("WDU", "DFON", "DFOF", "BRT", "DIM", "FDUP", "FDDOWN", "FDSTOP"):
        assert _button(verb).entity_registry_enabled_default is False, verb
    assert _button("DISCOVER").entity_registry_enabled_default is True
    assert _button("BEEP").entity_registry_enabled_default is True


async def test_command_button_translates_node_command_error() -> None:
    """A controller-side rejection on a plugin-command button press
    becomes HomeAssistantError so HA surfaces the failure to the user
    (was previously unhandled — the raw NodeCommandError bubbled out
    of async_press)."""
    from unittest.mock import AsyncMock, patch

    import pytest
    from homeassistant.exceptions import HomeAssistantError
    from pyisyox import Node, NodeCommandError
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.button import ISYNodeCommandButtonEntity

    controller = make_controller(make_load_result())
    node = make_node(make_node_record("AA AA AA 1", "Lamp"), controller)
    isy_data = isy_data_for(controller)
    button = ISYNodeCommandButtonEntity(
        isy_data,
        node,
        command_id="DISCOVER",
        name="Discover",
        unique_id="x_discover",
        device_info=None,  # type: ignore[arg-type]
    )
    with (
        patch.object(
            Node, "send_command", new=AsyncMock(side_effect=NodeCommandError("nope"))
        ),
        pytest.raises(HomeAssistantError, match="Unable to send DISCOVER"),
    ):
        await button.async_press()


async def test_beep_button_translates_node_command_error() -> None:
    """BEEP button surfaces NodeCommandError as HomeAssistantError."""
    from unittest.mock import AsyncMock, patch

    import pytest
    from homeassistant.exceptions import HomeAssistantError
    from pyisyox import Node, NodeCommandError
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.button import ISYNodeBeepButtonEntity

    controller = make_controller(make_load_result())
    node = make_node(make_node_record("AA AA AA 1", "Lamp"), controller)
    isy_data = isy_data_for(controller)
    button = ISYNodeBeepButtonEntity(
        isy_data,
        node,
        name="Beep",
        unique_id="x_beep",
        device_info=None,  # type: ignore[arg-type]
    )
    with (
        patch.object(
            Node, "send_command", new=AsyncMock(side_effect=NodeCommandError("nope"))
        ),
        pytest.raises(HomeAssistantError, match="Unable to beep node"),
    ):
        await button.async_press()


async def test_query_button_translates_node_command_error() -> None:
    """Node-target Query button surfaces NodeCommandError as HomeAssistantError
    using the node address (not the button's HA name)."""
    from unittest.mock import AsyncMock, patch

    import pytest
    from homeassistant.exceptions import HomeAssistantError
    from pyisyox import Node, NodeCommandError
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.button import ISYNodeQueryButtonEntity

    controller = make_controller(make_load_result())
    node = make_node(make_node_record("AA AA AA 1", "Lamp"), controller)
    isy_data = isy_data_for(controller)
    button = ISYNodeQueryButtonEntity(
        isy_data,
        node=node,
        name="Query",
        unique_id="x_query",
        device_info=None,  # type: ignore[arg-type]
    )
    with (
        patch.object(
            Node, "send_command", new=AsyncMock(side_effect=NodeCommandError("nope"))
        ),
        pytest.raises(HomeAssistantError, match="Unable to query node AA AA AA 1"),
    ):
        await button.async_press()


async def test_query_button_translates_controller_refresh_failure() -> None:
    """Controller-target Query button surfaces a broad-except as
    HomeAssistantError using the controller's UUID."""
    from unittest.mock import AsyncMock, patch

    import pytest
    from homeassistant import exceptions as ha_exceptions
    from pyisyox import Controller
    from pyisyox.testing import make_controller, make_load_result

    from custom_components.udi_iox.button import ISYNodeQueryButtonEntity

    controller = make_controller(make_load_result())
    isy_data = isy_data_for(controller)
    button = ISYNodeQueryButtonEntity(
        isy_data,
        node=controller,
        name="Query",
        unique_id="x_query",
        device_info=None,  # type: ignore[arg-type]
    )
    with (
        patch.object(
            Controller, "refresh", new=AsyncMock(side_effect=RuntimeError("boom"))
        ),
        pytest.raises(
            ha_exceptions.HomeAssistantError, match="Unable to refresh controller"
        ),
    ):
        await button.async_press()


async def test_network_resource_button_translates_run_failure() -> None:
    """Network-resource button surfaces a broad-except as HomeAssistantError."""
    from unittest.mock import AsyncMock, patch

    import pytest
    from homeassistant.exceptions import HomeAssistantError
    from pyisyox import NetworkResource
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_network_resource_record,
    )

    from custom_components.udi_iox.button import ISYNetworkResourceButtonEntity

    controller = make_controller(make_load_result())
    record = make_network_resource_record("1", "Doorbell")
    resource = NetworkResource(record, controller._client)
    isy_data = isy_data_for(controller)
    button = ISYNetworkResourceButtonEntity(
        isy_data,
        node=resource,
        name=resource.name,
        unique_id="x_netres",
        device_info=None,  # type: ignore[arg-type]
    )
    with (
        patch.object(
            NetworkResource, "run", new=AsyncMock(side_effect=RuntimeError("boom"))
        ),
        pytest.raises(HomeAssistantError, match="Unable to run network resource"),
    ):
        await button.async_press()


# --- Coverage: friendly-name fallback, availability, lifecycle, program errors ---


async def test_friendly_name_falls_back_to_titled_command_id() -> None:
    """``_friendly_name`` returns ``command_id.replace('_', ' ').title()``
    when the nodedef has the command but no human-readable ``name``
    (lines 65-66)."""
    from unittest.mock import patch

    from pyisyox import Node
    from pyisyox.schema.cmd import Command
    from pyisyox.schema.nodedef import NodeCommands, NodeDef
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.button import _command_label

    controller = make_controller(make_load_result())
    node = make_node(make_node_record("AA AA AA 1", "X"), controller)
    nameless = NodeDef(
        id="X",
        family_id="1",
        instance_id="1",
        cmds=NodeCommands(accepts=[Command(id="MY_CMD")]),  # no name
    )
    with patch.object(
        Node, "nodedef", new_callable=lambda: property(lambda _self: nameless)
    ):
        assert _command_label(node, "MY_CMD") == "My Cmd"


async def test_async_setup_entry_skips_program_button_without_device_info(hass) -> None:
    """A program in ``program_devices`` whose DeviceInfo wasn't
    registered is silently skipped (line 158-159)."""
    from unittest.mock import MagicMock

    from pyisyox import Program
    from pyisyox.testing import make_controller, make_load_result, make_program_record

    from custom_components.udi_iox.button import async_setup_entry

    controller = make_controller(make_load_result())
    record = make_program_record("0010", "Sunset Lights", path="X/Y")
    isy_data = isy_data_for(controller)
    isy_data.program_devices = [Program(record, controller._client)]
    # No isy_data.devices entry under "program_0010" → skip path.
    entry = MagicMock()
    entry.runtime_data = isy_data
    collected: list = []
    await async_setup_entry(hass, entry, collected.extend)
    # The system-wide query button always lands; the per-program buttons
    # must NOT be created when device_info is missing.
    from custom_components.udi_iox.button import (
        ISYProgramRunButton,
        ISYProgramStopButton,
    )

    assert not any(
        isinstance(e, (ISYProgramRunButton, ISYProgramStopButton)) for e in collected
    )


async def test_node_button_unavailable_when_ws_disconnected() -> None:
    """``available`` returns False when the controller's WS is down,
    independent of node-enabled (line 207)."""
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.button import ISYNodeQueryButtonEntity

    controller = make_controller(make_load_result())
    node = make_node(make_node_record("A 1", "X"), controller)
    isy_data = isy_data_for(controller)
    isy_data.controller_events.ws_connected = False  # type: ignore[attr-defined]
    button = ISYNodeQueryButtonEntity(
        isy_data,
        node=node,
        name="Query",
        unique_id="x",
        device_info=None,  # type: ignore[arg-type]
    )
    assert button.available is False


async def test_on_ws_status_writes_state_and_lifecycle_handler_filters() -> None:
    """``_on_ws_status`` calls ``async_write_ha_state`` (line 222);
    ``_on_lifecycle`` ignores frames for other addresses / unrelated
    actions and refreshes when the address+action match (lines 233-238)."""
    from unittest.mock import patch

    from pyisyox import NodeLifecycleAction, NodeLifecycleEvent
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.button import ISYNodeQueryButtonEntity

    controller = make_controller(make_load_result())
    node = make_node(make_node_record("A 1", "X"), controller)
    isy_data = isy_data_for(controller)
    button = ISYNodeQueryButtonEntity(
        isy_data,
        node=node,
        name="Query",
        unique_id="x",
        device_info=None,  # type: ignore[arg-type]
    )
    write_calls = []
    with patch.object(
        ISYNodeQueryButtonEntity,
        "async_write_ha_state",
        lambda s: write_calls.append(1),
    ):
        button._on_ws_status(True)
        # Wrong address → ignored.
        button._on_lifecycle(
            NodeLifecycleEvent(
                action=NodeLifecycleAction.NODE_ENABLED,
                node_address="B 2",
                raw_action="NE",
                seqnum=1,
            )
        )
        # Right address, wrong action → ignored.
        button._on_lifecycle(
            NodeLifecycleEvent(
                action=NodeLifecycleAction.NODE_RENAMED,
                node_address="A 1",
                raw_action="NN",
                seqnum=2,
            )
        )
        # Match → refresh.
        button._on_lifecycle(
            NodeLifecycleEvent(
                action=NodeLifecycleAction.NODE_ENABLED,
                node_address="A 1",
                raw_action="NE",
                seqnum=3,
            )
        )
    assert write_calls == [1, 1]  # one ws + one matching lifecycle


async def test_program_button_raises_when_verb_missing() -> None:
    """A subclass whose ``_verb`` resolves to ``None`` on the program
    object raises a clear HomeAssistantError (line 367-368)."""
    from homeassistant.exceptions import HomeAssistantError
    from pyisyox import Program
    from pyisyox.testing import make_controller, make_load_result, make_program_record

    from custom_components.udi_iox.button import ISYProgramRunButton

    controller = make_controller(make_load_result())
    record = make_program_record("0010", "X", path="X")
    program = Program(record, controller._client)
    isy_data = isy_data_for(controller)
    button = ISYProgramRunButton(isy_data, program, device_info={})  # type: ignore[arg-type]
    # Force-clear the .run method so the lookup returns None.
    button._verb = "method_that_does_not_exist"  # type: ignore[misc]
    with pytest.raises(HomeAssistantError, match="has no verb"):
        await button.async_press()


async def test_program_button_translates_runtime_error() -> None:
    """Any exception during the verb call surfaces as HomeAssistantError
    (lines 373-374)."""
    from unittest.mock import AsyncMock, patch

    from homeassistant.exceptions import HomeAssistantError
    from pyisyox import Program
    from pyisyox.testing import make_controller, make_load_result, make_program_record

    from custom_components.udi_iox.button import ISYProgramRunButton

    controller = make_controller(make_load_result())
    record = make_program_record("0010", "X", path="X")
    program = Program(record, controller._client)
    isy_data = isy_data_for(controller)
    button = ISYProgramRunButton(isy_data, program, device_info={})  # type: ignore[arg-type]
    with (
        patch.object(Program, "run", new=AsyncMock(side_effect=RuntimeError("boom"))),
        pytest.raises(HomeAssistantError, match="Unable to run program"),
    ):
        await button.async_press()
