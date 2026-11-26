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
                                                {rings[-1]: True}))
    assert pol["promotion_order"] == rings
    assert set(pol["rings"]) == set(rings)
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
    config.update_workflow_choices(root, ["dev", "qa", "prod"])
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


# --- promotion App private key -----------------------------------------------
KEY = "-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----\n"


def _pem_wizard(tmp_path, feed=None, answers=None, home=None, monkeypatch=None):
    from fleet_setup.wizard import Wizard
    if monkeypatch and home:
        monkeypatch.setattr("pathlib.Path.home", lambda: home)
    ui = UI(answers=answers, out=io.StringIO(), inp=feed)
    return Wizard(tmp_path, ui, FakeGH())


def test_pem_prompt_accepts_the_folder(tmp_path, monkeypatch):
    d = tmp_path / "dl"
    d.mkdir()
    (d / "fleet-promote.2026-11-25.private-key.pem").write_text(KEY)
    answers = iter([str(d)])
    wiz = _pem_wizard(tmp_path, feed=lambda p: next(answers), home=tmp_path, monkeypatch=monkeypatch)
    assert wiz._ask_pem("fleet-promote").name == "fleet-promote.2026-11-25.private-key.pem"


def test_pem_prompt_lets_you_pick_among_several(tmp_path, monkeypatch):
    d = tmp_path / "dl"
    d.mkdir()
    (d / "other.pem").write_text(KEY)
    (d / "fleet-promote.private-key.pem").write_text(KEY)
    answers = iter([str(d), "2"])          # folder, then pick the 2nd listed
    wiz = _pem_wizard(tmp_path, feed=lambda p: next(answers), home=tmp_path, monkeypatch=monkeypatch)
    assert wiz._ask_pem("fleet-promote").name == "other.pem"   # app's own key is listed first


def test_pem_prompt_defaults_to_the_apps_key_in_downloads(tmp_path, monkeypatch):
    dl = tmp_path / "Downloads"
    dl.mkdir()
    key = dl / "fleet-promote.2026-11-25.private-key.pem"
    key.write_text(KEY)
    wiz = _pem_wizard(tmp_path, feed=lambda p: "", home=tmp_path, monkeypatch=monkeypatch)
    assert wiz._ask_pem("fleet-promote") == key                 # Enter accepts it


def test_pem_prompt_rejects_non_keys_and_explains_folders(tmp_path, monkeypatch):
    from fleet_setup.wizard import _pem_file
    (tmp_path / "x.pem").write_text("not a key")
    assert "isn't a .pem private key" in _pem_file(str(tmp_path / "x.pem"))
    assert "is a folder" in _pem_file(str(tmp_path))


def test_pem_unattended_folder_with_no_key_fails_clearly(tmp_path, monkeypatch):
    (tmp_path / "empty").mkdir()
    wiz = _pem_wizard(tmp_path, answers={"app": {"private_key_path": str(tmp_path / "empty")}},
                      home=tmp_path, monkeypatch=monkeypatch)
    with pytest.raises(AnswerError, match="no .pem file"):
        wiz._ask_pem("fleet-promote")


def test_doctor_warns_without_history_boundary_then_ok_after_setup(tmp_path, monkeypatch):
    from fleet_setup.checks import WARN, OK
    root = scratch_fleet(tmp_path)

    def status():
        res = [x for _s, rs in Doctor(root, gh=None, offline=True).run() for x in rs
               if x.title.startswith("orphan deletion only touches")]
        return res[0].status

    assert status() == WARN                       # template history, no setup yet
    monkeypatch.setenv("WIZ_TEST_KEY", "k")
    from test_setup_wizard_e2e import _answers, _wizard
    _wizard(root, _answers("qa-prod", ["qa", "prod"]), FakeGH())[0].run()
    assert status() == OK


# --- promotion App installation check -----------------------------------------
import shutil as _shutil
import subprocess as _sp


@pytest.mark.skipif(not _shutil.which("openssl"), reason="openssl not installed")
def test_app_jwt_is_a_valid_rs256_signature(tmp_path):
    import base64
    from fleet_setup.gh import app_jwt
    key, pub = tmp_path / "k.pem", tmp_path / "k.pub"
    _sp.run(["openssl", "genrsa", "-out", str(key), "2048"], check=True, capture_output=True)
    _sp.run(["openssl", "rsa", "-in", str(key), "-pubout", "-out", str(pub)], check=True, capture_output=True)
    tok = app_jwt(123, key, now=1_700_000_000)
    head, body, sig = tok.split(".")
    pad = lambda s: s + "=" * (-len(s) % 4)  # noqa: E731
    claims = json.loads(base64.urlsafe_b64decode(pad(body)))
    assert claims == {"iat": 1_700_000_000 - 60, "exp": 1_700_000_000 + 540, "iss": "123"}
    (tmp_path / "sig").write_bytes(base64.urlsafe_b64decode(pad(sig)))
    v = _sp.run(["openssl", "dgst", "-sha256", "-verify", str(pub), "-signature", str(tmp_path / "sig")],
                input=f"{head}.{body}".encode(), capture_output=True)
    assert v.returncode == 0, v.stdout + v.stderr


def _app_opener(installed=True, app_status=200):
    def opener(req, timeout):
        assert req.get_header("Authorization").startswith("Bearer ")
        if req.full_url.endswith("/app"):
            if app_status != 200:
                raise urllib.error.HTTPError(req.full_url, app_status, "x", {}, None)
            return _Resp(json.dumps({"slug": "fleet-promote", "client_id": "Iv1abc"}).encode())
        if installed:
            return _Resp(b'{"id": 1}')
        raise urllib.error.HTTPError(req.full_url, 404, "x", {}, None)
    return opener


@pytest.mark.parametrize("installed", [True, False])
def test_check_app_reports_installation(monkeypatch, installed):
    from fleet_setup import gh as ghmod
    monkeypatch.setattr(ghmod, "app_jwt", lambda *a, **k: "jwt")
    got = ghmod.check_app(1, "k.pem", "acme/fleet", opener=_app_opener(installed))
    assert got == {"slug": "fleet-promote", "client_id": "Iv1abc", "installed": installed}


def test_check_app_rejects_mismatched_key(monkeypatch):
    from fleet_setup import gh as ghmod
    monkeypatch.setattr(ghmod, "app_jwt", lambda *a, **k: "jwt")
    with pytest.raises(ghmod.GhError) as e:
        ghmod.check_app(1, "k.pem", "acme/fleet", opener=_app_opener(app_status=401))
    assert e.value.status == 401


@pytest.mark.parametrize("host,api", [("github.com", "https://api.github.com"),
                                      ("acme.ghe.com", "https://api.acme.ghe.com"),
                                      ("git.acme.local", "https://git.acme.local/api/v3")])
def test_api_base(host, api):
    from fleet_setup.gh import api_base
    assert api_base(host) == api


def test_wizard_waits_until_app_is_installed(tmp_path):
    from fleet_setup.wizard import Wizard
    answers = iter([""])                    # "Installed it?" -> Enter (check again)
    gh = FakeGH()
    gh.repo = "acme-co/fleet"
    wiz = Wizard(tmp_path, UI(out=io.StringIO(), inp=lambda p: next(answers)), gh)
    seq = iter([{"slug": "fleet-promote", "client_id": "Iv1", "installed": False},
                {"slug": "fleet-promote", "client_id": "Iv1", "installed": True}])
    wiz.check_app = lambda *a: next(seq)
    info = wiz._confirm_app_installed("1", tmp_path / "k.pem")
    out = wiz.ui.out.getvalue()
    assert info["installed"] and "/apps/fleet-promote/installations/new" in out


def test_doctor_flags_configured_but_uninstalled_app(tmp_path):
    root = scratch_fleet(tmp_path)
    gh = FakeGH()
    gh.set_variable("FLEET_APP_ID", "5928393")
    gh.set_secret("FLEET_APP_PRIVATE_KEY", "k")
    gh.app_installed = False
    assert "promotion App installed" in _fails(root, gh)
    gh.app_installed = True
    assert "promotion App installed" not in _fails(root, gh)
