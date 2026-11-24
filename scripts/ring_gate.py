#!/usr/bin/env python3
"""
ring_gate.py — the single path-aware required check that enforces ring policy.

Branch protection can't vary required checks by path, so ONE check reads a PR's
diff, works out which ring's pin file changed, and enforces that ring's policy
from fleet/policies.yml as code:

  no-skip        a version entering qa/prod must ALREADY be pinned in its source
                 ring ON THE BASE BRANCH (dev for qa, qa for prod) — you cannot
                 skip a ring, and you cannot satisfy no-skip by adding the version
                 to the source ring in the same PR.
  soak           that version must have sat in the source ring's pin file ON THE
                 BASE BRANCH for at least `min_soak_days`. The clock is the
                 base branch's FIRST-PARENT history (the moment the version
                 landed on main), never a PR's own commit timestamps — those are
                 author-controlled and would let a backdated or long-lived branch
                 arrive "pre-soaked".
                 MSSP-authored packs (source under Packs/) are exempt ONLY when
                 promoting out of a head ring (dev -> qa): tenant-first content
                 is never installed in dev, so head-ring soak is a dead timer.
                 Once the pack is running in qa, soak is meaningful and the
                 qa -> prod gate applies to every pack alike.
  change window  a prod pin change may only MERGE inside a declared UTC window.
                 Re-checked at merge time (merge_group) and again by converge
                 before it deploys a windowed ring (--converge-window).
  rollback       break-glass: with the `rollback` label on the PR, a gated ring
                 may return to a version it ALREADY RAN (present in that ring's
                 own base-branch history). No-skip, soak and the change window
                 are waived for such changes — the version already proved itself
                 in this ring. Review/approval (CODEOWNERS, the ring's
                 Environment) still applies.

TRUST: run this file from the BASE branch, judging the PR working tree via
--repo. Policy (policies.yml), the MSSP-authored id set and the source ring's
pins are all read from the base ref, so nothing in the PR's own diff can relax
the gate that judges it. ring-gate.yml extracts this script from the base ref
before running it for the same reason.

Human layers sit ALONGSIDE this, not inside it: CODEOWNERS decides who reviews
each pin file; GitHub Environments decide who clicks deploy.

Dev has no source ring, so dev pin changes always pass. Pin REMOVALS are subject
only to the change window (there is no source version to soak).

The "local" sentinel (deploy an MSSP-authored pack from Packs/ source, see
resolve.py) is allowed ONLY in head rings: a `local` pin entering a gated ring is
a hard violation.

Exit code: 0 pass, 1 on any violation.

Usage:
    python ring_gate.py --repo . --base origin/main
    python ring_gate.py --repo . --base origin/main --labels rollback
    python ring_gate.py --repo . --base origin/main --now 2026-07-21T15:00:00Z
    # converge-time window re-check for one ring between two commits:
    python ring_gate.py --repo . --converge-window prod --before <sha> --after <sha>
"""
import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
FLEET = REPO / "fleet"
ROLLBACK_LABEL = "rollback"
_WD = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5, "Sun": 6}


def set_repo(path):
    """Point the gate at the working tree it judges (may differ from where this
    script lives — CI runs the base branch's copy against the PR checkout)."""
    global REPO, FLEET
    REPO = Path(path).resolve()
    FLEET = REPO / "fleet"


def sh(*args):
    return subprocess.run(["git", "-C", str(REPO), *args],
                          capture_output=True, text=True).stdout


def policies():
    return yaml.safe_load(open(FLEET / "policies.yml"))


def policies_at(ref):
    """Ring policy as it exists on `ref` (the base branch), NOT the PR head.

    Otherwise a single PR could lower min_soak_days / drop a change window in
    policies.yml AND promote in the same diff. Falls back to the working tree
    only when the file can't be read from ref (local dev, first commit)."""
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


def mssp_authored_ids(ref):
    """Pack ids whose SOURCE lives in this repo, read at `ref` (the base branch)
    so a PR can't soak-exempt a pack by adding Packs/ metadata in its own diff."""
    ids = set()
    for line in sh("ls-tree", "-r", "--name-only", ref, "--", "Packs").splitlines():
        parts = line.strip().split("/")
        if len(parts) == 3 and parts[2] == "pack_metadata.json":
            try:
                ids.add(json.loads(sh("show", f"{ref}:{line.strip()}")).get("id") or parts[1])
            except Exception:  # noqa: BLE001 — unreadable metadata: dir name is the id
                ids.add(parts[1])
    return ids


def ring_history(ring, ref):
    """[(sha, committed_at, pins)] for the ring's pin file along `ref`'s
    FIRST-PARENT history, oldest first. On a protected main every first-parent
    commit is a merge/squash/rebase GitHub created at merge time, so its
    committer date is when the change actually landed."""
    log = sh("log", "--first-parent", "--reverse", "--format=%H|%cI", ref,
             "--", f"fleet/pins/{ring}.yml")
    out = []
    for line in log.splitlines():
        if "|" not in line:
            continue
        sha, iso = line.split("|", 1)
        out.append((sha, datetime.fromisoformat(iso.strip()), pins_at(sha, ring)))
    return out


def soak_days(source_ring, pack, version, now, ref):
    """Days the source ring has CONTINUOUSLY carried pack@version on `ref`.

    The clock starts at the first-parent commit that (re)introduced the version
    and resets if the pin moved away and came back. None if `ref` does not
    currently carry that version (treated as unsoaked)."""
    started = None
    for _sha, when, pins in ring_history(source_ring, ref):
        if pins.get(pack) == version:
            if started is None:
                started = when
        else:
            started = None
    if started is None:
        return None
    return (now - started).total_seconds() / 86400.0


def previously_pinned(ring, pack, version, ref):
    """True if `ring` itself carried pack@version at some point on `ref`."""
    return any(p.get(pack) == version for _s, _w, p in ring_history(ring, ref))


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


def evaluate(ring, pol, merge_base, now, labels=(), trust_rollback=False):
    """Return (violations, notes) for one changed ring pin file.

    `labels` are the PR's labels; `trust_rollback` treats a return to a
    previously-run version as a rollback without the label (merge_group runs,
    where labels aren't visible but the PR check already required them)."""
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

    # The source ring as it is on the BASE branch: a version only counts as
    # "in the source ring" once that promotion has itself merged.
    src = pins_at(merge_base, source)
    rollback_ok = trust_rollback or ROLLBACK_LABEL in set(labels or ())
    source_is_head = (pol["rings"].get(source) or {}).get("source") is None
    soak_exempt = mssp_authored_ids(merge_base) if source_is_head else set()
    gated_changes = 0      # changes that are NOT rollbacks (subject to the window)
    for pack, version in sorted(changed.items()):
        if version == "local":
            violations.append(
                f"{ring}: {pack} -> 'local' — the local-source sentinel is only "
                f"allowed in head rings; promote a released version instead")
            continue
        # break-glass rollback: back to a version this ring already ran
        if src.get(pack) != version and previously_pinned(ring, pack, version, merge_base):
            if rollback_ok:
                notes.append(f"{ring}: {pack} -> {version} ROLLBACK (previously run "
                             f"in {ring}) — no-skip/soak/window waived ⚠")
                continue
            violations.append(
                f"{ring}: {pack} -> {version} is not in source ring '{source}' but "
                f"{ring} ran it before — if this is a rollback, add the "
                f"'{ROLLBACK_LABEL}' label to the PR")
            continue
        gated_changes += 1
        # no-skip: the version must already be in the source ring on base
        if src.get(pack) != version:
            violations.append(
                f"{ring}: {pack} -> {version} not present in source ring '{source}' "
                f"on the base branch (has {src.get(pack, 'nothing')}); promote "
                f"through {source} first and merge that before promoting here")
            continue
        if pack in soak_exempt:
            notes.append(f"{ring}: {pack} {version} MSSP-authored — soak exempt ✓")
            continue
        days = soak_days(source, pack, version, now, merge_base)
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

    # change window applies to any non-rollback change (promotion or removal)
    if ringpol.get("require_change_window") and (gated_changes or removed):
        if in_change_window(ringpol.get("change_windows"), now):
            notes.append(f"{ring}: within change window ✓")
        else:
            violations.append(
                f"{ring}: change lands outside the allowed change window "
                f"({now.isoformat()})")

    for pack in removed:
        notes.append(f"{ring}: removing {pack} — orphan-deletes on next converge")

    return violations, notes


def converge_window(ring, pol, before, after, now):
    """(allowed, reason) for converge deploying `ring` for the push before..after.

    Allowed when the ring has no window requirement, when `now` is inside a
    window, or when every pin change in the push returns the ring to a version
    it already ran (a rollback that ring-gate approved under the label)."""
    ringpol = (pol.get("rings") or {}).get(ring) or {}
    if not ringpol.get("require_change_window"):
        return True, f"{ring}: no change window required"
    if in_change_window(ringpol.get("change_windows"), now):
        return True, f"{ring}: within change window"
    if before:
        old, new = pins_at(before, ring), pins_at(after, ring)
        changed = {p: v for p, v in new.items() if old.get(p) != v}
        removed = [p for p in old if p not in new]
        if changed and not removed and all(
                previously_pinned(ring, p, v, before) for p, v in changed.items()):
            return True, f"{ring}: rollback-only push — window waived"
    return False, (f"{ring}: outside the change window ({now.isoformat()}); "
                   f"re-run converge for this ring inside a window")


def main():
    ap = argparse.ArgumentParser(description="Enforce ring promotion policy on a PR diff.")
    ap.add_argument("--repo", default=str(REPO),
                    help="working tree to judge (default: this script's repo)")
    ap.add_argument("--base", default="origin/main", help="base ref to diff against")
    ap.add_argument("--labels", default="",
                    help="comma-separated PR labels (e.g. 'rollback')")
    ap.add_argument("--merge-group", action="store_true",
                    help="merge-queue run: labels aren't visible, so a return to a "
                         "previously-run version is accepted as a rollback")
    ap.add_argument("--now", help="override current time (ISO8601, for testing)")
    ap.add_argument("--converge-window", metavar="RING",
                    help="converge-time check: may RING deploy now?")
    ap.add_argument("--before", help="with --converge-window: pushed-from sha")
    ap.add_argument("--after", default="HEAD", help="with --converge-window: pushed sha")
    args = ap.parse_args()
    set_repo(args.repo)

    now = (datetime.fromisoformat(args.now.replace("Z", "+00:00"))
           if args.now else datetime.now(timezone.utc))

    if args.converge_window:
        ok, reason = converge_window(args.converge_window, policies_at(args.after),
                                     args.before, args.after, now)
        print(("✓ " if ok else "✗ ") + reason)
        sys.exit(0 if ok else 1)

    merge_base = sh("merge-base", args.base, "HEAD").strip() or args.base
    rings = changed_rings(merge_base)
    pol = policies_at(args.base)   # judge by the policy on the base branch, not this PR
    labels = [lbl.strip() for lbl in args.labels.split(",") if lbl.strip()]

    if not rings:
        print("ring-gate: no pin files changed — nothing to enforce.")
        return

    all_violations, all_notes = [], []
    for ring in rings:
        v, n = evaluate(ring, pol, merge_base, now, labels=labels,
                        trust_rollback=args.merge_group)
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
