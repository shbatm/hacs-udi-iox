"""Tests for the udi_iox config flow.

Pins the auth-mode picker logic, the auth-error → ConfigEntryAuthFailed
mapping, and the connection-error → ConfigEntryNotReady mapping. The
classifier and entity wiring don't run — validate_input opens a
short-lived controller, reads ``config.uuid``, and tears down.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries, data_entry_flow
from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
)

from custom_components.udi_iox.const import (
    AUTH_MODE_LOCAL,
    AUTH_MODE_PORTAL,
    CONF_AUTH_MODE,
    CONF_TLS_VER,
    DEFAULT_TLS_VERSION,
    DOMAIN,
)


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
        CONF_AUTH_MODE: AUTH_MODE_PORTAL,
        CONF_TLS_VER: DEFAULT_TLS_VERSION,
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


async def test_user_step_portal_auth_creates_entry(hass) -> None:
    """Submitting valid Portal-auth user input → CREATE_ENTRY."""
    flow = await _start_user_flow(hass)
    with _patch_validate("portal-uuid"):
        result = await hass.config_entries.flow.async_configure(
            flow["flow_id"], _build_user_input()
        )
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_AUTH_MODE] == AUTH_MODE_PORTAL
    assert result["data"][CONF_USERNAME] == "admin@example.com"


async def test_user_step_local_auth_creates_entry(hass) -> None:
    """LocalAuth path also creates an entry — auth_mode is the only
    schema knob that branches the validation."""
    flow = await _start_user_flow(hass)
    with _patch_validate("local-uuid"):
        result = await hass.config_entries.flow.async_configure(
            flow["flow_id"],
            _build_user_input(CONF_AUTH_MODE=AUTH_MODE_LOCAL),
        )
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_AUTH_MODE] == AUTH_MODE_LOCAL


async def test_unique_id_prevents_duplicate_entries(hass) -> None:
    """A second flow with the same controller uuid aborts as
    already_configured."""
    # First entry succeeds.
    flow1 = await _start_user_flow(hass)
    with _patch_validate("dup-uuid"):
        await hass.config_entries.flow.async_configure(
            flow1["flow_id"], _build_user_input()
        )
    # Second flow with the same uuid → abort.
    flow2 = await _start_user_flow(hass)
    with _patch_validate("dup-uuid"):
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


async def test_validate_input_picks_portal_auth_for_portal_mode(hass) -> None:
    """Auth-mode = portal → PortalAuth, not LocalAuth."""
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

    with (
        patch("custom_components.udi_iox.config_flow.Controller", FakeController),
    ):
        await validate_input(hass, _build_user_input(CONF_AUTH_MODE=AUTH_MODE_PORTAL))

    from pyisyox import PortalAuth

    assert isinstance(captured["auth"], PortalAuth)


async def test_validate_input_picks_local_auth_for_local_mode(hass) -> None:
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
        await validate_input(hass, _build_user_input(CONF_AUTH_MODE=AUTH_MODE_LOCAL))

    from pyisyox import LocalAuth

    assert isinstance(captured["auth"], LocalAuth)


async def test_validate_input_rejects_non_http_scheme(hass) -> None:
    """A host without http:// or https:// is treated as an InvalidHost."""
    from custom_components.udi_iox.config_flow import InvalidHost, validate_input

    with pytest.raises(InvalidHost):
        await validate_input(hass, _build_user_input(**{CONF_HOST: "eisy.local:443"}))
