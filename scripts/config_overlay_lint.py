#!/usr/bin/env python3
"""
config_overlay_lint.py — structural gate on the fleet's Config Overlays.

A Config Overlay is the fleet-owned, per-tenant sparse patch of framework config
List values. It composes in two layers under fleet/config_overlays/:

    defaults.yml      the fleet-wide layer
    <tenant>.yml      the per-tenant layer (tenant wins on conflicts)

Each file is a top-level mapping of List id -> sparse patch, the patch itself a
nested mapping of that List's JSON content. Nothing else lives in these files —
no versions, no metadata; the pinned artifact supplies everything the patch does
not name.

Because a Config Overlay is applied by MERGING onto a pinned List at Convergence,
every mistake here fails SILENTLY at deploy time: a typo'd List id patches a List
that does not exist, a layer left behind by a renamed tenant looks live but is
never read, and a non-mapping patch cannot be merged at all. So the errors are
caught structurally, on the PR:

    1. every List id must be registered in defaults.config_overlay_lists
    2. each file must be a mapping, and each patch value must be a mapping
    3. every <tenant>.yml must match an existing Tenant Profile
       (defaults.yml — the fleet-wide layer — is exempt)

Offline: a pure function of the working tree. Exit code: 0 clean, 1 on any
violation.

Usage:
    python scripts/config_overlay_lint.py
    python scripts/config_overlay_lint.py --json
"""
import argparse
import json
import sys

import resolve
from resolve import load

DEFAULTS_LAYER = "defaults"


def _fleet():
    """Read resolve.FLEET at call time so tests can point at a temp fleet."""
    return resolve.FLEET


def registered_lists():
    """The tunable framework config List ids a Config Overlay may patch."""
    return load(_fleet() / "defaults.yml").get("config_overlay_lists") or []


def overlay_files():
    return sorted((_fleet() / "config_overlays").glob("*.yml"))


def tenant_names():
    return {p.stem for p in (_fleet() / "tenants").glob("*.yml")}


def lint():
    known = registered_lists()
    tenants = tenant_names()
    violations = []

    for f in overlay_files():
        name = f.stem

        # 3. a per-tenant layer must belong to a real Tenant Profile.
        if name != DEFAULTS_LAYER and name not in tenants:
            violations.append(
                f"{f.name}: no Tenant Profile at fleet/tenants/{name}.yml — a Config "
                f"Overlay layer must be named 'defaults' or an existing tenant")
            continue

        doc = load(f)
        if doc is None:  # an empty file is an empty layer, not a violation
            continue

        # 2a. the file itself must be a mapping of List id -> patch.
        if not isinstance(doc, dict):
            violations.append(
                f"{f.name}: file must be a mapping of List id -> patch, "
                f"got {type(doc).__name__}")
            continue

        for list_id, patch in doc.items():
            # 1. only registered List ids may be patched.
            if list_id not in known:
                violations.append(
                    f"{f.name}: List id '{list_id}' is not in "
                    f"defaults.config_overlay_lists")
            # 2b. the patch must be a mapping of the List's content.
            if not isinstance(patch, dict):
                violations.append(
                    f"{f.name}: patch for '{list_id}' must be a mapping, "
                    f"got {type(patch).__name__}")

    return violations


def main():
    ap = argparse.ArgumentParser(description="Lint the fleet's Config Overlays.")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    violations = lint()
    if args.json:
        print(json.dumps({"violations": violations, "ok": not violations}, indent=2))
    elif not violations:
        n = len(overlay_files())
        print(f"✓ Config Overlays clean — {n} layer file(s), "
              f"{len(registered_lists())} registered List id(s).")
    else:
        print(f"✗ {len(violations)} violation(s):")
        for v in violations:
            print(f"  - {v}")
    sys.exit(1 if violations else 0)


if __name__ == "__main__":
    main()
