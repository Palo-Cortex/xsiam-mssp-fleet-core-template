"""ring_gate.py — the "local" sentinel must not enter a gated ring.

Soak on the sentinel string is meaningless (the Packs/ source keeps changing
under it), so a `local` pin is only legal in head rings (source: None). These
tests drive evaluate() directly with pins_at/pins_now monkeypatched — no git
history needed.
"""
from datetime import datetime, timezone

import pytest

import ring_gate

NOW = datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone.utc)

POL = {"rings": {
    "dev": {"source": None},
    "qa": {"source": "dev", "min_soak_days": 0},
}}


def _pin_state(monkeypatch, old, now_by_ring):
    monkeypatch.setattr(ring_gate, "pins_at", lambda ref, ring: dict(old))
    monkeypatch.setattr(ring_gate, "pins_now",
                        lambda ring: dict(now_by_ring.get(ring, {})))


def test_local_pin_entering_gated_ring_is_a_violation(monkeypatch):
    """qa gaining `ExamplePack: local` must be rejected even though dev also
    carries `local` (no-skip alone would have waved it through)."""
    _pin_state(monkeypatch, old={}, now_by_ring={
        "qa": {"ExamplePack": "local"},
        "dev": {"ExamplePack": "local"},
    })

    violations, _notes = ring_gate.evaluate("qa", POL, "MERGE_BASE", NOW)

    assert len(violations) == 1
    assert "local" in violations[0]
    assert "ExamplePack" in violations[0]


def test_local_pin_in_head_ring_passes(monkeypatch):
    """dev (source: None) authors pins freely — `local` included."""
    _pin_state(monkeypatch, old={}, now_by_ring={"dev": {"ExamplePack": "local"}})

    violations, notes = ring_gate.evaluate("dev", POL, "MERGE_BASE", NOW)

    assert violations == []
    assert any("head ring" in n for n in notes)


def test_version_pin_into_gated_ring_still_gated_normally(monkeypatch):
    """The local-sentinel check must not affect normal version promotion: a
    version present in the source ring (soak 0) still passes."""
    _pin_state(monkeypatch, old={}, now_by_ring={
        "qa": {"soc-pack": "1.2.3"},
        "dev": {"soc-pack": "1.2.3"},
    })
    monkeypatch.setattr(ring_gate, "soak_days", lambda *a, **k: 1.0)

    violations, _notes = ring_gate.evaluate("qa", POL, "MERGE_BASE", NOW)

    assert violations == []
