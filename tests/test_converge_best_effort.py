"""deploy_tenant.py — best-effort per-pack Convergence.

Contract under test:

  * The failure unit is ONE pack's fetch + upload. Any exception raised while
    fetching or uploading a pack marks that pack failed and the loop continues
    to the remaining packs — upstream (download() -> sdk_upload()) and
    local-source (sdk_upload_source()) paths alike.
  * Orphan removal runs only after a fully successful upsert pass. If any pack
    failed, delete_packs() is NOT called; the would-be deletions print as held.
  * A run with any failure is an Incomplete Converge: it prints the
    `converge INCOMPLETE` summary plus a per-pack failure line and exits
    non-zero (DeployError). Never "converged", never "partial".
  * An all-success run is unchanged: deletes run (under FLEET_ALLOW_DELETE),
    the `# converged` summary prints, and nothing is raised.

Everything is mocked: no network (urllib), no subprocess (demisto-sdk), no live
tenant I/O. The local pack is the REAL Packs/ExamplePack fixture; the fleet is a
temp tree.
"""
import sys

import pytest

import deploy_tenant

from helpers_local_source import LOCAL_PACK_ID, LOCAL_SENTINEL

# Two upstream pack ids so the install loop has a "first" and a "later" pack.
FIRST_PACK = "soc-first-pack"
LATER_PACK = "soc-later-pack"


def _mock_live_seams(monkeypatch, tenant, installed=None):
    """Patch every seam converge() touches under --apply. Returns the recorder."""
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
            "zip_url": f"https://example.invalid/{pid}-{v}.zip", "exact": True
        },
    )
    monkeypatch.setattr(deploy_tenant.os, "unlink", lambda p: None)

    rec = {"downloaded": [], "uploaded": [], "source_uploaded": [], "deleted": []}
    monkeypatch.setattr(
        deploy_tenant, "download",
        lambda url: (rec["downloaded"].append(url) or f"/tmp/fake-{len(rec['downloaded'])}.zip"),
    )
    monkeypatch.setattr(
        deploy_tenant, "sdk_upload",
        lambda zip_path, c: rec["uploaded"].append(zip_path),
    )
    monkeypatch.setattr(
        deploy_tenant, "sdk_upload_source",
        lambda pack, c: rec["source_uploaded"].append(pack),
    )
    monkeypatch.setattr(
        deploy_tenant, "delete_packs",
        lambda c, ids: rec["deleted"].append(list(ids)),
    )
    return rec


def test_failing_upload_does_not_block_later_packs(temp_fleet, monkeypatch):
    """A pack whose sdk_upload() blows up must not stop the next pack from
    being attempted — the failure unit is one pack, not the tenant."""
    temp_fleet.pins("dev", {FIRST_PACK: "1.0.0", LATER_PACK: "2.0.0"})
    tenant = temp_fleet.tenant(
        "t_be_upload", ring="dev", extras=[FIRST_PACK, LATER_PACK]
    )
    rec = _mock_live_seams(monkeypatch, tenant)

    def _flaky_upload(zip_path, creds):
        if FIRST_PACK in zip_path:
            raise RuntimeError("HTTP 500 from tenant")
        rec["uploaded"].append(zip_path)

    monkeypatch.setattr(deploy_tenant, "download", lambda url: f"/tmp/{url.rsplit('/', 1)[-1]}")
    monkeypatch.setattr(deploy_tenant, "sdk_upload", _flaky_upload)

    with pytest.raises(deploy_tenant.DeployError):
        deploy_tenant.converge(tenant, apply=True, offline=False,
                               installed_fixture=None)

    assert any(LATER_PACK in z for z in rec["uploaded"]), (
        f"the later pack must still be attempted after an earlier failure: "
        f"{rec['uploaded']}"
    )


def test_failing_download_does_not_block_later_packs(temp_fleet, monkeypatch):
    """The fetch half of the failure unit: a download() that blows up is
    isolated to its own pack."""
    temp_fleet.pins("dev", {FIRST_PACK: "1.0.0", LATER_PACK: "2.0.0"})
    tenant = temp_fleet.tenant(
        "t_be_download", ring="dev", extras=[FIRST_PACK, LATER_PACK]
    )
    rec = _mock_live_seams(monkeypatch, tenant)

    def _flaky_download(url):
        if FIRST_PACK in url:
            raise RuntimeError("connection reset fetching the release zip")
        rec["downloaded"].append(url)
        return "/tmp/fake.zip"

    monkeypatch.setattr(deploy_tenant, "download", _flaky_download)

    with pytest.raises(deploy_tenant.DeployError):
        deploy_tenant.converge(tenant, apply=True, offline=False,
                               installed_fixture=None)

    assert any(LATER_PACK in url for url in rec["downloaded"]), (
        f"the later pack must still be fetched after an earlier fetch failure: "
        f"{rec['downloaded']}"
    )
    assert len(rec["uploaded"]) == 1, (
        f"only the later pack should have reached upload: {rec['uploaded']}"
    )


def test_failing_local_source_pack_does_not_block_later_packs(
    temp_fleet, monkeypatch
):
    """The local-source transport is isolated the same way: a failing
    sdk_upload_source() must not stop the upstream pack that follows it."""
    temp_fleet.pins("dev", {LOCAL_PACK_ID: LOCAL_SENTINEL, LATER_PACK: "2.0.0"})
    tenant = temp_fleet.tenant(
        "t_be_local", ring="dev", extras=[LOCAL_PACK_ID, LATER_PACK]
    )
    rec = _mock_live_seams(monkeypatch, tenant)

    def _flaky_source_upload(pack_name, creds):
        raise RuntimeError("demisto-sdk upload failed (1)")

    monkeypatch.setattr(deploy_tenant, "sdk_upload_source", _flaky_source_upload)

    with pytest.raises(deploy_tenant.DeployError):
        deploy_tenant.converge(tenant, apply=True, offline=False,
                               installed_fixture=None)

    assert any(LATER_PACK in url for url in rec["downloaded"]), (
        f"the upstream pack must still be attempted after the local-source "
        f"pack failed: {rec['downloaded']}"
    )
    assert len(rec["uploaded"]) == 1


def test_any_failure_holds_remove_and_exits_non_zero(
    temp_fleet, monkeypatch, capsys
):
    """Incomplete Converge: Orphan removal runs only after a fully successful
    upsert pass. With a failure present, delete_packs() must not be called even
    with FLEET_ALLOW_DELETE=true, the orphan prints as held, and the run exits
    non-zero with the `converge INCOMPLETE` wording."""
    # ExamplePack is installed but not composed -> an Orphan (the mocked catalog
    # is empty, so the bounded deletable set is the MSSP-authored ids).
    temp_fleet.pins("dev", {FIRST_PACK: "1.0.0", LATER_PACK: "2.0.0"})
    tenant = temp_fleet.tenant(
        "t_be_hold", ring="dev", extras=[FIRST_PACK, LATER_PACK]
    )
    rec = _mock_live_seams(monkeypatch, tenant,
                           installed={LOCAL_PACK_ID: "1.0.0"})
    monkeypatch.setenv("FLEET_ALLOW_DELETE", "true")

    def _flaky_upload(zip_path, creds):
        raise RuntimeError("HTTP 500 from tenant\nsecond line of noise")

    monkeypatch.setattr(deploy_tenant, "sdk_upload", _flaky_upload)
    monkeypatch.setattr(sys, "argv", ["deploy_tenant.py", tenant, "--apply"])

    with pytest.raises(SystemExit) as exc:
        deploy_tenant.main()
    assert exc.value.code not in (0, None), "an Incomplete Converge must exit non-zero"

    assert rec["deleted"] == [], (
        f"REMOVE must be held while any upsert failed: {rec['deleted']}"
    )

    out = capsys.readouterr().out
    assert "converge INCOMPLETE" in out
    assert "REMOVE held" in out
    assert f"hold delete {LOCAL_PACK_ID}" in out, (
        f"the would-be deletion must print as held:\n{out}"
    )
    # Per-pack failure summary: id, version, first line of the error only.
    assert f"{FIRST_PACK}" in out and "HTTP 500 from tenant" in out
    assert "second line of noise" not in out
    # Never claim a run with failures converged.
    assert "# converged" not in out
    for banned in ("partial converge", "partial deploy"):
        assert banned not in out.lower()


def test_failure_with_allow_delete_unset_holds_rather_than_skips(
    temp_fleet, monkeypatch, capsys
):
    """Branch-order pin: with upsert failures present AND FLEET_ALLOW_DELETE
    unset, the held wording wins — a wrong ordering (checking the allow gate
    before failures) would print "skip delete" here instead."""
    temp_fleet.pins("dev", {FIRST_PACK: "1.0.0"})
    tenant = temp_fleet.tenant("t_be_hold_unset", ring="dev", extras=[FIRST_PACK])
    rec = _mock_live_seams(monkeypatch, tenant,
                           installed={LOCAL_PACK_ID: "1.0.0"})
    monkeypatch.delenv("FLEET_ALLOW_DELETE", raising=False)

    def _flaky_upload(zip_path, creds):
        raise RuntimeError("HTTP 500 from tenant")

    monkeypatch.setattr(deploy_tenant, "sdk_upload", _flaky_upload)

    with pytest.raises(deploy_tenant.DeployError):
        deploy_tenant.converge(tenant, apply=True, offline=False,
                               installed_fixture=None)

    assert rec["deleted"] == []
    out = capsys.readouterr().out
    assert f"hold delete {LOCAL_PACK_ID}" in out
    assert "skip delete" not in out
    assert "REMOVE held" in out


def test_all_success_still_deletes_orphans_and_reports_converged(
    temp_fleet, monkeypatch, capsys
):
    """The happy path is unchanged: every pack upserts, REMOVE runs under
    FLEET_ALLOW_DELETE, the `# converged` summary prints, nothing raises."""
    temp_fleet.pins("dev", {FIRST_PACK: "1.0.0", LATER_PACK: "2.0.0"})
    tenant = temp_fleet.tenant(
        "t_be_ok", ring="dev", extras=[FIRST_PACK, LATER_PACK]
    )
    rec = _mock_live_seams(monkeypatch, tenant,
                           installed={LOCAL_PACK_ID: "1.0.0"})
    monkeypatch.setenv("FLEET_ALLOW_DELETE", "true")

    deploy_tenant.converge(tenant, apply=True, offline=False,
                           installed_fixture=None)

    assert len(rec["uploaded"]) == 2
    assert rec["deleted"] == [[LOCAL_PACK_ID]], (
        f"Orphan removal must run after a fully successful upsert pass: "
        f"{rec['deleted']}"
    )

    out = capsys.readouterr().out
    assert f"− delete {LOCAL_PACK_ID}" in out, (
        f"a successful Orphan delete must not use the ✗ failure marker:\n{out}"
    )
    assert "✗" not in out
    assert f"# converged {tenant}: 2 upserted, 1 deleted" in out
    assert "INCOMPLETE" not in out
    assert "REMOVE held" not in out


def test_all_success_without_allow_delete_still_says_skip_not_held(
    temp_fleet, monkeypatch, capsys
):
    """The FLEET_ALLOW_DELETE gate is untouched and its wording stays distinct
    from a held REMOVE: not-enabled is "skip delete", a failure is "hold delete".
    """
    temp_fleet.pins("dev", {FIRST_PACK: "1.0.0"})
    tenant = temp_fleet.tenant("t_be_skip", ring="dev", extras=[FIRST_PACK])
    rec = _mock_live_seams(monkeypatch, tenant,
                           installed={LOCAL_PACK_ID: "1.0.0"})
    monkeypatch.delenv("FLEET_ALLOW_DELETE", raising=False)

    deploy_tenant.converge(tenant, apply=True, offline=False,
                           installed_fixture=None)

    assert rec["deleted"] == []
    out = capsys.readouterr().out
    assert f"skip delete {LOCAL_PACK_ID}" in out
    assert "REMOVE held" not in out
    assert f"# converged {tenant}: 1 upserted, 0 deleted (1 skipped)" in out
