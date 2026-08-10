"""config_overlay_lint.py — the gate that keeps Config Overlays honest.

A Config Overlay file is a top-level mapping of List id -> sparse patch (itself a
mapping of the List's JSON content). The lint is offline and structural:

  * every List id must be registered in defaults.config_overlay_lists — a typo'd
    id would otherwise silently patch nothing
  * the file itself must be a mapping, and every patch value must be a mapping
  * a <tenant>.yml layer must correspond to a real Tenant Profile (defaults.yml,
    the fleet-wide layer, is exempt) — otherwise a renamed tenant leaves a dead
    layer behind that looks live

Exit code: 0 clean, 1 on any violation (mirrors namespace_lint.py).
"""
import config_overlay_lint


LIST_A = "SOCFrameworkActions_V3"
LIST_B = "SOCExecutionList_V3"
REGISTRY = [LIST_A, LIST_B]


def _write(temp_fleet, name, text):
    """Write a Config Overlay layer verbatim (for malformed-YAML shapes)."""
    path = temp_fleet.root / "config_overlays" / f"{name}.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


# --- clean trees ------------------------------------------------------------
def test_lint_passes_on_a_valid_defaults_and_tenant_layer(temp_fleet):
    temp_fleet.pins("dev", {})
    temp_fleet.config_overlay_lists(REGISTRY)
    tenant = temp_fleet.tenant("t_ok", ring="dev")
    temp_fleet.config_overlay("defaults", {LIST_A: {"escalate": {"enabled": True}}})
    temp_fleet.config_overlay(tenant, {LIST_B: {"threshold": 5}})

    assert config_overlay_lint.lint() == []


def test_lint_passes_when_no_config_overlays_directory_exists(temp_fleet):
    """The directory is optional — no Config Overlays is not a violation."""
    temp_fleet.pins("dev", {})
    temp_fleet.config_overlay_lists(REGISTRY)
    temp_fleet.tenant("t_none", ring="dev")

    assert config_overlay_lint.lint() == []


def test_lint_ignores_a_readme_in_the_config_overlays_directory(temp_fleet):
    """Only *.yml files are layers; the directory's README.md is not linted."""
    temp_fleet.pins("dev", {})
    temp_fleet.config_overlay_lists(REGISTRY)
    temp_fleet.tenant("t_readme", ring="dev")
    readme = temp_fleet.root / "config_overlays" / "README.md"
    readme.parent.mkdir(parents=True, exist_ok=True)
    readme.write_text("# Config Overlays\n")

    assert config_overlay_lint.lint() == []


# --- unregistered List id ---------------------------------------------------
def test_lint_rejects_a_list_id_not_in_the_registry(temp_fleet):
    temp_fleet.pins("dev", {})
    temp_fleet.config_overlay_lists(REGISTRY)
    tenant = temp_fleet.tenant("t_bad_id", ring="dev")
    temp_fleet.config_overlay(tenant, {"SOCTypoList_V3": {"threshold": 1}})

    violations = config_overlay_lint.lint()

    assert len(violations) == 1
    assert "SOCTypoList_V3" in violations[0]
    assert f"{tenant}.yml" in violations[0]


def test_lint_rejects_an_unregistered_list_id_in_the_defaults_layer(temp_fleet):
    temp_fleet.pins("dev", {})
    temp_fleet.config_overlay_lists(REGISTRY)
    temp_fleet.tenant("t_defaults_bad", ring="dev")
    temp_fleet.config_overlay("defaults", {"NotAFrameworkList": {"a": 1}})

    violations = config_overlay_lint.lint()

    assert len(violations) == 1
    assert "NotAFrameworkList" in violations[0]
    assert "defaults.yml" in violations[0]


# --- malformed shapes -------------------------------------------------------
def test_lint_rejects_a_file_that_is_not_a_mapping(temp_fleet):
    temp_fleet.pins("dev", {})
    temp_fleet.config_overlay_lists(REGISTRY)
    tenant = temp_fleet.tenant("t_not_mapping", ring="dev")
    _write(temp_fleet, tenant, "- just\n- a\n- list\n")

    violations = config_overlay_lint.lint()

    assert len(violations) == 1
    assert f"{tenant}.yml" in violations[0]
    assert "mapping" in violations[0].lower()


def test_lint_rejects_a_patch_value_that_is_not_a_mapping(temp_fleet):
    """The patch must be a nested mapping of the List's content — a bare scalar
    could never be merged onto a List's JSON."""
    temp_fleet.pins("dev", {})
    temp_fleet.config_overlay_lists(REGISTRY)
    tenant = temp_fleet.tenant("t_scalar_patch", ring="dev")
    temp_fleet.config_overlay(tenant, {LIST_A: "true"})

    violations = config_overlay_lint.lint()

    assert len(violations) == 1
    assert LIST_A in violations[0]
    assert "mapping" in violations[0].lower()


def test_lint_accepts_an_empty_file(temp_fleet):
    """An empty layer file is an empty patch set, not a malformed mapping."""
    temp_fleet.pins("dev", {})
    temp_fleet.config_overlay_lists(REGISTRY)
    tenant = temp_fleet.tenant("t_empty_file", ring="dev")
    _write(temp_fleet, tenant, "")

    assert config_overlay_lint.lint() == []


# --- orphan tenant layer ----------------------------------------------------
def test_lint_rejects_a_layer_whose_tenant_profile_does_not_exist(temp_fleet):
    temp_fleet.pins("dev", {})
    temp_fleet.config_overlay_lists(REGISTRY)
    temp_fleet.tenant("t_real", ring="dev")
    temp_fleet.config_overlay("t_ghost", {LIST_A: {"threshold": 1}})

    violations = config_overlay_lint.lint()

    assert len(violations) == 1
    assert "t_ghost" in violations[0]


def test_lint_exempts_the_defaults_layer_from_the_tenant_check(temp_fleet):
    """defaults.yml is the fleet-wide layer, not a tenant — never an orphan."""
    temp_fleet.pins("dev", {})
    temp_fleet.config_overlay_lists(REGISTRY)
    temp_fleet.tenant("t_only", ring="dev")
    temp_fleet.config_overlay("defaults", {LIST_A: {"threshold": 1}})

    assert config_overlay_lint.lint() == []


# --- reporting --------------------------------------------------------------
def test_lint_reports_every_violation_not_just_the_first(temp_fleet):
    temp_fleet.pins("dev", {})
    temp_fleet.config_overlay_lists(REGISTRY)
    tenant = temp_fleet.tenant("t_multi", ring="dev")
    temp_fleet.config_overlay(tenant, {"BadListOne": {"a": 1}, "BadListTwo": {"b": 2}})
    temp_fleet.config_overlay("t_ghost2", {LIST_A: {"c": 3}})

    violations = config_overlay_lint.lint()

    assert len(violations) == 3


def test_cli_exits_nonzero_on_violations_and_zero_when_clean(
    temp_fleet, monkeypatch, capsys
):
    import sys

    import pytest

    temp_fleet.pins("dev", {})
    temp_fleet.config_overlay_lists(REGISTRY)
    tenant = temp_fleet.tenant("t_cli", ring="dev")
    monkeypatch.setattr(sys, "argv", ["config_overlay_lint.py"])

    # clean -> exit 0 with a one-line ✓ summary
    temp_fleet.config_overlay(tenant, {LIST_A: {"threshold": 1}})
    with pytest.raises(SystemExit) as ok:
        config_overlay_lint.main()
    assert ok.value.code == 0
    assert "✓" in capsys.readouterr().out

    # dirty -> exit 1, and the violation is named
    temp_fleet.config_overlay(tenant, {"BadList_V3": {"threshold": 1}})
    with pytest.raises(SystemExit) as bad:
        config_overlay_lint.main()
    assert bad.value.code == 1
    assert "BadList_V3" in capsys.readouterr().out
