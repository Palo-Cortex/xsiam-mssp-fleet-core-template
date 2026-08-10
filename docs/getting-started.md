# Getting started — adopting this template

This repo is a working fleet with **example everything**: four example tenants
(`devlab01`, `qacanary01`, `prodcustomer01`, `msspparent`), three example
groups, per-ring pins, and one example MSSP-authored pack. It runs green
offline with zero setup — adoption is replacing the examples with your fleet,
then flipping CI live.

Read the [README](../README.md) first for the model (composition ⊕ pins,
promotion, convergence). The [runbook](runbook.md) covers day-to-day operations
once you're set up.

## 1. Create your repo

Use this repo as a template (or fork/clone it) into your own org. Then point
the MSSP release base at the new repo in `fleet/defaults.yml`:

```yaml
mssp_catalog:
  release_base: https://github.com/YOUR-ORG/YOUR-FLEET-REPO/releases/download
```

and regenerate the committed catalog so it matches:

```bash
python scripts/mssp_catalog.py --write
```

(`ring-gate` fails any PR where `mssp_pack_catalog.json` drifts from `Packs/`
metadata + `release_base`, so this must stay in sync.)

## 2. Local setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

pytest                      # the suite ring-gate runs on every PR
scripts/check.sh            # fast offline manifest checks
git config core.hooksPath .githooks   # optional: run check.sh pre-commit
```

## 3. Replace the example tenants

Each file in `fleet/tenants/` is one managed tenant. Delete the examples you
don't want and add your own (the runbook's "Add a new managed tenant" has the
full checklist). One constraint: every ring with a pin file in `fleet/pins/`
must keep at least one tenant — `resolve.py --ring` (run by check.sh and
ring-gate) errors on an empty ring. To drop a whole ring, delete its pin file
and its tenants together, and remove it from `promotion_order` in
`fleet/policies.yml` and the ring choices in `converge.yml`'s
`workflow_dispatch` input.

```yaml
tenant: acmecorp01            # must appear in the tenant's XSIAM hostname —
display_name: Acme Corp       #   the host guard refuses to deploy otherwise
ring: prod                    # dev | qa | prod | mssp
credentials_ref: ACMECORP01   # env-var prefix for its CI secret
groups: []                    # subscribe to fleet/groups/*.yml bundles
extras: []                    # or list vendor pack ids directly
pin_overrides: {}             # hold this tenant at a specific version
custom: []                    # customer-owned pack ids (referenced only)
```

Keep tenant names lowercase-alphanumeric: the name doubles as the default
`host_token` the host guard matches against the tenant's XSIAM subdomain, and
`credentials_ref` becomes an env-var prefix. If the fleet name can't match the
subdomain, set an explicit `host_token:`.

Every pack a tenant composes must have a pin in its ring's
`fleet/pins/<ring>.yml` — `scripts/resolve.py <tenant>` verifies before you
merge. `devlab01` shows the pattern: it subscribes to the
`financial-endpoint-heavy` group, and every pack that group lists is pinned in
`fleet/pins/dev.yml`. The other two groups are unsubscribed catalog examples —
subscribing a tenant to one means adding pins for its packs in that tenant's
ring (dev first; qa/prod fill in by promotion).

Adjust the example groups in `fleet/groups/` and the base set in
`fleet/defaults.yml` to your catalog. All example pin **versions** are
snapshots that will go stale — re-pin against the current upstream catalog as
part of adoption (`resolve.py` + a dry-run converge will confirm the versions
fetch cleanly).

If customers deploy their own content to the same tenants, register their
prefixes under `customer_namespaces:` in `fleet/defaults.yml` (the `acme-`
entry is an example).

## 4. Wire up CI secrets

Nothing secret lives in this repo — each tenant profile only carries a
`credentials_ref`. In GitHub, create one secret per tenant, preferably the
packed form:

```
<CREDENTIALS_REF>_CREDS = {"base_url":"https://api-<tenant>...", "api_key":"...", "auth_id":"..."}
```

(The flat trio `<REF>_BASE_URL` / `<REF>_API_KEY` / `<REF>_AUTH_ID` also
works.) Scope prod tenants' secrets to a `fleet-prod` GitHub Environment so
nonprod jobs cannot read them; `fleet/policies.yml` names the intended
Environment per ring (`fleet-nonprod`, `fleet-prod`, `fleet-mssp`).

Recommended repo settings:

- Branch protection on `main` with **ring-gate** as a required check.
- A `fleet-prod` Environment with required reviewers; when going live, add it
  to the converge job's prod ring so prod merges deploy under approval.
- Settings → Actions → General: enable **"Allow GitHub Actions to create and
  approve pull requests"** — the promote workflow opens its pin-copy PR with
  `peter-evans/create-pull-request` and fails without it.
- Create a `drift` issue label — the drift workflow labels the issues it opens
  and GitHub's API does not auto-create labels.

## 5. Go live

CI ships safe: `converge` runs a **dry-run** on every push (prints the plan,
touches no tenant, needs no secrets). When your tenants and secrets are in
place, set the repo variable:

```
FLEET_LIVE = true
```

From then on merges to `main` touching `fleet/pins/**`, `Packs/**`, or
`fleet/config_overlays/**` converge the affected tenants for real. Orphan
**deletion** stays additionally gated behind the repo variable
`FLEET_ALLOW_DELETE = true` until you've proven the pack-delete path against a
lab tenant (see the README's "What still needs proving" section).

## 6. Tune the promotion policy

`fleet/policies.yml` ships with lab-friendly values — qa soak is 0 days for
fast testing. For production use, raise `min_soak_days` (e.g. qa: 2, prod: 5)
and set your real prod change windows before onboarding customer tenants.

Dev → qa soak gates **third-party packs only**: MSSP-authored packs (source
under `Packs/`) are soak-exempt out of the head ring, so raising qa's
`min_soak_days` never slows your own content. Qa → prod soak applies to every
pack (see the runbook's tenant-authored content section).

## Where things live

| You want to… | Edit |
|---|---|
| Change what every tenant gets | `fleet/defaults.yml › base` |
| Bundle vendor packs | `fleet/groups/<group>.yml` |
| Add/adjust a tenant | `fleet/tenants/<tenant>.yml` |
| Bump a version (dev first) | `fleet/pins/dev.yml` |
| Promote a version | `scripts/promote.py` / the promote workflow |
| Tune a config List per tenant | `fleet/config_overlays/<tenant>.yml` |
| Author your own pack | `Packs/<PackId>/` (see `Packs/ExamplePack`) |
| Change promotion rules | `fleet/policies.yml` |
