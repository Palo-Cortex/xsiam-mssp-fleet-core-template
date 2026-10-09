"""release_check.py — a pinned MSSP pack version must be released, or be the
version this merge releases.

problems() is driven directly with a fake has_release, so no network; the
pin-collection half runs against a temp fleet.
"""
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
