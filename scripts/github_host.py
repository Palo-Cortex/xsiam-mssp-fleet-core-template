#!/usr/bin/env python3
"""
github_host.py — which GitHub this fleet runs on, and its release URLs.

The fleet's own pack releases live on the same GitHub as the repo, which is
github.com, GitHub Enterprise Cloud with data residency (<company>.ghe.com), or
GitHub Enterprise Server (your own hostname). Actions sets two variables in
every job that say which:

    GITHUB_SERVER_URL   https://github.com | https://acme.ghe.com | https://github.acme.com
    GITHUB_API_URL      https://api.github.com | https://api.acme.ghe.com | https://github.acme.com/api/v3

Outside Actions (local runs) neither is set and github.com is assumed.

Only URLs on THIS server are treated as releases the workflow token can read.
A release URL on any other host — e.g. the upstream SOC Framework on github.com
while the fleet runs on GHES — is public content: it's downloaded without the
token, and never sent to this server's API.
"""
import os
import re
import urllib.parse

GITHUB_COM = "https://github.com"

_RELEASE_PATH = re.compile(
    r"^/(?P<owner>[^/]+)/(?P<repo>[^/]+)/releases/download/(?P<tag>[^/]+)/(?P<asset>[^/]+)$")
_RELEASE_BASE_PATH = re.compile(r"^/(?P<owner>[^/]+)/(?P<repo>[^/]+)/releases/download/?$")


def server_url():
    """This GitHub's web root, e.g. https://github.acme.com (no trailing slash)."""
    return (os.environ.get("GITHUB_SERVER_URL") or GITHUB_COM).rstrip("/")


def api_url():
    """This GitHub's REST API root. Actions sets GITHUB_API_URL; the fallbacks
    cover local runs that only set GITHUB_SERVER_URL."""
    explicit = os.environ.get("GITHUB_API_URL")
    if explicit:
        return explicit.rstrip("/")
    server = server_url()
    host = urllib.parse.urlsplit(server).hostname or ""
    if server == GITHUB_COM:
        return "https://api.github.com"
    if host.endswith(".ghe.com"):
        return f"https://api.{host}"
    return f"{server}/api/v3"  # GitHub Enterprise Server


def _on_this_server(url):
    """(path) of `url` when it's an https URL on this server's host, else None."""
    u, s = urllib.parse.urlsplit(url), urllib.parse.urlsplit(server_url())
    if u.scheme != "https" or (u.hostname or "").lower() != (s.hostname or "").lower():
        return None
    return u.path


def parse_release_asset(url):
    """{owner, repo, tag, asset} for a release-asset download URL on this
    server, or None for anything else (another host, or not a release URL)."""
    path = _on_this_server(url)
    m = _RELEASE_PATH.match(path) if path is not None else None
    return m.groupdict() if m else None


def parse_release_base(base):
    """(owner, repo) for a `<server>/<owner>/<repo>/releases/download` base on
    this server, or None."""
    path = _on_this_server(base)
    m = _RELEASE_BASE_PATH.match(path) if path is not None else None
    return (m["owner"], m["repo"]) if m else None
