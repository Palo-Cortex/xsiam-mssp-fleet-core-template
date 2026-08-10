#!/usr/bin/env python3
"""
mssp_catalog.py — generate / verify the MSSP pack catalog (mssp_pack_catalog.json).

The fleet's OWN packs (source under Packs/) are released as per-pack tagged
GitHub release zips — tag `<id>-v<version>`, asset `<id>-v<version>.zip` — by
the `release-packs` workflow. This catalog is the id+version → zip_url index
for those artifacts: the MSSP mirror of the upstream pack_catalog.json
, with one deliberate simplification — entries carry a direct
`zip_url` (no xsoar_config indirection; nothing needs it yet, and the field
can be added later without breaking the shape).

The catalog is COMMITTED to this repo and read from the local working tree by
catalog.py, so a pack's metadata bump, its catalog entry, and any pin that
references it land atomically in one PR — and resolution needs no network.
ring-gate runs `--check` so the catalog can never drift from Packs/ metadata.

zip_url is derived from `mssp_catalog.release_base` in fleet/defaults.yml plus
the tag convention. It is deterministic, so the entry is written BEFORE the
release workflow has published the artifact. Merge order still matters for
deploys: merge the pack bump first (the release workflow publishes the zip),
flip the pin second — a pin referencing an unpublished version fails the
converge fetch loudly, it never deploys something else.

Usage:
    python scripts/mssp_catalog.py --write   # regenerate from Packs/ metadata
    python scripts/mssp_catalog.py --check   # exit 1 if out of sync (CI)
"""
import argparse
import json
import sys
from pathlib import Path

import yaml

from resolve import local_packs

REPO = Path(__file__).resolve().parents[1]
FLEET = REPO / "fleet"
CATALOG_PATH = REPO / "mssp_pack_catalog.json"


class MsspCatalogError(Exception):
    """The MSSP catalog could not be generated or read."""


def release_base():
    cfg = (yaml.safe_load(open(FLEET / "defaults.yml")) or {}).get("mssp_catalog") or {}
    base = cfg.get("release_base")
    if not base:
        raise MsspCatalogError("fleet/defaults.yml has no mssp_catalog.release_base")
    return base.rstrip("/")


def expected_catalog():
    """The catalog as derived from Packs/*/pack_metadata.json — the single
    source of truth. Same outer shape as the upstream pack_catalog.json."""
    base = release_base()
    packs = []
    for pid, info in sorted(local_packs().items()):
        version = info.get("version")
        if not version:
            raise MsspCatalogError(
                f"Packs/{info['name']}/pack_metadata.json has no currentVersion "
                f"— every MSSP pack must declare one"
            )
        tag = f"{pid}-v{version}"
        packs.append({
            "id": pid,
            "version": version,
            "path": f"Packs/{info['name']}",
            "zip_url": f"{base}/{tag}/{tag}.zip",
        })
    return {"packs": packs}


def current_catalog():
    """The committed catalog, or None if it doesn't exist yet."""
    if not CATALOG_PATH.exists():
        return None
    try:
        return json.loads(CATALOG_PATH.read_text())
    except json.JSONDecodeError as e:
        raise MsspCatalogError(f"{CATALOG_PATH.name} is not valid JSON: {e}") from e


def main():
    ap = argparse.ArgumentParser(description="Generate/verify mssp_pack_catalog.json.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--write", action="store_true", help="regenerate from Packs/ metadata")
    g.add_argument("--check", action="store_true", help="exit 1 if out of sync (CI)")
    args = ap.parse_args()

    try:
        want = expected_catalog()
        if args.write:
            CATALOG_PATH.write_text(json.dumps(want, indent=2) + "\n")
            print(f"wrote {CATALOG_PATH.name}: {len(want['packs'])} pack(s)")
            return
        if current_catalog() != want:
            print(
                f"✗ {CATALOG_PATH.name} out of sync with Packs/ metadata — run:\n"
                f"    python scripts/mssp_catalog.py --write",
                file=sys.stderr,
            )
            sys.exit(1)
        print(f"✓ {CATALOG_PATH.name} in sync ({len(want['packs'])} pack(s))")
    except MsspCatalogError as e:
        sys.exit(f"ERROR: {e}")


if __name__ == "__main__":
    main()
