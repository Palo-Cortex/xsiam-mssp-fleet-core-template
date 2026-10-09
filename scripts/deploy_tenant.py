#!/usr/bin/env python3
"""
deploy_tenant.py — converge ONE tenant to its resolved state via demisto-sdk.

There is no in-platform manager and no `using=<instance>` routing: the fleet
talks to each tenant directly with that tenant's own credentials.

Convergence has two halves:

  INSTALL/UPDATE  resolve the effective set -> for each MSSP-owned pack, fetch
                  its pinned release zip (scripts/catalog.py) -> `demisto-sdk
                  upload` the zip to the tenant. Uploading an older zip over a
                  newer install is a valid rollback. Upserts are
                  best-effort per pack: one pack's fetch/upload failure is
                  recorded and the rest are still attempted (an "Incomplete
                  Converge"). A tenant's Config Overlay is
                  merged into the STAGED copy of each fetched pack before it is
                  uploaded, so its config Lists deploy as `pinned ⊕ Config
                  Overlay`; a patched List id that no deployed
                  pack carries is itself a failure.

  REMOVE          an MSSP-owned pack installed on the tenant that is no longer
                  in its resolved composition (base + extras) is an ORPHAN and
                  is deleted via
                  the XSIAM pack-delete API. The deletable set is BOUNDED to ids
                  the fleet could have installed (upstream catalog ∪ MSSP-
                  authored packs); anything else on the tenant (e.g. a console-
                  installed Marketplace pack, or customer-prefixed content) is
                  never touched. Held (not run) if any
                  upsert failed.

Dry-run is the default: it prints the plan and touches nothing. --apply performs
the fetch/upload/delete and requires the tenant's creds in the environment
(DEMISTO_BASE_URL, DEMISTO_API_KEY, XSIAM_AUTH_ID). In dry-run you may pass
--installed <state.yml> to preview orphan deletions offline.

Usage:
    python scripts/deploy_tenant.py <tenant>                      # dry-run plan
    python scripts/deploy_tenant.py <tenant> --installed s.yml    # + orphan preview
    python scripts/deploy_tenant.py <tenant> --apply              # live converge
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from resolve import (resolve, owned_pins, ResolveError, load, local_packs,
                     deep_merge)
from catalog import resolve_artifact, load_catalog, CatalogError
from github_host import api_url, parse_release_asset

REPO = Path(__file__).resolve().parents[1]
CRED_ENV = ("DEMISTO_BASE_URL", "DEMISTO_API_KEY", "XSIAM_AUTH_ID")


class DeployError(Exception):
    pass


# --- credentials ------------------------------------------------------------
def load_creds():
    creds = {k: os.environ.get(k) for k in CRED_ENV}
    missing = [k for k, v in creds.items() if not v]
    if missing:
        raise DeployError(
            "missing tenant credentials in env: " + ", ".join(missing) +
            "\n  (CI maps <credentials_ref>_BASE_URL/_API_KEY/_AUTH_ID into these)"
        )
    return creds


def assert_host_matches(r, creds):
    """Refuse to deploy if the tenant's creds resolve to a host that isn't its own.

    A swapped/wrong <REF>_CREDS secret would otherwise silently cross-deploy this
    tenant's content to whatever host the creds point at (and CI would still go
    green). We assert the resolved host contains the tenant's expected host token
    (default: the normalized tenant name; override: `host_token:` in the tenant
    profile). Hard fail, not a warning — the failure mode is a silent wrong-tenant
    deploy.
    """
    token = r.get("host_token")
    host = urllib.parse.urlparse(creds["DEMISTO_BASE_URL"]).hostname
    if not host:
        raise DeployError(
            f"refusing to deploy {r['tenant']}: DEMISTO_BASE_URL "
            f"'{creds['DEMISTO_BASE_URL']}' has no parseable host"
        )
    if not token:
        raise DeployError(
            f"refusing to deploy {r['tenant']}: no host_token to verify the "
            f"target host against"
        )
    if token not in host.lower():
        raise DeployError(
            f"refusing to deploy {r['tenant']} to host '{host}' — wrong secret? "
            f"(expected host to contain '{token}')"
        )


# --- the bounded deletable set ------------------------------
def mssp_authored_ids():
    """Pack ids whose source lives in this repo (Packs/<p>/pack_metadata.json)."""
    ids = []
    packs = REPO / "Packs"
    if not packs.exists():
        return ids
    for meta in packs.glob("*/pack_metadata.json"):
        try:
            ids.append(json.loads(meta.read_text()).get("id") or meta.parent.name)
        except Exception:  # noqa: BLE001
            ids.append(meta.parent.name)
    return ids


def deletable_set(catalog):
    """Only ids the fleet could have installed are eligible for deletion."""
    return {e.get("id") for e in catalog.get("packs", [])} | set(mssp_authored_ids())


# --- live tenant I/O (only exercised under --apply) -------------------------
def _api(creds, method, path, body=None, expect_status=None):
    url = creds["DEMISTO_BASE_URL"].rstrip("/") + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, method=method, data=data)
    req.add_header("Authorization", creds["DEMISTO_API_KEY"])
    req.add_header("x-xdr-auth-id", creds["XSIAM_AUTH_ID"])
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=60) as resp:
        status = resp.status
        rbody = resp.read().decode("utf-8")
    if expect_status is not None and status != expect_status:
        raise DeployError(f"{method} {path} returned HTTP {status}, expected {expect_status}")
    return json.loads(rbody) if rbody else {}


def list_installed(creds):
    """{id: version} of content packs installed on the tenant.

    Path verified live against XSIAM 3.6: the XSOAR-compatible content
    API is served under /xsoar/public/v1 on the api-<tenant> host. The bare
    /contentpacks/... and /public/v1/contentpacks/... forms return 500.
    """
    data = _api(creds, "GET", "/xsoar/public/v1/contentpacks/metadata/installed")
    return {p["id"]: p.get("currentVersion") for p in data} if isinstance(data, list) else {}


def delete_packs(creds, pack_ids):
    """XSIAM pack-delete — the only removal path (demisto-sdk has no uninstall).

    Verified live: a BATCH POST, not a per-id DELETE —
        POST /xsoar/contentpacks/installed/delete   body {"ids": [...]}  -> 200 {}
    Gated behind FLEET_ALLOW_DELETE at the call site.
    """
    return _api(creds, "POST", "/xsoar/contentpacks/installed/delete",
                body={"ids": list(pack_ids)}, expect_status=200)


def _github_token():
    return os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")


def _gh_api_json(url, token):
    req = urllib.request.Request(url, headers={
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def download(url):
    """Fetch a release zip to a temp file.

    A plain GET covers public artifacts (the upstream catalog's zips). A
    browser_download_url on a PRIVATE repo 404s even with a token, so when the
    URL is a release asset on THIS GitHub (github.com, <company>.ghe.com, or
    GitHub Enterprise Server — see github_host.py) and a GH_TOKEN/GITHUB_TOKEN
    is present (CI always has one), resolve tag -> asset id via this GitHub's
    API and download through the assets endpoint with Accept: octet-stream.
    Release URLs on any other host are fetched with a plain GET, without the
    token. This keeps catalog zip_urls
    canonical/deterministic (see scripts/mssp_catalog.py) — the auth dance
    lives only here.
    """
    fd, path = tempfile.mkstemp(suffix=".zip")
    os.close(fd)

    m = parse_release_asset(url)
    token = _github_token()
    if m and token:
        try:
            rel = _gh_api_json(
                f"{api_url()}/repos/{m['owner']}/{m['repo']}"
                f"/releases/tags/{m['tag']}", token)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise DeployError(
                    f"release {m['tag']} not found in {m['owner']}/{m['repo']} — "
                    f"MSSP packs are released from Packs/ metadata by the release "
                    f"job at the start of converge; check that job's log, and that "
                    f"the pinned version was ever a pack's currentVersion") from e
            raise
        assets = {a["name"]: a["id"] for a in rel.get("assets", [])}
        if m["asset"] not in assets:
            raise DeployError(
                f"release {m['tag']} has no asset named {m['asset']} "
                f"(has: {', '.join(sorted(assets)) or 'none'}) — re-run the "
                f"release-packs workflow to repair it")
        req = urllib.request.Request(
            f"{api_url()}/repos/{m['owner']}/{m['repo']}"
            f"/releases/assets/{assets[m['asset']]}",
            headers={"Authorization": f"token {token}",
                     "Accept": "application/octet-stream"})
        with urllib.request.urlopen(req, timeout=60) as resp, open(path, "wb") as f:
            f.write(resp.read())
        return path

    urllib.request.urlretrieve(url, path)
    return path


def apply_config_overlay(pack_dir, config_overlay):
    """Merge a tenant's Config Overlay into a STAGED pack, in place.

    For each List id in the Config Overlay that this pack carries, the pinned
    List content is replaced by `pinned ⊕ Config Overlay` — `deep_merge(pinned,
    patch)` — and written back into the staged tree. The List→pack mapping is
    DISCOVERED, not declared: no manifest says which pack owns a List, so we look
    for the List's file under the pack's Lists/ and whichever staged pack carries
    it consumes that id.

    Canonical framework layout is two files per List,
    `Lists/<ListID>/<ListID>.json` (metadata — id/name/type/fromVersion, never
    touched) and `Lists/<ListID>/<ListID>_data.json` (the List content object).
    A single-file `Lists/<ListID>.json` carrying a `data` mapping is also
    accepted; the metadata file has no such key, so it can never be mistaken for
    content.

    Returns the set of List ids this pack CONSUMED. Ids no pack carries are the
    caller's problem (see converge()).
    """
    consumed = set()
    lists_dir = pack_dir / "Lists"
    if not config_overlay or not lists_dir.is_dir():
        return consumed
    for list_id, patch in config_overlay.items():
        for path in sorted(lists_dir.rglob(f"{list_id}_data.json")):
            path.write_text(json.dumps(deep_merge(json.loads(path.read_text()), patch),
                                       indent=4) + "\n")
            consumed.add(list_id)
            break
        else:
            for path in sorted(lists_dir.rglob(f"{list_id}.json")):
                doc = json.loads(path.read_text())
                if not isinstance(doc.get("data"), dict):
                    continue
                doc["data"] = deep_merge(doc["data"], patch)
                path.write_text(json.dumps(doc, indent=4) + "\n")
                consumed.add(list_id)
                break
    return consumed


def sdk_upload(zip_path, creds, config_overlay=None):
    """Upload one fetched release zip to an XSIAM tenant via demisto-sdk.

    Verified live. demisto-sdk needs a content repo (a Packs/ dir) to
    even start, and for XSIAM it uploads a pack DIRECTORY with --xsiam --zip — not a
    raw zip. So we stage the release (whose single top-level dir is the pack) into a
    temp Packs/ and upload that dir from the staged root. TLS is verified (the
    api-<tenant> host has a valid public cert), so no --insecure.

    The staged copy is also where a Config Overlay is applied: the patch is merged
    into the pack's config Lists after extraction and before upload, because XSIAM
    only overwrites a pack's List from inside that same pack. The
    merge is EPHEMERAL — the fetched zip on disk is never modified — so the pinned
    artifact stays byte-for-byte pristine and the deployed pack remains a pure
    function of (Pin, Config Overlay).

    Returns the set of Config Overlay List ids this pack consumed (empty when no
    Config Overlay was passed).
    """
    env = {**os.environ, **creds, "DEMISTO_SDK_IGNORE_CONTENT_WARNING": "1"}
    with tempfile.TemporaryDirectory() as root:
        packs = Path(root) / "Packs"
        packs.mkdir()
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(packs)
        pack_dirs = [d for d in packs.iterdir() if d.is_dir()]
        if len(pack_dirs) != 1:
            raise DeployError(
                f"expected exactly one pack dir in {zip_path}, "
                f"found {[d.name for d in pack_dirs]}"
            )
        consumed = apply_config_overlay(pack_dirs[0], config_overlay)
        subprocess.run(
            ["demisto-sdk", "upload", "-i", f"Packs/{pack_dirs[0].name}", "--xsiam", "--zip"],
            cwd=root, env=env, check=True,
        )
    return consumed


def sdk_upload_source(pack_name, creds):
    """Upload an MSSP-authored pack straight from its in-repo source directory.

    Unlike sdk_upload() (which stages a fetched release zip), local-source packs
    already live in this repo's Packs/, so we upload that directory in place from
    the repo root:

        demisto-sdk upload -i Packs/<pack_name> --xsiam --zip

    `pack_name` is the on-disk directory name under Packs/ (which may differ from
    the pack id in general). Creds are threaded into the subprocess env exactly as
    in sdk_upload(). TLS is verified, so no --insecure.
    """
    env = {**os.environ, **creds, "DEMISTO_SDK_IGNORE_CONTENT_WARNING": "1"}
    subprocess.run(
        ["demisto-sdk", "upload", "-i", f"Packs/{pack_name}", "--xsiam", "--zip"],
        cwd=REPO, env=env, check=True,
    )


# --- planning ---------------------------------------------------------------
def build_plan(r, offline):
    """Return (installs, unresolved, catalog). installs: [{id, version, zip_url, exact, source}].

    MSSP-authored packs (source=="local") deploy from Packs/<name>/ via demisto-sdk
    and are never resolved against the catalog: they carry no zip_url, are always
    exact, and never land in `unresolved`. Upstream packs keep the existing catalog
    resolution path.
    """
    installs, unresolved = [], []
    catalog = None if offline else load_catalog(r["catalog"], r["catalog_pinned_commit"])
    for p in r["base"] + r["extras"]:
        if p.get("source") == "local":
            if p["version"] is None:
                raise DeployError(
                    f"refusing to deploy MSSP-authored pack '{p['id']}': no "
                    f"currentVersion in Packs/<name>/pack_metadata.json"
                )
            installs.append({"id": p["id"], "version": p["version"],
                             "zip_url": None, "exact": True, "source": "local"})
            continue
        item = {"id": p["id"], "version": p["version"], "zip_url": None,
                "exact": None, "source": "upstream"}
        if not offline:
            try:
                art = resolve_artifact(p["id"], p["version"], r["catalog"],
                                       r["catalog_pinned_commit"])
                item["zip_url"], item["exact"] = art["zip_url"], art["exact"]
            except CatalogError as e:
                unresolved.append((p["id"], str(e)))
        installs.append(item)
    return installs, unresolved, catalog


def compute_orphans(r, installed, catalog):
    """Installed MSSP-owned packs no longer resolved, within the deletable set."""
    if catalog is None:
        return None  # cannot bound the set offline without the catalog
    want = set(owned_pins(r))
    deletable = deletable_set(catalog)
    return sorted(pid for pid in installed if pid in deletable and pid not in want)


# --- convergence ------------------------------------------------------------
def converge(tenant, apply, offline, installed_fixture):
    r = resolve(tenant)
    installs, unresolved, catalog = build_plan(r, offline)

    mode = "APPLY" if apply else "DRY-RUN"
    print(f"# [{mode}] converge {r['tenant']} "
          f"(ring={r['ring']}, creds={r['credentials_ref']})")
    print(f"# catalog {r['catalog']}"
          + (f" @ {r['catalog_pinned_commit']}" if r["catalog_pinned_commit"] else ""))

    if unresolved:
        print("\n# !! could not resolve an artifact for:")
        for pid, err in unresolved:
            print(f"#    {pid}: {err.splitlines()[0]}")

    # installed state: live under apply, fixture in dry-run (optional)
    creds = None
    installed = None
    if apply:
        creds = load_creds()
        assert_host_matches(r, creds)
        installed = list_installed(creds)
    elif installed_fixture:
        installed = (load(Path(installed_fixture)) or {}).get(r["tenant"], {})

    orphans = compute_orphans(r, installed, catalog) if installed is not None else None

    # ---- INSTALL / UPDATE ----
    print(f"\n# INSTALL/UPDATE — {len(installs)} MSSP-owned pack(s)")
    for it in installs:
        if it["source"] == "local":
            print(f"  {it['id']:<34} {(it['version'] or '?'):<10} (local source)")
            continue
        tag = "" if it["exact"] in (True, None) else "  (catalog!=pin, retagged)"
        print(f"  {it['id']:<34} {(it['version'] or '?'):<10} {it['zip_url'] or '(offline)'}{tag}")
    for c in r["custom"]:
        print(f"  # custom (owner={c.get('owner','customer')}) {c['id']} — not managed here")

    # ---- CONFIG OVERLAY ----
    # Printed only when the tenant has one, so a tenant with no Config Overlay
    # sees exactly the plan it saw before.
    overlay = r.get("config_overlay") or {}
    if overlay:
        print(f"\n# CONFIG OVERLAY — {len(overlay)} config List(s), pinned ⊕ Config Overlay")
        for list_id in sorted(overlay):
            patch = overlay[list_id]
            keys = ", ".join(sorted(patch)) if isinstance(patch, dict) else "(whole value)"
            print(f"  {list_id:<34} {keys}")
        print("  # merged into whichever staged pack carries the List"
              + ("" if apply else " (at --apply)"))

    # ---- REMOVE ----
    print("\n# REMOVE — orphaned MSSP-owned packs (bounded deletable set)")
    if orphans is None:
        print("  (removal is computed from live tenant state at --apply, or pass"
              " --installed <state.yml> to preview)")
    elif not orphans:
        print("  (none)")
    else:
        for pid in orphans:
            print(f"  DELETE {pid}   (installed {installed.get(pid)}, no longer pinned)")

    if not apply:
        print("\n# dry-run — nothing fetched, uploaded, or deleted.")
        return

    # ---- EXECUTE (apply only) ----
    if unresolved:
        raise DeployError("refusing to apply with unresolved artifacts (see above)")
    print("\n# applying...")
    local = local_packs()  # pack id -> {"version", "name" (on-disk dir)}
    failures = []  # [(id, version, first line of the error)] — Incomplete Converge
    pack_failures = 0  # the subset of `failures` that are pack upserts
    consumed_lists = set()  # Config Overlay List ids a deployed pack carried
    for it in installs:
        print(f"  ↑ upload {it['id']} {it['version']}")
        # Best-effort per pack: the failure unit is ONE pack's fetch + upload, so
        # one pack's transient failure never blocks the rest.
        # No retry here by design — re-running the tenant's converge job is the
        # retry. KeyboardInterrupt/SystemExit still abort the run.
        try:
            if it["source"] == "local":
                # MSSP-authored: upload straight from Packs/<dir>/ — no fetch.
                dir_name = local.get(it["id"], {}).get("name", it["id"])
                sdk_upload_source(dir_name, creds)
            else:
                zpath = download(it["zip_url"])
                try:
                    # The Config Overlay is offered to every fetched pack; the
                    # pack that CARRIES a patched List consumes its id, and the
                    # merge rides this pack's failure unit. With
                    # no Config Overlay this is exactly the previous call.
                    if overlay:
                        consumed_lists |= sdk_upload(zpath, creds, config_overlay=overlay)
                    else:
                        sdk_upload(zpath, creds)
                finally:
                    os.unlink(zpath)
        except Exception as e:  # noqa: BLE001
            first = (str(e).splitlines() or [""])[0] or e.__class__.__name__
            failures.append((it["id"], it["version"], first))
            pack_failures += 1
            print(f"  ✗ FAILED {it['id']} {it['version']}: {first}")

    # A patched List no deployed pack carried is a silent no-op at deploy time —
    # a typo'd id, a List that moved packs, or the pack that carries it having
    # failed above (a failed upload consumes nothing). Treat it as a failure so
    # the run follows Incomplete Converge semantics unchanged.
    for list_id in sorted(set(overlay) - consumed_lists):
        err = f"no deployed pack carries List '{list_id}'"
        failures.append((f"config-overlay:{list_id}", "-", err))
        print(f"  ✗ FAILED config-overlay:{list_id}: {err}")

    to_delete = list(orphans or [])
    deleted = 0
    if to_delete:
        if failures:
            # Orphan removal runs only after a fully successful upsert pass: a
            # replacement whose new pack failed to upload would otherwise leave
            # the tenant with neither pack. Deletes are self-healing on the
            # re-run.
            for pid in to_delete:
                print(f"  ⚠ hold delete {pid} — upsert failures, REMOVE held")
        elif os.environ.get("FLEET_ALLOW_DELETE") == "true":
            # Printed before the call; delete_packs raises on any non-200, so a
            # failed delete still fails the job. ✗ is reserved for failures.
            for pid in to_delete:
                print(f"  − delete {pid}")
            delete_packs(creds, to_delete)          # one batch POST for all orphans
            deleted = len(to_delete)
        else:
            for pid in to_delete:
                print(f"  ⚠ skip delete {pid} — set FLEET_ALLOW_DELETE=true to enable")
    skipped = len(to_delete) - deleted
    if failures:
        upserted = len(installs) - pack_failures
        print(f"\n# converge INCOMPLETE for {r['tenant']}: {upserted} upserted, "
              f"{len(failures)} FAILED (REMOVE held)")
        for pid, ver, err in failures:
            print(f"#    {pid} {ver}: {err}")
        detail = f"{pack_failures} of {len(installs)} pack upserts failed"
        unconsumed = len(failures) - pack_failures
        if unconsumed:
            detail += f"; {unconsumed} Config Overlay List id(s) unconsumed"
        raise DeployError(f"{detail} for {r['tenant']}")
    print(f"\n# converged {r['tenant']}: {len(installs)} upserted, {deleted} deleted"
          f"{f' ({skipped} skipped)' if skipped else ''}.")


def main():
    ap = argparse.ArgumentParser(description="Converge one tenant via demisto-sdk.")
    ap.add_argument("tenant")
    ap.add_argument("--apply", action="store_true", help="perform fetch/upload/delete")
    ap.add_argument("--offline", action="store_true",
                    help="skip catalog network calls (plan only, no zip URLs)")
    ap.add_argument("--installed", help="dry-run: YAML of installed versions per tenant"
                                        " to preview orphan deletions")
    args = ap.parse_args()

    try:
        converge(args.tenant, args.apply, args.offline, args.installed)
    except (ResolveError, DeployError, CatalogError) as e:
        sys.exit(f"ERROR: {e}")
    except subprocess.CalledProcessError as e:
        sys.exit(f"ERROR: demisto-sdk upload failed ({e.returncode})")


if __name__ == "__main__":
    main()
