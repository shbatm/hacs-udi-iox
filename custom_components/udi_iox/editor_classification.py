"""Editor-shape → HA platform mapping for IoX controls.

The IoX profile ships an *editor* per nodedef property / command
parameter — UOM, numeric bounds, decimal precision, an optional
discrete ``subset`` of valid raw ints, and an optional ``names`` map
(raw int → display string, which is also the bidirectional codec for
enum values). pyisyox owns "what is this control and which editor
governs it"; this module owns "therefore it's HA platform X" — keeping
that HA-specific decision out of pyisyox.

Lives in its own module (rather than ``helpers.py``) so the entity
classes can import it without dragging in the ``models`` / ``event``
import cycle.
"""

from __future__ import annotations

from homeassistant.const import Platform
from pyisyox import Controller, Node
from pyisyox.schema.editor import Editor, EditorRange

from .const import (
    UOM_8_BIT_RANGE,
    UOM_BARRIER,
    UOM_FAN_MODES,
    UOM_HVAC_ACTIONS,
    UOM_HVAC_MODE_GENERIC,
    UOM_HVAC_MODE_INSTEON,
    UOM_INDEX,
    UOM_PERCENTAGE,
)

#: UOMs that mark a value as binary (two-state). Mirrors pyisyox's
#: private ``_BINARY_UOMS`` — kept here because pyisyox doesn't export
#: it and the aux fan-out / read classification both need it.
BINARY_UOMS = frozenset({"2", "78", "79"})

#: UOMs whose values are an *index* into a list of named options — the
#: natural HA entity is a SELECT. UOM 25 is the generic index type; the
#: rest are the named IoX enumerations that ``UOM_TO_STATES`` (and the
#: editor ``names`` table) cover. UOM 51 (percent) and UOM 100 (byte
#: 0-255) are deliberately *not* here — those stay NUMBER even when the
#: profile expresses them as a wide ``subset`` (0-100 / 0-255) rather
#: than ``min``/``max``.
INDEX_UOMS = frozenset(
    {
        UOM_INDEX,
        UOM_HVAC_ACTIONS,
        UOM_HVAC_MODE_GENERIC,
        UOM_HVAC_MODE_INSTEON,
        UOM_FAN_MODES,
        UOM_BARRIER,
    }
)

#: Generic editor ids that name a value *shape* regardless of UOM. PG3
#: plugin nodedefs lean on these; firmware nodedefs use ``I_*`` / ``ZW_*``
#: ids whose shape we read off the range instead.
_NUMERIC_EDITOR_IDS = frozenset({"INTEGER", "FLOAT"})
_BOOL_EDITOR_IDS = frozenset({"BOOL", "bool", "I_BOOL"})


def resolve_editor(controller: Controller, node: Node, control: str) -> Editor | None:
    """Resolve the editor governing ``control`` on ``node``.

    The editor reference lives on the nodedef *property* for settable
    status props (``OL``, ``RR``, setpoints, …); for command-only
    controls (``BL`` backlight — no backing property, value tracked
    optimistically) it's on the accept command's first parameter.
    Resolution is scoped to ``(family_id, instance_id)`` so the same
    control id can land on a different editor per nodedef. Returns
    ``None`` when the nodedef isn't loaded or the control carries no
    usable editor reference.
    """
    nodedef = node.nodedef
    if nodedef is None:
        return None
    editor_id = ""
    if (prop := nodedef.properties.get(control)) is not None:
        editor_id = prop.editor_id
    else:
        for cmd in nodedef.cmds.accepts:
            if cmd.id == control and cmd.parameters:
                editor_id = cmd.parameters[0].editor_id
                break
    if not editor_id:
        return None
    editor = controller.profile.find_editor(editor_id, node.family_id, node.instance_id)
    if editor is None or not editor.ranges:
        return None
    return editor


def range_for_control(
    controller: Controller, node: Node, control: str, prop_uom: str | None = None
) -> EditorRange | None:
    """Resolve the editor for ``control`` and pick the matching range.

    ``prop_uom`` (the control's live reported UOM) disambiguates
    multi-range editors (°F vs °C variants); falls back to the first
    range when there's no hint or no match. ``None`` when the editor
    can't be resolved.
    """
    editor = resolve_editor(controller, node, control)
    if editor is None:
        return None
    return editor.range_for(prop_uom)


def platform_for_control(
    editor: Editor | None, prop_uom: str | None = None, *, writable: bool
) -> Platform | None:
    """Map an editor-governed control to an HA platform, or ``None`` when
    the editor doesn't pin it down (caller falls back to the static map
    / sensor).

    Layered, cheapest signal first (see issue #10):

    1. **Editor id** — ``INTEGER`` / ``FLOAT`` → NUMBER even though they
       report UOM 25 (treating them by UOM would build a 1001-option
       dropdown); ``BOOL`` / ``bool`` / ``I_BOOL`` → SWITCH when
       writable, else BINARY_SENSOR.
    2. **Always-numeric UOMs** — UOM 51 (percent 0-100) and UOM 100
       (byte 0-255) → NUMBER, regardless of how the range is expressed.
    3. **Binary UOMs** (2 / 78 / 79) → SWITCH if writable else
       BINARY_SENSOR.
    4. **Index UOMs** (25 + the named IoX enumerations) → SELECT.
    5. **Range shape** (for editors whose UOM didn't decide) — ``names``
       with no numeric ``min``/``max`` → SELECT (a ``subset`` then
       narrows which names are valid options); a pure numeric
       ``subset`` → SELECT; ``min``/``max`` present → NUMBER (covers
       ``I_OL``'s 0-100 slider that labels 0 as "Off").

    ``writable`` reflects where the editor reference came from — a
    nodedef property (read, ``False``) vs. an accept-command parameter
    (write, ``True``).
    """
    if editor is None or not editor.ranges:
        return None
    if editor.id in _NUMERIC_EDITOR_IDS:
        return Platform.NUMBER
    if editor.id in _BOOL_EDITOR_IDS:
        return Platform.SWITCH if writable else Platform.BINARY_SENSOR
    rng = editor.range_for(prop_uom)
    if rng.uom in (UOM_PERCENTAGE, UOM_8_BIT_RANGE):
        return Platform.NUMBER
    if rng.uom in BINARY_UOMS:
        return Platform.SWITCH if writable else Platform.BINARY_SENSOR
    if rng.uom in INDEX_UOMS:
        return Platform.SELECT
    has_numeric_bounds = rng.min is not None or rng.max is not None
    if rng.names and not has_numeric_bounds:
        return Platform.SELECT
    if rng.subset and not rng.names and not has_numeric_bounds:
        return Platform.SELECT
    if has_numeric_bounds:
        return Platform.NUMBER
    return None
