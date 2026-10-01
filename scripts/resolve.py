#!/usr/bin/env python3
"""
resolve.py — compute the EFFECTIVE install set for a tenant, and act as the
shared resolver library the other scripts import.

    composition  = base (defaults.base) ⊕ extras (subscribed groups ∪ direct)
    pin(pack)     = tenant.pin_overrides[pack]  or  fleet/pins/<ring>.yml[pack]
    effective     = composition, each pack resolved to its ring's pin

Composition (WHICH packs) lives in defaults/groups/tenant files; pins (WHAT to
deploy) live per ring in fleet/pins/<ring>.yml. The same pack can therefore be
at different versions in dev, qa, and prod at once. A pack a
tenant composes but that has no pin in its ring is a hard error — the fleet must
never deploy an unpinned "latest".

Routing is PIN-DRIVEN. Every composed pack MUST have a pin, and
the pin's VALUE — not the presence of a source dir under Packs/ — decides how the
pack is deployed:

    pin == "local"    -> deploy from local source Packs/<name>/; version comes
                         from that pack's pack_metadata.json "currentVersion".
                         source="local". Requires a source dir to exist (else a
                         hard ResolveError).
    pin == "1.2.3"    -> deploy as a fetched upstream artifact at that exact
                         version. source="upstream".
    no pin/override   -> missing -> hard ResolveError.

The "local" sentinel is valid in BOTH fleet/pins/<ring>.yml and a tenant's
pin_overrides. An MSSP-authored pack under Packs/ is NOT routed to local source
merely because it exists on disk — it is only routed local when its pin value is
the "local" sentinel.

Ring membership is derived from each tenant's own `ring:` field — there is no
separate rings.yml.

Usage:
    python scripts/resolve.py <tenant>
    python scripts/resolve.py <tenant> --json
    python scripts/resolve.py --ring prod          # every tenant in a ring
"""
import argparse
import copy
import json
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
FLEET = REPO / "fleet"


class ResolveError(Exception):
    """A tenant/ring cannot be resolved (missing file, unpinned pack, etc.)."""


def load(path):
    with open(path) as fh:
        return yaml.safe_load(fh)


def load_tenant(name):
    f = FLEET / "tenants" / f"{name}.yml"
    if not f.exists():
        raise ResolveError(f"no tenant manifest at {f}")
    return load(f)


def load_group(name):
    f = FLEET / "groups" / f"{name}.yml"
    if not f.exists():
        raise ResolveError(f"tenant references missing group '{name}' ({f})")
    return load(f)


def load_pins(ring):
    f = FLEET / "pins" / f"{ring}.yml"
    if not f.exists():
        raise ResolveError(f"no pin file for ring '{ring}' ({f})")
    return load(f).get("pins") or {}


def local_packs():
    """MSSP-authored packs whose source lives in this repo under Packs/<name>/.

    Mirrors deploy_tenant.py:mssp_authored_ids() and namespace_lint.py: read each
    Packs/*/pack_metadata.json, key on its "id" (falling back to the directory
    name), and record the dir name and declared currentVersion. Such a pack is
    eligible to be routed to LOCAL SOURCE — but only when its pin value is the
    "local" sentinel; routing is pin-driven, not presence-driven. This map is used
    (a) to supply the local install's version (the pack_metadata currentVersion)
    and dir name, and (b) as the existence check behind the "local" pin guard.

    Returns {pack_id: {"version": <currentVersion or None>, "name": <dir name>}}.
    A missing/unreadable currentVersion yields None rather than crashing.
    """
    out = {}
    packs = REPO / "Packs"
    if not packs.exists():
        return out
    for meta in sorted(packs.glob("*/pack_metadata.json")):
        dir_name = meta.parent.name
        try:
            data = json.loads(meta.read_text())
            pid = data.get("id") or dir_name
            version = data.get("currentVersion")
        except Exception:  # noqa: BLE001 — a malformed metadata file is still a local pack
            pid, version = dir_name, None
        out[pid] = {"version": version, "name": dir_name}
    return out


def load_config_overlay_layer(name):
    """One Config Overlay layer: fleet/config_overlays/<name>.yml, or {} if absent.

    `name` is "defaults" (the fleet-wide layer) or a tenant name (the per-tenant
    layer). An absent file is an EMPTY layer, not an error — most tenants carry
    no Config Overlay at all.
    """
    f = FLEET / "config_overlays" / f"{name}.yml"
    if not f.exists():
        return {}
    return load(f) or {}


def resolve_config_overlay(tenant_name):
    """Compose a tenant's Config Overlay: defaults ⊕ tenant.

    Two layers only — there is no Group layer. Returns {list_id: merged patch},
    or {} when neither layer exists.
    """
    return deep_merge(
        load_config_overlay_layer("defaults"),
        load_config_overlay_layer(tenant_name),
    )


def all_tenants():
    return [p.stem for p in sorted((FLEET / "tenants").glob("*.yml"))]


def tenants_in_ring(ring):
    """Ring membership = every tenant whose `ring:` field matches."""
    members = [t for t in all_tenants() if (load_tenant(t).get("ring") == ring)]
    if not members:
        raise ResolveError(f"no tenants in ring '{ring}'")
    return members


def deep_merge(base, over):
    """Recursively merge `over` onto `base`; `over` wins.

    Where BOTH sides hold a mapping for the same key the two are merged
    recursively; any other value (scalar, list, or a type change) is REPLACED
    wholesale by `over`. Lists are never element-merged or concatenated —
    a Config Overlay that names a list means "this exact list".

    The result is a fully independent structure: it shares no references with
    either input, so callers may mutate it freely (and neither input is ever
    mutated). That guarantee matters at Convergence, where the merged List
    JSON is edited into a staged pack.

    This is the single merge semantic for Config Overlays: it composes the two
    Config Overlay layers (defaults ⊕ tenant) here, and Convergence reuses it to
    apply the composed patch onto a pinned pack's List content
    (pinned ⊕ Config Overlay).
    """
    out = {key: copy.deepcopy(value) for key, value in (base or {}).items()}
    for key, value in (over or {}).items():
        if isinstance(out.get(key), dict) and isinstance(value, dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _dedupe(ids):
    """Preserve first-seen order, drop duplicates."""
    seen, out = set(), []
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def normalize_token(s):
    """Lowercase s and keep only ascii alphanumerics (drop ., _, spaces, etc.)."""
    if not s:
        return ""
    return "".join(c for c in str(s).lower() if c.isalnum() or c == '-')


def resolve(tenant_name):
    defaults = load(FLEET / "defaults.yml")
    tenant = load_tenant(tenant_name)
    ring = tenant.get("ring")
    if not ring:
        raise ResolveError(f"tenant '{tenant_name}' declares no ring")

    pins = load_pins(ring)
    overrides = tenant.get("pin_overrides") or {}
    local = local_packs()  # MSSP-authored packs deploying from local source

    # composition (pack IDs only) ----------------------------------------------
    base_ids = _dedupe(defaults.get("base") or [])
    extra_ids = []
    for g in tenant.get("groups") or []:
        extra_ids.extend(load_group(g).get("extras") or [])
    extra_ids.extend(tenant.get("extras") or [])
    extra_ids = _dedupe(extra_ids)

    # resolve each composed pack to its version --------------------------------
    # Routing is PIN-DRIVEN: the pin VALUE — not presence under Packs/ — selects
    # the deploy path. Precedence for the VALUE is pin_override, then the ring
    # pins, else the pack is missing (a hard error — the fleet never deploys an
    # unpinned "latest"). Then the value is interpreted:
    #   value == "local"  -> local source (Packs/<name>/); version from that
    #                        pack's pack_metadata currentVersion. The source dir
    #                        MUST exist, else a ResolveError (collected below).
    #   value == "1.2.3"  -> upstream artifact at that exact version.
    # Each entry carries a "source" of "local" or "upstream" so the deployer knows
    # whether to upload from Packs/ or fetch the pinned zip.
    missing = []
    bad_local = []  # packs pinned to "local" with no source dir under Packs/

    def resolve_pack(pid):
        if pid in overrides:
            value = overrides[pid]
        elif pid in pins:
            value = pins[pid]
        else:
            missing.append(pid)
            return {"id": pid, "version": None, "source": "upstream"}
        if value == "local":  # the sentinel — route to local source
            if pid not in local:  # no Packs/<name>/ source on disk
                bad_local.append(pid)
                return {"id": pid, "version": None, "source": "local"}
            return {"id": pid, "version": local[pid].get("version"), "source": "local"}
        return {"id": pid, "version": value, "source": "upstream"}

    base = [resolve_pack(pid) for pid in base_ids]
    extras = [resolve_pack(pid) for pid in extra_ids]

    if missing:
        raise ResolveError(
            f"tenant '{tenant_name}' (ring {ring}) composes packs with no pin in "
            f"fleet/pins/{ring}.yml and no pin_override: {', '.join(sorted(set(missing)))}"
        )
    if bad_local:
        raise ResolveError(
            f"tenant '{tenant_name}' (ring {ring}) pins "
            f"{', '.join(repr(pid) for pid in sorted(set(bad_local)))} to 'local' but "
            f"no source exists at Packs/<name>/ (pack_metadata.json not found)"
        )

    host_token = normalize_token(tenant.get("host_token") or tenant["tenant"])

    return {
        "tenant": tenant["tenant"],
        "display_name": tenant.get("display_name", tenant["tenant"]),
        "ring": ring,
        "credentials_ref": tenant.get("credentials_ref"),
        "catalog": defaults["catalog"]["url"],
        "catalog_pinned_commit": defaults["catalog"].get("pinned_commit"),
        "customer_namespace": tenant.get("customer_namespace"),
        "config_overlay": resolve_config_overlay(tenant_name),
        "base": base,
        "extras": extras,
        "custom": tenant.get("custom") or [],
        "groups": tenant.get("groups") or [],
        "host_token": host_token,
    }


def owned_pins(r):
    """MSSP-owned packs (base + extras) as {id: version}. Custom is excluded."""
    return {p["id"]: p["version"] for p in (r["base"] + r["extras"])}


def print_human(r):
    line = "─" * 64
    print(line)
    print(f"  {r['display_name']}   [{r['tenant']}]")
    print(f"  ring={r['ring']}   creds={r['credentials_ref']}")
    print(line)

    def block(label, pins):
        print(f"\n  {label}")
        if not pins:
            print("    (none)")
        for p in pins:
            print(f"    • {p['id']:<34} {p.get('version','')}")

    block("BASE (MSSP-owned · foundation)", r["base"])
    grp = f" ⟵ {', '.join(r['groups'])}" if r["groups"] else " (direct)"
    block(f"GROUPED / EXTRA (MSSP-owned){grp}", r["extras"])
    print("\n  CUSTOM (customer-owned · referenced only)")
    if not r["custom"]:
        print("    (none)")
    for c in r["custom"]:
        print(f"    • {c['id']:<34} owner={c.get('owner','customer')}")

    # Config Overlay — the fleet-owned sparse patch of framework config Lists
    #. Omitted entirely when the tenant has no Config Overlay.
    overlay = r.get("config_overlay") or {}
    if overlay:
        print("\n  CONFIG OVERLAY (fleet-owned · defaults ⊕ tenant)")
        for list_id in sorted(overlay):
            patch = overlay[list_id]
            keys = ", ".join(sorted(patch)) if isinstance(patch, dict) else "(whole value)"
            print(f"    • {list_id:<34} {keys}")

    owned = len(r["base"]) + len(r["extras"])
    print(f"\n  → {owned} MSSP-owned packs to install/reconcile, "
          f"{len(r['custom'])} customer pack(s) referenced.")
    print(line + "\n")


def main():
    ap = argparse.ArgumentParser(description="Resolve a tenant's effective install set.")
    ap.add_argument("tenant", nargs="?", help="tenant name (file stem in fleet/tenants/)")
    ap.add_argument("--ring", help="resolve every tenant in this ring")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    args = ap.parse_args()

    try:
        if args.ring:
            results = [resolve(t) for t in tenants_in_ring(args.ring)]
        elif args.tenant:
            results = [resolve(args.tenant)]
        else:
            ap.print_help()
            sys.exit(1)
    except ResolveError as e:
        sys.exit(f"ERROR: {e}")

    if args.json:
        print(json.dumps(results if args.ring else results[0], indent=2))
    else:
        for r in results:
            print_human(r)


if __name__ == "__main__":
    main()
