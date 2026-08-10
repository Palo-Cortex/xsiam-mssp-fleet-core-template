#!/usr/bin/env python3
"""
drift_check.py — compare each tenant's RESOLVED versions (pins) against its
INSTALLED versions, and report drift.

Drift is defined at the pack level for MSSP-owned packs only (base + extras).
Two things are deliberately NOT drift:

  • customer-prefixed content — owned by the customer overlay repo, the second
    writer; the fleet never versions it.
  • packs installed on the tenant that the fleet doesn't resolve — these are
    reported separately as UNMANAGED (a console-installed Marketplace pack, or an
    orphan awaiting the next converge). Drift never deletes; it only reports.

Ring membership is derived from tenant `ring:` fields (no rings.yml). In prod,
installed versions come live from the tenant (GET /contentpacks/metadata/installed);
this offline path reads a fixture in the same {tenant: {id: version}} shape.

LIST-CONTENT DRIFT (opt-in)
---------------------------------------------
The fleet owns the framework config Lists outright, so a
tenant's List content should always be `pinned ⊕ Config Overlay`. A console
hand-edit between Convergence runs — a destructive action quietly taken live
that the fleet still believes is shadowed — self-heals at the next converge and
would otherwise never be seen. Two OPTIONAL fixtures turn that comparison on:

    --pinned-lists   {ring: {list_id: content}}    the pinned pack's shipped
                     List content, keyed by RING because pins are per ring
    --lists-state    {tenant: {list_id: content}}  the live content (the same
                     role --state plays for installed versions)

Expected = pinned baseline deep-merged with the tenant's composed Config Overlay;
drift is reported per key as a dotted path. A fleet-owned List the tenant lacks
is LIST MISSING; a live List that is neither pinned nor overlaid is reported like
UNMANAGED. Drift never deletes. List drift is counted and exited on separately
from pack drift. Both flags absent -> behavior is exactly as it was before.

Live read path (NOT implemented — this drift check never touches a tenant): live
List content would come from the XSOAR-compatible content API on the
`api-<tenant>` host — the `/xsoar/public/v1` base — via its
lists endpoint, into the same {tenant: {list_id: content}} shape. That endpoint
must be verified against a live tenant before any live mode ships.

Usage:
    python scripts/drift_check.py --state fixtures/installed_state.sample.yml
    python scripts/drift_check.py --state ... --ring prod
    python scripts/drift_check.py --state ... --json
    python scripts/drift_check.py --state fixtures/installed_state.sample.yml \
        --lists-state fixtures/lists_state.sample.yml \
        --pinned-lists fixtures/pinned_lists.sample.yml
"""
import argparse
import json
import sys
from pathlib import Path

from resolve import (resolve, owned_pins, load, deep_merge, FLEET, all_tenants,
                     tenants_in_ring, ResolveError)


def registered_prefixes():
    return load(FLEET / "defaults.yml").get("customer_namespaces") or []


def _is_customer(pack_id, prefixes):
    return any(pack_id.startswith(p) for p in prefixes)


def diff_content(expected, live, prefix=""):
    """Recursively diff live List content against expected, key by key.

    Returns a list of {"key": <dotted path>, "kind": ..., "expected": ..., "live": ...}
    with one entry per drifted leaf:

        changed     — both sides carry the key, with different values
        missing     — expected (pinned or Config Overlay) but absent live
        unexpected  — live but neither pinned nor overlaid

    Two mappings recurse; anything else (scalar, list, or a type change) compares
    whole, matching deep_merge's "a list means this exact list" semantics. A
    missing or unexpected subtree is ONE entry at its root, not one per leaf.
    """
    out = []
    if not isinstance(expected, dict) or not isinstance(live, dict):
        if expected != live:
            out.append({"key": prefix or "(root)", "kind": "changed",
                        "expected": expected, "live": live})
        return out
    for key in expected:
        path = f"{prefix}.{key}" if prefix else key
        if key not in live:
            out.append({"key": path, "kind": "missing",
                        "expected": expected[key], "live": None})
        else:
            out.extend(diff_content(expected[key], live[key], path))
    for key in live:
        if key not in expected:
            path = f"{prefix}.{key}" if prefix else key
            out.append({"key": path, "kind": "unexpected",
                        "expected": None, "live": live[key]})
    return out


def check_lists(tenant_name, r, lists_state, pinned_lists, prefixes):
    """List-content rows for one tenant: live vs `pinned ⊕ Config Overlay`.

    `pinned_lists` is keyed by RING (Pins are per Ring, so the baseline is too);
    `lists_state` by tenant, keyed exactly like `--state`. The candidate set is
    every List the ring's pinned packs ship plus every List the tenant's Config
    Overlay patches — a List the fleet expects but the tenant does not carry is
    LIST MISSING, and a live List in neither is unmanaged (reported, never
    deleted). A tenant the fixture does not cover at all was simply not read:
    it yields no rows rather than a fleet of LIST MISSING.
    """
    if tenant_name not in lists_state:
        return [], []
    live_lists = lists_state.get(tenant_name) or {}
    if r["ring"] not in pinned_lists:
        # Falling back to an empty baseline here would reclassify every
        # fleet-owned List as unmanaged — a fixture typo would silently hide
        # exactly the drift this check exists to surface. Hard error, like
        # resolve.py on an unpinned pack.
        raise ResolveError(
            f"tenant '{tenant_name}' is covered by --lists-state but its ring "
            f"'{r['ring']}' has no entry in --pinned-lists"
        )
    baseline = pinned_lists.get(r["ring"]) or {}
    overlay = r.get("config_overlay") or {}

    rows = []
    for list_id in sorted(set(baseline) | set(overlay)):
        expected = deep_merge(baseline.get(list_id) or {}, overlay.get(list_id) or {})
        if list_id not in live_lists:
            rows.append({"list": list_id, "status": "LIST MISSING",
                         "expected": expected, "keys": []})
            continue
        keys = diff_content(expected, live_lists[list_id])
        rows.append({"list": list_id,
                     "status": "LIST DRIFT" if keys else "ok",
                     "keys": keys})

    unmanaged = sorted(
        lid for lid in live_lists
        if lid not in baseline and lid not in overlay
        and not _is_customer(lid, prefixes)
    )
    return rows, unmanaged


def check(tenant_name, state, prefixes, lists_state=None, pinned_lists=None):
    r = resolve(tenant_name)
    pinned = owned_pins(r)
    installed = state.get(tenant_name, {})

    rows = []
    for pid, want in pinned.items():
        have = installed.get(pid)
        status = "MISSING" if have is None else ("DRIFT" if have != want else "ok")
        rows.append({"pack": pid, "pinned": want, "installed": have, "status": status})

    # installed but not resolved and not customer-owned -> UNMANAGED (report only)
    unmanaged = sorted(
        pid for pid in installed
        if pid not in pinned and not _is_customer(pid, prefixes)
    )
    drift = [row for row in rows if row["status"] != "ok"]
    report = {"tenant": tenant_name, "ring": r["ring"], "rows": rows,
              "drift": drift, "unmanaged": unmanaged}

    # List-content drift is opt-in via the two fixtures; without them the report
    # keeps exactly the shape (and the output) it had before that feature.
    if lists_state is not None:
        list_rows, unmanaged_lists = check_lists(
            tenant_name, r, lists_state, pinned_lists, prefixes)
        report["list_rows"] = list_rows
        report["list_drift"] = [row for row in list_rows if row["status"] != "ok"]
        report["unmanaged_lists"] = unmanaged_lists
    return report


def _key_detail(k):
    """One drifted key as a line of prose; values render as JSON (true/false)."""
    if k["kind"] == "missing":
        return f"missing live  (expected {json.dumps(k['expected'])})"
    if k["kind"] == "unexpected":
        return f"unexpected live {json.dumps(k['live'])}  (not pinned, not overlaid)"
    return f"live {json.dumps(k['live'])} ≠ expected {json.dumps(k['expected'])}"


def print_human(report):
    sym = {"ok": "  ✓", "DRIFT": "  ✗", "MISSING": "  ?"}
    print(f"\n{report['tenant']}  (ring={report['ring']})")
    for row in report["rows"]:
        detail = ""
        if row["status"] == "DRIFT":
            detail = f"   installed {row['installed']}  ≠  pinned {row['pinned']}"
        elif row["status"] == "MISSING":
            detail = f"   not installed  (pinned {row['pinned']})"
        print(f"{sym[row['status']]} {row['pack']:<34}{detail}")
    for pid in report["unmanaged"]:
        print(f"  ~ {pid:<34}   installed but not resolved (unmanaged — not deleted here)")
    for row in report.get("list_rows", []):
        label = f"LIST {row['list']}"
        if row["status"] == "ok":
            print(f"  ✓ {label:<34}")
        elif row["status"] == "LIST MISSING":
            print(f"  ? {label:<34}   not present on tenant "
                  f"(expected pinned ⊕ Config Overlay)")
        else:
            print(f"  ✗ {label:<34}   {len(row['keys'])} key(s) drifted")
            for k in row["keys"]:
                print(f"        {k['key']}: {_key_detail(k)}")
    for lid in report.get("unmanaged_lists", []):
        print(f"  ~ LIST {lid:<29}   present but not fleet-owned "
              f"(unmanaged — not deleted here)")
    if not report["drift"] and not report.get("list_drift"):
        print("    → in sync")


def main():
    ap = argparse.ArgumentParser(
        description="Detect pack-level drift per tenant, and — with the two List "
                    "fixtures — config-List content drift vs pinned ⊕ Config Overlay.")
    ap.add_argument("--state", required=True, help="YAML of installed versions per tenant")
    ap.add_argument("--lists-state", help="YAML of live List content per tenant "
                                          "(requires --pinned-lists)")
    ap.add_argument("--pinned-lists", help="YAML of the pinned packs' List content "
                                           "per ring (requires --lists-state)")
    ap.add_argument("--ring", help="limit to tenants in this ring")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    # The two List fixtures are the two halves of one comparison (live content vs
    # `pinned ⊕ Config Overlay`); neither half alone can be checked.
    if bool(args.lists_state) != bool(args.pinned_lists):
        ap.error("--lists-state and --pinned-lists must be given together "
                 "(live List content is compared against the pinned baseline)")

    state = load(Path(args.state))
    lists_state = load(Path(args.lists_state)) or {} if args.lists_state else None
    pinned_lists = load(Path(args.pinned_lists)) or {} if args.pinned_lists else None
    prefixes = registered_prefixes()

    try:
        tenants = tenants_in_ring(args.ring) if args.ring else all_tenants()
        reports = [check(t, state, prefixes, lists_state, pinned_lists) for t in tenants]
    except ResolveError as e:
        sys.exit(f"ERROR: {e}")

    total_drift = sum(len(r["drift"]) for r in reports)
    total_list_drift = sum(len(r.get("list_drift", [])) for r in reports)

    if args.json:
        payload = {"reports": reports, "total_drift": total_drift}
        if lists_state is not None:
            payload["total_list_drift"] = total_list_drift
        print(json.dumps(payload, indent=2))
    else:
        for rep in reports:
            print_human(rep)
        print("\n" + "═" * 56)
        if total_drift or total_list_drift:
            counts = f"{total_drift} pack(s)"
            if lists_state is not None:
                counts += f", {total_list_drift} List(s)"
            print(f"  DRIFT DETECTED: {counts} out of sync across the fleet.")
        else:
            print("  Fleet in sync — no drift.")
        print("═" * 56 + "\n")

    sys.exit(1 if (total_drift or total_list_drift) else 0)


if __name__ == "__main__":
    main()
