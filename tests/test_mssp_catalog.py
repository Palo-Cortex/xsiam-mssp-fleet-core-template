"""MSSP catalog — direct zip_url resolution for MSSP-authored packs.

Contract under test:

  * mssp_catalog.expected_catalog() derives entries from Packs/*/pack_metadata
    with zip_url = <release_base>/<id>-v<ver>/<id>-v<ver>.zip.
  * The COMMITTED mssp_pack_catalog.json is in sync with Packs/ metadata (the
    same invariant ring-gate enforces with --check).
  * catalog.resolve_artifact() resolves an id found in the MSSP catalog from
    the local file — upstream catalog is never consulted (no network).
  * A pin older than the catalog's (latest) version retags the zip_url to the
    pinned version (exact=False), same semantics as upstream.
  * An id NOT in the MSSP catalog falls through to the upstream path.
"""
import json

import pytest

import catalog as catalog_mod
import mssp_catalog

from helpers_local_source import LOCAL_PACK_ID, local_pack_current_version


# --- generation / sync ------------------------------------------------------
def test_expected_catalog_derives_examplepack_entry():
    cat = mssp_catalog.expected_catalog()
    entries = {e["id"]: e for e in cat["packs"]}
    assert LOCAL_PACK_ID in entries

    e = entries[LOCAL_PACK_ID]
    ver = local_pack_current_version()
    tag = f"{LOCAL_PACK_ID}-v{ver}"
    assert e["version"] == ver
    assert e["path"] == f"Packs/{LOCAL_PACK_ID}"
    assert e["zip_url"].endswith(f"/{tag}/{tag}.zip")


def test_committed_catalog_in_sync_with_packs_metadata():
    """The repo invariant ring-gate enforces: the committed catalog always
    matches what Packs/ metadata derives."""
    assert mssp_catalog.current_catalog() == mssp_catalog.expected_catalog()


# --- resolution -------------------------------------------------------------
@pytest.fixture
def mssp_cat(tmp_path, monkeypatch):
    """Point catalog.MSSP_CATALOG at a temp catalog with one entry @ 1.2.0."""
    p = tmp_path / "mssp_pack_catalog.json"
    tag = f"{LOCAL_PACK_ID}-v1.2.0"
    p.write_text(json.dumps({"packs": [{
        "id": LOCAL_PACK_ID,
        "version": "1.2.0",
        "path": f"Packs/{LOCAL_PACK_ID}",
        "zip_url": f"https://example.invalid/dl/{tag}/{tag}.zip",
    }]}))
    monkeypatch.setattr(catalog_mod, "MSSP_CATALOG", p)
    return p


def _no_upstream(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("upstream catalog must not be consulted for an MSSP pack")
    monkeypatch.setattr(catalog_mod, "load_catalog", _boom)


def test_resolve_artifact_prefers_mssp_catalog_no_network(mssp_cat, monkeypatch):
    _no_upstream(monkeypatch)

    r = catalog_mod.resolve_artifact(LOCAL_PACK_ID, "1.2.0")

    assert r["exact"] is True
    assert r["catalog_version"] == "1.2.0"
    tag = f"{LOCAL_PACK_ID}-v1.2.0"
    assert r["zip_url"] == f"https://example.invalid/dl/{tag}/{tag}.zip"
    assert r["xsoar_config"] is None


def test_resolve_artifact_mssp_retags_older_pin(mssp_cat, monkeypatch):
    """Pinning a version behind the catalog's latest (prod behind dev) retags
    the deterministic URL to the pin — both the tag and the filename."""
    _no_upstream(monkeypatch)

    r = catalog_mod.resolve_artifact(LOCAL_PACK_ID, "1.0.0")

    assert r["exact"] is False
    tag = f"{LOCAL_PACK_ID}-v1.0.0"
    assert r["zip_url"] == f"https://example.invalid/dl/{tag}/{tag}.zip"


def test_resolve_artifact_unknown_id_falls_through_to_upstream(mssp_cat, monkeypatch):
    """An id not in the MSSP catalog takes the upstream path unchanged."""
    monkeypatch.setattr(catalog_mod, "load_catalog", lambda *a, **k: {"packs": []})

    with pytest.raises(catalog_mod.CatalogError) as exc:
        catalog_mod.resolve_artifact("soc-not-ours", "9.9.9")
    assert "not found in catalog" in str(exc.value)


def test_resolve_artifact_mssp_entry_without_zip_url_errors(tmp_path, monkeypatch):
    p = tmp_path / "mssp_pack_catalog.json"
    p.write_text(json.dumps({"packs": [{"id": LOCAL_PACK_ID, "version": "1.0.0"}]}))
    monkeypatch.setattr(catalog_mod, "MSSP_CATALOG", p)

    with pytest.raises(catalog_mod.CatalogError) as exc:
        catalog_mod.resolve_artifact(LOCAL_PACK_ID, "1.0.0")
    assert "zip_url" in str(exc.value)
