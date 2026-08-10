#!/usr/bin/env python3
"""
ring_gate.py — the single path-aware required check that enforces ring policy.

Branch protection can't vary required checks by path, so ONE check reads a PR's
diff, works out which ring's pin file changed, and enforces that ring's policy
from fleet/policies.yml as code:

  no-skip        a version entering qa/prod must ALREADY be pinned in its source
                 ring (dev for qa, qa for prod) — you cannot skip a ring.
  soak           that version must have sat in the source ring's pin file for at
                 least `min_soak_days`, measured from git history (not memory).
  change window  a prod pin change may only MERGE inside a declared UTC window.

Human layers sit ALONGSIDE this, not inside it: CODEOWNERS decides who reviews
each pin file; GitHub Environments decide who clicks deploy.

Dev has no source ring, so dev pin changes always pass. Pin REMOVALS are subject
only to the change window (there is no source version to soak).

The "local" sentinel (deploy an MSSP-authored pack from Packs/ source, see
resolve.py) is allowed ONLY in head rings: a `local` pin entering a gated ring is
a hard violation. Soak is meaningless for `local` — the string never changes
while the Packs/ source underneath it does — and a gated ring converging from
whatever main's Packs/ holds is not reproducible. Promote MSSP packs beyond the
head ring as real release-artifact versions.

Exit code: 0 pass, 1 on any violation. Designed to run in CI on pull_request.

Usage:
    python scripts/ring_gate.py --base origin/main
    python scripts/ring_gate.py --base origin/main --now 2026-07-21T15:00:00Z
"""
import argparse
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

FLEET = Path(__file__).resolve().parents[1] / "fleet"
_WD = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5, "Sun": 6}


def sh(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True).stdout


def policies():
    return yaml.safe_load(open(FLEET / "policies.yml"))


def policies_at(ref):
    """Ring policy as it exists on `ref` (the base branch), NOT the PR head.

    The gate must be judged by the policy already on main — otherwise a single PR
    could lower min_soak_days / drop a change window in policies.yml AND promote in
    the same diff, and the weakened policy would wave itself through. Relaxing the
    gate therefore requires a separate, already-merged policy change. Falls back to
    the working tree only when the file can't be read from ref (e.g. local dev with
    no such ref, or the very first commit)."""
    text = sh("show", f"{ref}:fleet/policies.yml")
    if text.strip():
        try:
            return yaml.safe_load(text)
        except yaml.YAMLError:
            pass
    return policies()


def pins_from_text(text):
    try:
        return (yaml.safe_load(text) or {}).get("pins") or {}
    except yaml.YAMLError:
        return {}


def pins_at(ref, ring):
    """Pins for a ring as of a git ref ({} if the file didn't exist there)."""
    return pins_from_text(sh("show", f"{ref}:fleet/pins/{ring}.yml"))


def pins_now(ring):
    f = FLEET / "pins" / f"{ring}.yml"
    return pins_from_text(f.read_text()) if f.exists() else {}


def changed_rings(merge_base):
    """Rings whose pin file changed between merge_base and the working tree."""
    out = sh("diff", "--name-only", merge_base, "--", "fleet/pins")
    rings = []
    for line in out.splitlines():
        name = Path(line).stem
        if line.strip() and name not in rings:
            rings.append(name)
    return rings


def soak_days(source_ring, pack, version, now):
    """Days since the source ring's pin file first carried pack@version.

    Walks the source file's history oldest->newest; the first commit where the
    pin equals `version` starts the clock. Returns None if never committed at
    that version (e.g. it only exists uncommitted in this PR) — treated as unsoaked.
    """
    log = sh("log", "--reverse", "--format=%H|%cI", "--", f"fleet/pins/{source_ring}.yml")
    for line in log.splitlines():
        if "|" not in line:
            continue
        sha, iso = line.split("|", 1)
        if pins_at(sha, source_ring).get(pack) == version:
            started = datetime.fromisoformat(iso.strip())
            return (now - started).total_seconds() / 86400.0
    return None


def in_change_window(windows, now):
    for w in windows or []:
        days = {_WD[d] for d in w.get("days", []) if d in _WD}
        if now.weekday() not in days:
            continue
        start = datetime.strptime(w["start"], "%H:%M").time()
        end = datetime.strptime(w["end"], "%H:%M").time()
        if start <= now.timetz().replace(tzinfo=None) <= end:
            return True
    return False


def evaluate(ring, pol, merge_base, now):
    """Return (violations, notes) for one changed ring pin file."""
    ringpol = pol["rings"].get(ring)
    violations, notes = [], []
    if not ringpol:
        return [f"ring '{ring}' has no policy in policies.yml"], notes

    source = ringpol.get("source")
    if source is None:
        notes.append(f"{ring}: head ring — pins authored freely, no gate")
        return violations, notes

    old = pins_at(merge_base, ring)
    new = pins_now(ring)
    changed = {p: v for p, v in new.items() if old.get(p) != v}      # added or bumped
    removed = [p for p in old if p not in new]

    src = pins_now(source)
    for pack, version in sorted(changed.items()):
        # "local" never enters a gated ring: soak on the sentinel string is
        # meaningless (the Packs/ source keeps changing under it) and the deploy
        # is not reproducible. MSSP packs promote as real versions.
        if version == "local":
            violations.append(
                f"{ring}: {pack} -> 'local' — the local-source sentinel is only "
                f"allowed in head rings; promote a released version instead")
            continue
        # no-skip: the version must already be in the source ring
        if src.get(pack) != version:
            violations.append(
                f"{ring}: {pack} -> {version} not present in source ring '{source}' "
                f"(has {src.get(pack, 'nothing')}); promote through {source} first")
            continue
        # soak: measured from source ring history
        days = soak_days(source, pack, version, now)
        need = ringpol.get("min_soak_days", 0)
        if days is None:
            violations.append(
                f"{ring}: {pack} {version} has no committed soak in '{source}' "
                f"(needs {need}d)")
        elif days < need:
            violations.append(
                f"{ring}: {pack} {version} soaked {days:.1f}d in '{source}', "
                f"needs {need}d")
        else:
            notes.append(f"{ring}: {pack} {version} soaked {days:.1f}d ≥ {need}d ✓")

    # change window applies to any change (promotion or removal) in a gated ring
    if ringpol.get("require_change_window") and (changed or removed):
        if in_change_window(ringpol.get("change_windows"), now):
            notes.append(f"{ring}: within change window ✓")
        else:
            violations.append(
                f"{ring}: change lands outside the allowed change window "
                f"({now.isoformat()})")

    for pack in removed:
        notes.append(f"{ring}: removing {pack} — orphan-deletes on next converge")

    return violations, notes


def main():
    ap = argparse.ArgumentParser(description="Enforce ring promotion policy on a PR diff.")
    ap.add_argument("--base", default="origin/main", help="base ref to diff against")
    ap.add_argument("--now", help="override current time (ISO8601, for testing)")
    args = ap.parse_args()

    now = (datetime.fromisoformat(args.now.replace("Z", "+00:00"))
           if args.now else datetime.now(timezone.utc))

    merge_base = sh("merge-base", args.base, "HEAD").strip() or args.base
    rings = changed_rings(merge_base)
    pol = policies_at(args.base)   # judge by the policy on the base branch, not this PR

    if not rings:
        print("ring-gate: no pin files changed — nothing to enforce.")
        return

    all_violations, all_notes = [], []
    for ring in rings:
        v, n = evaluate(ring, pol, merge_base, now)
        all_violations += v
        all_notes += n

    print(f"ring-gate: changed ring(s): {', '.join(rings)}")
    for n in all_notes:
        print(f"  · {n}")
    if all_violations:
        print(f"\n✗ {len(all_violations)} policy violation(s):")
        for v in all_violations:
            print(f"  - {v}")
        sys.exit(1)
    print("\n✓ ring policy satisfied.")


if __name__ == "__main__":
    main()
