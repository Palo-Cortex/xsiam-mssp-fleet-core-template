import sys
from pathlib import Path

import pytest
import yaml

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
# Make the shared helper module importable by bare name from the test files.
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

# Minimal, self-contained catalog/defaults so temp fleets don't hit the network
# and don't depend on the real fleet/defaults.yml contents.
_DEFAULTS = {
    "catalog": {
        "url": "https://example.invalid/pack_catalog.json",
        "pinned_commit": None,
    },
    "base": [],
    "credentials_backend": "github-environments",
    "customer_namespaces": ["acme-"],
}


@pytest.fixture(autouse=True)
def _github_com_by_default(monkeypatch):
    """Tests assume github.com unless they set these themselves, so the suite
    gives the same results on a GitHub Enterprise Server runner."""
    monkeypatch.delenv("GITHUB_SERVER_URL", raising=False)
    monkeypatch.delenv("GITHUB_API_URL", raising=False)


def _write_yaml(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False))


@pytest.fixture
def temp_fleet(tmp_path, monkeypatch):
    """Build an isolated fleet/ tree and point resolve.FLEET at it.

    Returns a small builder object so each test can declare exactly the tenant
    and pins it needs without mutating the real fleet/ directory.

    The builder writes:
      <tmp>/fleet/defaults.yml
      <tmp>/fleet/pins/<ring>.yml
      <tmp>/fleet/tenants/<name>.yml
      <tmp>/fleet/groups/<name>.yml   (only if a group is declared)
    and monkeypatches the module-level FLEET path on both resolve and (if
    present) deploy_tenant so imported functions read the temp tree.
    """
    import resolve

    fleet = tmp_path / "fleet"
    _write_yaml(fleet / "defaults.yml", dict(_DEFAULTS))

    monkeypatch.setattr(resolve, "FLEET", fleet, raising=True)
    # Consumers import resolve.* by name; resolve() itself reads resolve.FLEET,
    # so patching resolve.FLEET is sufficient for resolution. Modules that also
    # bound their own FLEET at import time get it patched too, defensively.
    for mod_name in ("deploy_tenant", "drift_check"):
        try:
            mod = __import__(mod_name)
            if hasattr(mod, "FLEET"):
                monkeypatch.setattr(mod, "FLEET", fleet, raising=False)
        except Exception:
            pass

    class Builder:
        root = fleet

        def pins(self, ring, mapping):
            _write_yaml(fleet / "pins" / f"{ring}.yml",
                        {"ring": ring, "pins": mapping})
            return self

        def group(self, name, extras):
            _write_yaml(fleet / "groups" / f"{name}.yml", {"extras": extras})
            return self

        def config_overlay(self, name, patches):
            """Write a Config Overlay layer: fleet/config_overlays/<name>.yml.

            `name` is either "defaults" (the fleet-wide layer) or a tenant name
            (the per-tenant layer). `patches` is {list_id: sparse patch}.
            """
            _write_yaml(fleet / "config_overlays" / f"{name}.yml", patches)
            return self

        def config_overlay_lists(self, list_ids):
            """Set the registry of tunable List ids in defaults.yml."""
            d = yaml.safe_load((fleet / "defaults.yml").read_text())
            d["config_overlay_lists"] = list_ids
            _write_yaml(fleet / "defaults.yml", d)
            return self

        def tenant(self, name, ring="dev", extras=None, base=None,
                   groups=None, pin_overrides=None, custom=None,
                   credentials_ref=None):
            # Allow overriding defaults.base per test (preserving any other
            # defaults a builder call already wrote, e.g. config_overlay_lists).
            if base is not None:
                d = yaml.safe_load((fleet / "defaults.yml").read_text())
                d["base"] = base
                _write_yaml(fleet / "defaults.yml", d)
            manifest = {
                "tenant": name,
                "display_name": f"{name} (test)",
                "ring": ring,
                "credentials_ref": credentials_ref or name.upper(),
                "groups": groups or [],
                "extras": extras or [],
                "pin_overrides": pin_overrides or {},
                "custom": custom or [],
            }
            _write_yaml(fleet / "tenants" / f"{name}.yml", manifest)
            return name

    return Builder()
