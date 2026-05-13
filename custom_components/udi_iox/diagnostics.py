"""Diagnostics support for the IoX integration.

HA's integration quality scale lists a per-entry diagnostics download as
a Silver-tier rule (and a strong Bronze soft requirement). This module
ships ``async_get_config_entry_diagnostics`` returning a redacted JSON
snapshot of the entry, the controller, the loaded profile/nodedefs, and
the live node / group / program / variable / network-resource state.

Redaction is intentionally narrow — only PII (portal email, password,
controller host, MAC-shaped UUID, portal host). Node addresses and
human-set names stay verbatim so bug reports retain enough context to
diagnose: a Z-Wave parameter problem on ``ZW003_1`` reads cleanly in
the diagnostics download instead of a mangled placeholder.

The profile JSON is *included* (sizes around 100-340 KB depending on
which families are loaded). The legacy isy994 integration didn't carry
it, but ``udi_iox``'s editor-codec / classifier work all routes through
the profile — so having it in the diagnostics download lets a reviewer
reproduce decisions without asking the user to grab `/rest/profiles`
out-of-band.

References:
- <https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/diagnostics>
"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_USERNAME,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntry

from .models import IsyConfigEntry

#: Keys we redact from ``entry.data`` before exposing it. The portal
#: email lives at ``CONF_USERNAME`` for PortalAuth installs and is the
#: account identifier on UD's side; the password is the matching JWT
#: credential. ``CONF_HOST`` is redacted too — for portal mode it
#: encodes the LAN address of the controller and for LocalAuth it's the
#: local URL the user typed in, both of which can be sensitive on
#: shared bug reports.
TO_REDACT_ENTRY_DATA = frozenset({CONF_PASSWORD, CONF_USERNAME, CONF_HOST})

#: Sentinel surfaced in place of redacted scalar values.
_REDACTED = "**REDACTED**"


def _redact_controller_uuid(uuid: str) -> str:
    """Mask the controller's MAC-shaped UUID.

    The eisy / Polisy report their LAN MAC as ``ControllerConfig.uuid``
    (``00:21:b9:XX:XX:XX``) — that's a unique per-device identifier
    consumers may consider PII. Keep the first three octets (the UD OUI
    ``00:21:b9``) so the device family is still identifiable, and mask
    the last three.
    """
    if not uuid:
        return ""
    parts = uuid.split(":")
    if len(parts) == 6:
        return ":".join([*parts[:3], "**", "**", "**"])
    return _REDACTED


def _redact_portal_host(portal_host: str | None) -> str | None:
    """Mask the portal hostname.

    The UD portal returns the per-account subdomain in
    ``ControllerConfig.portal_host`` (e.g. ``my-eisy-1234.isy.io``); the
    subdomain can leak account identifiers on a shared bug report.
    """
    if not portal_host:
        return None
    return _REDACTED


def _serialize_dataclass(obj: Any) -> Any:
    """``dataclasses.asdict`` with a passthrough for non-dataclasses.

    Handles the typical schema-side dataclasses (``NodeDef`` /
    ``Editor`` / ``Command`` / ``NodeProperty`` / ``NodeCommands`` /
    ``EditorRange``) — they're all slot dataclasses that ``asdict``
    walks recursively, producing pure dict/list/scalar trees that
    survive JSON encoding.
    """
    if is_dataclass(obj) and not isinstance(obj, type):
        return asdict(obj)
    return obj


def _serialize_profile(profile: Any) -> dict[str, Any]:
    """Serialize a :class:`pyisyox.schema.profile.Profile` for diagnostics.

    ``Profile`` is a slot dataclass but its ``nodedef_lookup`` is keyed
    by a ``(nodedef_id, family_id, instance_id)`` tuple which JSON can't
    encode as dict keys. The lookup is also redundant — it's an index
    rebuilt from ``families[].instances[]`` on load — so we drop it from
    the diagnostics payload and keep the structural ``families`` tree
    plus the merged NLS table (Z-Wave dynamic-nodedef label source).
    """
    if profile is None:
        return {}
    return {
        "timestamp": getattr(profile, "timestamp", ""),
        "families": {
            family_id: asdict(family)
            for family_id, family in getattr(profile, "families", {}).items()
        },
        "nls": asdict(profile.nls) if getattr(profile, "nls", None) else {},
        "nodedef_lookup_count": len(getattr(profile, "nodedef_lookup", {})),
    }


def _serialize_property_value(value: Any) -> dict[str, Any] | None:
    """One ``Node.properties`` entry as a plain dict.

    ``NodePropertyValue`` is a dataclass; ``asdict`` flattens it cleanly.
    """
    return None if value is None else _serialize_dataclass(value)


def _serialize_node(node: Any) -> dict[str, Any]:
    """Snapshot a runtime ``Node`` (or ``Group`` / ``Folder``).

    Addresses + names are intentionally kept verbatim — reviewers
    triaging an issue need to correlate the diagnostics with the user's
    log lines (which carry the same wire addresses). Same posture as
    PyISY 3.x's legacy diagnostics: scrub credentials, not topology.
    """
    payload: dict[str, Any] = {
        "address": getattr(node, "address", None),
        "name": getattr(node, "name", None),
        "nodedef_id": getattr(node, "nodedef_id", None),
        "family_id": getattr(node, "family_id", None),
        "instance_id": getattr(node, "instance_id", None),
        "type": getattr(node, "type", None),
        "protocol": getattr(node, "protocol", None),
        "parent_address": getattr(node, "parent_address", None),
        "primary_address": getattr(node, "primary_address", None),
        "enabled": getattr(node, "enabled", None),
        "flag": getattr(node, "flag", None),
    }
    properties = getattr(node, "properties", None)
    if isinstance(properties, dict):
        payload["properties"] = {
            key: _serialize_property_value(val) for key, val in properties.items()
        }
    # Group / Folder don't expose properties — include their member
    # tables when present so the snapshot is useful for scene-routing
    # bug reports.
    for attr in ("member_addresses", "controller_addresses"):
        members = getattr(node, attr, None)
        if members is not None:
            payload[attr] = list(members)
    return payload


def _serialize_program(program: Any) -> dict[str, Any]:
    """Snapshot a runtime ``Program``."""
    return {
        "address": getattr(program, "address", None),
        "name": getattr(program, "name", None),
        "status": getattr(program, "status", None),
        "running": getattr(program, "running", None),
        "enabled": getattr(program, "enabled", None),
        "run_at_startup": getattr(program, "run_at_startup", None),
        "last_run_time": _isoformat(getattr(program, "last_run_time", None)),
        "last_finish_time": _isoformat(getattr(program, "last_finish_time", None)),
        "parent_address": getattr(program, "parent_address", None),
    }


def _serialize_variable(variable: Any) -> dict[str, Any]:
    """Snapshot a runtime ``Variable``.

    Address shape is ``{type_id}.{id}`` (e.g. ``"2.5"``); ``type_id`` is
    ``"1"`` for integer variables, ``"2"`` for state variables.
    """
    return {
        "type_id": getattr(variable, "type_id", None),
        "id": getattr(variable, "id", None),
        "address": getattr(variable, "address", None),
        "name": getattr(variable, "name", None),
        "value": getattr(variable, "value", None),
        "init": getattr(variable, "init", None),
        "precision": getattr(variable, "precision", None),
    }


def _serialize_network_resource(resource: Any) -> dict[str, Any]:
    """Snapshot a runtime ``NetworkResource``."""
    return {
        "address": getattr(resource, "address", None),
        "name": getattr(resource, "name", None),
    }


def _isoformat(value: Any) -> str | None:
    """``datetime.isoformat`` when possible, else passthrough."""
    if value is None:
        return None
    iso = getattr(value, "isoformat", None)
    if callable(iso):
        return iso()
    return str(value)


def _serialize_isy_data_shape(isy_data: Any) -> dict[str, Any]:
    """Per-platform counts from the integration's ``IsyData`` registry."""

    def _platform_counts(mapping: dict[Any, list[Any]] | None) -> dict[str, int]:
        if not mapping:
            return {}
        return {str(platform): len(items) for platform, items in mapping.items()}

    return {
        "primary_nodes": _platform_counts(getattr(isy_data, "nodes", None)),
        "root_nodes": _platform_counts(getattr(isy_data, "root_nodes", None)),
        "aux_properties": _platform_counts(getattr(isy_data, "aux_properties", None)),
        "programs": _platform_counts(getattr(isy_data, "programs", None)),
        "variables": _platform_counts(getattr(isy_data, "variables", None)),
        "groups": len(getattr(isy_data, "groups", []) or []),
        "net_resources": len(getattr(isy_data, "net_resources", []) or []),
        "event_triggers": len(getattr(isy_data, "node_triggers", {}) or {}),
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: IsyConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    isy_data = entry.runtime_data
    controller = isy_data.root

    config = controller.config
    websocket = getattr(controller, "websocket", None)

    return {
        "entry": {
            "title": entry.title,
            "version": entry.version,
            "domain": entry.domain,
            "data": async_redact_data(dict(entry.data), TO_REDACT_ENTRY_DATA),
            "options": dict(entry.options),
        },
        "controller": {
            "uuid": _redact_controller_uuid(config.uuid),
            "version": config.version,
            "portal_host": _redact_portal_host(config.portal_host),
            "base_url": _REDACTED,
            "connected": getattr(controller, "connected", None),
            "websocket": {
                "status": websocket.status.value if websocket is not None else None,
                "last_event_at": _isoformat(getattr(websocket, "last_event_at", None)),
            },
        },
        "counts": {
            "nodes": len(controller.nodes),
            "groups": len(controller.groups),
            "folders": len(controller.folders),
            "programs": len(controller.programs),
            "program_folders": len(controller.program_folders),
            "network_resources": len(controller.network_resources),
            "variables": {
                str(type_id): len(vars_)
                for type_id, vars_ in controller.variables.items()
            },
        },
        "isy_data_shape": _serialize_isy_data_shape(isy_data),
        "profile": _serialize_profile(controller.profile),
        "nodes": [_serialize_node(node) for node in controller.nodes.values()],
        "groups": [_serialize_node(group) for group in controller.groups.values()],
        "folders": [_serialize_node(folder) for folder in controller.folders.values()],
        "programs": [
            _serialize_program(program) for program in controller.programs.values()
        ],
        "program_folders": [
            _serialize_program(folder) for folder in controller.program_folders.values()
        ],
        "variables": {
            str(type_id): [_serialize_variable(var) for var in vars_.values()]
            for type_id, vars_ in controller.variables.items()
        },
        "network_resources": [
            _serialize_network_resource(resource)
            for resource in controller.network_resources.values()
        ],
        "event_triggers": {
            address: [cmd.id if hasattr(cmd, "id") else str(cmd) for cmd in commands]
            for address, commands in isy_data.node_triggers.items()
        },
    }


async def async_get_device_diagnostics(
    hass: HomeAssistant, entry: IsyConfigEntry, device: DeviceEntry
) -> dict[str, Any]:
    """Return diagnostics for one device — the matching IoX node(s)."""
    isy_data = entry.runtime_data
    controller = isy_data.root
    uuid = controller.config.uuid

    # DeviceInfo identifiers carry ``(DOMAIN, "<uuid>_<address>")`` or
    # ``(DOMAIN, "<uuid>")`` for the controller stub. Walk every
    # identifier on this device and surface every matching node — a
    # KeypadLinc parent device has its sub-buttons folded under the
    # same primary, so a device-level diagnostic can legitimately span
    # multiple node addresses.
    matching_addresses: list[str] = []
    for _ident_domain, ident_id in device.identifiers:
        if ident_id.startswith(f"{uuid}_"):
            matching_addresses.append(ident_id[len(uuid) + 1 :])

    matched: list[dict[str, Any]] = []
    for address in matching_addresses:
        node = controller.nodes.get(address) or controller.groups.get(address)
        if node is not None:
            matched.append(_serialize_node(node))

    return {
        "device": {
            "id": device.id,
            "name": device.name,
            "model": device.model,
            "manufacturer": device.manufacturer,
            "sw_version": device.sw_version,
        },
        "matched_addresses": matching_addresses,
        "nodes": matched,
    }
