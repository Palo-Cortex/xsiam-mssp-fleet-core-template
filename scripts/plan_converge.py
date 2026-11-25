#!/usr/bin/env python3
"""
plan_converge.py — which tenants does this converge run deploy, and how?

Called by converge.yml's `plan` job. Emits JSON on stdout:

    {
      "canary":       [entry, ...],   # deployed first (tenants with canary: true)
      "fleet":        [entry, ...],   # deployed after every canary succeeded
      "max_parallel": 5,              # smallest max_parallel of the rings in "fleet"
      "blocked":      ["prod: outside the change window ..."],
      "notes":        ["prod: outside the change window — dry-run only, ..."]
    }
    entry = {"tenant", "ring", "credentials_ref", "environment"}

Affected tenants, for a push:
  * every tenant in a ring whose fleet/pins/<ring>.yml changed;
  * rings / tenants pinning a changed Packs/<dir>/ pack to `local`;
  * tenants a changed Config Overlay layer composes into (defaults.yml -> all).
The diff covers the WHOLE push (before..after), not just its last commit — a
rebase-merge or multi-commit push otherwise skips rings changed earlier in it.
For workflow_dispatch: every tenant of the chosen ring.

Change windows are enforced HERE as well as on the PR: a ring with
require_change_window is dropped (and reported in "blocked") when converge runs
outside its window, unless the push only rolls the ring back to versions it
already ran, or a dispatch sets --break-glass.
With --dry-run (FLEET_LIVE off) nothing is deployed, so nothing is withheld:
every tenant gets its dry-run plan and the window is reported in "notes".

Each entry carries the tenant's credentials_ref and its ring's Environment
(fleet/policies.yml rings.<ring>.environment) so the per-tenant job maps only
that tenant's secret and runs under that Environment's protection rules.

Usage:
    python scripts/plan_converge.py --event push --before <sha> --after <sha>
    python scripts/plan_converge.py --event workflow_dispatch --ring prod [--break-glass]
"""
import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

import ring_gate

REPO = Path(__file__).resolve().parents[1]
ZERO_SHA = "0" * 40
DEFAULT_MAX_PARALLEL = 20


def git(*args):
    return subprocess.run(["git", "-C", str(REPO), *args],
                          capture_output=True, text=True)


def load(path):
    with open(path) as fh:
        return yaml.safe_load(fh) or {}


def resolve_before(before, after):
    """A usable 'pushed-from' commit: the given one if it exists locally, else
    after's first parent (new branch / force-push / shallow history)."""
    if before and before != ZERO_SHA and git("cat-file", "-e", f"{before}^{{commit}}").returncode == 0:
        return before
    r = git("rev-parse", f"{after}^")
    return r.stdout.strip() if r.returncode == 0 else None


def diff_names(before, after, *paths):
    if not before:
        return []
    out = git("diff", "--name-only", before, after, "--", *paths).stdout
    return [line for line in out.splitlines() if line.strip()]


def tenants():
    return {f.stem: load(f) for f in sorted((REPO / "fleet" / "tenants").glob("*.yml"))}


def affected(event, ring, before, after, profiles):
    """Ordered list of tenant names this run converges."""
    if event == "workflow_dispatch":
        return [t for t, p in profiles.items() if p.get("ring") == ring]

    rings = sorted({Path(line).stem for line in diff_names(before, after, "fleet/pins")})
    extra = []

    changed_dirs = sorted({line.split("/")[1] for line in diff_names(before, after, "Packs")
                           if line.count("/") >= 1})
    ids = set()
    for d in changed_dirs:
        try:
            ids.add(json.loads((REPO / "Packs" / d / "pack_metadata.json").read_text()).get("id") or d)
        except Exception:  # noqa: BLE001 — dir deleted/renamed or metadata unreadable
            ids.add(d)
    for f in sorted((REPO / "fleet" / "pins").glob("*.yml")):
        pins = load(f).get("pins") or {}
        if f.stem not in rings and any(pins.get(i) == "local" for i in ids):
            rings.append(f.stem)
    for t, p in profiles.items():
        if any((p.get("pin_overrides") or {}).get(i) == "local" for i in ids):
            extra.append(t)

    layers = [Path(line).stem for line in diff_names(before, after, "fleet/config_overlays")
              if line.endswith(".yml")]
    if "defaults" in layers:
        rings += [f.stem for f in sorted((REPO / "fleet" / "pins").glob("*.yml"))
                  if f.stem not in rings]
    extra += [t for t in layers if t != "defaults" and t in profiles]

    seen = []
    for r in rings:
        seen += [t for t, p in profiles.items() if p.get("ring") == r and t not in seen]
    seen += [t for t in extra if t not in seen]
    return seen


def plan(event, ring=None, before=None, after="HEAD", break_glass=False, now=None,
         dry_run=False):
    now = now or datetime.now(timezone.utc)
    ring_gate.set_repo(REPO)
    pol = load(REPO / "fleet" / "policies.yml")
    rings_pol = pol.get("rings") or {}
    profiles = tenants()
    before = resolve_before(before, after) if event != "workflow_dispatch" else None
    names = affected(event, ring, before, after, profiles)

    blocked, notes, allowed_rings = [], [], {}
    for r in sorted({profiles[t].get("ring") for t in names}):
        if event == "workflow_dispatch" and break_glass:
            allowed_rings[r] = True
            continue
        ok, msg = ring_gate.converge_window(r, pol, before, after, now)
        if ok or dry_run:
            allowed_rings[r] = True
            if not ok:
                notes.append(f"{msg.split(';')[0]} — dry-run only, so planned anyway "
                             f"(a live converge would be withheld)")
        else:
            allowed_rings[r] = False
            blocked.append(msg)

    canary, fleet = [], []
    for t in names:
        p = profiles[t]
        r = p.get("ring")
        if not allowed_rings.get(r):
            continue
        entry = {"tenant": t, "ring": r,
                 "credentials_ref": p.get("credentials_ref") or t.upper(),
                 "environment": (rings_pol.get(r) or {}).get("environment") or ""}
        (canary if p.get("canary") else fleet).append(entry)

    caps = [(rings_pol.get(e["ring"]) or {}).get("max_parallel") for e in fleet]
    caps = [int(c) for c in caps if c]
    return {"canary": canary, "fleet": fleet,
            "max_parallel": min(caps) if caps else DEFAULT_MAX_PARALLEL,
            "blocked": blocked, "notes": notes}


def main():
    ap = argparse.ArgumentParser(description="Plan a converge run.")
    ap.add_argument("--event", required=True, choices=["push", "workflow_dispatch"])
    ap.add_argument("--ring", help="workflow_dispatch: ring to converge")
    ap.add_argument("--before", help="push: github.event.before")
    ap.add_argument("--after", default="HEAD", help="push: github.sha")
    ap.add_argument("--break-glass", action="store_true",
                    help="workflow_dispatch: deploy a windowed ring outside its window")
    ap.add_argument("--dry-run", action="store_true",
                    help="converge won't deploy (FLEET_LIVE off): don't withhold for windows")
    ap.add_argument("--now", help="override current time (ISO8601, testing)")
    args = ap.parse_args()
    if args.event == "workflow_dispatch" and not args.ring:
        ap.error("--ring is required for workflow_dispatch")
    now = datetime.fromisoformat(args.now.replace("Z", "+00:00")) if args.now else None
    print(json.dumps(plan(args.event, args.ring, args.before, args.after,
                          args.break_glass, now, dry_run=args.dry_run)))


if __name__ == "__main__":
    sys.exit(main())
