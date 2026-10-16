"""Tests for scripts/tenant_tui.py.

The model layer (validation, rendering, file writes, pin append) is tested
directly against the temp_fleet fixture; the UI gets headless pilot tests for
the main flows (create, save-with-missing-pins, edit, delete).
"""
import asyncio
import re

import pytest
import yaml

import tenant_tui
from tenant_tui import TenantTUIApp


def _manifest(name="acme01", ring="dev", **kw):
    # Define variable (dict) of a minimal valid tenant manifest
    m = {
        "tenant": name,
        "display_name": f"{name} (test)",
        "ring": ring,
        "credentials_ref": name.upper(),
        "groups": [],
        "extras": [],
        "pin_overrides": {},
        "custom": [],
    }
    m.update(kw)
    return m


# ---------------------------------------------------------------- model layer

def test_list_tenants_status(temp_fleet):
    temp_fleet.pins("dev", {"pack-a": "1.0.0"})
    temp_fleet.tenant("good01", ring="dev", extras=["pack-a"])
    temp_fleet.tenant("bad01", ring="dev", extras=["pack-b"])
    # Define variable (dict) of tenant name to row, for easy lookup
    rows = {r["name"]: r for r in tenant_tui.list_tenants()}
    assert rows["good01"]["ok"] is True
    assert rows["bad01"]["ok"] is False
    assert "pack-b" in rows["bad01"]["status"]


def test_validate_manifest_clean(temp_fleet):
    temp_fleet.pins("dev", {"pack-a": "1.0.0"})
    # Define variables (list, list) of validation errors and warnings
    errors, warnings = tenant_tui.validate_manifest(
        _manifest(extras=["pack-a"]), "acme01")
    assert errors == []


def test_validate_manifest_unpinned(temp_fleet):
    temp_fleet.pins("dev", {"pack-a": "1.0.0"})
    # Define variable (list) of validation errors, warnings ignored
    errors, _ = tenant_tui.validate_manifest(
        _manifest(extras=["pack-b"]), "acme01")
    assert any("pack-b" in e for e in errors)


def test_validate_manifest_format_errors(temp_fleet):
    temp_fleet.pins("dev", {})
    # Define variable (list) of validation errors, warnings ignored
    errors, _ = tenant_tui.validate_manifest(_manifest(), "Acme01")
    assert any("lowercase" in e for e in errors)
    errors, _ = tenant_tui.validate_manifest(_manifest(tenant="other"), "acme01")
    assert any("must equal" in e for e in errors)
    temp_fleet.tenant("acme01", ring="dev")
    errors, _ = tenant_tui.validate_manifest(_manifest(), "acme01")
    assert any("already exists" in e for e in errors)
    errors, _ = tenant_tui.validate_manifest(_manifest(), "acme01", exists=True)
    assert not any("already exists" in e for e in errors)


def test_validate_manifest_groups_and_namespaces(temp_fleet):
    temp_fleet.pins("dev", {"pack-a": "1.0.0"})
    temp_fleet.group("g1", ["pack-a"])
    # Define variable (list) of validation errors, warnings ignored
    errors, _ = tenant_tui.validate_manifest(_manifest(groups=["g1"]), "acme01")
    assert errors == []
    errors, _ = tenant_tui.validate_manifest(_manifest(groups=["nope"]), "acme01")
    assert any("nope" in e for e in errors)
    errors, _ = tenant_tui.validate_manifest(
        _manifest(customer_namespace="zz-"), "acme01")
    assert any("customer_namespace" in e for e in errors)
    errors, _ = tenant_tui.validate_manifest(
        _manifest(custom=[{"id": "noprefix", "owner": "customer"}]), "acme01")
    assert any("noprefix" in e for e in errors)
    errors, _ = tenant_tui.validate_manifest(
        _manifest(custom=[{"id": "acme-x", "owner": "customer"}]), "acme01")
    assert errors == []


def test_missing_pins(temp_fleet):
    temp_fleet.pins("dev", {"pack-a": "1.0.0"})
    temp_fleet.group("g1", ["pack-a", "pack-b"])
    # Define variable (dict) of a manifest composing g1 plus an override
    m = _manifest(groups=["g1"], extras=["pack-c"],
                  pin_overrides={"pack-c": "2.0.0"})
    assert tenant_tui.missing_pins(m) == [("pack-b", "group g1")]
    # an override satisfies the pin requirement
    m2 = _manifest(extras=["pack-b"], pin_overrides={"pack-b": "local"})
    assert tenant_tui.missing_pins(m2) == []
    # base packs count too (all pinned here, so nothing missing)
    temp_fleet.tenant("base01", ring="dev", base=["pack-a"])
    assert tenant_tui.missing_pins(_manifest()) == []


def test_unused_pins(temp_fleet):
    temp_fleet.pins("dev", {"pack-a": "1.0.0", "pack-b": "2.0.0",
                            "pack-c": "3.0.0"})
    temp_fleet.tenant("t1", ring="dev", extras=["pack-a"])
    # t2 composes pack-b but overrides it, so the ring pin for pack-b is unused
    temp_fleet.tenant("t2", ring="dev", extras=["pack-b"],
                      pin_overrides={"pack-b": "9.9.9"})
    # pack-a is needed (t1); pack-b's ring pin is unused (t2 overrides it);
    # pack-c is composed by nobody.
    assert tenant_tui.unused_pins("dev") == [("pack-b", "2.0.0"),
                                             ("pack-c", "3.0.0")]


def test_unused_pins_with_editing(temp_fleet):
    temp_fleet.pins("dev", {"pack-a": "1.0.0", "pack-b": "2.0.0"})
    temp_fleet.tenant("t1", ring="dev", extras=["pack-a", "pack-b"])
    # In-progress edit drops pack-b from t1 -> its pin becomes unused.
    m = _manifest("t1", ring="dev", extras=["pack-a"])
    assert tenant_tui.unused_pins("dev", editing=("t1", m)) == \
        [("pack-b", "2.0.0")]
    # ...and while the edit still composes pack-b, nothing is unused.
    m2 = _manifest("t1", ring="dev", extras=["pack-a", "pack-b"])
    assert tenant_tui.unused_pins("dev", editing=("t1", m2)) == []
    # A not-yet-saved tenant counts too: its composition protects its pins.
    m3 = _manifest("new01", ring="dev", extras=["pack-b"])
    assert tenant_tui.unused_pins("dev", editing=("new01", m3)) == []


def test_remove_pins(temp_fleet):
    # Define variable (Path) of the pins dir, created for the test
    pins_dir = temp_fleet.root / "pins"
    pins_dir.mkdir(parents=True, exist_ok=True)
    # Define variable (Path) of the pin file to hand-write
    path = pins_dir / "dev.yml"
    path.write_text(
        "# header comment\n"
        "ring: dev\n"
        "pins:\n"
        "  pack-a: 1.0.0\n"
        "  pack-b: 2.0.0   # inline comment goes with the pin\n"
        "  pack-c: 3.0.0\n")
    tenant_tui.remove_pins("dev", ["pack-b"])
    # Define variable (str) of the pin file after the removal
    text = path.read_text()
    assert "pack-b" not in text
    assert "inline comment" not in text
    assert text.startswith("# header comment")
    assert yaml.safe_load(text)["pins"] == {"pack-a": "1.0.0",
                                             "pack-c": "3.0.0"}
    # removing the last pins leaves a valid (empty) pins block
    tenant_tui.remove_pins("dev", ["pack-a", "pack-c"])
    assert yaml.safe_load(path.read_text())["pins"] in (None, {})


def test_remove_pins_errors(temp_fleet):
    temp_fleet.pins("dev", {"pack-a": "1.0.0"})
    with pytest.raises(tenant_tui.TuiError):
        tenant_tui.remove_pins("dev", ["nope"])
    with pytest.raises(tenant_tui.TuiError):
        tenant_tui.remove_pins("qa", ["pack-a"])


def test_render_yaml_roundtrip_and_order(temp_fleet):
    # Define variable (dict) of a manifest exercising every key
    m = _manifest(
        display_name="Acme Corp",
        groups=["g1"],
        extras=["VirusTotal"],
        pin_overrides={"pack-a": "1.0"},
        custom=[{"id": "acme-x", "owner": "customer"}],
        host_token="acmecorp01",
    )
    # Define variable (str) of the rendered YAML
    text = tenant_tui.render_yaml(m)
    # Define variable (dict) of the re-parsed YAML
    data = yaml.safe_load(text)
    assert data["tenant"] == "acme01"
    assert data["pin_overrides"] == {"pack-a": "1.0"}  # version stays a string
    # Define variable (list) of the top-level keys in file order
    top = [ln.split(":")[0] for ln in text.splitlines()
           if re.match(r"^[a-z_]+:", ln)]
    assert top.index("tenant") < top.index("ring")
    assert top.index("ring") < top.index("groups") < top.index("custom")


def test_save_delete_rename(temp_fleet):
    # Define variable (dict) of a minimal manifest to save
    m = _manifest()
    # Define variable (Path) of the saved tenant file
    path = tenant_tui.save_tenant("acme01", m)
    assert path.exists()
    assert yaml.safe_load(path.read_text())["tenant"] == "acme01"
    tenant_tui.rename_tenant("acme01", "acme02")
    assert not path.exists()
    # Define variable (Path) of the renamed tenant file
    new_path = temp_fleet.root / "tenants" / "acme02.yml"
    assert new_path.exists()
    assert yaml.safe_load(new_path.read_text())["tenant"] == "acme02"
    with pytest.raises(tenant_tui.TuiError):
        tenant_tui.rename_tenant("acme02", "Bad_Name")
    temp_fleet.tenant("other01", ring="dev")
    with pytest.raises(tenant_tui.TuiError):
        tenant_tui.rename_tenant("acme02", "other01")
    tenant_tui.delete_tenant("acme02")
    assert not new_path.exists()
    with pytest.raises(tenant_tui.TuiError):
        tenant_tui.delete_tenant("acme02")


def test_is_last_in_ring(temp_fleet):
    temp_fleet.pins("dev", {})
    temp_fleet.tenant("acme01", ring="dev")
    assert tenant_tui.is_last_in_ring("acme01") is True
    temp_fleet.tenant("acme02", ring="dev")
    assert tenant_tui.is_last_in_ring("acme01") is False


def test_add_pins_preserves_comments(temp_fleet):
    # Define variable (Path) of the pin file to hand-write
    pins_file = temp_fleet.root / "pins" / "dev.yml"
    pins_file.parent.mkdir(parents=True, exist_ok=True)
    pins_file.write_text(
        "# header comment\nring: dev\npins:\n  pack-a: 1.0.0  # inline\n")
    tenant_tui.add_pins("dev", {"pack-b": "2.0.0", "pack-c": "1.0"})
    # Define variable (str) of the pin file after the append
    text = pins_file.read_text()
    assert "# header comment" in text
    assert "# inline" in text
    # Define variable (dict) of the re-parsed pin file
    data = yaml.safe_load(text)
    assert data["pins"]["pack-b"] == "2.0.0"
    assert data["pins"]["pack-c"] == "1.0"
    with pytest.raises(tenant_tui.TuiError):
        tenant_tui.add_pins("dev", {"pack-a": "9.9.9"})


def test_add_pins_missing_ring_file(temp_fleet):
    with pytest.raises(tenant_tui.TuiError):
        tenant_tui.add_pins("qa", {"pack-a": "1.0.0"})


def test_add_pins_empty_flow_mapping(temp_fleet):
    # yaml.safe_dump({"ring": "dev", "pins": {}}) writes 'pins: {}' — the
    # modal flow must still be able to insert into it.
    # Define variable (Path) of the pin file to hand-write
    pins_file = temp_fleet.root / "pins" / "dev.yml"
    pins_file.parent.mkdir(parents=True, exist_ok=True)
    pins_file.write_text(yaml.safe_dump({"ring": "dev", "pins": {}}))
    tenant_tui.add_pins("dev", {"pack-b": "1.0.0"})
    # Define variable (dict) of the re-parsed pin file
    data = yaml.safe_load(pins_file.read_text())
    assert data["pins"]["pack-b"] == "1.0.0"
    assert data["ring"] == "dev"


def test_pin_warnings(temp_fleet):
    # NOTE: hand-written policies so each ring-gate rule can be exercised
    (temp_fleet.root / "policies.yml").write_text(
        "promotion_order: [dev, qa, prod]\n"
        "rings:\n"
        "  dev:\n    source: null\n"
        "  qa:\n    source: dev\n"
        "  prod:\n    source: qa\n    require_change_window: true\n"
        "    change_windows:\n      - days: [Tue]\n"
        "        start: '14:00'\n        end: '22:00'\n")
    temp_fleet.pins("dev", {"pack-a": "1.0.0"})
    temp_fleet.pins("qa", {})
    # head ring: nothing to advise
    assert tenant_tui.pin_warnings("dev", {"pack-b": "1.0.0"}) == []
    # gated ring, version already in source: clean
    assert tenant_tui.pin_warnings("qa", {"pack-a": "1.0.0"}) == []
    # gated ring, version not in source: no-skip warning
    assert any("no-skip" in w
               for w in tenant_tui.pin_warnings("qa", {"pack-b": "2.0.0"}))
    # local in a gated ring
    assert any("'local'" in w
               for w in tenant_tui.pin_warnings("qa", {"pack-a": "local"}))
    # prod: change window advisory
    assert any("change window" in w
               for w in tenant_tui.pin_warnings("prod", {"pack-a": "1.0.0"}))


def test_default_version_for(temp_fleet):
    # NOTE: hand-written policies so the source-ring lookup can be exercised
    (temp_fleet.root / "policies.yml").write_text(
        "rings:\n  dev:\n    source: null\n  qa:\n    source: dev\n")
    temp_fleet.pins("dev", {"pack-a": "1.0.0"})
    temp_fleet.pins("qa", {})
    assert tenant_tui.default_version_for("pack-a", "qa") == "1.0.0"
    assert tenant_tui.default_version_for("pack-b", "qa") == ""


# ------------------------------------------------------------------- the UI
# Headless Textual pilot tests. The ring Select auto-selects its first option
# (allow_blank=False), so create flows only need the text fields typed.

def test_pilot_create_and_save(temp_fleet):
    temp_fleet.pins("dev", {})

    async def scenario():
        # Define variable (TenantTUIApp) of the app under test
        app = TenantTUIApp()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.press("n")
            await pilot.click("#tenant-name")
            await pilot.press(*"acme01")
            await pilot.click("#display-name")
            await pilot.press(*"Acme")
            await pilot.pause(0.5)  # let the debounced validation run
            await pilot.click("#save")
            await pilot.pause()

    asyncio.run(scenario())
    # Define variable (Path) of the saved tenant file
    path = temp_fleet.root / "tenants" / "acme01.yml"
    assert path.exists()
    # Define variable (dict) of the re-parsed tenant file
    data = yaml.safe_load(path.read_text())
    assert data["tenant"] == "acme01"
    assert data["display_name"] == "Acme"
    assert data["ring"] == "dev"
    assert data["credentials_ref"] == "ACME01"


def test_pilot_save_with_missing_pins(temp_fleet):
    temp_fleet.pins("dev", {})

    async def scenario():
        # Define variable (TenantTUIApp) of the app under test
        app = TenantTUIApp()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.press("n")
            await pilot.click("#tenant-name")
            await pilot.press(*"acme01")
            await pilot.click("#extra-input")
            await pilot.press(*"pack-b")
            await pilot.click("#extra-add")
            await pilot.pause(0.5)
            await pilot.click("#fix-pins")
            await pilot.pause()
            await pilot.click("#pin-ver-0")
            await pilot.press(*"1.0.0")
            await pilot.click("#pin-confirm")
            await pilot.pause()
            await pilot.pause(0.5)  # re-validation after the pins landed
            await pilot.click("#save")
            await pilot.pause()

    asyncio.run(scenario())
    assert (temp_fleet.root / "tenants" / "acme01.yml").exists()
    # Define variable (dict) of the re-parsed pin file
    pins = yaml.safe_load((temp_fleet.root / "pins" / "dev.yml").read_text())
    assert pins["pins"]["pack-b"] == "1.0.0"


def test_pilot_edit_existing(temp_fleet):
    temp_fleet.pins("dev", {"pack-a": "1.0.0"})
    temp_fleet.tenant("acme01", ring="dev", extras=["pack-a"])

    async def scenario():
        # Define variable (TenantTUIApp) of the app under test
        app = TenantTUIApp(initial_tenant="acme01")
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await pilot.click("#display-name")
            await pilot.press("ctrl+shift+a")  # select all
            await pilot.press(*"Acme Renamed")
            await pilot.pause(0.5)
            await pilot.click("#save")
            await pilot.pause()

    asyncio.run(scenario())
    # Define variable (dict) of the re-parsed tenant file
    data = yaml.safe_load(
        (temp_fleet.root / "tenants" / "acme01.yml").read_text())
    assert data["display_name"] == "Acme Renamed"
    assert data["extras"] == ["pack-a"]


def test_pilot_delete(temp_fleet):
    temp_fleet.pins("dev", {})
    temp_fleet.tenant("acme01", ring="dev")

    async def scenario():
        # Define variable (TenantTUIApp) of the app under test
        app = TenantTUIApp()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await pilot.click("#tenants")
            await pilot.press("down")  # ensure the cursor is on the row
            await pilot.press("d")
            await pilot.pause()
            await pilot.click("#del-confirm")
            await pilot.pause()

    asyncio.run(scenario())
    assert not (temp_fleet.root / "tenants" / "acme01.yml").exists()


def test_pilot_remove_unused_pins(temp_fleet):
    temp_fleet.pins("dev", {"pack-a": "1.0.0", "pack-b": "2.0.0"})
    temp_fleet.tenant("acme01", ring="dev", extras=["pack-a", "pack-b"])

    async def scenario():
        # Define variable (TenantTUIApp) of the app under test
        app = TenantTUIApp(initial_tenant="acme01")
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            # Drop pack-b (row 2) from the extras table.
            await pilot.click("#extras")
            await pilot.press("down")
            await pilot.press("down")
            await pilot.click("#extra-remove")
            await pilot.pause(0.5)  # debounced validation enables #prune-pins
            await pilot.click("#prune-pins")
            await pilot.pause()
            await pilot.click("#prune-confirm")
            await pilot.pause()

    asyncio.run(scenario())
    # Define variable (dict) of the re-parsed pin file
    pins = yaml.safe_load((temp_fleet.root / "pins" / "dev.yml").read_text())
    assert pins["pins"] == {"pack-a": "1.0.0"}


