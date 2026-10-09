"""release_check.py — a pinned MSSP pack version must be released, or be the
version this merge releases.

problems() is driven directly with a fake has_release, so no network; the
pin-collection half runs against a temp fleet; main()'s skip/fail paths stub
the API call.
"""
import urllib.error

import pytest

import release_check

CURRENT = {"ExamplePack": "1.2.0"}


def _released(*tags):
    return lambda tag: tag in tags


def test_current_version_needs_no_release_yet():
    """A pack bump and its pin in one PR: converge's release job publishes it."""
    pins = [("fleet/pins/prod.yml", "ExamplePack", "1.2.0")]
    assert release_check.problems(pins, CURRENT, _released()) == []


def test_older_released_version_passes():
    pins = [("fleet/pins/prod.yml", "ExamplePack", "1.1.0")]
    assert release_check.problems(pins, CURRENT, _released("ExamplePack-v1.1.0")) == []


def test_unreleased_non_current_version_fails():
    """A typo (1.1.9) or a version that was skipped can never deploy."""
    pins = [("fleet/pins/prod.yml", "ExamplePack", "1.1.9")]
    found = release_check.problems(pins, CURRENT, _released("ExamplePack-v1.1.0"))
    assert len(found) == 1
    assert "ExamplePack 1.1.9 has no release" in found[0]
    assert "fleet/pins/prod.yml" in found[0]


def test_local_and_upstream_pins_are_not_checked():
    def boom(tag):
        raise AssertionError(f"looked up {tag}")
    pins = [("fleet/pins/dev.yml", "ExamplePack", "local"),
            ("fleet/pins/prod.yml", "soc-optimization-unified", "3.10.3")]
    assert release_check.problems(pins, CURRENT, boom) == []


def test_pin_overrides_are_collected(temp_fleet):
    temp_fleet.pins("prod", {"ExamplePack": "1.2.0"})
    temp_fleet.tenant("t_held", ring="prod", extras=["ExamplePack"],
                      pin_overrides={"ExamplePack": "1.0.5"})

    pins = release_check.pinned_versions(temp_fleet.root)

    assert ("fleet/pins/prod.yml", "ExamplePack", "1.2.0") in pins
    assert ("fleet/tenants/t_held.yml pin_overrides", "ExamplePack", "1.0.5") in pins
    found = release_check.problems(pins, CURRENT, _released())
    assert len(found) == 1 and "t_held.yml pin_overrides" in found[0]


# --- main(): when the check skips, fails, or caches ---------------------------
OLD_PIN = [("fleet/pins/prod.yml", "ExamplePack", "1.1.0")]


def _main_env(monkeypatch, base, repo="acme/fleet", token="tok"):
    monkeypatch.setattr(release_check.resolve, "local_packs",
                        lambda: {"ExamplePack": {"version": "1.2.0"}})
    monkeypatch.setattr(release_check, "pinned_versions", lambda fleet: list(OLD_PIN))
    monkeypatch.setattr(release_check, "release_base", lambda: base)
    monkeypatch.setenv("GITHUB_REPOSITORY", repo)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    if token:
        monkeypatch.setenv("GH_TOKEN", token)
    else:
        monkeypatch.delenv("GH_TOKEN", raising=False)


def _http(code):
    return urllib.error.HTTPError("u", code, "x", {}, None)


def test_release_base_on_another_host_skips(monkeypatch, capsys):
    """release_base on a different GitHub than the one the workflow runs on:
    the token isn't valid there, so the gate can't verify and must not fail."""
    _main_env(monkeypatch, "https://ghe.example.com/acme/fleet/releases/download")
    release_check.main()
    assert "skipped" in capsys.readouterr().out


def test_unreadable_other_repo_skips(monkeypatch, capsys):
    """Packs moved to another repo the workflow token can't read."""
    _main_env(monkeypatch, "https://github.com/acme/packs/releases/download")

    def fake_get(url, token):
        raise _http(404)
    monkeypatch.setattr(release_check, "_get_json", fake_get)

    release_check.main()
    assert "can't read acme/packs" in capsys.readouterr().out


def test_rate_limit_fails_with_clear_message(monkeypatch):
    _main_env(monkeypatch, "https://github.com/acme/fleet/releases/download")

    def fake_get(url, token):
        raise _http(403)
    monkeypatch.setattr(release_check, "_get_json", fake_get)

    with pytest.raises(SystemExit) as exc:
        release_check.main()
    assert "could not query releases" in str(exc.value)


def test_same_tag_is_looked_up_once(monkeypatch):
    """The same version pinned in several rings/overrides costs one API call."""
    calls = []
    monkeypatch.setattr(release_check, "_get_json",
                        lambda url, token: calls.append(url) or {"assets": []})
    has_release = release_check.github_has_release("acme", "fleet", "tok")

    assert has_release("ExamplePack-v1.1.0") is False
    assert has_release("ExamplePack-v1.1.0") is False
    assert len(calls) == 1


def test_empty_pin_values_are_ignored(temp_fleet):
    temp_fleet.pins("prod", {"ExamplePack": None})
    assert release_check.pinned_versions(temp_fleet.root) == []
