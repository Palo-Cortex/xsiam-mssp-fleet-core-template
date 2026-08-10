"""List-content drift: live List content vs `pinned ⊕ Config Overlay`.

Contract under test:

  * The fleet owns the framework config Lists outright, so a tenant's expected
    List content is `pinned ⊕ Config Overlay` — the pinned pack's shipped List
    content deep-merged with the composed (defaults ⊕ tenant) Config Overlay.
    Anything else live on the tenant is a console hand-edit: drift.
  * Offline, the two halves come from fixtures mirroring `--state`:
    `--pinned-lists` (per Ring, because Pins are per Ring) and `--lists-state`
    (per tenant, the stand-in for live List content).
  * Both flags absent -> drift_check behaves exactly as it did before this
    feature: pack rows only, same summary, same JSON shape.
  * List drift is counted and reported separately from pack drift; either kind
    makes the exit code non-zero. Drift never deletes — an unknown live List is
    reported like an UNMANAGED pack.
"""
import json
import sys

import pytest
import yaml

import drift_check

PACK = "soc-optimization-unified"
LIST_ID = "SOCFrameworkActions_V3"
OTHER_LIST_ID = "SOCExecutionList_V3"

SAMPLE_STATE = "fixtures/installed_state.sample.yml"

# The pinned pack's shipped List content — the baseline every expectation
# starts from (one action live, one shadowed, one carrying a nested threshold).
PINNED_CONTENT = {
    "isolate_host": {"shadow_mode": True, "timeout": 300},
    "block_indicator": {"shadow_mode": True},
    "triage": {"thresholds": {"high": 80, "low": 20}},
}


def _run(monkeypatch, capsys, *argv):
    """Run drift_check.main() with argv; return (exit_code, stdout)."""
    monkeypatch.setattr(sys, "argv", ["drift_check.py", *argv])
    with pytest.raises(SystemExit) as exc:
        drift_check.main()
    return exc.value.code, capsys.readouterr().out


def _yaml(tmp_path, name, data):
    p = tmp_path / name
    p.write_text(yaml.safe_dump(data, sort_keys=False))
    return str(p)


# --- no new fixtures: byte-identical behavior --------------------------------
def test_without_list_fixtures_output_and_exit_are_unchanged(monkeypatch, capsys):
    """The existing invocation against the real fleet is untouched: no List row
    anywhere, the pack-only summary line, exit 1 for the two seeded pack drifts."""
    code, out = _run(monkeypatch, capsys, "--state", SAMPLE_STATE)

    assert code == 1
    assert "LIST" not in out
    assert "  DRIFT DETECTED: 2 pack(s) out of sync across the fleet." in out


def test_without_list_fixtures_json_shape_is_unchanged(monkeypatch, capsys):
    """--json carries exactly the keys it carried before this feature."""
    code, out = _run(monkeypatch, capsys, "--state", SAMPLE_STATE, "--json")

    assert code == 1
    data = json.loads(out)
    assert set(data) == {"reports", "total_drift"}
    for report in data["reports"]:
        assert set(report) == {"tenant", "ring", "rows", "drift", "unmanaged"}


# --- in sync ------------------------------------------------------------------
def _tenant_with_list(temp_fleet, tmp_path, name, live, overlay=None, pinned=None):
    """One dev tenant, its pack pinned+installed, plus the two List fixtures.

    Returns the argv tail wiring drift_check to all three fixtures.
    """
    temp_fleet.pins("dev", {PACK: "1.0.0"})
    tenant = temp_fleet.tenant(name, ring="dev", extras=[PACK])
    if overlay is not None:
        temp_fleet.config_overlay(tenant, overlay)
    return [
        "--state", _yaml(tmp_path, f"{name}-state.yml", {tenant: {PACK: "1.0.0"}}),
        "--lists-state", _yaml(tmp_path, f"{name}-live.yml", {tenant: live}),
        "--pinned-lists", _yaml(tmp_path, f"{name}-pinned.yml",
                                {"dev": pinned if pinned is not None
                                 else {LIST_ID: PINNED_CONTENT}}),
    ]


def test_list_matching_the_pinned_content_is_in_sync(temp_fleet, tmp_path,
                                                     monkeypatch, capsys):
    """No Config Overlay: expected is the pinned content alone, and a tenant
    carrying exactly that reports no List drift."""
    argv = _tenant_with_list(temp_fleet, tmp_path, "t_list_pinned_ok",
                             live={LIST_ID: PINNED_CONTENT})

    code, out = _run(monkeypatch, capsys, *argv)

    assert code == 0, out
    assert f"✓ LIST {LIST_ID}" in out
    assert "Fleet in sync — no drift." in out


def test_list_matching_pinned_merged_with_the_config_overlay_is_in_sync(
    temp_fleet, tmp_path, monkeypatch, capsys
):
    """With a Config Overlay, expected is `pinned ⊕ Config Overlay`: live content
    carrying the overlaid value (and the untouched pinned keys) is in sync."""
    argv = _tenant_with_list(
        temp_fleet, tmp_path, "t_list_overlay_ok",
        overlay={LIST_ID: {"isolate_host": {"shadow_mode": False}}},
        live={LIST_ID: {
            "isolate_host": {"shadow_mode": False, "timeout": 300},
            "block_indicator": {"shadow_mode": True},
            "triage": {"thresholds": {"high": 80, "low": 20}},
        }},
    )

    code, out = _run(monkeypatch, capsys, *argv)

    assert code == 0, out
    assert f"✓ LIST {LIST_ID}" in out


# --- drift --------------------------------------------------------------------
def test_hand_edited_nested_key_is_reported_with_a_dotted_path(
    temp_fleet, tmp_path, monkeypatch, capsys
):
    """A console hand-edit of one nested value — the shadow flip the fleet
    believes is still shadowed — names the dotted path, both values, and the
    List, counts as one List out of sync, and exits non-zero."""
    live = json.loads(json.dumps(PINNED_CONTENT))
    live["isolate_host"]["shadow_mode"] = False        # <- the hand-edit
    argv = _tenant_with_list(temp_fleet, tmp_path, "t_list_drift", live={LIST_ID: live})

    code, out = _run(monkeypatch, capsys, *argv)

    assert code == 1
    assert f"✗ LIST {LIST_ID}" in out
    assert "1 key(s) drifted" in out
    assert "isolate_host.shadow_mode: live false ≠ expected true" in out
    assert "DRIFT DETECTED: 0 pack(s), 1 List(s) out of sync" in out


def test_a_key_the_config_overlay_expects_but_the_tenant_lacks_is_drift(
    temp_fleet, tmp_path, monkeypatch, capsys
):
    """Missing and unexpected keys are drift too: the overlay's new action never
    landed, and an action nobody pinned or overlaid is live."""
    live = json.loads(json.dumps(PINNED_CONTENT))
    live["rogue_action"] = {"shadow_mode": False}      # <- never pinned, never overlaid
    argv = _tenant_with_list(
        temp_fleet, tmp_path, "t_list_missing_key",
        overlay={LIST_ID: {"quarantine_user": {"shadow_mode": True}}},
        live={LIST_ID: live},
    )

    code, out = _run(monkeypatch, capsys, *argv)

    assert code == 1
    assert "2 key(s) drifted" in out
    assert "quarantine_user: missing live" in out
    assert "rogue_action: unexpected live" in out


def test_list_drift_is_counted_separately_from_pack_drift(temp_fleet, tmp_path,
                                                          monkeypatch, capsys):
    """The summary keeps the two kinds apart so the drift issue can distinguish
    a pack-version drift from a List-content drift."""
    temp_fleet.pins("dev", {PACK: "1.0.0"})
    tenant = temp_fleet.tenant("t_both_kinds", ring="dev", extras=[PACK])
    live = json.loads(json.dumps(PINNED_CONTENT))
    live["block_indicator"]["shadow_mode"] = False
    argv = [
        "--state", _yaml(tmp_path, "both-state.yml", {tenant: {PACK: "0.9.0"}}),
        "--lists-state", _yaml(tmp_path, "both-live.yml", {tenant: {LIST_ID: live}}),
        "--pinned-lists", _yaml(tmp_path, "both-pinned.yml",
                                {"dev": {LIST_ID: PINNED_CONTENT}}),
        "--json",
    ]

    code, out = _run(monkeypatch, capsys, *argv)

    assert code == 1
    data = json.loads(out)
    assert data["total_drift"] == 1 and data["total_list_drift"] == 1
    report = data["reports"][0]
    assert [d["pack"] for d in report["drift"]] == [PACK]
    assert [d["list"] for d in report["list_drift"]] == [LIST_ID]
    assert report["list_drift"][0]["keys"] == [
        {"key": "block_indicator.shadow_mode", "kind": "changed",
         "expected": True, "live": False}]


# --- present/absent Lists -----------------------------------------------------
def test_a_fleet_owned_list_absent_from_the_tenant_is_list_missing(
    temp_fleet, tmp_path, monkeypatch, capsys
):
    """A List the ring's pin ships (or the Config Overlay patches) that the
    tenant does not carry at all is LIST MISSING — out of sync, not silence."""
    argv = _tenant_with_list(
        temp_fleet, tmp_path, "t_list_absent",
        live={LIST_ID: PINNED_CONTENT},
        pinned={LIST_ID: PINNED_CONTENT, OTHER_LIST_ID: {"branch": "default"}},
    )

    code, out = _run(monkeypatch, capsys, *argv)

    assert code == 1
    assert f"? LIST {OTHER_LIST_ID}" in out
    assert "not present on tenant" in out
    assert "DRIFT DETECTED: 0 pack(s), 1 List(s) out of sync" in out


def test_a_list_the_fleet_does_not_own_is_report_only(temp_fleet, tmp_path,
                                                      monkeypatch, capsys):
    """A live List in neither the pinned baseline nor the Config Overlay is
    reported like an UNMANAGED pack — never drift, never deleted — and a
    customer-prefixed List is ignored outright."""
    argv = _tenant_with_list(
        temp_fleet, tmp_path, "t_list_unmanaged",
        live={LIST_ID: PINNED_CONTENT,
              "SomeConsoleList": {"a": 1},
              "acme-runbook-list": {"b": 2}},
    )

    code, out = _run(monkeypatch, capsys, *argv)

    assert code == 0, out
    assert "~ LIST SomeConsoleList" in out
    assert "not deleted here" in out
    assert "acme-runbook-list" not in out


def test_a_tenant_absent_from_the_lists_fixture_reports_no_list_rows(
    temp_fleet, tmp_path, monkeypatch, capsys
):
    """The fixture stands in for a live read, so a tenant it does not cover is
    simply unread — not a fleet of LIST MISSING rows."""
    temp_fleet.pins("dev", {PACK: "1.0.0"})
    covered = temp_fleet.tenant("t_covered", ring="dev", extras=[PACK])
    temp_fleet.tenant("t_unread", ring="dev", extras=[PACK])
    argv = [
        "--state", _yaml(tmp_path, "u-state.yml",
                         {covered: {PACK: "1.0.0"}, "t_unread": {PACK: "1.0.0"}}),
        "--lists-state", _yaml(tmp_path, "u-live.yml",
                               {covered: {LIST_ID: PINNED_CONTENT}}),
        "--pinned-lists", _yaml(tmp_path, "u-pinned.yml",
                                {"dev": {LIST_ID: PINNED_CONTENT}}),
    ]

    code, out = _run(monkeypatch, capsys, *argv)

    assert code == 0, out
    assert out.count(f"LIST {LIST_ID}") == 1
    assert "t_unread" in out and "LIST MISSING" not in out


# --- flag pairing -------------------------------------------------------------
def test_covered_tenant_whose_ring_lacks_a_baseline_is_an_error(
    temp_fleet, tmp_path, monkeypatch, capsys
):
    """A ring absent from --pinned-lists must hard-error, not fall back to an
    empty baseline — the fallback would reclassify every fleet-owned List as
    unmanaged, silently hiding exactly the drift this check exists to surface
    (a real hand-edit would read `~ LIST ... unmanaged` instead of `✗ LIST`)."""
    temp_fleet.pins("dev", {PACK: "1.0.0"})
    tenant = temp_fleet.tenant("t_list_no_ring", ring="dev", extras=[PACK])
    argv = [
        "--state", _yaml(tmp_path, "nr-state.yml", {tenant: {PACK: "1.0.0"}}),
        "--lists-state", _yaml(tmp_path, "nr-live.yml",
                               {tenant: {LIST_ID: PINNED_CONTENT}}),
        "--pinned-lists", _yaml(tmp_path, "nr-pinned.yml",
                                {"qa": {LIST_ID: PINNED_CONTENT}}),  # dev absent
    ]

    code, out = _run(monkeypatch, capsys, *argv)

    assert code not in (0, 1), "must ERROR out, not report (or hide) drift"
    err = str(code)
    assert tenant in err and "dev" in err and "--pinned-lists" in err


def test_type_conflict_at_the_list_root_is_drift_with_a_root_marker(
    temp_fleet, tmp_path, monkeypatch, capsys
):
    """A live List whose whole content is the wrong shape (a scalar where the
    baseline ships a mapping) is one drifted entry labeled at the root — never
    a crash, never an empty key path."""
    argv = _tenant_with_list(temp_fleet, tmp_path, "t_list_root_type",
                             live={LIST_ID: "oops a string"})

    code, out = _run(monkeypatch, capsys, *argv)

    assert code == 1, out
    assert f"✗ LIST {LIST_ID}" in out
    assert "(root)" in out


def test_either_list_fixture_alone_is_an_error(monkeypatch, capsys, tmp_path):
    """The two fixtures are the two halves of one comparison; one alone cannot
    be checked, so the run errors instead of silently checking nothing."""
    half = _yaml(tmp_path, "half.yml", {})
    for flag in ("--lists-state", "--pinned-lists"):
        monkeypatch.setattr(sys, "argv",
                            ["drift_check.py", "--state", SAMPLE_STATE, flag, half])
        with pytest.raises(SystemExit) as exc:
            drift_check.main()
        err = capsys.readouterr().err
        assert exc.value.code != 0
        assert "--lists-state" in err and "--pinned-lists" in err
