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
passes the workflow token (contents: read is enough). The check is SKIPPED with
a notice — never failed — when it cannot see the releases at all:
  * no token (offline local runs);
  * release_base is not on the GitHub this workflow runs on (the token is only
    valid there);
  * release_base names a repo the token cannot read (packs moved to their own
    repo, or the YOUR-ORG placeholder not yet replaced).
Works on github.com, <company>.ghe.com and GitHub Enterprise Server alike: the
host and API come from GITHUB_SERVER_URL / GITHUB_API_URL (see github_host.py).
Any other API failure (rate limit, 5xx, network) fails with a clear message.

Usage:
    GH_TOKEN=... python scripts/release_check.py
"""
import functools
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

import yaml

import resolve
from github_host import api_url, parse_release_base, server_url
from mssp_catalog import release_base


def pinned_versions(fleet):
    """[(where, pack_id, version)] for every exact pin in pin files and overrides."""
    out = []
    for f in sorted((fleet / "pins").glob("*.yml")):
        for pid, ver in ((yaml.safe_load(f.read_text()) or {}).get("pins") or {}).items():
            if ver not in (None, ""):  # an empty pin is resolve.py's error to report
                out.append((f"fleet/pins/{f.name}", pid, str(ver)))
    for f in sorted((fleet / "tenants").glob("*.yml")):
        overrides = (yaml.safe_load(f.read_text()) or {}).get("pin_overrides") or {}
        for pid, ver in overrides.items():
            if ver not in (None, ""):
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


class ReleaseLookupError(Exception):
    """The releases API could not be queried (not a plain 'no such release')."""


def _get_json(url, token):
    req = urllib.request.Request(url, headers={
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def repo_readable(owner, repo, token):
    """True if the token can see owner/repo (GitHub answers 404 for both
    'missing' and 'no access', so either way the releases are invisible)."""
    try:
        _get_json(f"{api_url()}/repos/{owner}/{repo}", token)
        return True
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        raise ReleaseLookupError(f"GET repos/{owner}/{repo}: HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise ReleaseLookupError(f"GET repos/{owner}/{repo}: {e.reason}") from e


def github_has_release(owner, repo, token):
    """has_release(tag) backed by the GitHub releases API, one call per tag."""
    @functools.lru_cache(maxsize=None)
    def has_release(tag):
        try:
            rel = _get_json(
                f"{api_url()}/repos/{owner}/{repo}/releases/tags/{tag}", token)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return False
            raise ReleaseLookupError(f"release {tag}: HTTP {e.code}") from e
        except urllib.error.URLError as e:
            raise ReleaseLookupError(f"release {tag}: {e.reason}") from e
        return f"{tag}.zip" in {a.get("name") for a in rel.get("assets", [])}
    return has_release


def skip(reason):
    print(f"· release check skipped — {reason}")


def main():
    current = {pid: info.get("version") for pid, info in resolve.local_packs().items()}
    pins = pinned_versions(resolve.FLEET)
    if not any(pid in current and ver != "local" for _, pid, ver in pins):
        print("✓ release check — no exact MSSP pack pins to check")
        return

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        return skip("no GH_TOKEN/GITHUB_TOKEN (runs in ring-gate)")
    base = release_base()
    parsed = parse_release_base(base)
    if not parsed:
        return skip(f"release_base '{base}' is not a releases URL on {server_url()}, "
                    f"the GitHub this workflow's token belongs to")
    owner, repo = parsed

    try:
        this_repo = os.environ.get("GITHUB_REPOSITORY", "").lower()
        if f"{owner}/{repo}".lower() != this_repo and not repo_readable(owner, repo, token):
            return skip(f"this workflow's token can't read {owner}/{repo} (release_base)")
        found = problems(pins, current, github_has_release(owner, repo, token))
    except ReleaseLookupError as e:
        sys.exit(f"ERROR: could not query releases in {owner}/{repo} ({e}) — "
                 f"re-run the check; a rate limit or GitHub outage is usually transient")

    if found:
        print(f"✗ {len(found)} pinned MSSP pack version(s) can never deploy:")
        for msg in found:
            print(f"  - {msg}")
        sys.exit(1)
    print("✓ release check — every pinned MSSP pack version is released or "
          "is the current version in Packs/")


if __name__ == "__main__":
    main()
