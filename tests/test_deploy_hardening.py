"""deploy_tenant.py / lock.py / load_creds.py hardening.

  * artifact lock: a locked pack deploys from its locked URL (no catalog walk);
    a require_lock ring refuses an unlocked pack; a sha256 mismatch fails the
    pack before upload; lock.py --check reports unlocked pins.
  * orphan deletion is bounded by what the fleet manages, never by the upstream
    catalog, and excludes customer prefixes, custom: ids and never_delete:.
  * post-deploy verify: an installed version != uploaded is an Incomplete
    Converge and holds REMOVE.
  * load_creds reads ONLY the per-tenant FLEET_TENANT_* env vars.
"""
import hashlib
import json

import pytest
import yaml

import deploy_tenant
import load_creds
import lock


def _set_policy(fleet, rings):
    (fleet / "policies.yml").write_text(yaml.safe_dump({"rings": rings}))


def _write_lock(fleet, artifacts):
    (fleet / lock.LOCK_NAME).write_text(yaml.safe_dump({"artifacts": artifacts}))


def _defaults(fleet, **extra):
    d = yaml.safe_load((fleet / "defaults.yml").read_text())
    d.update(extra)
    (fleet / "defaults.yml").write_text(yaml.safe_dump(d))


def _seams(monkeypatch, tenant, installed=None, verify=None):
    creds = {"DEMISTO_BASE_URL": f"https://api-{tenant}.example.invalid",
             "DEMISTO_API_KEY": "k", "XSIAM_AUTH_ID": "7"}
    rec = {"downloaded": [], "uploaded": [], "deleted": []}
    monkeypatch.setattr(deploy_tenant, "load_creds", lambda: creds)
    monkeypatch.setattr(deploy_tenant, "assert_host_matches", lambda r, c: None)
    monkeypatch.setattr(deploy_tenant, "list_installed", lambda c: dict(installed or {}))
    monkeypatch.setattr(deploy_tenant, "load_catalog", lambda *a, **k: {"packs": []})
    monkeypatch.setattr(deploy_tenant, "verify_installed",
                        verify or (lambda c, e: {}))
    monkeypatch.setattr(deploy_tenant.shutil, "rmtree", lambda p: None)
    monkeypatch.setattr(deploy_tenant, "sdk_upload",
                        lambda z, c, config_overlay=None: rec["uploaded"].append(z))
    monkeypatch.setattr(deploy_tenant, "delete_packs",
                        lambda c, ids: rec["deleted"].append(sorted(ids)))
    return rec


# --- artifact lock -----------------------------------------------------------
def test_locked_pack_uses_locked_url_without_catalog(temp_fleet, monkeypatch):
    temp_fleet.pins("prod", {"P": "1.0.0"})
    t = temp_fleet.tenant("tlock", ring="prod", extras=["P"])
    _write_lock(temp_fleet.root, {"P": {"1.0.0": {"zip_url": "https://x.invalid/P.zip",
                                                  "sha256": "ab" * 32}}})
    monkeypatch.setattr(deploy_tenant, "resolve_artifact",
                        lambda *a: pytest.fail("catalog must not be walked for a locked pack"))
    monkeypatch.setattr(deploy_tenant, "load_catalog", lambda *a, **k: {"packs": []})

    installs, unresolved, _ = deploy_tenant.build_plan(deploy_tenant.resolve(t), offline=False)

    assert unresolved == []
    assert installs[0]["zip_url"] == "https://x.invalid/P.zip"
    assert installs[0]["locked"] and installs[0]["exact"] is True


def test_require_lock_ring_refuses_unlocked_pack(temp_fleet, monkeypatch):
    temp_fleet.pins("prod", {"P": "1.0.0"})
    t = temp_fleet.tenant("tnolock", ring="prod", extras=["P"])
    _set_policy(temp_fleet.root, {"prod": {"source": "qa", "require_lock": True}})
    monkeypatch.setattr(deploy_tenant, "load_catalog", lambda *a, **k: {"packs": []})

    _installs, unresolved, _ = deploy_tenant.build_plan(deploy_tenant.resolve(t), offline=False)

    assert unresolved and "requires a lock entry" in unresolved[0][1]


def test_sha256_mismatch_fails_pack_before_upload(temp_fleet, monkeypatch, tmp_path):
    temp_fleet.pins("dev", {"P": "1.0.0"})
    t = temp_fleet.tenant("tsha", ring="dev", extras=["P"])
    _write_lock(temp_fleet.root, {"P": {"1.0.0": {"zip_url": "https://x.invalid/P.zip",
                                                  "sha256": "00" * 32}}})
    rec = _seams(monkeypatch, t)
    z = tmp_path / "P.zip"
    z.write_bytes(b"tampered")
    monkeypatch.setattr(deploy_tenant, "download", lambda url: str(z))

    with pytest.raises(deploy_tenant.DeployError):
        deploy_tenant.converge(t, apply=True, offline=False, installed_fixture=None)

    assert rec["uploaded"] == []


def test_sha256_match_uploads(temp_fleet, monkeypatch, tmp_path):
    temp_fleet.pins("dev", {"P": "1.0.0"})
    t = temp_fleet.tenant("tshaok", ring="dev", extras=["P"])
    z = tmp_path / "P.zip"
    z.write_bytes(b"good bytes")
    _write_lock(temp_fleet.root, {"P": {"1.0.0": {
        "zip_url": "https://x.invalid/P.zip",
        "sha256": hashlib.sha256(b"good bytes").hexdigest()}}})
    rec = _seams(monkeypatch, t)
    monkeypatch.setattr(deploy_tenant, "download", lambda url: str(z))

    deploy_tenant.converge(t, apply=True, offline=False, installed_fixture=None)

    assert rec["uploaded"] == [str(z)]


def test_lock_check_reports_only_require_lock_rings(temp_fleet):
    temp_fleet.pins("dev", {"A": "1.0.0"})
    temp_fleet.pins("prod", {"A": "1.0.0", "B": "2.0.0"})
    temp_fleet.tenant("tp", ring="prod", pin_overrides={"C": "3.0.0"})
    _set_policy(temp_fleet.root, {"dev": {"source": None},
                                  "prod": {"source": "dev", "require_lock": True}})
    _write_lock(temp_fleet.root, {"A": {"1.0.0": {"zip_url": "u", "sha256": "s"}}})

    missing = {(p, v) for _w, p, v in lock.missing_entries()}

    assert missing == {("B", "2.0.0"), ("C", "3.0.0")}


# --- orphan bounding ---------------------------------------------------------
def test_orphans_bounded_by_fleet_managed_ids_not_catalog(temp_fleet, monkeypatch):
    temp_fleet.pins("dev", {"Kept": "1.0.0", "Removed": "1.0.0"})
    t = temp_fleet.tenant("torph", ring="dev", extras=["Kept"],
                          custom=[{"id": "acme-thing"}])
    _defaults(temp_fleet.root, never_delete=["Pinned-By-Hand"])
    r = deploy_tenant.resolve(t)
    monkeypatch.setattr(deploy_tenant, "pinned_history_ids",
                        lambda: {"Removed", "acme-old", "Pinned-By-Hand"})
    installed = {"Kept": "1.0.0", "Removed": "1.0.0", "acme-old": "1",
                 "acme-thing": "1", "Pinned-By-Hand": "1", "MarketplaceOnly": "1"}

    orphans = deploy_tenant.compute_orphans(r, installed)

    # Removed: fleet-managed, no longer composed. Everything else is either still
    # wanted, customer-prefixed, never_delete, or never managed by the fleet.
    assert orphans == ["Removed"]


def test_pinned_history_ids_parses_git_log(monkeypatch):
    log = "\n".join(["--- a/fleet/pins/dev.yml", "+++ b/fleet/pins/dev.yml",
                     "+  VirusTotal: 2.7.22", "-  OldPack: 1.0.0  # comment",
                     "+ring: dev", "+pins:", "+# header"])

    class R:
        stdout = log
    monkeypatch.setattr(deploy_tenant.subprocess, "run", lambda *a, **k: R())

    assert deploy_tenant.pinned_history_ids() == {"VirusTotal", "OldPack"}


# --- post-deploy verify ------------------------------------------------------
def test_verify_mismatch_is_incomplete_and_holds_remove(temp_fleet, monkeypatch, capsys):
    temp_fleet.pins("dev", {"P": "2.0.0", "Gone": "1.0.0"})
    t = temp_fleet.tenant("tver", ring="dev", extras=["P"])
    rec = _seams(monkeypatch, t, installed={"Gone": "1.0.0"},
                 verify=lambda c, e: {"P": ("2.0.0", "1.0.0")})
    monkeypatch.setattr(deploy_tenant, "download", lambda url: "/tmp/P.zip")
    monkeypatch.setattr(deploy_tenant, "resolve_artifact",
                        lambda *a: {"zip_url": "https://x.invalid/P.zip", "exact": True})
    monkeypatch.setenv("FLEET_ALLOW_DELETE", "true")

    with pytest.raises(deploy_tenant.DeployError):
        deploy_tenant.converge(t, apply=True, offline=False, installed_fixture=None)

    out = capsys.readouterr().out
    assert "FAILED verify P" in out and "REMOVE held" in out
    assert rec["deleted"] == []


def test_verify_installed_retries_then_passes(monkeypatch):
    seq = iter([{"P": "1.0.0"}, {"P": "2.0.0"}])
    monkeypatch.setattr(deploy_tenant, "list_installed", lambda c: next(seq))
    monkeypatch.setattr(deploy_tenant.time, "sleep", lambda s: None)

    assert deploy_tenant.verify_installed({}, {"P": "2.0.0"}) == {}


# --- scoped credentials ------------------------------------------------------
def test_load_creds_reads_scoped_packed_secret(monkeypatch):
    monkeypatch.delenv("ALL_SECRETS", raising=False)
    monkeypatch.setenv("FLEET_TENANT_CREDS", json.dumps(
        {"base_url": "https://api-x", "api_key": "k", "auth_id": "1"}))
    assert load_creds._from_github("REF")["api_key"] == "k"


def test_load_creds_reads_scoped_flat_secrets(monkeypatch):
    monkeypatch.delenv("ALL_SECRETS", raising=False)
    monkeypatch.delenv("FLEET_TENANT_CREDS", raising=False)
    monkeypatch.setenv("FLEET_TENANT_BASE_URL", "https://api-x")
    monkeypatch.setenv("FLEET_TENANT_API_KEY", "k")
    monkeypatch.setenv("FLEET_TENANT_AUTH_ID", "1")
    assert load_creds._from_github("REF")["auth_id"] == "1"


def test_load_creds_errors_clearly_without_secrets(monkeypatch):
    for v in ("ALL_SECRETS", "FLEET_TENANT_CREDS", "FLEET_TENANT_BASE_URL",
              "FLEET_TENANT_API_KEY", "FLEET_TENANT_AUTH_ID"):
        monkeypatch.delenv(v, raising=False)
    with pytest.raises(load_creds.CredsError, match="FLEET_TENANT_CREDS"):
        load_creds._from_github("REF")
