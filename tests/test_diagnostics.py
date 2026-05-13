"""Tests for the udi_iox diagnostics module.

Drives the existing ``populated_controller`` + ``init_integration``
fixtures through ``async_get_config_entry_diagnostics`` and snapshots
the redacted output. The snapshot freezes the *shape* (keys, redaction
sentinels, presence of every section) — not the raw fixture content —
so future changes to the controller fixture don't churn the file.
"""

from __future__ import annotations

import pytest
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    SnapshotAssertion,
)

from custom_components.udi_iox.diagnostics import (
    TO_REDACT_ENTRY_DATA,
    _redact_controller_uuid,
    _redact_portal_host,
    async_get_config_entry_diagnostics,
)


@pytest.fixture
def platforms() -> list[Platform]:
    """No platforms — diagnostics doesn't need entities registered."""
    return []


def test_redact_controller_uuid_keeps_oui_masks_device_octets() -> None:
    """The first three octets are the UD OUI (``00:21:b9``) and survive
    masking; the last three are the per-device serial bytes and get
    blanked. A UUID that isn't MAC-shaped falls back to a full redact."""
    assert _redact_controller_uuid("00:21:b9:01:23:45") == "00:21:b9:**:**:**"
    assert _redact_controller_uuid("00:21:b9:ab:cd:ef") == "00:21:b9:**:**:**"
    # Not MAC-shaped → full redact sentinel.
    assert _redact_controller_uuid("not-a-mac") == "**REDACTED**"
    # Empty stays empty (no controller config yet).
    assert _redact_controller_uuid("") == ""


def test_redact_portal_host_masks_subdomain_keeps_none() -> None:
    """A non-empty portal host always redacts (subdomain is per-account);
    ``None`` passes through so LocalAuth installs render cleanly."""
    assert _redact_portal_host("my-eisy-1234.isy.io") == "**REDACTED**"
    assert _redact_portal_host(None) is None
    assert _redact_portal_host("") is None


async def test_async_get_config_entry_diagnostics_snapshot(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    snapshot: SnapshotAssertion,
) -> None:
    """End-to-end shape check — the diagnostics payload matches the
    snapshot. Captures the redaction sentinels, the per-platform count
    map, and the presence of every section (profile, nodes, groups,
    folders, programs, variables, network resources, event triggers).
    """
    payload = await async_get_config_entry_diagnostics(hass, init_integration)
    assert payload == snapshot


async def test_diagnostics_redacts_entry_credentials(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
) -> None:
    """``entry.data`` keys named in ``TO_REDACT_ENTRY_DATA`` (password /
    username / host) are surfaced as the HA redact sentinel rather than
    the original values. Pin this explicitly so a future refactor that
    forgets to wire the redact helper trips immediately."""
    payload = await async_get_config_entry_diagnostics(hass, init_integration)
    entry_data = payload["entry"]["data"]
    for key in TO_REDACT_ENTRY_DATA:
        if key in entry_data:
            assert entry_data[key] == "**REDACTED**", (
                f"entry.data[{key!r}] was not redacted"
            )


async def test_diagnostics_preserves_node_addresses_and_names(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
) -> None:
    """Node ``address`` and ``name`` are *not* redacted — bug-report
    diagnosis needs the wire address to correlate with log lines, and
    the human name to identify what the user actually has.
    """
    payload = await async_get_config_entry_diagnostics(hass, init_integration)
    assert payload["nodes"], "snapshot fixture should produce at least one node"
    for node in payload["nodes"]:
        assert node["address"]
        # name might legitimately be empty for some hidden nodes, but
        # the key must be present and not the redact sentinel when set.
        assert node["name"] != "**REDACTED**"


async def test_diagnostics_includes_profile_payload(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
) -> None:
    """The loaded profile (nodedefs + editors + linkdefs + NLS tables)
    is part of the diagnostics download — the editor codec / classifier
    logic all routes through it, so a reviewer triaging a misclassified
    node needs the same profile pyisyox used."""
    payload = await async_get_config_entry_diagnostics(hass, init_integration)
    profile = payload["profile"]
    assert isinstance(profile, dict)
    # The bundled eisy6 fixture always carries family ``"1"`` (Insteon)
    # with at least one editor and one nodedef on the default instance;
    # the lookup count tracks how many ``(nodedef_id, family, instance)``
    # entries pyisyox built. Either signal is enough to prove the
    # profile blob landed in the diagnostics payload.
    assert profile.get("families"), "families tree empty in diagnostics"
    assert profile.get("nodedef_lookup_count", 0) > 0
