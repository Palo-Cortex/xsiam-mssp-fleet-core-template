# Fleet operator runbook

Every task is a reviewed diff — no console-only actions. Composition (which
packs) lives in `defaults.yml`/`groups`/`tenants`; versions live per ring in
`fleet/pins/<ring>.yml`.

## Add a new managed tenant
1. Create `fleet/tenants/<tenant>.yml` (copy the closest profile). Set `ring`,
   `credentials_ref`, and either `groups:` (managed as a group) or `extras:`
   (self-managed). Ring membership is just this `ring:` field — there is no
   rings.yml.
2. Ensure every pack the tenant composes is pinned in `fleet/pins/<ring>.yml`
   (add pins if you introduced new packs) — `resolve.py` errors on any unpinned pack.
3. Store its creds as one packed secret `<CREDENTIALS_REF>_CREDS` (JSON:
   `{"base_url":"…","api_key":"…","auth_id":"…"}`) in the ring's Environment.
   (The legacy flat trio `<REF>_BASE_URL/_API_KEY/_AUTH_ID` still works.)
   `scripts/load_creds.py <tenant>` is what CI uses to resolve them.
   Note: the `base_url` host must contain the tenant's name (or its `host_token:`
   override) — the converge hard-fails on a host mismatch to prevent deploying to
   the wrong tenant.
4. `resolve.py <tenant>` to sanity-check, then merge — `converge` deploys the ring.

## Add a group bundle / a vendor pack to a group
1. Create `fleet/groups/<group>.yml` (or edit one) listing pack **IDs** under `extras:`.
2. Add a pin for each new pack ID in every ring pin file that a subscriber lives in.
3. Subscribe tenants via `- <group>` under their `groups:`. One edit, fleet-wide.

## Promote a version (dev → qa → prod)
1. Author/bump the version in `fleet/pins/dev.yml`; merge → `converge` deploys dev.
2. Run **promote** (`promote.py --pack <id> --to qa`, or the workflow) → a PR
   copying the pin into `qa.yml`.
3. The PR must pass **ring-gate**: version present in the source ring (no-skip),
   soaked ≥ `min_soak_days` (from git history), and within a change window for prod.
4. Merge → `converge` deploys qa. Repeat `--to prod` (prod merges under the
   `fleet-prod` Environment approval). The artifact is identical across rings;
   only the pin advances.

## Package tenant-authored content (author in dev, validate in qa)
When custom content is built in the dev tenant's console (tenant-first), the
dev tenant keeps the raw content and never composes the packaged pack — qa is
the first ring that actually deploys it. Full worked example with per-step
file changes: [custom-pack-lifecycle.md](custom-pack-lifecycle.md).

1. Author in the dev tenant, then pull the content down into pack source
   (`demisto-sdk download -o Packs/<PackId> ...`), set `pack_metadata.json`
   (id, `currentVersion`), and regenerate the catalog
   (`python scripts/mssp_catalog.py --write`). PR → merge publishes the
   release zip (`release.yml`).
2. In a follow-up PR, pin the version in `fleet/pins/dev.yml` **without
   composing it on any dev tenant**. This deploys nothing — it is deliberate:
   the pin must enter at dev because ring-gate's no-skip rule only lets
   versions reach `qa.yml` by promotion from `dev.yml`. MSSP-authored packs
   are soak-exempt out of the head ring (soak on an artifact dev never runs is
   a dead timer), so promotion to qa is immediate regardless of qa's
   `min_soak_days` — dev soak gates third-party packs only. The qa → prod
   promotion then soaks normally (qa actually runs the pack), alongside
   review/sign-off (CODEOWNERS, the `fleet-prod` Environment) and the change
   window.
3. Promote (`promote.py --pack <PackId> --to qa`) and, in the same PR, compose
   the pack on a qa tenant (its `extras:` or a group it subscribes to).
   Merge → `converge` deploys qa — the first tenant to receive the pack.
4. The dev tenant intentionally diverges: it holds the content unpacked
   (console-authored), qa/prod hold it as the pinned pack. Drift never
   reconciles this — unpacked custom content is not a pack, so the drift job
   does not see it. Do not later compose the pack on the dev tenant: uploading
   it over the console originals risks ID collisions.

## Hold one tenant back
Set the version under that tenant's `pin_overrides:` (e.g. prodcustomer01 holds
`soc-optimization-unified: 3.9.4`). It wins over the ring pin for that tenant and
is not reported as drift.

## Flip an action live for a tenant
Framework config Lists are fleet-owned and tuned only through a **Config Overlay**
 — never a console edit, which convergence overwrites.

1. Edit `fleet/config_overlays/<tenant>.yml` (create it if absent) and set the
   action's key under its List, e.g. `shadow_mode: false` for `isolate_host`
   under `SOCFrameworkActions_V3`. Patch only what you are changing — the pinned
   artifact supplies the rest. Fleet-wide tuning goes in
   `fleet/config_overlays/defaults.yml`; the tenant layer wins on conflicts.
   Only List ids registered in `defaults.yml` → `config_overlay_lists` are legal.
2. `resolve.py <tenant>` shows the composed Config Overlay; `deploy_tenant.py
   <tenant>` shows which List ids the converge will patch.
3. PR → merge. `converge` merges the patch into the staged copy of whichever
   pinned pack carries that List and uploads it, so the deployed List is
   `pinned ⊕ Config Overlay`. No pin changes. If no deployed pack carries the
   List, the run is an Incomplete Converge — fix the id and re-run.
4. Rollback is `git revert` of the overlay commit: the next converge redeploys
   the pinned value.

## Respond to drift
1. The **drift** workflow (`workflow_dispatch`; enable its commented-out
   schedule when live) opens an issue listing `tenant / pack / installed ≠ resolved`.
2. Re-converge the tenant (re-run `converge` for its ring), or if the difference
   is intentional, set a `pin_overrides` entry so it is no longer drift.
3. Packs shown as **UNMANAGED** are installed but not resolved (a Marketplace pack
   or an orphan) — convergence deletes orphans within the bounded set; drift never deletes.
4. **LIST** rows are config-List content drift: a tenant's live List differs from
   `pinned ⊕ Config Overlay`, i.e. someone hand-edited it in the
   console. Re-converge to reset it; if the value should stick, land it as a
   Config Overlay change instead. Run it by adding the two List fixtures:
   `--lists-state fixtures/lists_state.sample.yml --pinned-lists fixtures/pinned_lists.sample.yml`.

## Remove a pack from a tenant/ring
Composition decides membership; pins only supply versions. So removal is a
**composition** edit:

1. Delete the pack ID from wherever it is composed — `defaults.yml` `base:`
   (fleet-wide), a group's `extras:`, or the tenant's `extras:`. That alone is
   what removes it: on the next `converge`, the pack drops out of the resolved
   set, becomes an orphan, and is deleted via the XSIAM pack-delete API — but
   only if it is in the bounded deletable set (upstream catalog ∪ MSSP-authored
   ids). Anything else is reported, never deleted.
2. Then clean up its now-unused entries in `fleet/pins/<ring>.yml`. This is
   hygiene only — a pin nothing composes is never read and deploys nothing.

Do **not** delete the pin first: a pack that is still composed anywhere in the
ring but has no pin is a hard `resolve.py` error, so ring-gate and converge
fail before anything is removed.

## Onboard a customer-owned (custom) pack
Full end-to-end (both repos' responsibilities, qa → prod on the customer
side, ownership transfer): [customer-pack-lifecycle.md](customer-pack-lifecycle.md).

1. The customer authors it in their overlay repo with the registered prefix
   (e.g. `acme-`) and deploys it with their own credentials.
2. Reference it here only if useful, under the tenant's `custom:` with
   `owner: customer`. The fleet never installs, versions, overwrites, or
   drift-checks it. `namespace_lint.py` enforces that referenced customer ids
   carry a registered prefix.
