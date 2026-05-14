#!/usr/bin/env python3
"""Pre-commit check: ``strings.json`` and ``translations/en.json`` agree on keys.

HA loads display strings from ``translations/<lang>.json`` at runtime;
``strings.json`` is the source/template the i18n upload pipeline reads.
Drift between them silently breaks entity friendly names, config-flow
labels, service descriptions, etc. — caught only by users at runtime.

This hook walks both JSON trees, collects every leaf-key path, and
fails when the two key sets diverge. Values are not compared (en.json
holds the user-facing English text; strings.json may differ once we
have other languages, though for English they typically match).

Run manually::

    python scripts/check_strings_sync.py
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
COMPONENT = REPO / "custom_components" / "udi_iox"
STRINGS_PATH = COMPONENT / "strings.json"
EN_PATH = COMPONENT / "translations" / "en.json"


def _key_paths(obj: object, prefix: tuple[str, ...] = ()) -> Iterator[tuple[str, ...]]:
    """Yield every leaf-key path inside a nested dict.

    A "leaf" is any non-dict value — including lists and primitives.
    Lists aren't descended into because the i18n schema doesn't use
    them in a way that needs structural comparison.
    """
    if not isinstance(obj, dict):
        return
    for key, value in obj.items():
        path = (*prefix, key)
        if isinstance(value, dict):
            yield from _key_paths(value, path)
        else:
            yield path


def main() -> int:
    """Compare key paths; print divergences and return non-zero on drift."""
    if not STRINGS_PATH.exists():
        print(f"FATAL: {STRINGS_PATH} not found", file=sys.stderr)
        return 2
    if not EN_PATH.exists():
        print(f"FATAL: {EN_PATH} not found", file=sys.stderr)
        return 2

    strings = json.loads(STRINGS_PATH.read_text(encoding="utf-8"))
    en = json.loads(EN_PATH.read_text(encoding="utf-8"))

    s_keys = set(_key_paths(strings))
    e_keys = set(_key_paths(en))

    missing_in_en = s_keys - e_keys
    extra_in_en = e_keys - s_keys

    if not (missing_in_en or extra_in_en):
        return 0

    print(
        "strings.json and translations/en.json have diverged. "
        "Sync them so HA picks up every translation key at runtime."
    )
    if missing_in_en:
        print("\nKeys in strings.json missing from translations/en.json:")
        for path in sorted(missing_in_en):
            print(f"  {'.'.join(path)}")
    if extra_in_en:
        print("\nKeys in translations/en.json missing from strings.json:")
        for path in sorted(extra_in_en):
            print(f"  {'.'.join(path)}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
