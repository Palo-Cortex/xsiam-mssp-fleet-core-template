# Setup wizard

One command sets up your fleet repository, from an empty template to protected
CI deploying to your XSIAM tenants:

```bash
python scripts/setup_wizard.py
```

It works whether or not you're an MSSP, for any of these promotion paths:

| Path | Typical use |
|---|---|
| **Dev → QA → Prod** | Build in a dev tenant, validate in QA, release to production |
| **QA → Prod** | Validate in QA, release to production |
| **Dev → Prod** | Build and validate in dev, release to production |

MSSPs can also put their own parent tenant on a separate track. If tenant
owners build and publish their own content (for example "build in production,
then release"), you register a name prefix for them so the fleet never touches
that content.

## Before you start

- **Python 3.10+** and `pip install pyyaml`
- **git**, and the **GitHub CLI** logged in: `gh auth login`
- **Admin** rights on your fleet repository (or create one — the wizard offers
  to create it from this template)
- For each XSIAM tenant: permission to create an API key (the wizard shows
  exactly where)

## What it does

| Step | You decide | The wizard writes / sets |
|---|---|---|
| 1 Check tools | — | verifies Python, git, `gh` login, your own repo, admin, clean `main` |
| 2 Organisation | MSSP or not, promotion path, soak days, prod change window, whether tenant owners publish their own content | `fleet/policies.yml`, `fleet/pins/<ring>.yml`, workflow ring choices, `release_base`; removes the template examples |
| 3 Content | packs every tenant gets (from the upstream catalog) | `fleet/defaults.yml › base`, pins at the catalog's current versions |
| 4 Tenants | each tenant's API URL, key ID, key; its ring; canary; owner prefix | key **checked live**, stored as `<TENANT>_CREDS` in the ring's GitHub Environment; `fleet/tenants/<name>.yml` |
| 5 Approvers | who approves prod, PR approvals, optional promotion App | `.github/CODEOWNERS`, `FLEET_APP_ID` / `FLEET_APP_PRIVATE_KEY` |
| 6 Check | lock artifacts? | runs ring-gate's checks, `fleet/artifacts.lock.yml`, `require_lock` for prod, dry-run plans |
| 7 Save | — | commits and pushes (straight to `main` the first time; a PR afterwards) |
| 8 Protect | merge queue? | Environments (prod: approvers, `main` only), labels, branch protection with ring-gate required |
| 9 Go live | when | tests every stored key from GitHub Actions, then sets `FLEET_LIVE=true` |

Nothing reaches a tenant until step 9. API keys go straight to GitHub's
encrypted secrets — they're never written to the repository or to the wizard's
progress file.

## Stopping and resuming

Press Ctrl-C at any time. Re-running the same command picks up where you left
off; finished steps are skipped (progress lives in `.fleet-setup/`, which git
ignores). To redo one step:

```bash
python scripts/setup_wizard.py --step org      # e.g. change the promotion path
```

## Afterwards

```bash
python scripts/setup_wizard.py add-tenant       # connect another tenant (opens a PR)
python scripts/setup_wizard.py doctor           # check everything, change nothing
python scripts/setup_wizard.py doctor --remote  # …and test every stored key from Actions
```

`doctor` reports each problem with the fix. Run it after any manual edit, after
rotating a key, or when something behaves unexpectedly. Its exit code is
non-zero when something is broken, so it can run in CI.

## Unattended (answers file)

For repeatable environments, put the answers in a YAML file:

```bash
ACME_DEV_KEY=... ACME_PROD_KEY=... \
  python scripts/setup_wizard.py --answers setup-answers.yml
```

Keys mirror the prompts; anything left out takes the wizard's default.
Secrets are **never** in the file — each `api_key_env` names an environment
variable. See [`setup-answers.example.yml`](setup-answers.example.yml).

## Troubleshooting

| Message | Meaning / fix |
|---|---|
| `HTTP 401` on a key | Key or key ID wrong, deleted or expired. The ID is the number in the API Keys table. |
| `HTTP 403` on a key | Key works but its role can't manage content — use **Instance Administrator**. |
| `HTTP 404` / "not the content API" | You pasted the console URL. Use **Copy API URL** (`https://api-<tenant>.xdr.<region>…`). |
| "host guard will expect …" | The tenant name isn't in the API host, so the wizard recorded the host part to check against. That's normal. |
| Branch protection / Environment approvers refused | Private repos need GitHub **Team** or **Enterprise** for these. The wizard says which setting failed; upgrade or make the repo internal/public. |
| Merge queue not available | Optional. converge still re-checks change windows before deploying. |
| "every ring needs at least one tenant" | Each ring needs a tenant before the checks pass — `add-tenant`. |
