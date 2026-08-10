"""deploy_tenant.py — source-upload path for sentinel-local packs.

Contract under test:

  * build_plan() for a source=="local" install (pack pinned to "local") must
    NOT call catalog.resolve_artifact, must mark the install item
    source=="local", set zip_url = None and exact True, and take the version
    from pack_metadata currentVersion. It must NOT appear in "unresolved".
  * A source=="upstream" install goes through resolve_artifact (mocked). If the
    catalog can't resolve it, it lands in `unresolved` (does NOT crash).
  * sdk_upload_source(pack_name, creds) runs
    `demisto-sdk upload -i Packs/<dirname> --xsiam --zip` from the repo root
    with creds threaded into the subprocess env.
  * Under --apply, converge routes local packs through sdk_upload_source and
    upstream packs through download() + sdk_upload(); local packs must NOT call
    download().

Everything is mocked: no network (urllib), no subprocess (demisto-sdk), no live
tenant I/O. The local pack is the REAL Packs/ExamplePack fixture; the fleet is a
temp tree. Routing is pin-driven: the local pack is pinned to the "local"
sentinel so resolve() routes it source=="local".
"""
import pytest

import deploy_tenant
import resolve as resolve_mod

from helpers_local_source import (
    LOCAL_PACK_ID,
    LOCAL_SENTINEL,
    UPSTREAM_PACK_ID,
    local_pack_current_version,
)


# --- helpers ---------------------------------------------------------------
# The contract fixes the marker key as item["source"] and the function name as
# sdk_upload_source. No tolerant OR-based / multi-name lookups.
def _find(items, pack_id):
    matches = [it for it in items if it["id"] == pack_id]
    assert matches, f"pack '{pack_id}' not in installs: {items}"
    return matches[0]


def _resolve(tenant):
    # Import inside so the temp_fleet monkeypatch of resolve.FLEET is in effect.
    from resolve import resolve
    return resolve(tenant)


# --- build_plan ------------------------------------------------------------
def test_build_plan_local_pack_skips_catalog_and_marks_source_local(
    temp_fleet, monkeypatch
):
    temp_fleet.pins("dev", {LOCAL_PACK_ID: LOCAL_SENTINEL})
    tenant = temp_fleet.tenant("t_plan_local", ring="dev", extras=[LOCAL_PACK_ID])
    r = _resolve(tenant)

    # resolve_artifact must NOT be called for a local pack -> blow up if it is.
    def _boom(*a, **k):
        raise AssertionError("resolve_artifact must not be called for a local pack")

    monkeypatch.setattr(deploy_tenant, "resolve_artifact", _boom)
    # Keep load_catalog offline-safe (it may still be called for the catalog obj).
    monkeypatch.setattr(deploy_tenant, "load_catalog", lambda *a, **k: {"packs": []})

    installs, unresolved, _catalog = deploy_tenant.build_plan(r, offline=False)

    item = _find(installs, LOCAL_PACK_ID)
    assert item["source"] == "local"
    assert item["zip_url"] is None
    assert item["exact"] is True
    assert item["version"] == local_pack_current_version()
    # A local pack is never "unresolved".
    assert all(pid != LOCAL_PACK_ID for (pid, _err) in unresolved)


def test_build_plan_upstream_pack_resolves_via_catalog(temp_fleet, monkeypatch):
    temp_fleet.pins("dev", {UPSTREAM_PACK_ID: "9.9.9"})
    tenant = temp_fleet.tenant("t_plan_up", ring="dev", extras=[UPSTREAM_PACK_ID])
    r = _resolve(tenant)

    calls = []

    def _fake_resolve_artifact(pid, version, url, commit):
        calls.append((pid, version))
        return {"zip_url": f"https://example.invalid/{pid}-{version}.zip", "exact": True}

    monkeypatch.setattr(deploy_tenant, "resolve_artifact", _fake_resolve_artifact)
    monkeypatch.setattr(deploy_tenant, "load_catalog", lambda *a, **k: {"packs": []})

    installs, unresolved, _catalog = deploy_tenant.build_plan(r, offline=False)

    assert (UPSTREAM_PACK_ID, "9.9.9") in calls  # catalog WAS consulted
    item = _find(installs, UPSTREAM_PACK_ID)
    assert item["source"] == "upstream"
    assert item["zip_url"] == f"https://example.invalid/{UPSTREAM_PACK_ID}-9.9.9.zip"
    assert unresolved == []


def test_build_plan_version_pinned_packs_pack_routes_upstream_not_local(
    temp_fleet, monkeypatch
):
    """A version-pinned pack that ALSO lives in Packs/ must go through the
    catalog (source upstream), not the local source path — pin value drives the
    plan, not presence in Packs/."""
    temp_fleet.pins("dev", {LOCAL_PACK_ID: "3.1.4"})
    tenant = temp_fleet.tenant("t_plan_ver", ring="dev", extras=[LOCAL_PACK_ID])
    r = _resolve(tenant)

    calls = []

    def _fake_resolve_artifact(pid, version, url, commit):
        calls.append((pid, version))
        return {"zip_url": f"https://example.invalid/{pid}-{version}.zip", "exact": True}

    monkeypatch.setattr(deploy_tenant, "resolve_artifact", _fake_resolve_artifact)
    monkeypatch.setattr(deploy_tenant, "load_catalog", lambda *a, **k: {"packs": []})

    installs, _unresolved, _catalog = deploy_tenant.build_plan(r, offline=False)

    assert (LOCAL_PACK_ID, "3.1.4") in calls  # catalog WAS consulted for it
    item = _find(installs, LOCAL_PACK_ID)
    assert item["source"] == "upstream"
    assert item["zip_url"] == f"https://example.invalid/{LOCAL_PACK_ID}-3.1.4.zip"


def test_build_plan_local_not_unresolved_when_upstream_fails(
    temp_fleet, monkeypatch
):
    """If the upstream artifact fails to resolve it lands in `unresolved`
    (no crash); the sentinel-local pack is unaffected."""
    from catalog import CatalogError

    temp_fleet.pins(
        "dev", {LOCAL_PACK_ID: LOCAL_SENTINEL, UPSTREAM_PACK_ID: "9.9.9"}
    )
    tenant = temp_fleet.tenant(
        "t_plan_mixed", ring="dev", extras=[LOCAL_PACK_ID, UPSTREAM_PACK_ID]
    )
    r = _resolve(tenant)

    def _fake_resolve_artifact(pid, version, url, commit):
        raise CatalogError(f"boom for {pid}")

    monkeypatch.setattr(deploy_tenant, "resolve_artifact", _fake_resolve_artifact)
    monkeypatch.setattr(deploy_tenant, "load_catalog", lambda *a, **k: {"packs": []})

    installs, unresolved, _catalog = deploy_tenant.build_plan(r, offline=False)

    unresolved_ids = {pid for (pid, _err) in unresolved}
    assert LOCAL_PACK_ID not in unresolved_ids
    assert UPSTREAM_PACK_ID in unresolved_ids  # did NOT crash, landed here

    local_item = _find(installs, LOCAL_PACK_ID)
    assert local_item["source"] == "local"
    assert local_item["zip_url"] is None


def test_build_plan_local_pack_with_null_currentversion_raises_deployerror(
    temp_fleet, monkeypatch
):
    """GUARD: a sentinel-local pack whose pack_metadata.json has a null/missing
    currentVersion (version resolves to None) must make build_plan() raise
    DeployError — a None version must never reach the upload/printer path.

    The real Packs/ExamplePack is untouched: we monkeypatch resolve.local_packs
    so the pack routes source=="local" with version None (as it would if its
    metadata lacked currentVersion). Offline so no catalog network.
    """
    monkeypatch.setattr(
        resolve_mod, "local_packs",
        lambda: {LOCAL_PACK_ID: {"version": None, "name": LOCAL_PACK_ID}},
    )
    temp_fleet.pins("dev", {LOCAL_PACK_ID: LOCAL_SENTINEL})
    tenant = temp_fleet.tenant("t_null_ver", ring="dev", extras=[LOCAL_PACK_ID])
    r = _resolve(tenant)

    # resolve() routes it local, but with version None (from the patched metadata).
    local_item = _find(r["base"] + r["extras"], LOCAL_PACK_ID)
    assert local_item["source"] == "local"
    assert local_item["version"] is None

    with pytest.raises(deploy_tenant.DeployError) as exc:
        deploy_tenant.build_plan(r, offline=True)
    assert LOCAL_PACK_ID in str(exc.value)


# --- sdk_upload_source -----------------------------------------------------
def test_sdk_upload_source_builds_correct_demisto_sdk_command(monkeypatch):
    captured = {}

    def _fake_run(cmd, *args, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs

        class _CP:
            returncode = 0
        return _CP()

    monkeypatch.setattr(deploy_tenant.subprocess, "run", _fake_run)

    creds = {
        "DEMISTO_BASE_URL": "https://api-t.xdr.us.paloaltonetworks.com",
        "DEMISTO_API_KEY": "k",
        "XSIAM_AUTH_ID": "7",
    }
    deploy_tenant.sdk_upload_source(LOCAL_PACK_ID, creds)

    cmd = list(captured["cmd"])
    assert "demisto-sdk" in cmd
    assert "upload" in cmd
    assert "--xsiam" in cmd
    assert "--zip" in cmd

    # -i <path> must reference Packs/<name>.
    assert "-i" in cmd, f"missing -i flag in {cmd}"
    i_arg = cmd[cmd.index("-i") + 1]
    assert i_arg == f"Packs/{LOCAL_PACK_ID}", (
        f'-i argument should be "Packs/{LOCAL_PACK_ID}", got "{i_arg}"'
    )


def test_sdk_upload_source_runs_from_repo_root(monkeypatch):
    """The source upload runs from the repo root (cwd == REPO)."""
    captured = {}

    def _fake_run(cmd, *args, **kwargs):
        captured["cwd"] = kwargs.get("cwd")

        class _CP:
            returncode = 0
        return _CP()

    monkeypatch.setattr(deploy_tenant.subprocess, "run", _fake_run)

    creds = {
        "DEMISTO_BASE_URL": "https://api-t.xdr.us.paloaltonetworks.com",
        "DEMISTO_API_KEY": "k",
        "XSIAM_AUTH_ID": "7",
    }
    deploy_tenant.sdk_upload_source(LOCAL_PACK_ID, creds)

    assert str(captured["cwd"]) == str(deploy_tenant.REPO), (
        f"source upload must run from repo root, got cwd={captured['cwd']!r}"
    )


def test_sdk_upload_source_passes_creds_via_env(monkeypatch):
    captured = {}

    def _fake_run(cmd, *args, **kwargs):
        captured["env"] = kwargs.get("env")

        class _CP:
            returncode = 0
        return _CP()

    monkeypatch.setattr(deploy_tenant.subprocess, "run", _fake_run)

    creds = {
        "DEMISTO_BASE_URL": "https://api-t.xdr.us.paloaltonetworks.com",
        "DEMISTO_API_KEY": "secret-key",
        "XSIAM_AUTH_ID": "42",
    }
    deploy_tenant.sdk_upload_source(LOCAL_PACK_ID, creds)

    env = captured.get("env")
    assert env is not None, "creds must be passed to demisto-sdk via env"
    for k, v in creds.items():
        assert env.get(k) == v, f"cred {k} not propagated into subprocess env"


def test_sdk_upload_source_does_not_download(monkeypatch):
    """Source upload must never fetch a zip over the network."""
    monkeypatch.setattr(
        deploy_tenant, "download",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("source upload must not call download()")
        ),
    )
    monkeypatch.setattr(
        deploy_tenant.subprocess, "run",
        lambda *a, **k: type("CP", (), {"returncode": 0})(),
    )

    creds = {
        "DEMISTO_BASE_URL": "https://api-t.xdr.us.paloaltonetworks.com",
        "DEMISTO_API_KEY": "k",
        "XSIAM_AUTH_ID": "7",
    }
    deploy_tenant.sdk_upload_source(LOCAL_PACK_ID, creds)  # must not raise


# --- converge routing ------------------------------------------------------
def test_converge_apply_routes_local_via_source_and_upstream_via_download(
    temp_fleet, monkeypatch
):
    """Local packs go through sdk_upload_source; upstream packs go through
    download() + sdk_upload(). Local packs must NOT be downloaded."""
    temp_fleet.pins(
        "dev", {LOCAL_PACK_ID: LOCAL_SENTINEL, UPSTREAM_PACK_ID: "9.9.9"}
    )
    tenant = temp_fleet.tenant(
        "t_conv",
        ring="dev",
        extras=[LOCAL_PACK_ID, UPSTREAM_PACK_ID],
        credentials_ref="TCONV",
    )

    # --- mock all live seams ---
    creds = {
        "DEMISTO_BASE_URL": f"https://api-{tenant}.xdr.us.paloaltonetworks.com",
        "DEMISTO_API_KEY": "k",
        "XSIAM_AUTH_ID": "7",
    }
    monkeypatch.setattr(deploy_tenant, "load_creds", lambda: creds)
    monkeypatch.setattr(deploy_tenant, "assert_host_matches", lambda r, c: None)
    monkeypatch.setattr(deploy_tenant, "list_installed", lambda c: {})
    monkeypatch.setattr(deploy_tenant, "load_catalog", lambda *a, **k: {"packs": []})
    monkeypatch.setattr(
        deploy_tenant, "resolve_artifact",
        lambda pid, v, url, commit: {
            "zip_url": f"https://example.invalid/{pid}-{v}.zip", "exact": True
        },
    )

    downloaded = []
    monkeypatch.setattr(
        deploy_tenant, "download",
        lambda url: (downloaded.append(url) or "/tmp/fake.zip"),
    )

    sdk_upload_calls = []
    monkeypatch.setattr(
        deploy_tenant, "sdk_upload",
        lambda zip_path, c: sdk_upload_calls.append(zip_path),
    )

    source_upload_calls = []
    monkeypatch.setattr(
        deploy_tenant, "sdk_upload_source",
        lambda pack, c: source_upload_calls.append(pack),
    )

    # Avoid deleting a real temp file in the finally-block.
    monkeypatch.setattr(deploy_tenant.os, "unlink", lambda p: None)

    deploy_tenant.converge(tenant, apply=True, offline=False, installed_fixture=None)

    # Local pack -> source path exactly once, and NOT via download().
    assert len(source_upload_calls) == 1, (
        f"local pack should go through the source-upload path exactly once: "
        f"{source_upload_calls}"
    )
    src_arg = source_upload_calls[0]
    # sdk_upload_source is called with the on-disk dir name (== id for ExamplePack).
    assert src_arg == LOCAL_PACK_ID or str(src_arg).endswith(LOCAL_PACK_ID), (
        f"source upload should reference {LOCAL_PACK_ID}, got {src_arg!r}"
    )

    # Upstream pack -> download() + sdk_upload(); local pack must NOT be downloaded.
    assert any(UPSTREAM_PACK_ID in url for url in downloaded), (
        f"upstream pack was not downloaded: {downloaded}"
    )
    assert not any(LOCAL_PACK_ID in url for url in downloaded), (
        f"local pack must not be downloaded: {downloaded}"
    )
    assert len(sdk_upload_calls) == 1, (
        f"upstream pack should be uploaded via sdk_upload exactly once: {sdk_upload_calls}"
    )
