"""Tests for the integration manifest.

These mirror the parts of Home Assistant's hassfest validation that are easy
to get wrong when adding a key, so a mistake shows up locally rather than in
CI.
"""

from __future__ import annotations

import json
from pathlib import Path

MANIFEST = (
    Path(__file__).parent.parent / "custom_components" / "truenas" / "manifest.json"
)


def _manifest() -> dict:
    return json.loads(MANIFEST.read_text())


def test_keys_are_sorted_the_way_hassfest_wants() -> None:
    """hassfest requires domain, name, then alphabetical order."""
    keys = list(_manifest())

    assert keys[:2] == ["domain", "name"]
    assert keys[2:] == sorted(keys[2:])


def test_required_keys_are_present() -> None:
    """The keys hassfest requires for a config-flow integration."""
    manifest = _manifest()

    for key in ("domain", "name", "codeowners", "documentation", "iot_class"):
        assert manifest.get(key), f"{key} missing from the manifest"

    assert manifest["config_flow"] is True
    assert manifest["iot_class"] == "local_polling"
    assert manifest["integration_type"] == "hub"
