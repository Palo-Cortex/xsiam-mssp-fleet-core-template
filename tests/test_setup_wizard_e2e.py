"""The setup wizard, end to end, unattended — for each promotion path.

Starts from a scratch copy of this template (own git repo + bare origin), runs
every step from an answers dict against a fake GitHub and a fake tenant, then
checks the result the way CI will: ring-gate's own scripts must pass on the
generated fleet, the doctor must report no problems, secrets must land in the
right Environment, and protections must be on.
"""
import json
import subprocess
import sys

import pytest
import yaml

from fleet_setup import config
from fleet_setup.checks import FAIL, Doctor
from fleet_setup.ui import UI
from fleet_setup.wizard import Abort, Wizard
from setup_fakes import CATALOG, FakeGH, git, scratch_fleet


def _answers(topology, rings):
    head, last = rings[0], rings[-1]
    tenants = [{"api_url": f"acme-{r}.xdr.us.paloaltonetworks.com", "api_key_id": "12",
                "api_key_env": "WIZ_TEST_KEY", "name": f"acme-{r}", "ring": r}
               for r in rings[:-1]]
    tenants[0].update({"add_extras": True, "extras": ["soc-crowdstrike-falcon"]})
    tenants.append({"api_url": "https://api-acmecorp.xdr.eu.paloaltonetworks.com",
                    "api_key_id": "7", "api_key_env": "WIZ_TEST_KEY", "name": "acme-prod",
                    "display_name": "Acme Corp", "ring": last, "canary": True,
                    "self_publish": True, "prefix": "acme-"})
    return {
        "org": {"mssp": True, "topology": topology, "mssp_ring": False,
                "soak": {r: 2 for r in rings[1:]}, "change_window": True,
                "window": {"days": "Tue,Thu", "start": "13:00", "end": "21:00"},
                "self_publish": True, "keep_example_pack": False},
        "content": {"base": ["soc-optimization-unified", "soc-framework-nist-ir-ai"]},
        "tenants": tenants,
        "owners": {"approvers": "@alice @acme-co/secops", "approvals": 1, "code_owners": True},
        "app": {"setup": False},
        "publish": {"confirm": True},
        "protect": {"merge_queue": True},
        "live": {"remote_check": False, "enable": False},
    }


def _wizard(root, answers, gh, checker=None):
    import io
    out = io.StringIO()
    ui = UI(answers=answers, out=out)
    wiz = Wizard(root, ui, gh, catalog_loader=lambda: CATALOG, network=False,
                 check_credentials=checker or (lambda url, kid, key: (True, "key works — 3 packs", 3)))
    return wiz, out


def _script(root, *args):
    return subprocess.run([sys.executable, str(root / "scripts" / args[0]), *args[1:]],
                          capture_output=True, text=True, cwd=root)


@pytest.mark.parametrize("topology", list(config.TOPOLOGIES))
def test_full_setup_produces_a_valid_protected_fleet(tmp_path, monkeypatch, topology):
    monkeypatch.setenv("WIZ_TEST_KEY", "s3cret-key")
    rings = config.TOPOLOGIES[topology]
    root = scratch_fleet(tmp_path)
    gh = FakeGH()
    wiz, out = _wizard(root, _answers(topology, rings), gh)

    wiz.run()

    # --- configuration -------------------------------------------------------
    pol = config.policies(root)
    assert pol["promotion_order"] == rings
    assert pol["rings"][rings[0]]["source"] is None
    for i in range(1, len(rings)):
        assert pol["rings"][rings[i]]["source"] == rings[i - 1]
        assert pol["rings"][rings[i]]["min_soak_days"] == 2
    assert pol["rings"][rings[-1]]["change_windows"] == [{"days": ["Tue", "Thu"], "start": "13:00", "end": "21:00"}]
    assert sorted(f.stem for f in (root / "fleet" / "pins").glob("*.yml")) == sorted(rings)
    assert config.workflow_choices(root) == {"converge": rings, "promote": rings[1:]}
    assert config.example_leftovers(root) == []
    d = config.defaults(root)
    assert d["base"] == ["soc-optimization-unified", "soc-framework-nist-ir-ai"]
    assert d["mssp_catalog"]["release_base"] == "https://github.com/acme-co/fleet/releases/download"
    assert d["customer_namespaces"] == ["acme-"]
    for r in rings:
        assert config.pins(root, r)["soc-optimization-unified"] == "3.31.19"
    assert config.pins(root, rings[0])["soc-crowdstrike-falcon"] == "2.2.8"

    tenants = config.tenants(root)
    assert sorted(tenants) == sorted([f"acme-{r}" for r in rings[:-1]] + ["acme-prod"])
    prod = tenants["acme-prod"]
    assert prod["ring"] == rings[-1] and prod["canary"] is True
    assert prod["host_token"] == "acmecorp"           # name isn't in the API host
    assert prod["customer_namespace"] == "acme-"
    assert prod["credentials_ref"] == "ACME_PROD"
    assert "acme-co/secops" in (root / ".github" / "CODEOWNERS").read_text()

    # --- CI's own checks pass on the generated fleet -------------------------
    for r in rings:
        assert _script(root, "resolve.py", "--ring", r, "--json").returncode == 0
    for args in (["namespace_lint.py"], ["config_overlay_lint.py"],
                 ["mssp_catalog.py", "--check"], ["lock.py", "--check"]):
        res = _script(root, *args)
        assert res.returncode == 0, res.stdout + res.stderr

    # --- GitHub --------------------------------------------------------------
    creds = json.loads(gh.secrets[("fleet-prod", "ACME_PROD_CREDS")])
    assert creds == {"base_url": "https://api-acmecorp.xdr.eu.paloaltonetworks.com",
                     "api_key": "s3cret-key", "auth_id": "7"}
    assert ("fleet-nonprod", f"ACME_{rings[0].upper()}_CREDS") in gh.secrets
    assert not any(e is None for e, _n in gh.secrets), "no tenant secret may be repo-level"
    assert gh.envs["fleet-prod"]["main_only"] and len(gh.envs["fleet-prod"]["reviewers"]) == 2
    assert gh.protection["main"]["required_status_checks"]["contexts"] == ["gate"]
    assert gh.merge_queue and {"rollback", "promotion"} <= gh.labels
    assert gh.actions_prs                      # no App -> GITHUB_TOKEN PR fallback
    assert "FLEET_LIVE" not in gh.vars

    # --- published, and nothing secret committed -----------------------------
    assert git(root, "status", "--porcelain") == ""
    assert git(root, "rev-parse", "HEAD") == git(root, "rev-parse", "origin/main")
    assert "s3cret-key" not in git(root, "log", "-p", "-1")
    assert "s3cret-key" not in (root / ".fleet-setup" / "state.json").read_text()

    # --- doctor agrees ---------------------------------------------------------
    sections = Doctor(root, gh=gh, catalog_loader=lambda: CATALOG).run()
    fails = [(s, x) for s, res in sections for x in res if x.status == FAIL]
    assert fails == []


def test_generated_fleet_passes_the_test_suite(tmp_path, monkeypatch):
    """ring-gate runs pytest on every PR: it must pass on a wizard-built fleet
    (no example tenants, no example pack, a different promotion path)."""
    monkeypatch.setenv("WIZ_TEST_KEY", "k")
    root = scratch_fleet(tmp_path)
    wiz, _ = _wizard(root, _answers("qa-prod", ["qa", "prod"]), FakeGH())
    wiz.run()
    res = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider",
                          "--ignore=tests/test_setup_wizard_e2e.py"],
                         capture_output=True, text=True, cwd=root)
    assert res.returncode == 0, res.stdout[-3000:]


def test_rerun_is_idempotent_and_resumes(tmp_path, monkeypatch):
    monkeypatch.setenv("WIZ_TEST_KEY", "k")
    root = scratch_fleet(tmp_path)
    answers = _answers("dev-qa-prod", ["dev", "qa", "prod"])
    gh = FakeGH()
    _wizard(root, answers, gh)[0].run()
    head = git(root, "rev-parse", "HEAD")

    wiz, out = _wizard(root, answers, gh)
    wiz.run()

    assert "Resuming setup" in out.getvalue()
    assert git(root, "rev-parse", "HEAD") == head, "a re-run with nothing new commits nothing"


def test_add_tenant_after_protection_opens_a_pull_request(tmp_path, monkeypatch):
    monkeypatch.setenv("WIZ_TEST_KEY", "k")
    root = scratch_fleet(tmp_path)
    gh = FakeGH()
    _wizard(root, _answers("qa-prod", ["qa", "prod"]), gh)[0].run()

    more = {"tenant": {"api_url": "https://api-beta.xdr.us.paloaltonetworks.com",
                       "api_key_id": "9", "api_key_env": "WIZ_TEST_KEY", "name": "beta",
                       "ring": "prod"},
            "verify": {"lock": False}, "publish": {"confirm": True}}
    wiz, _ = _wizard(root, more, gh)
    wiz.add_tenant_flow()

    assert gh.prs, "main is protected now, so the change must go through a PR"
    assert ("fleet-prod", "BETA_CREDS") in gh.secrets
    assert git(root, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert git(root, "rev-parse", "HEAD") == git(root, "rev-parse", "origin/main")


def test_bad_key_is_rejected_unattended(tmp_path, monkeypatch):
    monkeypatch.setenv("WIZ_TEST_KEY", "k")
    root = scratch_fleet(tmp_path)
    answers = _answers("qa-prod", ["qa", "prod"])
    answers["tenants"][0]["on_fail"] = "retry"
    wiz, _ = _wizard(root, answers, FakeGH(),
                     checker=lambda u, i, k: (False, "HTTP 401 — the key or key ID is wrong", None))
    from fleet_setup.ui import AnswerError
    with pytest.raises(AnswerError, match="401"):
        wiz.run()


def test_template_clone_is_redirected_to_a_new_repo(tmp_path):
    root = scratch_fleet(tmp_path)
    gh = FakeGH(name=config.TEMPLATE_REPO)
    created = []
    gh.create_from_template = lambda tpl, name, private=True: created.append((tpl, name, private))
    wiz, _ = _wizard(root, {"preflight": {"create_repo": True, "new_repo": "acme-co/fleet"}}, gh)
    with pytest.raises(Abort, match="cd "):
        wiz.run()
    assert created == [(config.TEMPLATE_REPO, "acme-co/fleet", True)]


def test_interactive_run_with_mostly_defaults(tmp_path):
    """Drive the real prompts with keystrokes (Enter = default), QA → Prod."""
    root = scratch_fleet(tmp_path)
    keys = iter([
        # step 2: not an MSSP, QA→Prod, defaults for soak/window, no self-publish, drop example pack
        "n", "2", "", "", "", "", "", "n", "",
        # step 3: default base packs
        "",
        # step 4: qa tenant (console URL is auto-corrected), then prod tenant as canary, then stop
        "", "acme-qa.xdr.us.paloaltonetworks.com", "5", "k1", "", "", "", "n",
        "", "https://api-acme.xdr.us.paloaltonetworks.com", "6", "k2", "acme-prod", "", "", "y", "n",
        "",
        # step 5: default approver/approvals/codeowners, skip the App
        "", "", "", "n",
        # step 7 publish, step 8 merge queue, step 9 no remote check, not live yet
        "", "", "n", "n",
    ])

    def feed(prompt=""):
        try:
            return next(keys)
        except StopIteration:
            raise AssertionError(f"wizard asked more than expected: {prompt!r}")

    import io
    out = io.StringIO()
    gh = FakeGH()
    wiz = Wizard(root, UI(out=out, inp=feed, secret_inp=feed), gh,
                 catalog_loader=lambda: CATALOG, network=False,
                 check_credentials=lambda u, i, k: (True, "key works", 1))
    wiz.run()

    assert next(keys, None) is None, "every scripted answer should have been used"
    assert config.chain(root) == ["qa", "prod"]
    t = config.tenants(root)
    assert t["acme-qa"]["ring"] == "qa" and t["acme-prod"]["ring"] == "prod"
    assert t["acme-prod"]["canary"] is True and t["acme-prod"]["host_token"] == "acme"
    assert json.loads(gh.secrets[("fleet-nonprod", "ACME_QA_CREDS")])["api_key"] == "k1"
    assert config.defaults(root)["base"] == ["soc-optimization-unified", "soc-framework-nist-ir-ai"]


def test_documented_example_answers_file_runs(tmp_path, monkeypatch):
    """docs/setup-answers.example.yml must keep working as documented."""
    from setup_fakes import REPO_ROOT
    import fleet_setup.wizard as wizmod
    monkeypatch.setenv("ACME_QA_KEY", "q")
    monkeypatch.setenv("ACME_PROD_KEY", "p")
    monkeypatch.setattr(wizmod.time, "sleep", lambda s: None)
    answers = yaml.safe_load((REPO_ROOT / "docs" / "setup-answers.example.yml").read_text())
    root = scratch_fleet(tmp_path)
    gh = FakeGH()
    wiz, out = _wizard(root, answers, gh)
    wiz.run()
    assert config.chain(root) == ["qa", "prod"]
    assert ("fleet-prod", "ACME_PROD_CREDS") in gh.secrets
    assert "doctor workflow didn't start" in out.getvalue()   # FakeGH never starts runs


def test_changing_promotion_path_after_protection_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("WIZ_TEST_KEY", "k")
    root = scratch_fleet(tmp_path)
    gh = FakeGH()
    answers = _answers("qa-prod", ["qa", "prod"])
    _wizard(root, answers, gh)[0].run()

    answers["org"].update({"reconfigure": True, "topology": "dev-qa-prod"})
    wiz, _ = _wizard(root, answers, gh)
    with pytest.raises(Abort, match="migration"):
        wiz.run(only="org")
    assert config.chain(root) == ["qa", "prod"]
