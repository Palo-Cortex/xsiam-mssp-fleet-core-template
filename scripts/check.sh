#!/usr/bin/env bash
# ============================================================================
# check.sh — the fast, offline subset of ring-gate, runnable anytime.
# ----------------------------------------------------------------------------
# Validates the fleet manifests as a pure function of the working tree:
#   * resolve.py --ring for all rings  (fails on any composed-but-unpinned pack)
#   * namespace_lint.py                (fleet side of the namespace contract)
#   * mssp_catalog.py --check          (catalog in sync with Packs/ metadata)
#   * config_overlay_lint.py           (Config Overlay ids + patch shape)
# No network, no git history. ring_gate.py (soak / no-skip / change-window) and
# pytest stay in CI — they need a PR base ref and are slower.
# Wired as the pre-commit hook via .githooks/pre-commit; ring-gate on the PR
# remains the authoritative check.
# ============================================================================
set -euo pipefail
cd "$(dirname "$0")/.."

PY=python3
[ -x .venv/bin/python ] && PY=.venv/bin/python

# Rings are derived from fleet/pins/*.yml — a ring exists iff its pin file
# does, and every ring must have at least one tenant (resolve errors otherwise).
# To remove a ring, delete its pin file AND its tenants together.
for pin in fleet/pins/*.yml; do
  ring=$(basename "$pin" .yml)
  "$PY" scripts/resolve.py --ring "$ring" --json > /dev/null
done
echo "✓ resolve — every tenant in every ring resolves against its ring's pins"

"$PY" scripts/namespace_lint.py
"$PY" scripts/mssp_catalog.py --check
"$PY" scripts/config_overlay_lint.py
