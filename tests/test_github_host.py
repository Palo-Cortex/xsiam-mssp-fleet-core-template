"""github_host.py — the fleet works on github.com, <company>.ghe.com and GHES.

The host and API root come from GITHUB_SERVER_URL / GITHUB_API_URL (set by
Actions in every job). Only release URLs on THIS server get the workflow token;
release URLs elsewhere (upstream on github.com while the fleet runs on GHES)
are public downloads and must never be sent to this server's API.
"""
import pytest

import deploy_tenant
import github_host
import release_check
from test_download_private_assets import _install_fakes

GHES = "https://github.acme.com"
GHES_API = "https://github.acme.com/api/v3"
GHES_ASSET = f"{GHES}/mssp/fleet/releases/download/ExamplePack-v1.0.0/ExamplePack-v1.0.0.zip"
UPSTREAM_ASSET = ("https://github.com/Palo-Cortex/secops-framework/releases/download/"
                  "soc-x-v1.0.0/soc-x-v1.0.0.zip")


@pytest.fixture
def on_ghes(monkeypatch):
    monkeypatch.setenv("GITHUB_SERVER_URL", GHES)
    monkeypatch.setenv("GITHUB_API_URL", GHES_API)


# --- server / API detection -------------------------------------------------
def test_defaults_to_github_com():
    assert github_host.server_url() == "https://github.com"
    assert github_host.api_url() == "https://api.github.com"


@pytest.mark.parametrize("server,api", [
    ("https://github.acme.com", "https://github.acme.com/api/v3"),   # GHES
    ("https://acme.ghe.com", "https://api.acme.ghe.com"),            # data residency
    ("https://github.com", "https://api.github.com"),
])
def test_api_url_derived_from_server_when_not_set(monkeypatch, server, api):
    monkeypatch.setenv("GITHUB_SERVER_URL", server)
    assert github_host.api_url() == api


def test_explicit_api_url_wins(monkeypatch):
    monkeypatch.setenv("GITHUB_SERVER_URL", GHES)
    monkeypatch.setenv("GITHUB_API_URL", "https://proxy.acme.com/gh/api/")
    assert github_host.api_url() == "https://proxy.acme.com/gh/api"


# --- which URLs count as this server's releases -------------------------------
def test_release_urls_on_this_server_parse(on_ghes):
    assert github_host.parse_release_asset(GHES_ASSET) == {
        "owner": "mssp", "repo": "fleet",
        "tag": "ExamplePack-v1.0.0", "asset": "ExamplePack-v1.0.0.zip"}
    assert github_host.parse_release_base(
        "https://GITHUB.acme.com/mssp/fleet/releases/download/") == ("mssp", "fleet")


def test_release_urls_on_other_hosts_do_not_parse(on_ghes):
    assert github_host.parse_release_asset(UPSTREAM_ASSET) is None
    assert github_host.parse_release_base(
        "https://github.com/mssp/fleet/releases/download") is None
    assert github_host.parse_release_asset(GHES_ASSET.replace("https", "http")) is None


# --- deploy_tenant.download() on GHES -----------------------------------------
def test_private_asset_on_ghes_uses_ghes_api(on_ghes, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "tok123")
    calls, retrieved = _install_fakes(
        monkeypatch, {"assets": [{"name": "ExamplePack-v1.0.0.zip", "id": 42}]})

    deploy_tenant.download(GHES_ASSET)

    assert [c["url"] for c in calls] == [
        f"{GHES_API}/repos/mssp/fleet/releases/tags/ExamplePack-v1.0.0",
        f"{GHES_API}/repos/mssp/fleet/releases/assets/42"]
    assert retrieved == []


def test_upstream_github_com_asset_on_ghes_is_a_plain_download(on_ghes, monkeypatch):
    """The GHES token must not be sent anywhere for a github.com URL."""
    monkeypatch.setenv("GH_TOKEN", "tok123")
    calls, retrieved = _install_fakes(monkeypatch, {"assets": []})

    deploy_tenant.download(UPSTREAM_ASSET)

    assert calls == []
    assert retrieved == [UPSTREAM_ASSET]


# --- release_check on GHES -----------------------------------------------------
def test_release_check_queries_ghes_api(on_ghes, monkeypatch, capsys):
    monkeypatch.setattr(release_check.resolve, "local_packs",
                        lambda: {"ExamplePack": {"version": "1.2.0"}})
    monkeypatch.setattr(release_check, "pinned_versions",
                        lambda fleet: [("fleet/pins/prod.yml", "ExamplePack", "1.1.0")])
    monkeypatch.setattr(release_check, "release_base",
                        lambda: f"{GHES}/mssp/fleet/releases/download")
    monkeypatch.setenv("GITHUB_REPOSITORY", "mssp/fleet")
    monkeypatch.setenv("GH_TOKEN", "tok")
    urls = []
    monkeypatch.setattr(release_check, "_get_json", lambda url, token: urls.append(url) or
                        {"assets": [{"name": "ExamplePack-v1.1.0.zip"}]})

    release_check.main()

    assert urls == [f"{GHES_API}/repos/mssp/fleet/releases/tags/ExamplePack-v1.1.0"]
    assert "✓ release check" in capsys.readouterr().out
