"""Tests for the generic API parser."""

from __future__ import annotations

from custom_components.truenas.apiparser import parse_api

VALS = [
    {"name": "version", "default": "unknown"},
    {"name": "physmem", "default": 0},
]
ENSURE_VALS = [
    {"name": "update_available", "type": "bool", "default": False},
    {"name": "cpu_temperature", "default": 0.0},
]


def test_ensure_vals_without_source() -> None:
    """A failed query must still leave the ensured keys in place."""
    data = parse_api(data={}, source=None, vals=VALS, ensure_vals=ENSURE_VALS)

    assert data["version"] == "unknown"
    assert data["update_available"] is False
    assert data["cpu_temperature"] == 0.0


def test_ensure_vals_without_source_keeps_previous_data() -> None:
    """Values fetched earlier survive a failed query."""
    data = parse_api(
        data={"version": "25.10.0", "update_available": True},
        source=None,
        vals=VALS,
        ensure_vals=ENSURE_VALS,
    )

    assert data["version"] == "25.10.0"
    assert data["update_available"] is True
    assert data["cpu_temperature"] == 0.0


def test_ensure_vals_with_source() -> None:
    """Ensured keys are added for keyed entries as well."""
    data = parse_api(
        data={},
        source=[{"id": "tank", "name": "tank"}],
        key="id",
        vals=[{"name": "name", "default": "unknown"}],
        ensure_vals=[{"name": "usage", "default": 0.0}],
    )

    assert data["tank"]["name"] == "tank"
    assert data["tank"]["usage"] == 0.0


def test_no_source_and_no_vals() -> None:
    """A query returning nothing without value definitions is a no-op."""
    assert parse_api(data={"kept": 1}, source=None) == {"kept": 1}
