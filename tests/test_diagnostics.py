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
    _redact_entry_title,
    _redact_portal_host,
    async_get_config_entry_diagnostics,
)


@pytest.fixture
def platforms() -> list[Platform]:
    """Load the switch platform so the diagnostics entity/device
    sections have real registry rows to capture."""
    return [Platform.SWITCH]


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


def test_redact_entry_title_strips_host_every_spelling() -> None:
    """``CONF_HOST`` is redacted in ``entry.data`` but the host also
    rides along in the entry title / hub device name — strip every
    spelling (full value, netloc, bare hostname), longest first."""
    # Title is "<name> (<bare hostname>)" — mask just the hostname.
    assert (
        _redact_entry_title("My eisy (eisy.local)", "http://eisy.local:8080")
        == "My eisy (**REDACTED**)"
    )
    # No controller name → title is the bare hostname (fixture case).
    assert _redact_entry_title("eisy.local", "http://eisy.local:8080") == "**REDACTED**"
    # host:port in the title → netloc masked before bare host, so no
    # leftover ":8080".
    assert (
        _redact_entry_title("Skynet ISY (eisy.local:8080)", "https://eisy.local:8080")
        == "Skynet ISY (**REDACTED**)"
    )
    # Scheme-less host still masks.
    assert _redact_entry_title("foo (eisy.local)", "eisy.local") == "foo (**REDACTED**)"
    # Nothing to do.
    assert _redact_entry_title(None, "http://x") is None
    assert _redact_entry_title("Title", None) == "Title"


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

    Nodes live under ``controller.nodes`` (keyed by address) per the
    ``Controller.to_dict()`` shape.
    """
    payload = await async_get_config_entry_diagnostics(hass, init_integration)
    nodes = payload["controller"]["nodes"]
    assert nodes, "snapshot fixture should produce at least one node"
    for address, node in nodes.items():
        assert address  # dict key carries the wire address
        assert node["address"] == address
        # name might legitimately be empty for some hidden nodes, but
        # the key must be present and not the redact sentinel when set.
        assert node["name"] != "**REDACTED**"


async def test_diagnostics_lists_entities_and_devices_without_leaks(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
) -> None:
    """The payload enumerates the HA entities + devices actually
    created (the controller dump alone doesn't show what HA built),
    and neither the host nor the unmasked controller MAC leaks into
    any of it (incl. unique_ids / device identifiers)."""
    import json
    from urllib.parse import urlparse

    from homeassistant.const import CONF_HOST

    payload = await async_get_config_entry_diagnostics(hass, init_integration)

    entities = payload["entities"]
    devices = payload["devices"]
    assert entities, "expected entity-registry rows"
    assert devices, "expected device-registry rows"
    assert any(e["entity_id"].startswith("switch.") for e in entities)
    assert all(e["entity_id"] and e["unique_id"] for e in entities)
    assert all(d["key"] for d in devices)
    # entity→device links resolve to a real device key.
    device_keys = {d["key"] for d in devices}
    assert any(e["device"] in device_keys for e in entities)

    host = init_integration.data[CONF_HOST]
    bare_host = urlparse(host).hostname or host
    real_uuid = init_integration.runtime_data.root.config.uuid
    blob = json.dumps(payload, default=str)
    assert bare_host not in blob, "host leaked into diagnostics"
    assert real_uuid not in blob, "unmasked controller MAC leaked"
    assert _redact_controller_uuid(real_uuid) in blob, "expected masked MAC"


def test_isy_data_shape_handles_missing_mappings() -> None:
    """``_platform_counts`` returns empty when the mapping is missing /
    falsy."""
    from custom_components.udi_iox.diagnostics import _isy_data_shape

    class _Empty:
        pass

    shape = _isy_data_shape(_Empty())
    assert shape["primary_nodes"] == {}
    assert shape["root_nodes"] == {}
    assert shape["aux_properties"] == {}
    assert shape["programs"] == {}
    assert shape["variables"] == {}
    assert shape["groups"] == 0
    assert shape["net_resources"] == 0
    assert shape["event_triggers"] == 0


async def test_async_get_device_diagnostics_returns_matched_nodes(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
) -> None:
    """``async_get_device_diagnostics`` returns the matching node(s)'
    ``to_dict()`` plus device-registry header info."""
    from homeassistant.helpers import device_registry as dr

    from custom_components.udi_iox.const import DOMAIN
    from custom_components.udi_iox.diagnostics import async_get_device_diagnostics

    isy_data = init_integration.runtime_data
    controller = isy_data.root
    uuid = controller.config.uuid
    # Pick a known node and synthesise a DeviceEntry for it. The per-node
    # devices aren't auto-created with platforms=[]; we don't need the
    # registry round-trip — only the identifiers.
    node_address = next(iter(controller.nodes))
    fake = dr.DeviceEntry(  # type: ignore[call-arg]
        identifiers={(DOMAIN, f"{uuid}_{node_address}")}
    )
    payload = await async_get_device_diagnostics(hass, init_integration, fake)
    assert payload["matched_addresses"] == [node_address]
    assert payload["nodes"]
    assert payload["nodes"][0]["address"] == node_address


async def test_async_get_device_diagnostics_handles_unknown_device(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
) -> None:
    """A device whose identifiers don't map to any node yields an empty
    ``nodes`` list — pin that the function doesn't crash."""
    from homeassistant.helpers import device_registry as dr

    from custom_components.udi_iox.diagnostics import async_get_device_diagnostics

    fake_device = dr.DeviceEntry(identifiers=set())  # type: ignore[call-arg]
    payload = await async_get_device_diagnostics(hass, init_integration, fake_device)
    assert payload["nodes"] == []
    assert payload["matched_addresses"] == []


async def test_diagnostics_includes_profile_payload(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
) -> None:
    """The loaded profile (nodedefs + editors + linkdefs + NLS tables)
    is part of the diagnostics download — the editor codec / classifier
    logic all routes through it, so a reviewer triaging a misclassified
    node needs the same profile pyisyox used.

    Lives under ``controller.profile`` per ``Controller.to_dict()``.
    """
    payload = await async_get_config_entry_diagnostics(hass, init_integration)
    profile = payload["controller"]["profile"]
    assert isinstance(profile, dict)
    assert profile.get("families"), "families tree empty in diagnostics"
    assert profile.get("nodedef_lookup_count", 0) > 0
