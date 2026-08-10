"""deploy_tenant.download() — private GitHub release assets via the assets API.

A browser_download_url on a private repo 404s even with a token; the working
path is tag -> asset id -> /releases/assets/<id> with Accept: octet-stream.
Contract:

  * GitHub release URL + token in env  -> two API calls (release-by-tag lookup,
    asset download) with the token; urlretrieve is NOT used.
  * GitHub release URL + no token      -> plain urlretrieve (public repo path).
  * Non-GitHub URL                     -> plain urlretrieve even with a token.
  * Asset name missing from the release -> DeployError naming the tag.
"""
import io
import json

import pytest

import deploy_tenant

RELEASE_URL = ("https://github.com/acme/fleet/releases/download/"
               "ExamplePack-v1.0.0/ExamplePack-v1.0.0.zip")


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _install_fakes(monkeypatch, release_json, asset_bytes=b"ZIPBYTES"):
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append({
            "url": req.full_url,
            "headers": {k.lower(): v for k, v in req.header_items()},
        })
        if "/releases/tags/" in req.full_url:
            return _Resp(json.dumps(release_json).encode())
        return _Resp(asset_bytes)

    retrieved = []
    monkeypatch.setattr(deploy_tenant.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(deploy_tenant.urllib.request, "urlretrieve",
                        lambda url, path: retrieved.append(url))
    return calls, retrieved


def test_private_asset_downloaded_via_assets_api(monkeypatch, tmp_path):
    monkeypatch.setenv("GH_TOKEN", "tok123")
    calls, retrieved = _install_fakes(monkeypatch, {
        "assets": [{"name": "ExamplePack-v1.0.0.zip", "id": 42}],
    })

    path = deploy_tenant.download(RELEASE_URL)

    assert retrieved == [], "must not fall back to plain urlretrieve"
    assert calls[0]["url"] == (
        "https://api.github.com/repos/acme/fleet/releases/tags/ExamplePack-v1.0.0")
    assert calls[1]["url"] == "https://api.github.com/repos/acme/fleet/releases/assets/42"
    assert calls[1]["headers"]["accept"] == "application/octet-stream"
    for c in calls:
        assert c["headers"]["authorization"] == "token tok123"
    assert open(path, "rb").read() == b"ZIPBYTES"


def test_no_token_uses_plain_download(monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    calls, retrieved = _install_fakes(monkeypatch, {})

    deploy_tenant.download(RELEASE_URL)

    assert calls == []
    assert retrieved == [RELEASE_URL]


def test_non_github_url_ignores_token(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "tok123")
    calls, retrieved = _install_fakes(monkeypatch, {})

    url = "https://example.invalid/dl/some-pack-v1.0.0.zip"
    deploy_tenant.download(url)

    assert calls == []
    assert retrieved == [url]


def test_missing_asset_raises_deployerror(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "tok123")
    _install_fakes(monkeypatch, {"assets": [{"name": "other.zip", "id": 7}]})

    with pytest.raises(deploy_tenant.DeployError) as exc:
        deploy_tenant.download(RELEASE_URL)
    assert "ExamplePack-v1.0.0" in str(exc.value)
