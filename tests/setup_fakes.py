"""Test doubles for the setup wizard: an in-memory GitHub and a scratch fleet
repo (this repo's code + the shipped template fleet from tests/fixtures/template,
with its own git history and a bare `origin`)."""
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CATALOG = {"packs": [
    {"id": "soc-optimization-unified", "version": "3.31.19"},
    {"id": "soc-framework-nist-ir-ai", "version": "1.6.17"},
    {"id": "soc-crowdstrike-falcon", "version": "2.2.8"},
    {"id": "soc-microsoft-entra-id", "version": "1.0.6"},
]}


class FakeGH:
    """Records every change; answers reads from that record."""

    def __init__(self, name="acme-co/fleet", owner_type="Organization", protected=False):
        self.repo = None
        self.cwd = None
        self.host = "github.com"
        self.name = name
        self._owner_type = owner_type
        self.envs = {}               # name -> {"reviewers": [...], "main_only": bool}
        self.secrets = {}            # (env|None, name) -> value
        self.vars = {}
        self.labels = set()
        self.protection = {} if not protected else {"main": {"required_status_checks": {"contexts": ["gate"]}}}
        self.merge_queue = False
        self.actions_prs = False
        self.workflow_runs = []
        self.prs = []

    @staticmethod
    def available():
        return True

    def auth_status(self):
        return True, "Logged in"

    def repo_info(self):
        return {"name_with_owner": self.name, "owner": self.name.split("/")[0],
                "visibility": "private", "default_branch": "main", "permission": "ADMIN",
                "is_template": False, "url": f"https://github.com/{self.name}"}

    def server_url(self):
        return "https://github.com"

    def current_user(self):
        return "alice"

    def owner_type(self, owner):
        return self._owner_type

    def user_id(self, login):
        return 1000 + len(login)

    def team_id(self, org, slug):
        return 9000 + len(slug)

    def environments(self):
        return {n: {"name": n, "protection_rules": (
            [{"type": "required_reviewers", "reviewers": [
                {"type": r["type"], "reviewer": {"login": str(r["id"])}} for r in e["reviewers"]]}]
            if e.get("reviewers") else [])} for n, e in self.envs.items()}

    @staticmethod
    def environment_reviewers(env):
        for rule in (env or {}).get("protection_rules", []):
            if rule.get("type") == "required_reviewers":
                return rule.get("reviewers") or []
        return []

    def ensure_environment(self, name, reviewers=None, main_only=False):
        e = self.envs.setdefault(name, {"reviewers": [], "main_only": False})
        if reviewers is not None:
            e["reviewers"] = reviewers
        e["main_only"] = e["main_only"] or main_only

    def set_secret(self, name, value, env=None):
        self.secrets[(env, name)] = value

    def secret_names(self, env=None):
        return {n for (e, n) in self.secrets if e == env}

    def set_variable(self, name, value):
        self.vars[name] = str(value)

    def variables(self):
        return dict(self.vars)

    def ensure_label(self, name, color, description):
        self.labels.add(name)

    def label_names(self):
        return set(self.labels)

    def allow_actions_prs(self):
        self.actions_prs = True

    def branch_protection(self, branch):
        return self.protection.get(branch)

    def protect_branch(self, branch, approvals, code_owners, enforce_admins=True, check="gate"):
        self.protection[branch] = {
            "required_status_checks": {"contexts": [check]},
            "required_pull_request_reviews": {"required_approving_review_count": approvals,
                                              "require_code_owner_reviews": code_owners},
            "enforce_admins": {"enabled": enforce_admins}}

    def rulesets(self):
        return [{"name": "fleet-merge-queue", "rules": [{"type": "merge_queue"}]}] if self.merge_queue else []

    def ensure_merge_queue(self, name="fleet-merge-queue"):
        self.merge_queue = True

    def latest_run(self, workflow):
        return None

    def run_workflow(self, workflow, ref=None, fields=None):
        self.workflow_runs.append((workflow, fields or {}))

    def watch_run(self, run_id):
        return True

    def run_jobs(self, run_id):
        return []

    def run(self, *args, input=None, check=True):
        if args[:2] == ("pr", "create"):
            self.prs.append(args)
            return "https://github.com/acme-co/fleet/pull/1\n"
        return ""


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def scratch_fleet(tmp_path):
    """A copy of this template as its own git repo on `main`, pushed to a bare
    origin — what a customer has right after creating a repo from the template."""
    root = tmp_path / "fleet"
    shutil.copytree(REPO_ROOT, root, ignore=shutil.ignore_patterns(
        ".git", ".venv", "__pycache__", ".pytest_cache", ".fleet-setup", "installed_state.yml"))
    # Start from the template AS SHIPPED, whatever this repo's own fleet now is
    # (these tests also run, via ring-gate, in customer fleets).
    tpl = REPO_ROOT / "tests" / "fixtures" / "template"
    for name in ("fleet", "Packs"):
        shutil.rmtree(root / name, ignore_errors=True)
    shutil.copytree(tpl / "fleet", root / "fleet")
    shutil.copytree(REPO_ROOT / "tests" / "fixtures" / "Packs", root / "Packs")
    shutil.copy(tpl / "mssp_pack_catalog.json", root / "mssp_pack_catalog.json")
    from fleet_setup import config
    config.update_workflow_choices(root, ["dev", "qa", "prod"])
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.invalid")
    git(root, "config", "user.name", "t")
    git(root, "config", "commit.gpgsign", "false")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "from template")
    git(root, "remote", "add", "origin", str(origin))
    git(root, "push", "-q", "-u", "origin", "main")
    return root
