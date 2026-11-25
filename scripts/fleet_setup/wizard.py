"""The guided setup flow behind `python scripts/setup_wizard.py`.

Steps (each safe to re-run; progress is kept in .fleet-setup/state.json, which
holds answers but never secrets):

  1 preflight   tools, GitHub login, your own repo, admin rights, clean main
  2 org         promotion path (Dev→QA→Prod / QA→Prod / Dev→Prod), soak,
                change window, tenant-owner content, template cleanup
  3 content     the packs every tenant gets, pinned from the upstream catalog
  4 tenants     each XSIAM tenant: API URL + key, verified live, secret stored
                in the ring's GitHub Environment
  5 owners      who approves prod, PR approvals, CODEOWNERS, promote App
  6 verify      the same checks ring-gate runs, artifact lock, dry-run plans
  7 publish     commit + push (direct while main is unprotected, else a PR)
  8 protect     Environments, labels, branch protection, merge queue
  9 live        remote credential check, then switch on live deployments

Collaborators are injected (ui, gh, credential checker, catalog loader, git)
so the whole flow runs unattended in tests and from an answers file.
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from . import config, xsiam
from .checks import Doctor, render
from .gh import GhError
from .ui import AnswerError, bold, dim

STATE_DIR = ".fleet-setup"
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
STEPS = [
    ("preflight", "Check your tools and repository"),
    ("org", "Your organisation and promotion path"),
    ("content", "Content every tenant gets"),
    ("tenants", "Connect your XSIAM tenants"),
    ("owners", "Who approves changes"),
    ("verify", "Check everything before saving"),
    ("publish", "Save the configuration to GitHub"),
    ("protect", "Turn on GitHub protections"),
    ("live", "Go live"),
]
DEFAULT_BASE = ["soc-optimization-unified", "soc-framework-nist-ir-ai"]


class Abort(Exception):
    """Stop the wizard cleanly with a message (progress so far is kept)."""


class Wizard:
    def __init__(self, repo, ui, gh, check_credentials=None, catalog_loader=None,
                 git=None, python=None, network=True):
        self.repo = Path(repo)
        self.ui = ui
        self.gh = gh
        self.check_credentials = check_credentials or xsiam.check_credentials
        self.catalog_loader = catalog_loader
        self.git = git or self._git
        self.python = python or sys.executable
        self.network = network    # False: no artifact downloads / catalog walks in verify
        self.state = self._load_state()
        self.meta = None
        self._catalog = None

    # --- state ---------------------------------------------------------------
    @property
    def _state_path(self):
        return self.repo / STATE_DIR / "state.json"

    def _load_state(self):
        try:
            return json.loads(self._state_path.read_text())
        except (OSError, json.JSONDecodeError):
            return {"done": [], "answers": {}}

    def save(self):
        self._state_path.parent.mkdir(exist_ok=True)
        self._state_path.write_text(json.dumps(self.state, indent=2) + "\n")

    def mark(self, step):
        if step not in self.state["done"]:
            self.state["done"].append(step)
        self.save()

    @property
    def a(self):
        return self.state["answers"]

    # --- helpers -------------------------------------------------------------
    def _git(self, *args, check=True):
        p = subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True, text=True)
        if check and p.returncode != 0:
            raise Abort(f"git {' '.join(args)} failed: {(p.stderr or p.stdout).strip()}")
        return p.stdout.strip()

    def script(self, *args):
        p = subprocess.run([self.python, str(self.repo / "scripts" / args[0]), *args[1:]],
                           capture_output=True, text=True, cwd=self.repo)
        return p.returncode, (p.stdout + p.stderr).strip()

    def catalog(self):
        """{pack_id: version} from the upstream catalog (cached)."""
        if self._catalog is None:
            loader = self.catalog_loader
            if loader is None:
                sys.path.insert(0, str(self.repo / "scripts"))
                from catalog import load_catalog
                d = config.defaults(self.repo).get("catalog") or {}
                loader = lambda: load_catalog(d.get("url"), d.get("pinned_commit"))  # noqa: E731
            cat = loader()
            self._catalog = {p["id"]: str(p.get("version")) for p in cat.get("packs", []) if p.get("id")}
        return self._catalog

    def rings(self):
        return config.TOPOLOGIES[self.a["topology"]] if self.a.get("topology") else config.chain(self.repo)

    def all_rings(self):
        return self.rings() + ([config.MSSP_RING] if self.a.get("mssp_ring") else [])

    def bootstrap(self):
        """True until the config has been published with protections on: pins
        may then be written to every ring at once (no promotion history yet)."""
        return "protect" not in self.state["done"]

    # --- driver --------------------------------------------------------------
    def run(self, only=None):
        order = [s for s, _ in STEPS]
        todo = [only] if only else order
        total = len(STEPS)
        if not only and self.state["done"]:
            done = [s for s in order if s in self.state["done"]]
            self.ui.say(bold("Resuming setup.") + dim(f" Done so far: {', '.join(done)}"))
        for step in todo:
            if step != "preflight" and self.meta is None:
                self._connect()
            n = order.index(step) + 1
            title = dict(STEPS)[step]
            if not only and step in self.state["done"] and step not in ("verify", "live"):
                self.ui.say(dim(f"  ✓ step {n}/{total} · {title} (done)"))
                continue
            self.ui.step(n, total, title)
            getattr(self, f"step_{step}")()
            self.mark(step)
        if not only:
            self.ui.say()
            self.ui.say(bold("Setup complete.") + " Re-check any time with: "
                        "python scripts/setup_wizard.py doctor")

    # --- 1 preflight ---------------------------------------------------------
    def step_preflight(self):
        ui = self.ui
        ui.explain("""
This wizard turns this repository into YOUR fleet: it records how content is
promoted between your environments, connects each XSIAM tenant, and switches on
the GitHub protections. Nothing reaches a tenant until the last step.
You can stop at any time (Ctrl-C) and re-run to continue where you left off.
""")
        if sys.version_info < (3, 10):
            raise Abort("Python 3.10 or newer is required")
        ui.ok(f"Python {sys.version_info.major}.{sys.version_info.minor}")
        if not (self.repo / "fleet" / "policies.yml").exists():
            raise Abort(f"{self.repo} is not a fleet repository (no fleet/policies.yml)")
        if not self.gh.available():
            raise Abort("the GitHub CLI is required: install https://cli.github.com, run "
                        "`gh auth login`, then re-run this wizard")
        ok, msg = self.gh.auth_status()
        if not ok:
            raise Abort("you're not logged in to GitHub: run `gh auth login`, then re-run")
        ui.ok("GitHub CLI logged in")
        try:
            self.meta = self.gh.repo_info()
        except GhError as e:
            raise Abort(f"can't read this repository on GitHub: {e}")
        self.gh.repo = self.meta["name_with_owner"]

        if self.meta["name_with_owner"].lower() == config.TEMPLATE_REPO.lower() or self.meta.get("is_template"):
            self._offer_new_repo()
            return
        ui.ok(f"repository {self.meta['name_with_owner']} ({self.meta['visibility']})")
        if self.meta.get("permission") != "ADMIN":
            ui.warn("you are not an admin of this repo — the protections step will fail. "
                    "Ask an admin to run it, or to grant you admin.")
            if not ui.confirm("preflight.continue_without_admin", "Continue anyway?", False):
                raise Abort("stopped — re-run once you have admin rights")
        else:
            ui.ok("you have admin rights")

        branch = self.git("rev-parse", "--abbrev-ref", "HEAD")
        if branch != self.meta["default_branch"]:
            raise Abort(f"switch to the default branch first: git checkout {self.meta['default_branch']}")
        self.git("fetch", "-q", "origin", branch, check=False)
        behind = self.git("rev-list", "--count", f"HEAD..origin/{branch}", check=False)
        if behind.isdigit() and int(behind):
            raise Abort(f"your {branch} is {behind} commit(s) behind GitHub: run `git pull` first")
        dirty = [line for line in self.git("status", "--porcelain").splitlines()
                 if STATE_DIR not in line]
        if dirty and "preflight" not in self.state["done"]:
            ui.warn(f"you have {len(dirty)} uncommitted change(s); they'll be included when the "
                    f"wizard saves to GitHub")
            if not ui.confirm("preflight.continue_dirty", "Continue?", False):
                raise Abort("stopped — commit or stash your changes, then re-run")
        ui.ok(f"on {branch}, up to date")

    def _connect(self):
        """Repo identity for steps run without preflight (resume, add-tenant)."""
        ok, _msg = self.gh.auth_status() if self.gh.available() else (False, "")
        if not ok:
            raise Abort("log in first: gh auth login")
        try:
            self.meta = self.gh.repo_info()
        except GhError as e:
            raise Abort(f"can't read this repository on GitHub: {e}")
        self.gh.repo = self.meta["name_with_owner"]

    def add_tenant_flow(self):
        """`setup_wizard.py add-tenant`: one tenant, checked and saved."""
        self._connect()
        self.ui.say(bold("Add a tenant"))
        self.ui.say(xsiam.HOW_TO_CREATE_KEY)
        existing = config.tenants(self.repo)
        missing = [r_ for r_ in self.all_rings()
                   if not any(p.get("ring") == r_ for p in existing.values())]
        if not self.add_tenant("tenant", default_ring=(missing or self.all_rings())[0]):
            return
        self.ui.say()
        self.ui.say(bold("Checking"))
        self.step_verify()
        self.ui.say()
        self.ui.say(bold("Saving"))
        self.step_publish()

    def _offer_new_repo(self):
        ui = self.ui
        ui.warn("this clone points at the shared TEMPLATE repository.")
        ui.explain("""
Your fleet needs its own private repository (your tenants, your secrets, your
approvals). The wizard can create one from this template now.
""")
        if not ui.confirm("preflight.create_repo", "Create your own repository from the template?", True):
            raise Abort("create one with:  gh repo create <org>/<name> --template "
                        f"{config.TEMPLATE_REPO} --private --clone   then run the wizard there")
        name = ui.ask("preflight.new_repo", "New repository (owner/name)",
                      validate=lambda v: None if re.match(r"^[\w.-]+/[\w.-]+$", v) else "use owner/name")
        self.gh.cwd = str(self.repo.parent)
        self.gh.create_from_template(config.TEMPLATE_REPO, name, private=True)
        target = self.repo.parent / name.split("/")[1]
        raise Abort(f"created {name}. Continue there:\n\n    cd {target}\n    "
                    f"python scripts/setup_wizard.py\n")

    # --- 2 org ---------------------------------------------------------------
    def step_org(self):
        ui = self.ui
        a = self.a
        if "org" in self.state["done"] and not ui.confirm(
                "org.reconfigure", "Your promotion path is already set. Change it?", False):
            return
        ui.explain("""
A "ring" is a stage your content passes through, each with its own XSIAM
tenant(s). A pack version is always deployed to the first ring, then PROMOTED
ring by ring with a pull request — never straight to production.
""")
        a["mssp"] = ui.confirm("org.mssp", "Do you manage XSIAM tenants on behalf of other "
                               "organisations (are you an MSSP)?", a.get("mssp", False))
        before = a.get("topology")
        a["topology"] = ui.choose("org.topology", "Which environments do you promote through?",
                                  [(k, config.TOPOLOGY_LABELS[k]) for k in config.TOPOLOGIES],
                                  a.get("topology", "dev-qa-prod"))
        if before and a["topology"] != before and not self.bootstrap():
            a["topology"] = before
            self.save()
            raise Abort("your fleet is already protected/live, so changing the promotion path "
                        "is a migration (pins, promotions, tenants move between rings). Plan it "
                        "as a reviewed pull request; see docs/runbook.md")
        rings = self.rings()
        existing_t = config.tenants(self.repo)
        stranded = [t for t, p in existing_t.items() if t not in config.EXAMPLE_TENANTS
                    and p.get("ring") not in rings + [config.MSSP_RING]]
        if stranded:
            raise Abort(f"tenant(s) {', '.join(stranded)} are in rings this path doesn't have — "
                        f"move or delete them first")
        a["mssp_ring"] = bool(a["mssp"]) and ui.confirm(
            "org.mssp_ring", "Also manage your own MSSP parent tenant on a separate track "
            "(outside the customer promotion path)?", a.get("mssp_ring", False))

        ui.explain("""
SOAK = how many days a version must have run in the previous ring before it
may be promoted. It is measured from when the change merged.
""")
        soak = {}
        for i, ring in enumerate(rings[1:], 1):
            dflt = a.get("soak", {}).get(ring, 3 if ring == rings[-1] else 1)
            soak[ring] = int(ui.ask(f"org.soak.{ring}",
                                    f"Days a version must run in {rings[i - 1]} before reaching {ring}",
                                    dflt, validate=_int_range(0, 60)))
        a["soak"] = soak

        last = rings[-1]
        ui.explain(f"""
A CHANGE WINDOW limits when {last} changes may merge and deploy (UTC). Emergency
rollbacks are still allowed outside it.
""")
        if ui.confirm("org.change_window", f"Restrict {last} changes to a change window?",
                      bool(a.get("change_window", True))):
            cw = a.get("change_window") or {"days": ["Tue", "Wed", "Thu"], "start": "14:00", "end": "22:00"}
            days = ui.ask("org.window.days", "Days (comma-separated, e.g. Tue,Wed,Thu)",
                          ",".join(cw["days"]), validate=_days)
            start = ui.ask("org.window.start", "Start time UTC (HH:MM)", cw["start"], validate=_hhmm)
            end = ui.ask("org.window.end", "End time UTC (HH:MM)", cw["end"], validate=_hhmm)
            a["change_window"] = {"days": [d.strip().title()[:3] for d in days.split(",") if d.strip()],
                                  "start": start, "end": end}
        else:
            a["change_window"] = None

        ui.explain("""
Some tenant owners build content on their OWN tenant and publish it themselves
(e.g. "build in production, then release"). The fleet keeps clear of that
content by a name PREFIX each such owner uses (e.g. acme-). You'll set the
prefix per tenant in step 4.
""")
        a["self_publish"] = ui.confirm("org.self_publish",
                                       "Do any tenant owners publish their own content to their tenant?",
                                       a.get("self_publish", False))

        left = config.example_leftovers(self.repo)
        keep_pack = False
        if left:
            ui.info("template examples present: " + ", ".join(left))
            keep_pack = (self.repo / "Packs" / config.EXAMPLE_PACK).exists() and ui.confirm(
                "org.keep_example_pack", "Keep the example pack (Packs/ExamplePack) as a starting "
                "point for your own content?", False)
        self.save()

        # ---- apply ----
        removed = config.remove_examples(self.repo, pack=not keep_pack)
        for f in removed:
            ui.info(f"removed example {f}")
        lock_now = {r_: bool((config.policies(self.repo).get("rings") or {}).get(r_, {}).get("require_lock"))
                    for r_ in rings}
        (self.repo / "fleet" / "policies.yml").write_text(config.render_policies(
            rings, soak, a["change_window"], require_lock=lock_now, mssp_ring=a["mssp_ring"]))
        gone = config.set_rings(self.repo, rings, a["mssp_ring"])
        for g in gone:
            ui.info(f"removed ring {g} (fleet/pins/{g}.yml)")
        config.update_workflow_choices(self.repo, rings, a["mssp_ring"])
        server = self.gh.server_url() if hasattr(self.gh, "server_url") else "https://github.com"
        # prefixes come only from real tenants (drops the template's example `acme-`)
        ns = sorted({p.get("customer_namespace") for p in config.tenants(self.repo).values()
                     if p.get("customer_namespace")})
        config.update_defaults(self.repo,
                               release_base=config.release_base_for(server, self.gh.repo),
                               customer_namespaces=ns)
        code, msg = self.script("mssp_catalog.py", "--write")
        if code:
            raise Abort(f"couldn't regenerate mssp_pack_catalog.json: {msg}")
        ui.ok(f"promotion path {' → '.join(rings)} written to fleet/policies.yml")

    # --- 3 content -----------------------------------------------------------
    def step_content(self):
        ui = self.ui
        rings = self.all_rings()
        if not self.bootstrap():
            ui.explain(f"""
Your fleet is live, so new versions start in {self.rings()[0]} and are promoted:
  1. edit fleet/pins/{self.rings()[0]}.yml (or fleet/defaults.yml › base) in a PR
  2. python scripts/promote.py --pack <id> --to <next ring>   (or the promote workflow)
""")
            return
        ui.explain("""
BASE packs are installed on every tenant. They come from the upstream SOC
Framework catalog and are pinned to an exact version (never "latest").
""")
        try:
            cat = self.catalog()
        except Exception as e:  # noqa: BLE001
            ui.warn(f"couldn't read the upstream catalog: {str(e).splitlines()[0]}")
            if not ui.confirm("content.skip_on_error", "Keep the current base packs and continue?", True):
                raise Abort("stopped — check network access to raw.githubusercontent.com")
            return
        current = self.a.get("base") or config.defaults(self.repo).get("base") or []
        gone = [p for p in current if p not in cat]
        for p in gone:
            ui.warn(f"{p} is no longer in the upstream catalog")
        dflt = [p for p in current if p in cat]
        if gone or not dflt:      # offer the recommended set in place of retired packs
            dflt += [p for p in DEFAULT_BASE if p in cat and p not in dflt]
        base = ui.choose_many("content.base", "Packs every tenant gets:",
                              [(pid, f"{pid}  {dim(v)}") for pid, v in sorted(cat.items())], dflt)
        self.a["base"] = base
        self.save()
        config.update_defaults(self.repo, base=base)
        # Keep only pins something still composes (tenant extras, group extras):
        # stale template example pins would otherwise linger undeployable.
        used = set()
        for p in config.tenants(self.repo).values():
            used |= set(p.get("extras") or [])
        for g in (self.repo / "fleet" / "groups").glob("*.yml"):
            used |= set(config.load_yaml(g).get("extras") or [])
        for i, ring in enumerate(rings):
            pins = {p: v for p, v in config.pins(self.repo, ring).items()
                    if p not in base and p in used}
            pins.update({p: cat[p] for p in base})
            src = None if ring == config.MSSP_RING or i == 0 else rings[i - 1]
            config.write_pins(self.repo, ring, src, pins)
        ui.ok(f"{len(base)} base pack(s) pinned in {', '.join(rings)}")

    # --- 4 tenants -----------------------------------------------------------
    def step_tenants(self):
        ui = self.ui
        ui.explain("""
For each XSIAM tenant you need an API key. The wizard checks the key against
the tenant right away and stores it as an encrypted GitHub secret, scoped to
that ring's Environment — it is never written to the repository.
""")
        ui.say(xsiam.HOW_TO_CREATE_KEY)
        planned = self.ui.lookup("tenants") if ui.unattended else None
        i = 0
        while True:
            existing = config.tenants(self.repo)
            missing = [r_ for r_ in self.all_rings()
                       if not any(p.get("ring") == r_ for p in existing.values())]
            if ui.unattended:
                if not isinstance(planned, list) or i >= len(planned):
                    break
            else:
                if existing:
                    ui.info("tenants so far: " + ", ".join(
                        f"{t} ({p.get('ring')})" for t, p in existing.items()))
                if missing:
                    ui.info("rings still without a tenant: " + ", ".join(missing))
                prompt = "Add a tenant?" if not existing else "Add another tenant?"
                if not ui.confirm(f"tenants.more.{i}", prompt, bool(missing)):
                    if missing:
                        ui.warn("every ring needs at least one tenant before you can publish — "
                                "add them later with: python scripts/setup_wizard.py add-tenant")
                    break
            self.add_tenant(f"tenants.{i}", default_ring=(missing or self.all_rings())[0])
            i += 1

    def add_tenant(self, key, default_ring=None):
        ui = self.ui
        rings = self.all_rings()
        existing = config.tenants(self.repo)
        while True:
            raw = ui.ask(f"{key}.api_url", "Tenant API URL (from \"Copy API URL\")",
                         validate=lambda v: None if xsiam.normalize_api_url(v)[0] else "required")
            url, note = xsiam.normalize_api_url(raw)
            if note:
                ui.info(note)
            key_id = ui.ask(f"{key}.api_key_id", "API key ID (the number in the keys table)",
                            validate=lambda v: None if v.isdigit() else "the key ID is a number")
            api_key = ui.secret(f"{key}.api_key", "API key")
            ui.info("checking the key against the tenant…")
            ok, msg, _count = self.check_credentials(url, key_id, api_key)
            if ok:
                ui.ok(msg)
                break
            ui.fail(msg)
            choice = ui.choose(f"{key}.on_fail", "What now?", [
                ("retry", "Re-enter the URL and key"),
                ("keep", "Save it anyway (I'll fix access later; doctor --remote re-checks)"),
                ("skip", "Skip this tenant")], "retry")
            if choice == "skip":
                return None
            if choice == "keep":
                break
            if ui.unattended:
                raise AnswerError(f"{key}: credential check failed: {msg}")

        name = ui.ask(f"{key}.name", "Short tenant name (used in file and secret names)",
                      xsiam.suggest_tenant_name(url),
                      validate=lambda v: config.tenant_name_error(v, existing))
        display = ui.ask(f"{key}.display_name", "Display name", name)
        ring = ui.choose(f"{key}.ring", "Which ring is this tenant in?",
                         [(r_, r_) for r_ in rings], default_ring or rings[0])
        t = {"name": name, "display_name": display, "ring": ring,
             "credentials_ref": config.credentials_ref(name), "extras": []}
        label = xsiam.tenant_label(url).lower()
        if name not in label:
            t["host_token"] = re.sub(r"[^a-z0-9-]", "", label)
            ui.info(f"host guard will expect '{t['host_token']}' in the API host "
                    f"(blocks deploying with another tenant's key)")
        if ring == self.rings()[-1] and ui.confirm(
                f"{key}.canary", f"Deploy to this tenant FIRST when {ring} changes (canary)?", False):
            t["canary"] = True
        if self.a.get("self_publish") and ui.confirm(
                f"{key}.self_publish", "Does this tenant's owner publish their own content?", False):
            prefix = ui.ask(f"{key}.prefix", "Their content name prefix", f"{name}-",
                            validate=config.prefix_error)
            t["customer_namespace"] = prefix
            ns = list(config.defaults(self.repo).get("customer_namespaces") or [])
            if prefix not in ns:
                config.update_defaults(self.repo, customer_namespaces=ns + [prefix])
        self._tenant_extras(key, t)

        env = config.environment_for(ring, self.rings())
        creds = json.dumps({"base_url": url, "api_key": api_key, "auth_id": str(key_id)})
        try:
            self.gh.ensure_environment(env)
            self.gh.set_secret(f"{t['credentials_ref']}_CREDS", creds, env=env)
            ui.ok(f"stored secret {t['credentials_ref']}_CREDS in Environment {env}")
        except GhError as e:
            ui.warn(f"couldn't store the secret ({str(e).splitlines()[0]}). Store it later with:\n"
                    f"      gh secret set {t['credentials_ref']}_CREDS --env {env}")
        config.write_tenant(self.repo, t)
        ui.ok(f"saved fleet/tenants/{name}.yml")
        self.a.setdefault("tenants", {})[name] = {k: v for k, v in t.items()}
        self.save()
        return t

    def _tenant_extras(self, key, t):
        """Optional packs for this tenant on top of base (bootstrap only)."""
        if not self.bootstrap():
            return
        try:
            cat = self.catalog()
        except Exception:  # noqa: BLE001
            return
        base = set(config.defaults(self.repo).get("base") or [])
        options = [(pid, f"{pid}  {dim(v)}") for pid, v in sorted(cat.items()) if pid not in base]
        if not options or not self.ui.confirm(f"{key}.add_extras",
                                              "Add packs for this tenant beyond the base set?", False):
            return
        extras = self.ui.choose_many(f"{key}.extras", "Extra packs:", options, ())
        t["extras"] = extras
        rings = self.rings()
        # pin into this ring and every ring before it, so later promotions line up
        upto = rings[:rings.index(t["ring"]) + 1] if t["ring"] in rings else [t["ring"]]
        for ring in upto:
            pins = config.pins(self.repo, ring)
            pins.update({p: cat[p] for p in extras if p not in pins})
            src = None if ring == config.MSSP_RING or ring == rings[0] else rings[rings.index(ring) - 1]
            config.write_pins(self.repo, ring, src, pins)

    # --- 5 owners ------------------------------------------------------------
    def step_owners(self):
        ui = self.ui
        a = self.a
        me = self.gh.current_user()
        org = self.gh.owner_type(self.meta["owner"]) == "Organization" if self.meta else False
        last = self.rings()[-1]
        ui.explain(f"""
APPROVERS must approve every deployment to {last} (a GitHub Environment rule).
Use GitHub usernames (@alice) or teams (@your-org/secops).
""")
        raw = ui.ask("owners.approvers", f"Who approves {last} deployments?",
                     " ".join(a.get("approvers") or [f"@{me}"]),
                     validate=lambda v: next((e for e in map(config.owner_error, v.split()) if e), None))
        a["approvers"] = raw.split()
        ui.explain("""
PULL REQUEST APPROVALS: how many people (other than the author) must approve a
change. Use 0 if you work alone — GitHub doesn't let you approve your own PR.
""")
        a["approvals"] = int(ui.ask("owners.approvals", "Required approvals per pull request",
                                    a.get("approvals", 1 if org else 0), validate=_int_range(0, 6)))
        a["code_owners"] = a["approvals"] > 0 and ui.confirm(
            "owners.code_owners", "Require the approvers above to review prod pins, policy, "
            "scripts and workflows (CODEOWNERS)?", a.get("code_owners", True))
        (self.repo / ".github" / "CODEOWNERS").write_text(config.render_codeowners(
            a["approvers"], a["approvers"], a["approvers"], last))
        ui.ok("wrote .github/CODEOWNERS")
        self.save()
        self._setup_app()

    def _setup_app(self):
        ui = self.ui
        if "FLEET_APP_ID" in self.gh.variables():
            ui.ok("promotion GitHub App already configured")
            return
        ui.explain("""
PROMOTION APP: the promote workflow opens pull requests. GitHub won't run
ring-gate on PRs opened by its built-in token, so a small GitHub App (owned by
you, only this repo, contents + pull requests) opens them instead. Optional —
without it you re-trigger ring-gate on promote PRs by hand.
""")
        if not ui.confirm("app.setup", "Set up the promotion App now? (about 2 minutes in the browser)", True):
            self.a["app"] = "skipped"
            return
        owner = self.meta["owner"]
        org = self.gh.owner_type(owner) == "Organization"
        base = (f"{self.gh.server_url()}/organizations/{owner}/settings/apps/new" if org
                else f"{self.gh.server_url()}/settings/apps/new")
        name = f"{self.gh.repo.split('/')[1]}-promote"[:34]
        url = (f"{base}?name={name}&url={self.gh.server_url()}/{self.gh.repo}&public=false"
               f"&webhook_active=false&contents=write&pull_requests=write")
        ui.explain(f"""
  1. Open (pre-filled):  {url}
  2. Click "Create GitHub App". Note the App ID shown at the top.
  3. Under "Private keys", click "Generate a private key". A file named like
     {name}.<date>.private-key.pem downloads (usually to ~/Downloads).
  4. Left menu → "Install App" → install it on ONLY {self.gh.repo}.
""")
        app_id = ui.ask("app.id", "App ID", validate=lambda v: None if v.isdigit() else "a number")
        pem = self._ask_pem(name)
        self.gh.set_variable("FLEET_APP_ID", app_id)
        self.gh.set_secret("FLEET_APP_PRIVATE_KEY", pem.read_text())
        ui.ok("stored FLEET_APP_ID and FLEET_APP_PRIVATE_KEY")
        ui.info(f"you can now delete {pem}")
        self.a["app"] = "configured"

    def _ask_pem(self, app_name):
        """The App's private-key file. Accepts the file itself or the folder it
        was downloaded to (the newest matching .pem there is offered)."""
        ui = self.ui
        dflt = _find_pem(Path.home() / "Downloads", app_name)
        while True:
            raw = ui.ask("app.private_key_path",
                         "Full path AND file name of the downloaded .pem "
                         "(or just the folder it's in)",
                         str(dflt) if dflt else None, validate=lambda v: None if v else "required")
            p = Path(os.path.expanduser(raw.strip().strip("'\"")))
            if p.is_dir():
                found = _pem_candidates(p, app_name)
                if not found:
                    ui.fail(f"no .pem file in {p}")
                    if ui.unattended:
                        raise AnswerError(f"app.private_key_path: no .pem file in {p}")
                    continue
                if len(found) == 1:
                    p = found[0]
                    ui.info(f"found {p.name}")
                else:
                    p = ui.choose("app.private_key_choice", f"Several .pem files in {raw}:",
                                  [(f, f.name) for f in found], found[0])
            err = _pem_file(str(p))
            if not err:
                return p
            ui.fail(err)
            if ui.unattended:
                raise AnswerError(f"app.private_key_path: {err}")

    # --- 6 verify ------------------------------------------------------------
    def step_verify(self):
        ui = self.ui
        problems = 0
        code, msg = self.script("mssp_catalog.py", "--write")
        on_disk = {f.stem for f in (self.repo / "fleet" / "pins").glob("*.yml")}
        rings = [r_ for r_ in self.all_rings() if r_ in on_disk] + sorted(on_disk - set(self.all_rings()))
        checks = [(f"ring {r_} resolves", ["resolve.py", "--ring", r_, "--json"]) for r_ in rings]
        checks += [("namespace contract", ["namespace_lint.py"]),
                   ("Config Overlays", ["config_overlay_lint.py"]),
                   ("MSSP pack catalog", ["mssp_catalog.py", "--check"])]
        for title, args in checks:
            code, msg = self.script(*args)
            if code == 0:
                ui.ok(title)
            else:
                problems += 1
                ui.fail(f"{title}:\n      " + "\n      ".join(msg.splitlines()[-4:]))

        last = self.rings()[-1]
        ui.explain(f"""
LOCKING records each pinned pack's download URL and checksum, so {last} only
ever installs the exact bytes you tested. Strongly recommended.
""")
        if not self.network:
            ui.info("offline: artifact locking skipped")
        elif ui.confirm("verify.lock", f"Lock artifacts now and require locks in {last}?", True):
            code, msg = self.script("lock.py", "--write")
            if code == 0:
                self._set_require_lock(last, True)
                ui.ok(f"artifacts locked; {last} requires locks")
            else:
                ui.warn("couldn't lock every pack (see below); require_lock left off:\n      "
                        + "\n      ".join(msg.splitlines()[-4:]))
        code, msg = self.script("lock.py", "--check")
        if code:
            problems += 1
            ui.fail("artifact lock:\n      " + "\n      ".join(msg.splitlines()[-4:]))

        for t in sorted(config.tenants(self.repo)):
            code, msg = self.script("deploy_tenant.py", t, *([] if self.network else ["--offline"]))
            bad = code != 0 or "could not resolve" in msg
            (ui.warn if bad else ui.ok)(f"dry-run plan for {t}" + (":\n      " + "\n      ".join(
                [ln for ln in msg.splitlines() if "!!" in ln or "ERROR" in ln or ln.startswith("#    ")][:5])
                if bad else ""))
        if problems:
            raise Abort(f"{problems} problem(s) above must be fixed before publishing. "
                        f"Fix them (or re-run the step that sets them), then re-run the wizard.")

    def _set_require_lock(self, ring, value):
        p = self.repo / "fleet" / "policies.yml"
        lines = p.read_text().splitlines(keepends=True)
        in_ring = False
        for i, line in enumerate(lines):
            if re.match(rf"^  {re.escape(ring)}:\s*$", line):
                in_ring = True
            elif re.match(r"^  \S", line):
                in_ring = False
            elif in_ring and re.match(r"^    require_lock:", line):
                lines[i] = f"    require_lock: {'true' if value else 'false'}\n"
        p.write_text("".join(lines))

    # --- 7 publish -----------------------------------------------------------
    def step_publish(self):
        ui = self.ui
        status = [ln for ln in self.git("status", "--porcelain").splitlines() if STATE_DIR not in ln]
        if not status:
            ui.ok("nothing new to save")
            return
        ui.info(f"{len(status)} file(s) changed")
        branch = self.meta["default_branch"] if self.meta else "main"
        protected = bool(self.gh.branch_protection(branch))
        msg = "Configure fleet with the setup wizard"
        if not protected:
            ui.explain(f"""
{branch} isn't protected yet, so this first save goes straight to {branch}
(protections are switched on in the next step).
""")
            if not ui.confirm("publish.confirm", f"Commit and push to {branch}?", True):
                raise Abort("stopped before saving — re-run to continue")
            self.git("add", "-A")
            self.git("commit", "-q", "-m", msg)
            self.git("push", "-q", "origin", branch)
            ui.ok(f"pushed to {branch}")
        else:
            work = f"setup/{time.strftime('%Y%m%d-%H%M%S')}"
            if not ui.confirm("publish.confirm", f"{branch} is protected — open a pull request?", True):
                raise Abort("stopped before saving — re-run to continue")
            self.git("checkout", "-q", "-b", work)
            self.git("add", "-A")
            self.git("commit", "-q", "-m", msg)
            self.git("push", "-q", "-u", "origin", work)
            out = self.gh.run("pr", "create", "--repo", self.gh.repo, "--base", branch,
                              "--head", work, "--title", msg, "--body",
                              "Changes made by `python scripts/setup_wizard.py`. "
                              "ring-gate checks them before merge.")
            self.git("checkout", "-q", branch)
            ui.ok(f"pull request: {out.strip()}")
            ui.info("merge it after ring-gate passes")

    # --- 8 protect -----------------------------------------------------------
    def step_protect(self):
        ui = self.ui
        a = self.a
        rings = self.rings()
        try:
            done_envs = set()
            for ring in self.all_rings():
                env = config.environment_for(ring, rings)
                if env in done_envs:
                    continue
                done_envs.add(env)
                if ring == rings[-1]:
                    reviewers = self._reviewer_ids(a.get("approvers") or [])
                    try:
                        self.gh.ensure_environment(env, reviewers=reviewers, main_only=True)
                        ui.ok(f"Environment {env}: approval by {' '.join(a.get('approvers') or [])}, main only")
                    except GhError as e:
                        self.gh.ensure_environment(env)
                        ui.warn(f"Environment {env} created without required approvers: "
                                f"{str(e).splitlines()[-1]}")
                else:
                    self.gh.ensure_environment(env)
                    ui.ok(f"Environment {env}")
            for name, color, desc in [("rollback", "b60205", "Break-glass rollback (ring-gate)"),
                                      ("promotion", "0e8a16", "Pin promotion PR"),
                                      ("drift", "d73a4a", "Fleet drift report")]:
                self.gh.ensure_label(name, color, desc)
            ui.ok("labels rollback, promotion, drift")
            if a.get("app") != "configured" and "FLEET_APP_ID" not in self.gh.variables():
                self.gh.allow_actions_prs()
                ui.ok("Actions may open pull requests (promote fallback without the App)")
        except GhError as e:
            raise Abort(f"GitHub refused a setting: {e}")

        branch = self.meta["default_branch"] if self.meta else "main"
        try:
            self.gh.protect_branch(branch, int(a.get("approvals", 0)), bool(a.get("code_owners")))
            ui.ok(f"{branch} protected: pull requests only, ring-gate required, "
                  f"{a.get('approvals', 0)} approval(s)")
        except GhError as e:
            ui.fail(f"couldn't protect {branch}: {e}")
            ui.warn("without this, pushes to main skip ring-gate. Fix the cause above and re-run.")
            raise Abort("branch protection is required — re-run once the issue above is fixed")
        if ui.confirm("protect.merge_queue", "Turn on the merge queue (re-checks the change window "
                      "at merge time)?", True):
            try:
                self.gh.ensure_merge_queue()
                ui.ok("merge queue on")
            except GhError as e:
                ui.warn(f"merge queue not available ({str(e).splitlines()[-1]}); converge still "
                        f"re-checks change windows")

    def _reviewer_ids(self, owners):
        out = []
        for o in owners:
            name = o.lstrip("@")
            try:
                if "/" in name:
                    org, slug = name.split("/", 1)
                    out.append({"type": "Team", "id": self.gh.team_id(org, slug)})
                else:
                    out.append({"type": "User", "id": self.gh.user_id(name)})
            except GhError:
                self.ui.warn(f"couldn't find {o} on GitHub — skipped as an approver")
        return out

    # --- 9 live --------------------------------------------------------------
    def step_live(self):
        ui = self.ui
        head = self.rings()[0]
        ui.explain("""
Pushing the configuration started a DRY-RUN converge: it shows what WOULD be
deployed to each tenant without touching any. Review it in the Actions tab.
""")
        try:
            run = self.gh.latest_run("converge.yml")
            if run:
                ui.info(f"latest converge run: {run.get('url')} ({run.get('conclusion') or run.get('status')})")
        except GhError:
            pass
        if ui.confirm("live.remote_check", "Check every tenant's stored key from GitHub Actions now?", True):
            self.remote_check()
        vars_ = self.gh.variables()
        if vars_.get("FLEET_LIVE") == "true":
            ui.ok("live deployments are already ON")
            return
        ui.explain(f"""
GOING LIVE: from then on, every merged change to pins, packs or Config
Overlays deploys to the affected tenants ({self.rings()[-1]} only after approval).
Pack DELETION stays off until you set FLEET_ALLOW_DELETE=true after testing it
on a lab tenant.
""")
        if ui.confirm("live.enable", "Turn on live deployments now?", False):
            self.gh.set_variable("FLEET_LIVE", "true")
            ui.ok("FLEET_LIVE=true")
            if ui.confirm("live.first_converge", f"Deploy the {head} ring now?", True):
                self.gh.run_workflow("converge.yml", fields={"ring": head})
                ui.ok(f"converge started for {head} — watch it in the Actions tab")
        else:
            ui.info(f"when ready:  gh variable set FLEET_LIVE --body true --repo {self.gh.repo}")

    def remote_check(self):
        """Run the doctor workflow: each tenant's secret, tested where it's used."""
        ui = self.ui
        try:
            before = self.gh.latest_run("doctor.yml")
            self.gh.run_workflow("doctor.yml")
            run = None
            for _ in range(30):
                time.sleep(2)
                run = self.gh.latest_run("doctor.yml")
                if run and (not before or run["databaseId"] != before["databaseId"]):
                    break
            if not run:
                ui.warn("the doctor workflow didn't start — is it on the default branch yet?")
                return False
            ui.info(f"running: {run.get('url')}")
            ui.info("prod checks wait for an approver if the Environment requires one")
            ok = self.gh.watch_run(run["databaseId"])
            for job in self.gh.run_jobs(run["databaseId"]):
                if job.get("name", "").startswith("check "):
                    (ui.ok if job.get("conclusion") == "success" else ui.fail)(
                        f"{job['name'][6:]}: {job.get('conclusion')}")
            return ok
        except GhError as e:
            ui.warn(f"remote check couldn't run: {str(e).splitlines()[0]}")
            return False

    # --- doctor --------------------------------------------------------------
    def doctor(self, offline=False, remote=False):
        d = Doctor(self.repo, gh=self.gh if self.gh.available() else None, offline=offline,
                   catalog_loader=self.catalog_loader)
        healthy = render(d.run(), self.ui)
        if remote:
            self.ui.say()
            self.ui.say(bold("Tenant keys (from GitHub Actions)"))
            if d.repo_meta:
                self.gh.repo = d.repo_meta["name_with_owner"]
            healthy = self.remote_check() and healthy
        return healthy


# --- validators --------------------------------------------------------------
def _int_range(lo, hi):
    def v(x):
        return None if x.isdigit() and lo <= int(x) <= hi else f"a whole number from {lo} to {hi}"
    return v


def _hhmm(x):
    return None if re.match(r"^([01]\d|2[0-3]):[0-5]\d$", x) else "use 24h HH:MM, e.g. 14:00"


def _days(x):
    parts = [p.strip().title()[:3] for p in x.split(",") if p.strip()]
    return None if parts and all(p in DAYS for p in parts) else "e.g. Tue,Wed,Thu"


def _pem_candidates(folder, app_name=""):
    """.pem files in `folder`, the App's own key first, then newest first."""
    try:
        pems = [f for f in Path(folder).glob("*.pem") if f.is_file()]
    except OSError:
        return []
    return sorted(pems, key=lambda f: (not f.name.startswith(app_name), -f.stat().st_mtime))


def _find_pem(folder, app_name):
    """The App's downloaded key in `folder`, if exactly recognisable."""
    own = [f for f in _pem_candidates(folder, app_name) if app_name and f.name.startswith(app_name)]
    return own[0] if own else None


def _pem_file(x):
    p = Path(os.path.expanduser(x))
    if p.is_dir():
        return f"{p} is a folder — give the .pem file inside it"
    if not p.is_file():
        return f"no file at {p}"
    return None if "PRIVATE KEY" in p.read_text(errors="replace")[:200] else "that isn't a .pem private key"
