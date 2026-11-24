"""Setup wizard building blocks: XSIAM URL/credential handling, prompts,
config writers and doctor's failure reporting."""
import io
import json
import urllib.error

import pytest
import yaml

from fleet_setup import config, xsiam
from fleet_setup.checks import FAIL, Doctor
from fleet_setup.ui import UI, AnswerError
from setup_fakes import CATALOG, FakeGH, scratch_fleet


# --- xsiam -------------------------------------------------------------------
@pytest.mark.parametrize("raw,url,noted", [
    ("https://api-acme.xdr.us.paloaltonetworks.com/", "https://api-acme.xdr.us.paloaltonetworks.com", False),
    ("api-acme.xdr.us.paloaltonetworks.com", "https://api-acme.xdr.us.paloaltonetworks.com", False),
    ("https://acme.xdr.us.paloaltonetworks.com/dashboard", "https://api-acme.xdr.us.paloaltonetworks.com", True),
    ("http://api-x.xdr.eu.paloaltonetworks.com", "https://api-x.xdr.eu.paloaltonetworks.com", True),
])
def test_normalize_api_url(raw, url, noted):
    got, note = xsiam.normalize_api_url(raw)
    assert got == url and bool(note) == noted


def test_tenant_label_and_suggested_name():
    assert xsiam.tenant_label("https://api-acme-dev.xdr.us.paloaltonetworks.com") == "acme-dev"
    assert xsiam.suggest_tenant_name("https://api-Acme_Dev.xdr.us.paloaltonetworks.com") == "acmedev"


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_check_credentials_success_sends_converge_headers():
    seen = {}

    def opener(req, timeout):
        seen["url"], seen["h"] = req.full_url, dict(req.header_items())
        return _Resp(json.dumps([{"id": "a"}, {"id": "b"}]).encode())

    ok, msg, n = xsiam.check_credentials("https://api-x.example", "12", "KEY", opener=opener)
    assert ok and n == 2 and "2 content pack" in msg
    assert seen["url"].endswith(xsiam.INSTALLED_PATH)
    assert seen["h"]["Authorization"] == "KEY" and seen["h"]["X-xdr-auth-id"] == "12"


@pytest.mark.parametrize("code,needle", [(401, "key ID is wrong"), (403, "Instance Administrator"),
                                         (404, "Copy API URL"), (500, "server error")])
def test_check_credentials_explains_http_errors(code, needle):
    def opener(req, timeout):
        raise urllib.error.HTTPError(req.full_url, code, "x", {}, None)
    ok, msg, _ = xsiam.check_credentials("https://api-x.example", "1", "k", opener=opener)
    assert not ok and needle in msg


def test_check_credentials_non_json_means_wrong_url():
    ok, msg, _ = xsiam.check_credentials("https://x", "1", "k",
                                         opener=lambda req, timeout: _Resp(b"<html>login</html>"))
    assert not ok and "Copy API URL" in msg


# --- ui ----------------------------------------------------------------------
def test_ui_unattended_reads_dotted_keys_and_defaults():
    ui = UI(answers={"org": {"topology": "qa-prod"}, "tenants": [{"name": "a"}]}, out=io.StringIO())
    assert ui.choose("org.topology", "?", [("qa-prod", ""), ("dev-prod", "")]) == "qa-prod"
    assert ui.ask("tenants.0.name", "?") == "a"
    assert ui.confirm("org.missing", "?", True) is True
    with pytest.raises(AnswerError, match="no value"):
        ui.ask("org.required", "?")


def test_ui_unattended_validates_and_rejects_unknown_choices():
    ui = UI(answers={"x": "Bad Name", "y": "nope"}, out=io.StringIO())
    with pytest.raises(AnswerError):
        ui.ask("x", "?", validate=config.tenant_name_error)
    with pytest.raises(AnswerError):
        ui.choose("y", "?", [("a", "")])


def test_ui_secret_only_from_env(monkeypatch):
    ui = UI(answers={"t": {"api_key": "inline-is-ignored", "api_key_env": "K"}}, out=io.StringIO())
    monkeypatch.setenv("K", "from-env")
    assert ui.secret("t.api_key", "?") == "from-env"
    monkeypatch.delenv("K")
    with pytest.raises(AnswerError):
        ui.secret("t.api_key", "?")


def test_ui_interactive_retries_until_valid():
    answers = iter(["", "Bad", "good-name"])
    ui = UI(out=io.StringIO(), inp=lambda prompt: next(answers))
    assert ui.ask("k", "name", validate=config.tenant_name_error) == "good-name"


def test_ui_interactive_choose_many():
    ui = UI(out=io.StringIO(), inp=lambda prompt: "3, 1")
    assert ui.choose_many("k", "?", [("a", ""), ("b", ""), ("c", "")], ("b",)) == ["c", "a"]
    ui = UI(out=io.StringIO(), inp=lambda prompt: "")
    assert ui.choose_many("k", "?", [("a", ""), ("b", "")], ("b",)) == ["b"]
    ui = UI(out=io.StringIO(), inp=lambda prompt: "-")
    assert ui.choose_many("k", "?", [("a", "")], ("a",)) == []


# --- config ------------------------------------------------------------------
@pytest.mark.parametrize("name,ref", [("acme-prod", "ACME_PROD"), ("1st", "T_1ST"), ("github-x", "T_GITHUB_X")])
def test_credentials_ref(name, ref):
    assert config.credentials_ref(name) == ref


@pytest.mark.parametrize("bad", ["Acme", "-x", "x-", "a_b", "", "x" * 41])
def test_tenant_name_rejects(bad):
    assert config.tenant_name_error(bad)


@pytest.mark.parametrize("topology", list(config.TOPOLOGIES))
def test_render_policies_is_a_valid_chain(topology):
    rings = config.TOPOLOGIES[topology]
    pol = yaml.safe_load(config.render_policies(rings, {r: 4 for r in rings}, None,
                                                {rings[-1]: True}, mssp_ring=True))
    assert pol["promotion_order"] == rings
    assert pol["rings"]["mssp"]["source"] is None
    for i, r in enumerate(rings):
        assert pol["rings"][r]["source"] == (rings[i - 1] if i else None)
    assert pol["rings"][rings[-1]]["require_lock"] is True
    assert pol["rings"][rings[-1]]["environment"] == "fleet-prod"
    assert "require_change_window" not in pol["rings"][rings[-1]]


def test_update_defaults_keeps_comments_and_other_keys(tmp_path):
    root = scratch_fleet(tmp_path)
    before = (root / "fleet" / "defaults.yml").read_text()
    config.update_defaults(root, base=["p1"], release_base="https://github.com/o/r/releases/download",
                           customer_namespaces=["a-", "b-"])
    after = (root / "fleet" / "defaults.yml").read_text()
    d = yaml.safe_load(after)
    assert d["base"] == ["p1"] and d["customer_namespaces"] == ["a-", "b-"]
    assert d["config_overlay_lists"] == yaml.safe_load(before)["config_overlay_lists"]
    assert "# Config Overlay registry" in after
    config.update_defaults(root, customer_namespaces=[])
    assert config.defaults(root)["customer_namespaces"] == []


def test_workflow_choices_round_trip(tmp_path):
    root = scratch_fleet(tmp_path)
    original = (root / ".github" / "workflows" / "converge.yml").read_text()
    config.update_workflow_choices(root, ["qa", "prod"])
    assert config.workflow_choices(root) == {"converge": ["qa", "prod"], "promote": ["prod"]}
    config.update_workflow_choices(root, ["dev", "qa", "prod"], mssp_ring=True)
    assert (root / ".github" / "workflows" / "converge.yml").read_text() == original


def test_rendered_tenant_resolves(temp_fleet):
    temp_fleet.pins("prod", {})
    p = config.write_tenant(temp_fleet.root.parent, {
        "name": "acme-prod", "display_name": "Acme: \"Prod\"", "ring": "prod",
        "credentials_ref": "ACME_PROD", "host_token": "acmecorp", "canary": True,
        "customer_namespace": "acme-", "extras": []})
    import resolve
    r = resolve.resolve("acme-prod")
    assert r["host_token"] == "acmecorp" and r["display_name"] == 'Acme: "Prod"'
    assert yaml.safe_load(p.read_text())["canary"] is True


# --- doctor ------------------------------------------------------------------
def _fails(root, gh):
    sections = Doctor(root, gh=gh, catalog_loader=lambda: CATALOG).run()
    return {x.title: x for _s, res in sections for x in res if x.status == FAIL}


def test_doctor_flags_template_state(tmp_path):
    root = scratch_fleet(tmp_path)
    fails = _fails(root, FakeGH())
    assert "release_base points at this repo" in fails
    assert "main is protected" in fails
    assert any(t.startswith("secret for ") for t in fails)
    assert "soc-framework-nist-ir 1.6.3 deployable" in fails     # stale example pin


def test_doctor_flags_topology_drift(tmp_path):
    root = scratch_fleet(tmp_path)
    (root / "fleet" / "pins" / "qa.yml").unlink()
    config.update_workflow_choices(root, ["qa", "prod"])
    fails = _fails(root, FakeGH())
    assert "one pin file per ring" in fails
    assert "workflow ring choices match" in fails


def test_doctor_flags_repo_level_secret_as_warning(tmp_path):
    from fleet_setup.checks import WARN
    root = scratch_fleet(tmp_path)
    gh = FakeGH()
    gh.set_secret("DEVLAB01_CREDS", "{}", env=None)
    sections = Doctor(root, gh=gh, offline=True).run()
    res = next(x for _s, rs in sections for x in rs if x.title == "secret for devlab01")
    assert res.status == WARN and "REPO secret" in res.detail
