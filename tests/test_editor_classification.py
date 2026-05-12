"""Unit truth-table for helpers.platform_for_control — the editor-shape →
HA platform decision that drives dynamic aux classification (issue #10).

Editor shapes here mirror real entries from the bundled eisy6 profile
(``I_OL``, ``I_RR``, ``I_BL``, ``I_BL_KP``, ``I_TSTAT_HCS``, the generic
``INTEGER`` / ``FLOAT`` / ``BOOL`` PG3 editors).
"""

from __future__ import annotations

import pytest
from homeassistant.const import Platform
from pyisyox.schema.editor import Editor

from custom_components.udi_iox.editor_classification import platform_for_control


def _e(eid: str, **rng: object) -> Editor:
    """Single-range editor from keyword range fields."""
    return Editor.from_json({"id": eid, "ranges": [rng]})


def _names(n: int) -> dict[str, str]:
    return {str(i): f"n{i}" for i in range(n)}


# Real-ish shapes from the bundled profile.
ED_INTEGER = _e("INTEGER", uom="25", prec=0, min=0, max=1000)
ED_FLOAT = _e("FLOAT", uom="56", min=0, max=1000)
ED_BOOL = _e("BOOL", uom="2", subset="0,1", names={"0": "False", "1": "True"})
ED_IBOOL = _e("I_BOOL", uom="2", subset="0,1")
ED_I_OL = _e("I_OL", uom="51", min=0, max=100, names={"0": "Off"})
ED_I_BL = _e("I_BL", uom="51", min=0, max=100)
ED_I_RR = _e("I_RR", uom="25", subset="0-31", names=_names(32))
ED_I_BL_KP = _e("I_BL_KP", uom="25", min=0, max=127, names=_names(128))
ED_I_TSTAT_HCS = _e("I_TSTAT_HCS", uom="66", subset="0-2", names=_names(11))
ED_I_BEEP_255 = _e("I_BEEP_255", uom="100", subset="0-255")
ED_PG_OL_ENUM = _e("PG_OL_ENUM", uom="56", names={"0": "Lo", "1": "Md", "2": "Hi"})
ED_PG_NUM = _e("PG_NUM", uom="56", min=0, max=500)
ED_PG_EMPTY = _e("PG_EMPTY", uom="56")


@pytest.mark.parametrize(
    ("editor", "writable", "expected"),
    [
        # Generic PG3 editors — editor id wins regardless of UOM.
        (ED_INTEGER, True, Platform.NUMBER),
        (ED_INTEGER, False, Platform.NUMBER),
        (ED_FLOAT, True, Platform.NUMBER),
        (ED_BOOL, True, Platform.SWITCH),
        (ED_BOOL, False, Platform.BINARY_SENSOR),
        (ED_IBOOL, True, Platform.SWITCH),
        # Insteon On Level — UOM 51 percent + a labeled 0 → still a slider.
        (ED_I_OL, True, Platform.NUMBER),
        # Backlight (DimmerLampSwitch) — UOM 51 → NUMBER.
        (ED_I_BL, True, Platform.NUMBER),
        # Ramp Rate — UOM 25 index → SELECT.
        (ED_I_RR, True, Platform.SELECT),
        # Keypad backlight — UOM 25 → SELECT.
        (ED_I_BL_KP, True, Platform.SELECT),
        # Thermostat heat/cool state — UOM 66 index → SELECT.
        (ED_I_TSTAT_HCS, False, Platform.SELECT),
        # UOM 100 byte range as a wide subset → still NUMBER.
        (ED_I_BEEP_255, True, Platform.NUMBER),
        # Plugin pure-enum editor (names, no numeric bounds) → SELECT.
        (ED_PG_OL_ENUM, True, Platform.SELECT),
        # Plain numeric range, no special UOM → NUMBER.
        (ED_PG_NUM, True, Platform.NUMBER),
        # Nothing usable → fall back.
        (ED_PG_EMPTY, True, None),
        (None, True, None),
    ],
)
def test_platform_for_control(
    editor: Editor | None, writable: bool, expected: Platform | None
) -> None:
    assert platform_for_control(editor, writable=writable) == expected


def test_multi_range_editor_picks_range_by_property_uom() -> None:
    """A °F/°C editor resolves via the property's live UOM without error."""
    ed = Editor.from_json(
        {
            "id": "I_TEMP",
            "ranges": [
                {"uom": "17", "prec": 1, "step": 0.5, "min": 37.0, "max": 120.0},
                {"uom": "4", "prec": 1, "step": 0.5, "min": 5.0, "max": 50.0},
            ],
        }
    )
    assert platform_for_control(ed, "4", writable=True) == Platform.NUMBER
    assert platform_for_control(ed, "17", writable=True) == Platform.NUMBER
