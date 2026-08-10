"""Convergence applies the Config Overlay to the staged pack.

Contract under test:

  * The merge is EPHEMERAL and happens in the staged copy of a fetched release
    zip: `sdk_upload()` extracts the zip into a temp Packs/, deep-merges each
    Config Overlay patch onto the matching List's `_data.json`, and uploads the
    staged pack. The pinned artifact on disk is never modified.
  * The List -> pack mapping is DISCOVERED, not declared: whichever staged pack
    carries `Lists/**/<ListID>_data.json` consumes that List id. `sdk_upload()`
    returns the set of ids it consumed.
  * A List id no deployed pack carried is a failure, joining the same `failures`
    list as a failed upsert — so an unconsumed id yields Incomplete Converge
    semantics unchanged: REMOVE held, `converge INCOMPLETE`, non-zero exit,
    every other pack still attempted.
  * A pack whose upload failed never counts as having consumed its Lists.
  * Dry-run prints the Config Overlay section and touches nothing; with no
    Config Overlay the output is exactly what it was before this feature.

The zip-level tests build real zips in a tmp dir and mock only the demisto-sdk
subprocess (snapshotting the staged tree from inside the fake `run`). The
converge-level tests use the mocked-seam style of test_converge_best_effort.py.
"""
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

import deploy_tenant

LIST_ID = "SOCFrameworkActions_V3"
OTHER_LIST_ID = "SOCExecutionList_V3"

PACK_DIR = "SocOptimizationUnified"
PACK_META = {"id": "soc-optimization-unified", "currentVersion": "3.9.4"}

# The canonical framework layout for a List inside a pack: a metadata file the
# fleet never touches, and the data file the Config Overlay patches.
LIST_META = {
    "id": LIST_ID,
    "name": LIST_ID,
    "type": "json",
    "fromVersion": "8.9.0",
}
PINNED_DATA = {
    "isolate_host": {"shadow_mode": True, "timeout": 300},
    "block_indicator": {"shadow_mode": True},
}


def _write_pack(root, pack_dir=PACK_DIR, lists=None, meta=None):
    """Lay out one pack directory: pack_metadata.json + optional Lists/."""
    pack = root / pack_dir
    (pack).mkdir(parents=True)
    (pack / "pack_metadata.json").write_text(json.dumps(meta or PACK_META, indent=4))
    for list_id, data in (lists or {}).items():
        d = pack / "Lists" / list_id
        d.mkdir(parents=True)
        (d / f"{list_id}.json").write_text(
            json.dumps({**LIST_META, "id": list_id, "name": list_id}, indent=4))
        (d / f"{list_id}_data.json").write_text(json.dumps(data, indent=4))
    return pack


def _zip_pack(tmp_path, name="pack.zip", **kw):
    """Build a release zip whose single top-level dir is the pack."""
    src = tmp_path / f"src-{name}"
    src.mkdir()
    pack = _write_pack(src, **kw)
    zip_path = tmp_path / name
    with zipfile.ZipFile(zip_path, "w") as zf:
        for p in sorted(pack.rglob("*")):
            if p.is_file():
                zf.write(p, p.relative_to(src))
    return zip_path


def _snapshotting_run(capture):
    """Stand-in for subprocess.run that records the staged tree it would upload."""
    def run(cmd, cwd=None, env=None, check=None):
        root = Path(cwd)
        capture["cmd"] = cmd
        capture["files"] = {
            str(p.relative_to(root)): p.read_text()
            for p in sorted(root.rglob("*")) if p.is_file()
        }
        return subprocess.CompletedProcess(cmd, 0)
    return run


CREDS = {
    "DEMISTO_BASE_URL": "https://api-t.xdr.us.paloaltonetworks.com",
    "DEMISTO_API_KEY": "k",
    "XSIAM_AUTH_ID": "7",
}


def test_staged_list_data_is_deep_merged_before_upload(tmp_path, monkeypatch):
    """The uploaded pack carries `pinned ⊕ Config Overlay`: patched keys are
    replaced, unpatched keys survive, and the List metadata file is untouched."""
    zip_path = _zip_pack(tmp_path, lists={LIST_ID: PINNED_DATA})
    cap = {}
    monkeypatch.setattr(deploy_tenant.subprocess, "run", _snapshotting_run(cap))

    consumed = deploy_tenant.sdk_upload(
        zip_path, CREDS,
        config_overlay={LIST_ID: {"isolate_host": {"shadow_mode": False}}},
    )

    assert consumed == {LIST_ID}
    staged = json.loads(cap["files"][f"Packs/{PACK_DIR}/Lists/{LIST_ID}/{LIST_ID}_data.json"])
    assert staged == {
        "isolate_host": {"shadow_mode": False, "timeout": 300},  # merged, not replaced
        "block_indicator": {"shadow_mode": True},                # untouched action
    }
    meta = json.loads(cap["files"][f"Packs/{PACK_DIR}/Lists/{LIST_ID}/{LIST_ID}.json"])
    assert meta["id"] == LIST_ID and meta["fromVersion"] == LIST_META["fromVersion"]
    assert cap["cmd"][:2] == ["demisto-sdk", "upload"]


def test_pack_without_the_list_uploads_unmodified(tmp_path, monkeypatch):
    """A staged pack that carries no patched List consumes nothing and is
    uploaded exactly as the pinned artifact shipped it."""
    zip_path = _zip_pack(tmp_path, lists={OTHER_LIST_ID: {"a": 1}})
    cap = {}
    monkeypatch.setattr(deploy_tenant.subprocess, "run", _snapshotting_run(cap))

    consumed = deploy_tenant.sdk_upload(
        zip_path, CREDS,
        config_overlay={LIST_ID: {"isolate_host": {"shadow_mode": False}}},
    )

    assert consumed == set()
    staged = json.loads(
        cap["files"][f"Packs/{PACK_DIR}/Lists/{OTHER_LIST_ID}/{OTHER_LIST_ID}_data.json"])
    assert staged == {"a": 1}


def test_single_file_list_variant_is_merged_and_consumed(tmp_path, monkeypatch):
    """The `Lists/<id>.json`-with-a-`data`-mapping variant is patched inside its
    `data` key and consumed; a metadata-only `<id>.json` (canonical layout with
    no data key) is never falsely consumed nor modified."""
    src = tmp_path / "src-variant"
    src.mkdir()
    pack = _write_pack(src)
    flat = pack / "Lists"
    flat.mkdir()
    (flat / f"{LIST_ID}.json").write_text(
        json.dumps({**LIST_META, "data": PINNED_DATA}, indent=4))
    (flat / f"{OTHER_LIST_ID}.json").write_text(          # metadata only, no data
        json.dumps({**LIST_META, "id": OTHER_LIST_ID}, indent=4))
    zip_path = tmp_path / "variant.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for p in sorted(pack.rglob("*")):
            if p.is_file():
                zf.write(p, p.relative_to(src))
    cap = {}
    monkeypatch.setattr(deploy_tenant.subprocess, "run", _snapshotting_run(cap))

    consumed = deploy_tenant.sdk_upload(
        zip_path, CREDS,
        config_overlay={
            LIST_ID: {"isolate_host": {"shadow_mode": False}},
            OTHER_LIST_ID: {"branch": "x"},
        },
    )

    assert consumed == {LIST_ID}
    staged = json.loads(cap["files"][f"Packs/{PACK_DIR}/Lists/{LIST_ID}.json"])
    assert staged["data"]["isolate_host"] == {"shadow_mode": False, "timeout": 300}
    assert staged["fromVersion"] == LIST_META["fromVersion"]
    untouched = json.loads(cap["files"][f"Packs/{PACK_DIR}/Lists/{OTHER_LIST_ID}.json"])
    assert "data" not in untouched


def test_malformed_list_data_fails_before_upload(tmp_path, monkeypatch):
    """A `_data.json` that isn't a mapping raises inside the merge — before the
    upload subprocess runs — so converge()'s per-pack failure unit contains it
    as one FAILED pack (Incomplete Converge), never a half-patched upload."""
    zip_path = _zip_pack(tmp_path, lists={LIST_ID: PINNED_DATA})
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(zip_path) as zin, zipfile.ZipFile(bad, "w") as zout:
        for item in zin.infolist():
            content = zin.read(item.filename)
            if item.filename.endswith(f"{LIST_ID}_data.json"):
                content = json.dumps(["not", "a", "mapping"]).encode()
            zout.writestr(item, content)
    cap = {}
    monkeypatch.setattr(deploy_tenant.subprocess, "run", _snapshotting_run(cap))

    with pytest.raises(Exception):
        deploy_tenant.sdk_upload(
            bad, CREDS,
            config_overlay={LIST_ID: {"isolate_host": {"shadow_mode": False}}},
        )

    assert "cmd" not in cap, "the upload subprocess must not run after a failed merge"


def test_pinned_zip_on_disk_is_never_modified(tmp_path, monkeypatch):
    """The ephemeral merge leaves the fetched artifact byte-for-byte
    pristine — with a Config Overlay applied and without one."""
    zip_path = _zip_pack(tmp_path, lists={LIST_ID: PINNED_DATA})
    before = zip_path.read_bytes()
    monkeypatch.setattr(deploy_tenant.subprocess, "run", _snapshotting_run({}))

    deploy_tenant.sdk_upload(zip_path, CREDS,
                             config_overlay={LIST_ID: {"isolate_host": {"shadow_mode": False}}})
    assert zip_path.read_bytes() == before

    assert deploy_tenant.sdk_upload(zip_path, CREDS) == set()
    assert zip_path.read_bytes() == before


# --- converge-level: consumed-id tracking and Incomplete Converge -------------
FIRST_PACK = "soc-first-pack"
LATER_PACK = "soc-later-pack"


def _mock_live_seams(monkeypatch, tenant, carries, installed=None, fails=()):
    """Patch every seam converge() touches under --apply.

    `carries` maps pack id -> the List ids that pack's staged copy carries, so a
    fake sdk_upload can return the consumed set the real one would. `fails` is
    the pack ids whose upload blows up. Returns the recorder.
    """
    creds = {
        "DEMISTO_BASE_URL": f"https://api-{tenant}.xdr.us.paloaltonetworks.com",
        "DEMISTO_API_KEY": "k",
        "XSIAM_AUTH_ID": "7",
    }
    monkeypatch.setattr(deploy_tenant, "load_creds", lambda: creds)
    monkeypatch.setattr(deploy_tenant, "assert_host_matches", lambda r, c: None)
    monkeypatch.setattr(deploy_tenant, "list_installed", lambda c: dict(installed or {}))
    monkeypatch.setattr(deploy_tenant, "load_catalog", lambda *a, **k: {"packs": []})
    monkeypatch.setattr(
        deploy_tenant, "resolve_artifact",
        lambda pid, v, url, commit: {
            "zip_url": f"https://example.invalid/{pid}-{v}.zip", "exact": True},
    )
    monkeypatch.setattr(deploy_tenant.os, "unlink", lambda p: None)
    monkeypatch.setattr(deploy_tenant, "download", lambda url: f"/tmp/{url.rsplit('/', 1)[-1]}")

    rec = {"uploaded": [], "overlays": [], "deleted": []}

    def _upload(zip_path, c, config_overlay=None):
        pid = next((p for p in carries if p in zip_path), None)
        rec["uploaded"].append(zip_path)
        rec["overlays"].append(config_overlay)
        if pid in fails:
            raise RuntimeError("HTTP 500 from tenant")
        # the real sdk_upload consumes only the ids this pack actually carries
        return {lid for lid in (config_overlay or {}) if lid in carries.get(pid, ())}

    monkeypatch.setattr(deploy_tenant, "sdk_upload", _upload)
    monkeypatch.setattr(deploy_tenant, "delete_packs",
                        lambda c, ids: rec["deleted"].append(list(ids)))
    return rec


def test_config_overlay_is_tracked_as_consumed_across_packs(temp_fleet, monkeypatch, capsys):
    """Every List id in the Config Overlay is offered to every staged pack, and
    ids carried by DIFFERENT packs are all consumed — a run that covers them all
    converges cleanly."""
    temp_fleet.pins("dev", {FIRST_PACK: "1.0.0", LATER_PACK: "2.0.0"})
    tenant = temp_fleet.tenant("t_co_ok", ring="dev", extras=[FIRST_PACK, LATER_PACK])
    temp_fleet.config_overlay(tenant, {
        LIST_ID: {"isolate_host": {"shadow_mode": False}},
        OTHER_LIST_ID: {"tier1": {"enabled": True}},
    })
    rec = _mock_live_seams(monkeypatch, tenant,
                           carries={FIRST_PACK: [LIST_ID], LATER_PACK: [OTHER_LIST_ID]})

    deploy_tenant.converge(tenant, apply=True, offline=False, installed_fixture=None)

    assert len(rec["uploaded"]) == 2
    assert all(set(o) == {LIST_ID, OTHER_LIST_ID} for o in rec["overlays"]), (
        f"each staged pack must be offered the whole Config Overlay: {rec['overlays']}")
    out = capsys.readouterr().out
    assert f"# converged {tenant}: 2 upserted" in out
    assert "INCOMPLETE" not in out


def test_unconsumed_list_id_is_an_incomplete_converge(temp_fleet, monkeypatch, capsys):
    """A List id no deployed pack carries is a failure: it joins the failure
    summary, REMOVE is held, the run exits non-zero — and the packs that had
    nothing to do with it were still upserted."""
    temp_fleet.pins("dev", {FIRST_PACK: "1.0.0", LATER_PACK: "2.0.0"})
    tenant = temp_fleet.tenant("t_co_unconsumed", ring="dev",
                               extras=[FIRST_PACK, LATER_PACK])
    temp_fleet.config_overlay(tenant, {OTHER_LIST_ID: {"tier1": {"enabled": True}}})
    rec = _mock_live_seams(monkeypatch, tenant,
                           carries={FIRST_PACK: [LIST_ID], LATER_PACK: []},
                           installed={"ExamplePack": "1.0.0"})
    monkeypatch.setenv("FLEET_ALLOW_DELETE", "true")
    monkeypatch.setattr(sys, "argv", ["deploy_tenant.py", tenant, "--apply"])

    with pytest.raises(SystemExit) as exc:
        deploy_tenant.main()
    assert exc.value.code not in (0, None)

    assert len(rec["uploaded"]) == 2, "both packs must still be attempted"
    assert rec["deleted"] == [], "REMOVE must be held when a List id went unconsumed"

    out = capsys.readouterr().out
    assert "converge INCOMPLETE" in out
    assert "REMOVE held" in out
    assert "hold delete ExamplePack" in out
    assert f"config-overlay:{OTHER_LIST_ID}" in out
    assert f"no deployed pack carries List '{OTHER_LIST_ID}'" in out
    assert "# converged" not in out


def test_failed_upload_does_not_consume_its_lists(temp_fleet, monkeypatch, capsys):
    """A pack that failed to upload never landed its merged List, so its List id
    counts as unconsumed and is reported alongside the pack failure."""
    temp_fleet.pins("dev", {FIRST_PACK: "1.0.0"})
    tenant = temp_fleet.tenant("t_co_failed_pack", ring="dev", extras=[FIRST_PACK])
    temp_fleet.config_overlay(tenant, {LIST_ID: {"isolate_host": {"shadow_mode": False}}})
    _mock_live_seams(monkeypatch, tenant, carries={FIRST_PACK: [LIST_ID]},
                     fails=(FIRST_PACK,))

    with pytest.raises(deploy_tenant.DeployError):
        deploy_tenant.converge(tenant, apply=True, offline=False, installed_fixture=None)

    out = capsys.readouterr().out
    assert f"FAILED {FIRST_PACK}" in out
    assert f"config-overlay:{LIST_ID}" in out, (
        f"a pack whose upload failed must not count as having consumed its "
        f"Lists:\n{out}")


# --- dry-run -----------------------------------------------------------------
def _forbid_side_effects(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("dry-run must touch nothing")
    for seam in ("download", "sdk_upload", "sdk_upload_source", "delete_packs",
                 "list_installed", "load_creds"):
        monkeypatch.setattr(deploy_tenant, seam, boom)


def test_dry_run_prints_the_config_overlay_plan_and_touches_nothing(
    temp_fleet, monkeypatch, capsys
):
    """The plan names each patched List id and its top-level patched keys, and
    says the merge happens in whichever staged pack carries the List at apply."""
    temp_fleet.pins("dev", {FIRST_PACK: "1.0.0"})
    tenant = temp_fleet.tenant("t_co_dry", ring="dev", extras=[FIRST_PACK])
    temp_fleet.config_overlay("defaults", {LIST_ID: {"isolate_host": {"shadow_mode": True}}})
    temp_fleet.config_overlay(tenant, {LIST_ID: {"block_indicator": {"shadow_mode": False}}})
    _forbid_side_effects(monkeypatch)

    deploy_tenant.converge(tenant, apply=False, offline=True, installed_fixture=None)

    out = capsys.readouterr().out
    assert "CONFIG OVERLAY" in out
    assert LIST_ID in out
    # composed defaults ⊕ tenant — both layers' top-level keys are listed
    assert "block_indicator" in out and "isolate_host" in out
    assert "staged pack" in out
    assert "dry-run — nothing fetched, uploaded, or deleted." in out


def test_dry_run_without_a_config_overlay_is_unchanged(temp_fleet, monkeypatch, capsys):
    """No Config Overlay -> the plan is exactly what it was before this feature."""
    temp_fleet.pins("dev", {FIRST_PACK: "1.0.0"})
    tenant = temp_fleet.tenant("t_co_none", ring="dev", extras=[FIRST_PACK])
    _forbid_side_effects(monkeypatch)

    deploy_tenant.converge(tenant, apply=False, offline=True, installed_fixture=None)

    out = capsys.readouterr().out
    assert "CONFIG OVERLAY" not in out
    assert "overlay" not in out.lower()
    assert "# INSTALL/UPDATE — 1 MSSP-owned pack(s)" in out
