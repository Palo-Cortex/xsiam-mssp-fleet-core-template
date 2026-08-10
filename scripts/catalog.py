#!/usr/bin/env python3
"""
catalog.py — resolve a pinned pack (id + version) to its release-zip URL.

The fleet never vendors upstream packs. To install one it walks
the upstream catalog to the actual artifact:

    pack_catalog.json            (id -> version -> xsoar_config URL)
        └── <pack>/xsoar_config.json     (custom_packs[].url -> the .zip)

Catalog source and artifact source are SEPARATE: the catalog says which pack and
which xsoar_config to read; the zip itself is a GitHub release asset named for a
specific version. `pinned_commit` freezes the catalog read for reproducibility.

MSSP-AUTHORED packs resolve FIRST against the committed mssp_pack_catalog.json
(see scripts/mssp_catalog.py): entries carry a direct `zip_url` (no xsoar_config
hop) and are read from the local working tree — no network, and pinned to the
same commit as the ring pins by construction. Ids not in the MSSP catalog fall
through to the upstream catalog below.

Two honesty notes this surfaces (rather than hides):
  • If the catalog's published version != the pinned version, resolution is
    marked exact=False and the release tag is best-effort rewritten to the pin —
    verify such a fetch against a live tenant before trusting it.
  • Only stdlib is used, so this runs in a bare CI step.

Usage:
    python scripts/catalog.py <pack_id> [version]
    python scripts/catalog.py <pack_id> [version] --catalog <url> --json
"""
import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

import yaml

FLEET = Path(__file__).resolve().parents[1] / "fleet"
MSSP_CATALOG = Path(__file__).resolve().parents[1] / "mssp_pack_catalog.json"


class CatalogError(Exception):
    """The catalog or an artifact could not be resolved."""


def _get(url):
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            return resp.read().decode("utf-8")
    except Exception as e:  # noqa: BLE001 — surface any transport failure uniformly
        raise CatalogError(f"fetch failed: {url}\n  {e}") from e


def default_catalog_url():
    return yaml.safe_load(open(FLEET / "defaults.yml"))["catalog"]["url"]


def _pin_commit(url, pinned_commit):
    """Freeze a raw.githubusercontent.com URL to a specific commit/ref."""
    if not pinned_commit:
        return url
    # https://raw.githubusercontent.com/<org>/<repo>/<ref>/<path>
    m = re.match(r"(https://raw\.githubusercontent\.com/[^/]+/[^/]+/)[^/]+(/.*)", url)
    return f"{m.group(1)}{pinned_commit}{m.group(2)}" if m else url


def load_catalog(catalog_url=None, pinned_commit=None):
    url = _pin_commit(catalog_url or default_catalog_url(), pinned_commit)
    try:
        return json.loads(_get(url))
    except json.JSONDecodeError as e:
        raise CatalogError(f"catalog is not valid JSON: {url}\n  {e}") from e


def find_pack(catalog, pack_id):
    for entry in catalog.get("packs", []):
        if entry.get("id") == pack_id:
            return entry
    return None


def load_mssp_catalog():
    """The committed MSSP catalog, read from the local working tree ({} shape
    with no packs when the file doesn't exist yet)."""
    if not MSSP_CATALOG.exists():
        return {"packs": []}
    try:
        return json.loads(MSSP_CATALOG.read_text())
    except json.JSONDecodeError as e:
        raise CatalogError(f"MSSP catalog is not valid JSON: {MSSP_CATALOG}\n  {e}") from e


def _resolve_mssp(entry, version):
    """Resolve an MSSP catalog entry: direct zip_url, retagged to the pin when
    it differs from the catalog's (latest) version. Retagging is reliable here —
    the URL scheme is our own <id>-v<version> convention."""
    zip_url = entry.get("zip_url")
    if not zip_url:
        raise CatalogError(f"MSSP pack '{entry.get('id')}' has no zip_url in catalog")
    catalog_version = entry.get("version")
    exact = (version is None) or (version == catalog_version)
    if not exact:
        zip_url = _retag(zip_url, version)
    return {
        "id": entry.get("id"),
        "wanted_version": version,
        "catalog_version": catalog_version,
        "exact": exact,
        "xsoar_config": None,
        "zip_url": zip_url,
        "marketplace_packs": [],
    }


def _retag(zip_url, version):
    """Best-effort: rewrite a release tag/filename to the pinned version.

    e.g. .../releases/download/soc-optimization-v2.1.48/soc-optimization-v2.1.48.zip
    with version 2.1.49  ->  ...-v2.1.49/...-v2.1.49.zip
    """
    return re.sub(r"v\d+\.\d+\.\d+", f"v{version}", zip_url)


def resolve_artifact(pack_id, version=None, catalog_url=None, pinned_commit=None):
    # MSSP-authored packs win: resolved from the committed local catalog, no
    # network. An id collision with upstream resolves to our own pack.
    mssp_entry = find_pack(load_mssp_catalog(), pack_id)
    if mssp_entry:
        return _resolve_mssp(mssp_entry, version)

    catalog = load_catalog(catalog_url, pinned_commit)
    entry = find_pack(catalog, pack_id)
    if not entry:
        raise CatalogError(f"pack '{pack_id}' not found in catalog")

    xsoar_config_url = entry.get("xsoar_config")
    if not xsoar_config_url:
        raise CatalogError(f"pack '{pack_id}' has no xsoar_config in catalog")

    cfg = json.loads(_get(xsoar_config_url))
    custom = cfg.get("custom_packs") or []
    if not custom:
        raise CatalogError(f"pack '{pack_id}' xsoar_config has no custom_packs (no zip)")
    zip_url = custom[0].get("url")

    catalog_version = entry.get("version")
    exact = (version is None) or (version == catalog_version)
    if not exact and zip_url:
        zip_url = _retag(zip_url, version)

    return {
        "id": pack_id,
        "wanted_version": version,
        "catalog_version": catalog_version,
        "exact": exact,
        "xsoar_config": xsoar_config_url,
        "zip_url": zip_url,
        "marketplace_packs": [p.get("id") for p in cfg.get("marketplace_packs") or []],
    }


def main():
    ap = argparse.ArgumentParser(description="Resolve a pack id+version to its zip URL.")
    ap.add_argument("pack_id")
    ap.add_argument("version", nargs="?", help="pinned version (optional)")
    ap.add_argument("--catalog", help="override catalog URL")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    try:
        r = resolve_artifact(args.pack_id, args.version, args.catalog)
    except CatalogError as e:
        sys.exit(f"ERROR: {e}")

    if args.json:
        print(json.dumps(r, indent=2))
        return
    print(f"pack           {r['id']}")
    print(f"wanted version {r['wanted_version'] or '(any)'}")
    print(f"catalog version {r['catalog_version']}")
    print(f"exact match    {r['exact']}")
    if not r["exact"]:
        print("  ! catalog publishes a different version than the pin; zip tag was")
        print("    best-effort rewritten. Verify against a live tenant.")
    print(f"zip_url        {r['zip_url']}")


if __name__ == "__main__":
    main()
