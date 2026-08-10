# Config Overlays

A **Config Overlay** is the fleet-owned, per-tenant sparse patch of framework
config List values (shadow flips, thresholds, routing) applied at Convergence:
a tenant's deployed List is always `pinned ⊕ Config Overlay`.

Two layers compose here — `defaults.yml` (fleet-wide) and `<tenant>.yml` (per
tenant, wins on conflicts); an absent file is an empty layer. Each file is a
mapping of List id → sparse patch, and only ids registered in
`fleet/defaults.yml` → `config_overlay_lists` are allowed
(`scripts/config_overlay_lint.py` enforces both).

See `docs/runbook.md` ("Flip an action live for a tenant") for the checklist
and `docs/config-overlay-lifecycle.md` for the full end-to-end walkthrough.
