"""GitHub operations for the setup wizard, through the `gh` CLI.

Using `gh` (not raw HTTP) means the wizard inherits the user's existing login,
works on github.com, <company>.ghe.com and GitHub Enterprise Server alike
(`gh` follows the repo's host), and never handles a GitHub token itself.

Every method is idempotent: "ensure" calls create or update to the desired
state and are safe to repeat. Failures raise GhError carrying GitHub's message
plus a plain-English hint for the common causes (plan limits, permissions).
"""
import json
import os
import shutil
import subprocess
import urllib.parse


class GhError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def _hint(stderr):
    s = stderr.lower()
    if "upgrade to github pro" in s or "not available for this repository" in s \
            or "requires github" in s or "plan" in s and "upgrade" in s:
        return ("this feature isn't available on the repository's GitHub plan "
                "(private repos need GitHub Team/Enterprise for it)")
    if "must have admin" in s or "resource not accessible" in s or "http 403" in s:
        return "your GitHub account needs ADMIN permission on this repository"
    if "http 404" in s:
        return "not found — or your account can't see it"
    if "gh auth login" in s or "authentication" in s:
        return "run `gh auth login` first"
    return None


class GitHub:
    def __init__(self, repo=None, cwd=None):
        self.repo = repo          # "owner/name"
        self.cwd = cwd
        self.host = None          # set by repo_info(); GH_HOST for non-github.com

    def _env(self):
        if self.host and self.host != "github.com":
            return {**os.environ, "GH_HOST": self.host}
        return None

    # --- plumbing ------------------------------------------------------------
    @staticmethod
    def available():
        return shutil.which("gh") is not None

    def run(self, *args, input=None, check=True):
        p = subprocess.run(["gh", *args], input=input, capture_output=True,
                           text=True, cwd=self.cwd, env=self._env())
        if check and p.returncode != 0:
            err = (p.stderr or p.stdout).strip()
            status = None
            for tok in err.replace("(", " ").replace(")", " ").split():
                if tok.isdigit() and len(tok) == 3:
                    status = int(tok)
                    break
            hint = _hint(err)
            raise GhError(err + (f"\n  → {hint}" if hint else ""), status)
        return p.stdout

    def api(self, method, path, body=None, check=True):
        args = ["api", "-X", method, path, "-H", "Accept: application/vnd.github+json"]
        if body is not None:
            args += ["--input", "-"]
        out = self.run(*args, input=json.dumps(body) if body is not None else None,
                       check=check)
        return json.loads(out) if out.strip() else {}

    def try_api(self, method, path, body=None):
        """(ok, result_or_error) without raising."""
        try:
            return True, self.api(method, path, body)
        except GhError as e:
            return False, e

    def _r(self):
        return ["--repo", self.repo] if self.repo else []

    # --- identity / repo -----------------------------------------------------
    def auth_status(self):
        p = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True)
        return p.returncode == 0, (p.stdout + p.stderr).strip()

    def current_user(self):
        return self.api("GET", "user").get("login")

    def repo_info(self):
        out = self.run("repo", "view", *([self.repo] if self.repo else []), "--json",
                       "nameWithOwner,owner,visibility,defaultBranchRef,viewerPermission,isTemplate,url")
        d = json.loads(out)
        self.host = urllib.parse.urlparse(d.get("url") or "").hostname or self.host
        return {"name_with_owner": d["nameWithOwner"],
                "owner": d["owner"]["login"],
                "visibility": (d.get("visibility") or "").lower(),
                "default_branch": (d.get("defaultBranchRef") or {}).get("name") or "main",
                "permission": d.get("viewerPermission"),
                "is_template": d.get("isTemplate"),
                "url": d.get("url")}

    def server_url(self):
        return f"https://{self.host or 'github.com'}"

    def owner_type(self, owner):
        return self.api("GET", f"users/{owner}").get("type")   # "User" | "Organization"

    def user_id(self, login):
        return self.api("GET", f"users/{login}")["id"]

    def team_id(self, org, slug):
        return self.api("GET", f"orgs/{org}/teams/{slug}")["id"]

    def create_from_template(self, template, new_repo, private=True):
        self.run("repo", "create", new_repo, "--template", template,
                 "--private" if private else "--public", "--clone")

    # --- environments --------------------------------------------------------
    def environments(self):
        d = self.api("GET", f"repos/{self.repo}/environments?per_page=100")
        return {e["name"]: e for e in d.get("environments", [])}

    def ensure_environment(self, name, reviewers=None, main_only=False):
        """Create/update an Environment. reviewers: [{"type": "User"|"Team", "id": n}]."""
        body = {}
        if reviewers is not None:
            body["reviewers"] = reviewers
        if main_only:
            body["deployment_branch_policy"] = {"protected_branches": True,
                                                "custom_branch_policies": False}
        return self.api("PUT", f"repos/{self.repo}/environments/{name}", body)

    @staticmethod
    def environment_reviewers(env):
        for rule in (env or {}).get("protection_rules", []):
            if rule.get("type") == "required_reviewers":
                return rule.get("reviewers") or []
        return []

    # --- secrets & variables -------------------------------------------------
    def set_secret(self, name, value, env=None):
        """Value goes over stdin — never on a command line or in a log."""
        args = ["secret", "set", name, *self._r()]
        if env:
            args += ["--env", env]
        self.run(*args, input=value)

    def secret_names(self, env=None):
        args = ["secret", "list", *self._r(), "--json", "name"]
        if env:
            args += ["--env", env]
        try:
            return {s["name"] for s in json.loads(self.run(*args) or "[]")}
        except GhError:
            return set()

    def set_variable(self, name, value):
        self.run("variable", "set", name, *self._r(), "--body", str(value))

    def org_app_installations(self, org):
        """{app_id: installation} for an organisation (needs org admin), or
        None when they can't be listed."""
        ok, res = self.try_api("GET", f"orgs/{org}/installations?per_page=100")
        if not ok:
            return None
        return {i.get("app_id"): i for i in res.get("installations", [])}

    def variables(self):
        try:
            out = self.run("variable", "list", *self._r(), "--json", "name,value")
            return {v["name"]: v["value"] for v in json.loads(out or "[]")}
        except GhError:
            return {}

    # --- labels --------------------------------------------------------------
    def ensure_label(self, name, color, description):
        self.run("label", "create", name, *self._r(), "--color", color,
                 "--description", description, "--force")

    def label_names(self):
        try:
            out = self.run("label", "list", *self._r(), "--json", "name", "--limit", "200")
            return {lbl["name"] for lbl in json.loads(out or "[]")}
        except GhError:
            return set()

    # --- actions settings ----------------------------------------------------
    def workflow_permissions(self):
        return self.api("GET", f"repos/{self.repo}/actions/permissions/workflow")

    def allow_actions_prs(self):
        self.api("PUT", f"repos/{self.repo}/actions/permissions/workflow",
                 {"default_workflow_permissions": "read",
                  "can_approve_pull_request_reviews": True})

    # --- branch protection / merge queue -------------------------------------
    def branch_protection(self, branch):
        ok, res = self.try_api("GET", f"repos/{self.repo}/branches/{branch}/protection")
        return res if ok else None

    def protect_branch(self, branch, approvals, code_owners, enforce_admins=True,
                       check="gate"):
        reviews = None
        if approvals > 0 or code_owners:
            reviews = {"required_approving_review_count": approvals,
                       "require_code_owner_reviews": bool(code_owners),
                       "dismiss_stale_reviews": True}
        body = {"required_status_checks": {"strict": False, "contexts": [check]},
                "enforce_admins": enforce_admins,
                "required_pull_request_reviews": reviews,
                "restrictions": None,
                "allow_force_pushes": False,
                "allow_deletions": False}
        # With 0 approvals and no code owners, still require a PR.
        if reviews is None:
            body["required_pull_request_reviews"] = {"required_approving_review_count": 0}
        return self.api("PUT", f"repos/{self.repo}/branches/{branch}/protection", body)

    def rulesets(self):
        ok, res = self.try_api("GET", f"repos/{self.repo}/rulesets")
        return res if ok and isinstance(res, list) else []

    def ensure_merge_queue(self, name="fleet-merge-queue"):
        body = {"name": name, "target": "branch", "enforcement": "active",
                "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
                "rules": [{"type": "merge_queue", "parameters": {
                    "check_response_timeout_minutes": 60,
                    "grouping_strategy": "ALLGREEN",
                    "max_entries_to_build": 5, "max_entries_to_merge": 5,
                    "merge_method": "MERGE",           # keeps first-parent merge commits
                    "min_entries_to_merge": 1,
                    "min_entries_to_merge_wait_minutes": 1}}]}
        existing = next((r for r in self.rulesets() if r.get("name") == name), None)
        if existing:
            return self.api("PUT", f"repos/{self.repo}/rulesets/{existing['id']}", body)
        return self.api("POST", f"repos/{self.repo}/rulesets", body)

    # --- workflows -----------------------------------------------------------
    def run_workflow(self, workflow, ref=None, fields=None):
        args = ["workflow", "run", workflow, *self._r()]
        if ref:
            args += ["--ref", ref]
        for k, v in (fields or {}).items():
            args += ["-f", f"{k}={v}"]
        self.run(*args)

    def latest_run(self, workflow):
        out = self.run("run", "list", *self._r(), "--workflow", workflow, "-L", "1",
                       "--json", "databaseId,status,conclusion,url,createdAt")
        runs = json.loads(out or "[]")
        return runs[0] if runs else None

    def watch_run(self, run_id):
        p = subprocess.run(["gh", "run", "watch", str(run_id), *self._r(), "--exit-status"],
                           cwd=self.cwd, env=self._env())
        return p.returncode == 0

    def run_jobs(self, run_id):
        out = self.run("run", "view", str(run_id), *self._r(), "--json", "jobs")
        return json.loads(out or "{}").get("jobs", [])


# --- GitHub App self-check (wizard) ------------------------------------------
def api_base(host):
    """REST API root for github.com, <company>.ghe.com, or Enterprise Server."""
    host = (host or "github.com").lower()
    if host == "github.com":
        return "https://api.github.com"
    if host.endswith(".ghe.com"):
        return f"https://api.{host}"
    return f"https://{host}/api/v3"


def _b64url(data):
    import base64
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def app_jwt(app_id, pem_path, now=None):
    """A 9-minute GitHub App JWT, signed with `openssl` (no Python crypto
    dependency). Raises GhError if openssl is missing or the key is unusable."""
    import time
    now = int(now or time.time())
    head = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    body = _b64url(json.dumps({"iat": now - 60, "exp": now + 540, "iss": str(app_id)}).encode())
    signing_input = f"{head}.{body}".encode()
    if not shutil.which("openssl"):
        raise GhError("openssl isn't installed, so the App can't be checked from here")
    p = subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(pem_path)],
                       input=signing_input, capture_output=True)
    if p.returncode != 0:
        raise GhError(f"couldn't sign with that key: {p.stderr.decode(errors='replace').strip()}")
    return f"{head}.{body}.{_b64url(p.stdout)}"


def check_app(app_id, pem_path, repo, host="github.com", opener=None):
    """What GitHub says about this App and repo, using the App's own key:
    {"slug", "client_id", "installed": bool}. Raises GhError when the App
    ID/key pair is rejected or GitHub can't be reached."""
    import urllib.error
    import urllib.request
    open_ = opener or urllib.request.urlopen
    token = app_jwt(app_id, pem_path)
    base = api_base(host)

    def get(path):
        req = urllib.request.Request(base + path, headers={
            "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
        with open_(req, timeout=20) as resp:
            return json.loads(resp.read().decode() or "{}")

    try:
        app = get("/app")
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise GhError("GitHub rejected the App ID / private key pair — check the App ID "
                          "matches the App the .pem was generated for", 401)
        raise GhError(f"GitHub returned HTTP {e.code} for the App", e.code)
    except urllib.error.URLError as e:
        raise GhError(f"can't reach {base}: {e.reason}")
    try:
        get(f"/repos/{repo}/installation")
        installed = True
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise GhError(f"GitHub returned HTTP {e.code} checking the installation", e.code)
        installed = False
    return {"slug": app.get("slug"), "client_id": app.get("client_id"), "installed": installed}
