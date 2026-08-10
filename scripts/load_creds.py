#!/usr/bin/env python3
"""
load_creds.py — resolve ONE tenant's XSIAM credentials for the converge step.

This is the single pluggable seam between the fleet's tenant profiles and whatever
store actually holds credentials. `deploy_tenant.py` stays backend-agnostic — it
only reads DEMISTO_BASE_URL / DEMISTO_API_KEY / XSIAM_AUTH_ID from the environment;
this script is the only thing that knows HOW those get populated. Swapping the
store later (Vault, AWS/GCP Secrets Manager, ...) is a change here and nowhere else.

It prints a shell snippet to stdout that the workflow eval's:

    eval "$(python scripts/load_creds.py <tenant>)"

The snippet first registers the sensitive values for log-masking, then exports the
three env vars. Backend is selected by `fleet/defaults.yml > credentials_backend`.

Backends
--------
github-environments  (default) — read from the CI job's secrets, passed in as the
    ALL_SECRETS env var (JSON of `${{ toJson(secrets) }}`). Preferred PACKED form is
    one secret per tenant (REF = the tenant's credentials_ref):

        <REF>_CREDS = {"base_url": "...", "api_key": "...", "auth_id": "..."}

    The legacy FLAT form is still accepted as a fallback:

        <REF>_BASE_URL / <REF>_API_KEY / <REF>_AUTH_ID

NOTE on masking: a value extracted from a packed JSON secret is a *substring* of the
registered secret, so the CI runner will not auto-mask it. This script emits an
explicit `::add-mask::` for each sensitive field, which is why the workflow must
eval its output rather than read the vars some other way.
"""
import json
import os
import shlex
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
FIELDS = ("base_url", "api_key", "auth_id")            # canonical field names
ENV = {"base_url": "DEMISTO_BASE_URL",
       "api_key": "DEMISTO_API_KEY",
       "auth_id": "XSIAM_AUTH_ID"}
SENSITIVE = {"api_key", "auth_id"}                     # masked in CI logs


class CredsError(Exception):
    pass


def _tenant_ref(tenant):
    f = REPO / "fleet" / "tenants" / f"{tenant}.yml"
    if not f.exists():
        raise CredsError(f"no tenant profile: {f}")
    ref = (yaml.safe_load(f.read_text()) or {}).get("credentials_ref")
    if not ref:
        raise CredsError(f"tenant '{tenant}' has no credentials_ref")
    return ref


def _backend():
    d = yaml.safe_load((REPO / "fleet" / "defaults.yml").read_text()) or {}
    return d.get("credentials_backend", "github-environments")


def _from_github(ref):
    """Resolve the trio from CI secrets (ALL_SECRETS JSON): packed, then flat."""
    raw = os.environ.get("ALL_SECRETS")
    if not raw:
        raise CredsError("ALL_SECRETS not set (workflow must pass toJson(secrets))")
    secrets = json.loads(raw)

    packed = secrets.get(f"{ref}_CREDS")
    if packed:
        try:
            obj = json.loads(packed)
        except json.JSONDecodeError as e:
            raise CredsError(f"secret {ref}_CREDS is not valid JSON: {e}")
        creds = {k: obj.get(k) for k in FIELDS}
    else:  # legacy flat fallback
        creds = {"base_url": secrets.get(f"{ref}_BASE_URL"),
                 "api_key": secrets.get(f"{ref}_API_KEY"),
                 "auth_id": secrets.get(f"{ref}_AUTH_ID")}

    missing = [k for k in FIELDS if not creds.get(k)]
    if missing:
        raise CredsError(
            f"missing {', '.join(missing)} for credentials_ref '{ref}' — expected "
            f"secret {ref}_CREDS (JSON) or {ref}_BASE_URL/_API_KEY/_AUTH_ID")
    return creds


BACKENDS = {"github-environments": _from_github}


def resolve(tenant):
    backend = _backend()
    fn = BACKENDS.get(backend)
    if fn is None:
        raise CredsError(f"unknown credentials_backend '{backend}' "
                         f"(known: {', '.join(sorted(BACKENDS))})")
    return fn(_tenant_ref(tenant))


def emit(creds):
    lines = [f"echo {shlex.quote('::add-mask::' + creds[f])}"
             for f in FIELDS if f in SENSITIVE]
    lines += [f"export {ENV[f]}={shlex.quote(creds[f])}" for f in FIELDS]
    return "\n".join(lines)


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: load_creds.py <tenant>")
    try:
        print(emit(resolve(sys.argv[1])))
    except CredsError as e:
        sys.exit(f"ERROR: {e}")


if __name__ == "__main__":
    main()
