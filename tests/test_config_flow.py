"""Tests for the udi_iox config flow.

Pins the auth-error → ConfigEntryAuthFailed mapping, the
connection-error → ConfigEntryNotReady mapping, and that the flow wires
up ``PortalAuth``. The classifier and entity wiring don't run —
validate_input opens a short-lived controller, reads ``config.uuid``,
and tears down.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries, data_entry_flow
from homeassistant.components import ssdp
from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
)
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.udi_iox.const import DOMAIN


async def _start_user_flow(hass) -> dict:
    """Kick off the user step and return the form result."""
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )


def _build_user_input(**overrides) -> dict:
    """Default valid user input; tests override fields they care about."""
    base = {
        CONF_HOST: "https://eisy.local:443",
        CONF_USERNAME: "admin@example.com",
        CONF_PASSWORD: "swordfish",
        CONF_VERIFY_SSL: False,
    }
    base.update(overrides)
    return base


def _patch_validate(uuid: str = "test-uuid", **kwargs):
    """Patch validate_input — most tests want to bypass the network."""
    return patch(
        "custom_components.udi_iox.config_flow.validate_input",
        AsyncMock(return_value={"title": "test (portal)", "uuid": uuid}, **kwargs),
    )


# --- happy paths ------------------------------------------------------


async def test_user_step_form_renders(hass) -> None:
    """Initial user step shows the form with defaults."""
    result = await _start_user_flow(hass)
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {}


async def test_user_step_creates_entry(hass) -> None:
    """Submitting valid user input → CREATE_ENTRY."""
    flow = await _start_user_flow(hass)
    with (
        _patch_validate("portal-uuid"),
        patch(
            "custom_components.udi_iox.async_setup_entry",
            AsyncMock(return_value=True),
        ),
    ):
        result = await hass.config_entries.flow.async_configure(
            flow["flow_id"], _build_user_input()
        )
        await hass.async_block_till_done()
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_USERNAME] == "admin@example.com"


async def test_unique_id_prevents_duplicate_entries(hass) -> None:
    """A second flow with the same controller uuid aborts as
    already_configured."""
    setup_patch = patch(
        "custom_components.udi_iox.async_setup_entry",
        AsyncMock(return_value=True),
    )
    # First entry succeeds.
    flow1 = await _start_user_flow(hass)
    with _patch_validate("dup-uuid"), setup_patch:
        await hass.config_entries.flow.async_configure(
            flow1["flow_id"], _build_user_input()
        )
        await hass.async_block_till_done()
    # Second flow with the same uuid → abort.
    flow2 = await _start_user_flow(hass)
    with _patch_validate("dup-uuid"), setup_patch:
        result = await hass.config_entries.flow.async_configure(
            flow2["flow_id"], _build_user_input()
        )
    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "already_configured"


# --- error paths ------------------------------------------------------


async def test_invalid_auth_surfaces_error_on_password(hass) -> None:
    from custom_components.udi_iox.config_flow import InvalidAuth

    flow = await _start_user_flow(hass)
    with patch(
        "custom_components.udi_iox.config_flow.validate_input",
        AsyncMock(side_effect=InvalidAuth),
    ):
        result = await hass.config_entries.flow.async_configure(
            flow["flow_id"], _build_user_input()
        )
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["errors"] == {CONF_PASSWORD: "invalid_auth"}


async def test_cannot_connect_surfaces_base_error(hass) -> None:
    from custom_components.udi_iox.config_flow import CannotConnect

    flow = await _start_user_flow(hass)
    with patch(
        "custom_components.udi_iox.config_flow.validate_input",
        AsyncMock(side_effect=CannotConnect),
    ):
        result = await hass.config_entries.flow.async_configure(
            flow["flow_id"], _build_user_input()
        )
    assert result["errors"] == {"base": "cannot_connect"}


async def test_invalid_host_surfaces_base_error(hass) -> None:
    from custom_components.udi_iox.config_flow import InvalidHost

    flow = await _start_user_flow(hass)
    with patch(
        "custom_components.udi_iox.config_flow.validate_input",
        AsyncMock(side_effect=InvalidHost),
    ):
        result = await hass.config_entries.flow.async_configure(
            flow["flow_id"], _build_user_input()
        )
    assert result["errors"] == {"base": "invalid_host"}


async def test_unknown_exception_surfaces_unknown_error(hass) -> None:
    """Defensive — any unexpected error in validate_input becomes an
    ``unknown`` form error rather than a 500."""
    flow = await _start_user_flow(hass)
    with patch(
        "custom_components.udi_iox.config_flow.validate_input",
        AsyncMock(side_effect=RuntimeError("synthetic")),
    ):
        result = await hass.config_entries.flow.async_configure(
            flow["flow_id"], _build_user_input()
        )
    assert result["errors"] == {"base": "unknown"}


# --- validate_input ---------------------------------------------------


async def test_validate_input_uses_portal_auth(hass) -> None:
    """``validate_input`` wires the controller up with ``PortalAuth``."""
    from custom_components.udi_iox.config_flow import validate_input

    captured: dict = {}

    class FakeController:
        def __init__(self, *args, auth=None, **kwargs):
            captured["auth"] = auth
            self.config = type("C", (), {"uuid": "u"})()

        async def connect(self, *, start_websocket=True):
            pass

        async def stop(self):
            pass

    with patch("custom_components.udi_iox.config_flow.Controller", FakeController):
        await validate_input(hass, _build_user_input())

    from pyisyox import PortalAuth

    assert isinstance(captured["auth"], PortalAuth)


async def test_validate_input_rejects_non_http_scheme(hass) -> None:
    """A host without http:// or https:// is treated as an InvalidHost."""
    from custom_components.udi_iox.config_flow import InvalidHost, validate_input

    with pytest.raises(InvalidHost):
        await validate_input(hass, _build_user_input(**{CONF_HOST: "eisy.local:443"}))


# --- discovery (SSDP / DHCP) ------------------------------------------

_UUID = "00:21:b9:01:23:45"

# pytest-homeassistant-custom-component stubs SsdpServiceInfo /
# DhcpServiceInfo to argument-less classes, so we duck-type the discovery
# payloads (the flow manager just forwards ``data`` to the step handler).


def _ssdp_info(model_version: str = "6.0.4", host: str = "1.2.3.4") -> SimpleNamespace:
    return SimpleNamespace(
        ssdp_location=f"https://{host}:443/desc",
        upnp={
            ssdp.ATTR_UPNP_FRIENDLY_NAME: "eisy Controller",
            ssdp.ATTR_UPNP_UDN: f"uuid:{_UUID}",
            "modelVersion": model_version,
        },
    )


def _dhcp_info(host: str = "1.2.3.4") -> SimpleNamespace:
    return SimpleNamespace(ip=host, hostname="eisy-abc", macaddress="0021b9012345")


async def test_ssdp_discovery_shows_user_form(hass) -> None:
    """A 6.x IoX device discovered via SSDP advances to the user step."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_SSDP}, data=_ssdp_info()
    )
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "user"


async def test_ssdp_rejects_pre_6_firmware(hass) -> None:
    """SSDP for a controller advertising a non-6.x modelVersion aborts."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_SSDP},
        data=_ssdp_info(model_version="5.0.16C"),
    )
    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "unsupported_firmware"


async def test_dhcp_discovery_shows_user_form(hass) -> None:
    """A device discovered via DHCP advances to the user step."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_DHCP}, data=_dhcp_info()
    )
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "user"


async def test_discovery_updates_host_of_existing_entry(hass) -> None:
    """SSDP rediscovery of an already-configured controller at a new IP
    aborts as already_configured *and* updates the stored host."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=_UUID,
        data={**_build_user_input(), CONF_HOST: "https://1.2.3.4:443"},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_SSDP},
        data=_ssdp_info(host="5.6.7.8"),
    )
    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_HOST] == "https://5.6.7.8:443"


# --- reauth -----------------------------------------------------------


def _add_entry(hass) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="reauth-uuid",
        title="eisy.local",
        data=_build_user_input(),
    )
    entry.add_to_hass(hass)
    return entry


async def test_reauth_flow_success(hass) -> None:
    """Reauth with good creds updates the entry and aborts as
    reauth_successful."""
    entry = _add_entry(hass)
    flow = await entry.start_reauth_flow(hass)
    assert flow["step_id"] == "reauth_confirm"

    with (
        _patch_validate("reauth-uuid"),
        patch(
            "custom_components.udi_iox.async_setup_entry",
            AsyncMock(return_value=True),
        ),
    ):
        result = await hass.config_entries.flow.async_configure(
            flow["flow_id"],
            {CONF_USERNAME: "new@example.com", CONF_PASSWORD: "newpass"},
        )
        await hass.async_block_till_done()
    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_USERNAME] == "new@example.com"
    assert entry.data[CONF_PASSWORD] == "newpass"


async def test_reauth_flow_invalid_auth(hass) -> None:
    """Bad creds during reauth re-show the form with the error."""
    from custom_components.udi_iox.config_flow import InvalidAuth

    entry = _add_entry(hass)
    flow = await entry.start_reauth_flow(hass)
    with patch(
        "custom_components.udi_iox.config_flow.validate_input",
        AsyncMock(side_effect=InvalidAuth),
    ):
        result = await hass.config_entries.flow.async_configure(
            flow["flow_id"],
            {CONF_USERNAME: "x@example.com", CONF_PASSWORD: "wrong"},
        )
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["errors"] == {CONF_PASSWORD: "invalid_auth"}


async def test_reauth_flow_cannot_connect(hass) -> None:
    """A connection error during reauth re-shows the form with a base
    error."""
    from custom_components.udi_iox.config_flow import CannotConnect

    entry = _add_entry(hass)
    flow = await entry.start_reauth_flow(hass)
    with patch(
        "custom_components.udi_iox.config_flow.validate_input",
        AsyncMock(side_effect=CannotConnect),
    ):
        result = await hass.config_entries.flow.async_configure(
            flow["flow_id"],
            {CONF_USERNAME: "x@example.com", CONF_PASSWORD: "p"},
        )
    assert result["errors"] == {"base": "cannot_connect"}


# --- options flow -----------------------------------------------------


async def test_options_flow(hass) -> None:
    """The options flow shows the per-platform toggles and stores them."""
    from custom_components.udi_iox.const import (
        CONF_ENABLE_NETWORKING,
        CONF_ENABLE_PROGRAMS,
        CONF_ENABLE_VARIABLES,
        CONF_IGNORE_STRING,
        CONF_RESTORE_LIGHT_STATE,
        CONF_SENSOR_STRING,
    )

    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="opt-uuid", data=_build_user_input()
    )
    entry.add_to_hass(hass)

    form = await hass.config_entries.options.async_init(entry.entry_id)
    assert form["type"] == data_entry_flow.FlowResultType.FORM
    assert form["step_id"] == "init"

    submitted = {
        CONF_IGNORE_STRING: "{SKIP}",
        CONF_SENSOR_STRING: "telemetry",
        CONF_RESTORE_LIGHT_STATE: True,
        CONF_ENABLE_VARIABLES: False,
        CONF_ENABLE_PROGRAMS: True,
        CONF_ENABLE_NETWORKING: True,
    }
    result = await hass.config_entries.options.async_configure(
        form["flow_id"], submitted
    )
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert entry.options == submitted
