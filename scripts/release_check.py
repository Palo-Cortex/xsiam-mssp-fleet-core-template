#!/usr/bin/env python3
"""
release_check.py — every pinned MSSP pack version must be deployable.

An MSSP-authored pack (source under Packs/) pinned to an exact version deploys
the release zip `<id>-v<version>.zip`. catalog.py builds that URL for ANY
version (it retags the catalog's URL), so a typo'd or never-released version
passes resolve.py and ring-gate today and only fails at converge, against a
live tenant. This check moves that failure to the PR.

A pinned version is deployable when either:
  * it equals the pack's current `currentVersion` in Packs/ — the release job
    at the start of converge publishes it on merge, so a pack bump and its pin
    may land in the same PR; or
  * the release `<id>-v<version>` already carries its `<id>-v<version>.zip`.

Checked everywhere a version can be pinned: fleet/pins/*.yml and every tenant's
pin_overrides. `local` pins and packs not under Packs/ (upstream) are skipped.
Releases are looked up in the repo named by fleet/defaults.yml
mssp_catalog.release_base — the same place converge downloads from.

Needs GH_TOKEN (or GITHUB_TOKEN) to read a private repo's releases; ring-gate
passes the workflow token (contents: read is enough). Without a token the
check is skipped with a notice, so it can run in offline local checks.

Usage:
    GH_TOKEN=... python scripts/release_check.py
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

import yaml

import resolve
from mssp_catalog import release_base

_REPO_RE = re.compile(r"https://github\.com/([^/]+)/([^/]+)/releases/download/?$")


def pinned_versions(fleet):
    """[(where, pack_id, version)] for every exact pin in pin files and overrides."""
    out = []
    for f in sorted((fleet / "pins").glob("*.yml")):
        for pid, ver in ((yaml.safe_load(f.read_text()) or {}).get("pins") or {}).items():
            out.append((f"fleet/pins/{f.name}", pid, str(ver)))
    for f in sorted((fleet / "tenants").glob("*.yml")):
        overrides = (yaml.safe_load(f.read_text()) or {}).get("pin_overrides") or {}
        for pid, ver in overrides.items():
            out.append((f"fleet/tenants/{f.name} pin_overrides", pid, str(ver)))
    return out


def problems(pins, current_versions, has_release):
    """Messages for pins that can never deploy.

    `current_versions` is {mssp pack id: currentVersion}; `has_release(tag)` is
    True when the release for `tag` carries its zip.
    """
    out = []
    for where, pid, ver in pins:
        if pid not in current_versions or ver == "local":
            continue
        if ver == current_versions[pid]:
            continue  # released by the release job when this merges
        tag = f"{pid}-v{ver}"
        if not has_release(tag):
            out.append(
                f"{where}: {pid} {ver} has no release ({tag}.zip) and isn't the "
                f"current version in Packs/ ({current_versions[pid]}), so it can "
                f"never be deployed — released versions are immutable")
    return out


def github_has_release(owner, repo, token):
    """has_release(tag) backed by the GitHub releases API."""
    def has_release(tag):
        req = urllib.request.Request(
            f"https://api.github.com/repos/{owner}/{repo}/releases/tags/{tag}",
            headers={"Authorization": f"token {token}",
                     "Accept": "application/vnd.github+json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                rel = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return False
            raise
        return f"{tag}.zip" in {a.get("name") for a in rel.get("assets", [])}
    return has_release


def main():
    current = {pid: info.get("version") for pid, info in resolve.local_packs().items()}
    pins = pinned_versions(resolve.FLEET)
    if not any(pid in current and ver != "local" for _, pid, ver in pins):
        print("✓ release check — no exact MSSP pack pins to check")
        return

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        print("· release check skipped — no GH_TOKEN/GITHUB_TOKEN (runs in ring-gate)")
        return
    m = _REPO_RE.match(release_base())
    if not m:
        sys.exit(f"ERROR: mssp_catalog.release_base '{release_base()}' is not a "
                 f"https://github.com/<owner>/<repo>/releases/download URL")

    found = problems(pins, current, github_has_release(m[1], m[2], token))
    if found:
        print(f"✗ {len(found)} pinned MSSP pack version(s) can never deploy:")
        for msg in found:
            print(f"  - {msg}")
        sys.exit(1)
    print("✓ release check — every pinned MSSP pack version is released or "
          "is the current version in Packs/")


if __name__ == "__main__":
    main()
