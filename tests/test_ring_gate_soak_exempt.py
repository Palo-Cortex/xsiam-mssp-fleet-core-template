"""ring_gate.py — MSSP-authored packs are exempt from soak out of the head ring.

A pack whose source lives in Packs/ (tenant-first content the dev ring never
installs) promotes dev -> qa without waiting out the soak window. The exemption
is head-ring-only: qa -> prod soak applies to every pack alike, and no-skip and
the local-sentinel ban always apply. As in test_ring_gate_local.py, evaluate()
is driven directly with pins_at/pins_now monkeypatched — no git history needed.
"""
from datetime import datetime, timezone

import ring_gate

NOW = datetime(2026, 8, 10, 12, 0, 0, tzinfo=timezone.utc)

POL = {"rings": {
    "dev": {"source": None},
    "qa": {"source": "dev", "min_soak_days": 2},
    "prod": {"source": "qa", "min_soak_days": 5},
}}


def _pin_state(monkeypatch, old, now_by_ring):
    monkeypatch.setattr(ring_gate, "pins_at", lambda ref, ring: dict(old))
    monkeypatch.setattr(ring_gate, "pins_now",
                        lambda ring: dict(now_by_ring.get(ring, {})))


def test_mssp_authored_pack_skips_soak(monkeypatch):
    """ExamplePack enters qa with no committed soak while the policy demands
    2 days — MSSP-authored ids are exempt."""
    _pin_state(monkeypatch, old={}, now_by_ring={
        "qa": {"ExamplePack": "1.0.0"},
        "dev": {"ExamplePack": "1.0.0"},
    })
    monkeypatch.setattr(ring_gate, "mssp_authored_ids", lambda ref: {"ExamplePack"})
    monkeypatch.setattr(ring_gate, "soak_days", lambda *a, **k: None)  # never soaked

    violations, notes = ring_gate.evaluate("qa", POL, "MERGE_BASE", NOW)

    assert violations == []
    assert any("soak exempt" in n for n in notes)


def test_third_party_pack_still_soaks(monkeypatch):
    """The exemption must not leak: an id NOT in Packs/ keeps the soak gate."""
    _pin_state(monkeypatch, old={}, now_by_ring={
        "qa": {"VirusTotal": "2.7.22"},
        "dev": {"VirusTotal": "2.7.22"},
    })
    monkeypatch.setattr(ring_gate, "mssp_authored_ids", lambda ref: {"ExamplePack"})
    monkeypatch.setattr(ring_gate, "soak_days", lambda *a, **k: 0.5)

    violations, _notes = ring_gate.evaluate("qa", POL, "MERGE_BASE", NOW)

    assert len(violations) == 1
    assert "soaked 0.5d" in violations[0]


def test_mssp_authored_pack_still_soaks_into_prod(monkeypatch):
    """The exemption is head-ring-only: qa -> prod soak (qa actually runs the
    pack) applies to MSSP-authored packs like any other."""
    _pin_state(monkeypatch, old={}, now_by_ring={
        "prod": {"ExamplePack": "1.0.0"},
        "qa": {"ExamplePack": "1.0.0"},
    })
    monkeypatch.setattr(ring_gate, "mssp_authored_ids", lambda ref: {"ExamplePack"})
    monkeypatch.setattr(ring_gate, "soak_days", lambda *a, **k: 1.0)

    violations, _notes = ring_gate.evaluate("prod", POL, "MERGE_BASE", NOW)

    assert len(violations) == 1
    assert "soaked 1.0d" in violations[0]


def test_exempt_pack_still_subject_to_no_skip(monkeypatch):
    """Soak exemption is not a promotion bypass: the version must still be
    present in the source ring."""
    _pin_state(monkeypatch, old={}, now_by_ring={
        "qa": {"ExamplePack": "1.1.0"},
        "dev": {"ExamplePack": "1.0.0"},
    })
    monkeypatch.setattr(ring_gate, "mssp_authored_ids", lambda ref: {"ExamplePack"})

    violations, _notes = ring_gate.evaluate("qa", POL, "MERGE_BASE", NOW)

    assert len(violations) == 1
    assert "not present in source ring" in violations[0]


def test_ids_read_from_base_ref_not_working_tree(monkeypatch):
    """mssp_authored_ids() reads Packs/ metadata at the given ref (same rule as
    policies_at): a PR adding metadata for a vendor pack in its own diff must
    not self-exempt. The fake git below only serves MERGE_BASE."""
    def fake_sh(*args):
        if args[0] == "ls-tree" and "MERGE_BASE" in args:
            return "Packs/ExamplePack/pack_metadata.json\n"
        if args[0] == "show" and args[1].startswith("MERGE_BASE:"):
            return '{"id": "ExamplePack"}'
        return ""
    monkeypatch.setattr(ring_gate, "sh", fake_sh)

    assert ring_gate.mssp_authored_ids("MERGE_BASE") == {"ExamplePack"}
    assert ring_gate.mssp_authored_ids("PR_HEAD") == set()
