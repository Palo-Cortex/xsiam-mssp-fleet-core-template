#!/usr/bin/env python3
"""remote_check.py <tenant> — run by .github/workflows/doctor.yml.

Inside the tenant's own GitHub Environment, with only its own secret loaded
(load_creds.py), check exactly what converge will need:
  * the API host belongs to this tenant (the converge host guard);
  * the key works and can read installed content packs.
Read-only; never changes the tenant.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))       # scripts/
sys.path.insert(0, str(Path(__file__).resolve().parent))           # scripts/fleet_setup

from deploy_tenant import DeployError, assert_host_matches, load_creds  # noqa: E402
from resolve import ResolveError, resolve  # noqa: E402
from xsiam import check_credentials  # noqa: E402


def main():
    tenant = sys.argv[1]
    try:
        r = resolve(tenant)
        creds = load_creds()
        assert_host_matches(r, creds)
    except (ResolveError, DeployError) as e:
        print(f"✗ {tenant}: {e}")
        return 1
    print(f"✓ {tenant}: host matches ({creds['DEMISTO_BASE_URL']})")
    ok, msg, _n = check_credentials(creds["DEMISTO_BASE_URL"], creds["XSIAM_AUTH_ID"],
                                    creds["DEMISTO_API_KEY"])
    print(("✓ " if ok else "✗ ") + f"{tenant}: {msg}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
