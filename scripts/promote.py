#!/usr/bin/env python3
"""
promote.py — copy a pin FORWARD from a ring into the next ring's pin file.

Promotion is the only way a version advances between rings. This
does the mechanical edit — copy source_ring[pack] into target_ring's pin file —
and leaves the gating to the ring gate on the resulting PR. A version can only be
promoted if it is currently pinned in the source ring (no inventing versions).

The edit is line-based so the pin file's header comments survive (a YAML
round-trip would strip them).

Usage:
    python scripts/promote.py --pack VirusTotal --to qa
    python scripts/promote.py --pack VirusTotal --version 2.7.22 --to qa
"""
import argparse
import re
import sys
from pathlib import Path

import yaml

FLEET = Path(__file__).resolve().parents[1] / "fleet"


def policies():
    return yaml.safe_load(open(FLEET / "policies.yml"))


def pins(ring):
    f = FLEET / "pins" / f"{ring}.yml"
    return (yaml.safe_load(f.read_text()) or {}).get("pins") or {}


def set_pin(ring, pack, version):
    """Line-based upsert of `  <pack>: <version>` under pins:, preserving comments."""
    path = FLEET / "pins" / f"{ring}.yml"
    lines = path.read_text().splitlines(keepends=True)
    pat = re.compile(rf"^(\s+){re.escape(pack)}:\s*.*$")
    for i, line in enumerate(lines):
        if pat.match(line):
            indent = pat.match(line).group(1)
            lines[i] = f"{indent}{pack}: {version}\n"
            path.write_text("".join(lines))
            return "updated"
    # not present — insert right after the `pins:` line
    for i, line in enumerate(lines):
        if re.match(r"^pins:\s*$", line):
            lines.insert(i + 1, f"  {pack}: {version}\n")
            path.write_text("".join(lines))
            return "added"
    sys.exit(f"ERROR: no `pins:` block in {path}")


def main():
    ap = argparse.ArgumentParser(description="Promote a pin into the next ring.")
    ap.add_argument("--pack", required=True)
    ap.add_argument("--version", help="defaults to the source ring's current pin")
    ap.add_argument("--to", required=True, help="target ring (e.g. qa, prod)")
    args = ap.parse_args()

    pol = policies()
    ringpol = pol["rings"].get(args.to)
    if not ringpol:
        sys.exit(f"ERROR: no policy for target ring '{args.to}'")
    source = ringpol.get("source")
    if not source:
        sys.exit(f"ERROR: ring '{args.to}' is the head — nothing promotes into it")

    src_pins = pins(source)
    if args.pack not in src_pins:
        sys.exit(f"ERROR: {args.pack} is not pinned in source ring '{source}'")
    version = args.version or src_pins[args.pack]
    if args.version and src_pins[args.pack] != args.version:
        sys.exit(f"ERROR: {args.pack} is {src_pins[args.pack]} in '{source}', "
                 f"not {args.version}; you can only promote what's in the source ring")

    if pins(args.to).get(args.pack) == version:
        print(f"{args.pack} already at {version} in {args.to} — no change")
        return
    action = set_pin(args.to, args.pack, version)
    print(f"{action}: {args.pack} -> {version} in fleet/pins/{args.to}.yml "
          f"(promoted from {source})")


if __name__ == "__main__":
    main()
