#!/usr/bin/env python3
"""
namespace_lint.py — fleet side of the two-writer namespace contract.

A tenant has two independent writers: this fleet repo and the customer's overlay
repo, each with its own credentials. `demisto-sdk upload` is last-write-wins with
zero namespace enforcement, so the boundary is kept purely STRUCTURAL — no ID
inventory is ever exchanged between the two private repos:

    fleet CI (here)   : NO MSSP-authored id may carry a registered customer prefix
    customer CI (there): EVERY customer id MUST carry the customer's prefix

Enforcing both halves makes a collision impossible by construction rather than
detected after the fact. This script is the fleet half. It also checks that any
customer content the fleet merely *references* is itself namespaced, and that
every declared customer_namespace is registered.

Registered prefixes live in fleet/defaults.yml -> customer_namespaces.

Exit code: 0 clean, 1 on any violation.

Usage:
    python scripts/namespace_lint.py
    python scripts/namespace_lint.py --json
"""
import argparse
import json
import sys
from pathlib import Path

import yaml

from resolve import FLEET, load

REPO = Path(__file__).resolve().parents[1]


def registered_prefixes():
    return load(FLEET / "defaults.yml").get("customer_namespaces") or []


def _has_registered_prefix(pack_id, prefixes):
    return next((p for p in prefixes if pack_id.startswith(p)), None)


def mssp_authored_ids():
    """Every pack id the fleet OWNS: base, group extras, tenant extras, pins,
    and locally-authored Packs/."""
    ids = set()
    ids.update(load(FLEET / "defaults.yml").get("base") or [])
    for g in (FLEET / "groups").glob("*.yml"):
        ids.update(load(g).get("extras") or [])
    for t in (FLEET / "tenants").glob("*.yml"):
        ids.update(load(t).get("extras") or [])
    for p in (FLEET / "pins").glob("*.yml"):
        ids.update((load(p).get("pins") or {}).keys())
    for meta in (REPO / "Packs").glob("*/pack_metadata.json"):
        try:
            ids.add(json.loads(meta.read_text()).get("id") or meta.parent.name)
        except Exception:  # noqa: BLE001
            ids.add(meta.parent.name)
    return sorted(ids)


def lint():
    prefixes = registered_prefixes()
    violations = []

    # 1. No MSSP-authored id may carry a registered customer prefix.
    for pid in mssp_authored_ids():
        hit = _has_registered_prefix(pid, prefixes)
        if hit:
            violations.append(
                f"MSSP-authored pack '{pid}' uses reserved customer prefix '{hit}'")

    # 2. Referenced customer packs must be namespaced with a registered prefix;
    #    declared customer_namespace values must be registered.
    for t in sorted((FLEET / "tenants").glob("*.yml")):
        doc = load(t)
        ns = doc.get("customer_namespace")
        if ns and ns not in prefixes:
            violations.append(
                f"{t.name}: customer_namespace '{ns}' is not in defaults.customer_namespaces")
        for c in doc.get("custom") or []:
            cid = c.get("id", "")
            if not _has_registered_prefix(cid, prefixes):
                violations.append(
                    f"{t.name}: referenced customer pack '{cid}' carries no registered prefix")

    return violations


def main():
    ap = argparse.ArgumentParser(description="Lint the fleet side of the namespace contract.")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    violations = lint()
    if args.json:
        print(json.dumps({"violations": violations, "ok": not violations}, indent=2))
    else:
        prefixes = registered_prefixes()
        print(f"registered customer prefixes: {', '.join(prefixes) or '(none)'}")
        if not violations:
            print("✓ namespace contract clean — no MSSP id collides with a customer prefix.")
        else:
            print(f"✗ {len(violations)} violation(s):")
            for v in violations:
                print(f"  - {v}")
    sys.exit(1 if violations else 0)


if __name__ == "__main__":
    main()
