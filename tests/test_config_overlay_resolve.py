"""resolve.py — Config Overlay composition.

A Config Overlay is the fleet-owned, per-tenant sparse patch of framework config
List values. It composes in two layers:

    config_overlay = fleet/config_overlays/defaults.yml
                   ⊕ fleet/config_overlays/<tenant>.yml

An absent file is an empty layer. The merge is a recursive deep-merge where both
sides are mappings (the tenant layer wins on conflicts); any non-mapping value
(scalar, list) is replaced wholesale by the tenant layer.

resolve() exposes the composed result under the "config_overlay" key as
{list_id: merged patch}, or {} when neither layer exists.
"""
import json
import sys

import resolve as resolve_mod
from resolve import resolve


LIST_A = "SOCFrameworkActions_V3"
LIST_B = "SOCExecutionList_V3"


def _run_cli(monkeypatch, capsys, argv):
    """Run resolve.py's CLI in-process against the temp fleet; return stdout."""
    monkeypatch.setattr(sys, "argv", ["resolve.py"] + argv)
    try:
        resolve_mod.main()
    except SystemExit as exc:  # argparse/ResolveError exits
        assert exc.code in (0, None), f"resolve CLI failed: {exc.code}"
    return capsys.readouterr().out


# --- no layers -------------------------------------------------------------
def test_config_overlay_is_empty_when_no_layer_files_exist(temp_fleet):
    """A tenant with neither a defaults nor a per-tenant layer resolves to {}."""
    temp_fleet.pins("dev", {})
    tenant = temp_fleet.tenant("t_no_overlay", ring="dev")

    r = resolve(tenant)

    assert r["config_overlay"] == {}


# --- defaults layer only ---------------------------------------------------
def test_defaults_layer_alone_is_the_config_overlay(temp_fleet):
    """With only the fleet-wide layer present, it IS the tenant's Config Overlay."""
    temp_fleet.pins("dev", {})
    temp_fleet.config_overlay("defaults", {LIST_A: {"escalate": {"enabled": True}}})
    tenant = temp_fleet.tenant("t_defaults_only", ring="dev")

    r = resolve(tenant)

    assert r["config_overlay"] == {LIST_A: {"escalate": {"enabled": True}}}


# --- tenant layer only -----------------------------------------------------
def test_tenant_layer_alone_is_the_config_overlay(temp_fleet):
    """With only the per-tenant layer present, it IS the tenant's Config Overlay."""
    temp_fleet.pins("dev", {})
    tenant = temp_fleet.tenant("t_tenant_only", ring="dev")
    temp_fleet.config_overlay(tenant, {LIST_B: {"threshold": 42}})

    r = resolve(tenant)

    assert r["config_overlay"] == {LIST_B: {"threshold": 42}}


def test_another_tenants_layer_is_not_applied(temp_fleet):
    """The per-tenant layer is keyed by tenant name — one tenant's Config Overlay
    never leaks into another's."""
    temp_fleet.pins("dev", {})
    mine = temp_fleet.tenant("t_mine", ring="dev")
    theirs = temp_fleet.tenant("t_theirs", ring="dev")
    temp_fleet.config_overlay(theirs, {LIST_A: {"threshold": 99}})

    assert resolve(mine)["config_overlay"] == {}
    assert resolve(theirs)["config_overlay"] == {LIST_A: {"threshold": 99}}


# --- both layers: deep merge, tenant wins ----------------------------------
def test_both_layers_deep_merge_with_tenant_winning_on_a_nested_key(temp_fleet):
    """Nested mappings merge key-by-key: the tenant layer overrides the one key
    it names and every other defaults key survives, at any depth."""
    temp_fleet.pins("dev", {})
    temp_fleet.config_overlay("defaults", {
        LIST_A: {
            "escalate": {"enabled": True, "severity": "high"},
            "notify": {"channel": "soc"},
        },
        LIST_B: {"threshold": 10},
    })
    tenant = temp_fleet.tenant("t_both", ring="dev")
    temp_fleet.config_overlay(tenant, {
        LIST_A: {"escalate": {"severity": "critical"}},
    })

    r = resolve(tenant)

    assert r["config_overlay"] == {
        LIST_A: {
            # tenant wins on the nested key it names ...
            "escalate": {"enabled": True, "severity": "critical"},
            # ... and every sibling from the defaults layer survives
            "notify": {"channel": "soc"},
        },
        LIST_B: {"threshold": 10},
    }


def test_tenant_layer_adds_a_list_id_the_defaults_layer_does_not_patch(temp_fleet):
    """The layers union at the List-id level, not just within one List."""
    temp_fleet.pins("dev", {})
    temp_fleet.config_overlay("defaults", {LIST_A: {"threshold": 1}})
    tenant = temp_fleet.tenant("t_union", ring="dev")
    temp_fleet.config_overlay(tenant, {LIST_B: {"threshold": 2}})

    r = resolve(tenant)

    assert r["config_overlay"] == {LIST_A: {"threshold": 1}, LIST_B: {"threshold": 2}}


# --- non-mapping values are REPLACED wholesale -----------------------------
def test_tenant_list_value_replaces_the_defaults_list_wholesale(temp_fleet):
    """A list is never element-merged or concatenated — the tenant layer's list
    replaces the defaults layer's list entirely."""
    temp_fleet.pins("dev", {})
    temp_fleet.config_overlay("defaults", {LIST_A: {"products": ["edr", "email"]}})
    tenant = temp_fleet.tenant("t_list_replace", ring="dev")
    temp_fleet.config_overlay(tenant, {LIST_A: {"products": ["identity"]}})

    r = resolve(tenant)

    assert r["config_overlay"] == {LIST_A: {"products": ["identity"]}}


def test_tenant_scalar_replaces_the_defaults_scalar(temp_fleet):
    temp_fleet.pins("dev", {})
    temp_fleet.config_overlay("defaults", {LIST_A: {"threshold": 10}})
    tenant = temp_fleet.tenant("t_scalar_replace", ring="dev")
    temp_fleet.config_overlay(tenant, {LIST_A: {"threshold": 99}})

    r = resolve(tenant)

    assert r["config_overlay"] == {LIST_A: {"threshold": 99}}


def test_tenant_scalar_replaces_a_defaults_mapping_wholesale(temp_fleet):
    """A type change (mapping -> scalar) is a wholesale replacement, not a merge."""
    temp_fleet.pins("dev", {})
    temp_fleet.config_overlay("defaults", {LIST_A: {"escalate": {"enabled": True}}})
    tenant = temp_fleet.tenant("t_type_change", ring="dev")
    temp_fleet.config_overlay(tenant, {LIST_A: {"escalate": False}})

    r = resolve(tenant)

    assert r["config_overlay"] == {LIST_A: {"escalate": False}}


def test_composing_the_layers_does_not_mutate_either_layer(temp_fleet):
    """Composition is pure: re-resolving yields the same result (no layer file
    state is carried across calls)."""
    temp_fleet.pins("dev", {})
    temp_fleet.config_overlay("defaults", {LIST_A: {"escalate": {"enabled": True}}})
    tenant = temp_fleet.tenant("t_pure", ring="dev")
    temp_fleet.config_overlay(tenant, {LIST_A: {"escalate": {"severity": "high"}}})

    first = resolve(tenant)["config_overlay"]
    second = resolve(tenant)["config_overlay"]

    assert first == second
    assert first == {LIST_A: {"escalate": {"enabled": True, "severity": "high"}}}


def test_deep_merge_result_shares_no_references_with_its_inputs():
    """The merged result is fully independent: mutating it never leaks back
    into either input. Convergence edits the merged List JSON in
    place inside a staged pack, so aliasing here would corrupt sibling state."""
    from resolve import deep_merge

    base = {"a": {"kept": [1, 2]}, "b": {"x": 1}}
    over = {"b": {"y": {"nested": True}}, "c": ["replaced"]}

    merged = deep_merge(base, over)

    merged["a"]["kept"].append(99)
    merged["b"]["x"] = "mutated"
    merged["b"]["y"]["nested"] = "mutated"
    merged["c"].append("mutated")

    assert base == {"a": {"kept": [1, 2]}, "b": {"x": 1}}
    assert over == {"b": {"y": {"nested": True}}, "c": ["replaced"]}


def test_deep_merge_tolerates_a_missing_layer_on_either_side():
    """Both arguments may be None/{} — an absent layer is an empty layer."""
    from resolve import deep_merge

    assert deep_merge(None, {"a": 1}) == {"a": 1}
    assert deep_merge({"a": 1}, None) == {"a": 1}
    assert deep_merge(None, None) == {}


# --- CLI: --json ------------------------------------------------------------
def test_json_output_carries_the_composed_config_overlay(temp_fleet, monkeypatch, capsys):
    """`resolve.py <tenant> --json` includes the composed Config Overlay."""
    temp_fleet.pins("dev", {})
    temp_fleet.config_overlay("defaults", {LIST_A: {"escalate": {"enabled": True}}})
    tenant = temp_fleet.tenant("t_json", ring="dev")
    temp_fleet.config_overlay(tenant, {LIST_A: {"escalate": {"severity": "high"}}})

    doc = json.loads(_run_cli(monkeypatch, capsys, [tenant, "--json"]))

    assert doc["config_overlay"] == {
        LIST_A: {"escalate": {"enabled": True, "severity": "high"}}
    }


def test_json_output_carries_an_empty_config_overlay_when_no_layers(
    temp_fleet, monkeypatch, capsys
):
    temp_fleet.pins("dev", {})
    tenant = temp_fleet.tenant("t_json_empty", ring="dev")

    doc = json.loads(_run_cli(monkeypatch, capsys, [tenant, "--json"]))

    assert doc["config_overlay"] == {}


# --- CLI: human output ------------------------------------------------------
def test_human_output_lists_patched_list_ids_and_their_top_level_keys(
    temp_fleet, monkeypatch, capsys
):
    """The human view names each patched List id and the top-level keys patched
    within it, so an operator can see the Config Overlay at a glance."""
    temp_fleet.pins("dev", {})
    temp_fleet.config_overlay("defaults", {LIST_A: {"escalate": {"enabled": True}}})
    tenant = temp_fleet.tenant("t_human", ring="dev")
    temp_fleet.config_overlay(tenant, {LIST_B: {"threshold": 5, "products": ["edr"]}})

    out = _run_cli(monkeypatch, capsys, [tenant])

    assert "CONFIG OVERLAY" in out
    assert LIST_A in out and "escalate" in out
    assert LIST_B in out and "threshold" in out and "products" in out


def test_human_output_omits_the_config_overlay_section_when_empty(
    temp_fleet, monkeypatch, capsys
):
    """No Config Overlay -> no section at all (not an empty heading)."""
    temp_fleet.pins("dev", {})
    tenant = temp_fleet.tenant("t_human_empty", ring="dev")

    out = _run_cli(monkeypatch, capsys, [tenant])

    assert "CONFIG OVERLAY" not in out


# --- no per-tenant shadow flag ----------------------------------------------
# Resolve output and deploy plans must not carry any per-tenant "shadow" field;
# Config Overlays are the only config-variation mechanism. These two tests are
# the regression guard, asserting on ANY "shadow"-prefixed key.
def test_resolved_dict_carries_no_shadow_flag(temp_fleet):
    temp_fleet.pins("dev", {})
    tenant = temp_fleet.tenant("t_plain", ring="dev")

    keys = list(resolve(tenant))

    assert not [k for k in keys if "shadow" in k.lower()], (
        f"the retired shadow flag must not reappear in the resolved dict: {keys}"
    )


def test_human_output_does_not_mention_a_shadow_flag(temp_fleet, monkeypatch, capsys):
    temp_fleet.pins("dev", {})
    # NB: the tenant name must not itself contain the word we assert against.
    tenant = temp_fleet.tenant("t_plain_human", ring="dev")

    out = _run_cli(monkeypatch, capsys, [tenant])

    assert "shadow" not in out.lower()
