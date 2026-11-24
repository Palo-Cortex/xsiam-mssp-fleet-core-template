"""`setup_wizard.py doctor` — read-only health check of a fleet repo.

Each check returns Result(status, title, detail, fix). Nothing here changes
files, GitHub settings or tenants. The local linters are run as the SAME
scripts ring-gate runs, so doctor can't disagree with CI.

Sections:
  Tools          python, git, gh login
  Configuration  promotion path, pin files, workflow choices, tenants, linters
  Content        pinned packs exist in the upstream catalog (network)
  GitHub         Environments, per-tenant secrets, protections, App, go-live
"""
import re
import subprocess
import sys
from collections import namedtuple
from pathlib import Path

from . import config
from .gh import GhError, GitHub

Result = namedtuple("Result", "status title detail fix")
OK, WARN, FAIL, INFO, SKIP = "ok", "warn", "fail", "info", "skip"
SECRET_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")


def r(status, title, detail="", fix=""):
    return Result(status, title, detail, fix)


class Doctor:
    def __init__(self, repo, gh=None, offline=False, catalog_loader=None):
        self.repo = Path(repo)
        self.gh = gh
        self.offline = offline
        self.catalog_loader = catalog_loader
        self.repo_meta = None

    # --- helpers -------------------------------------------------------------
    def _script(self, *args):
        p = subprocess.run([sys.executable, str(self.repo / "scripts" / args[0]), *args[1:]],
                           capture_output=True, text=True, cwd=self.repo)
        return p.returncode, (p.stdout + p.stderr).strip()

    def _rings(self):
        pol = config.policies(self.repo)
        return config.chain(self.repo), (pol.get("rings") or {})

    # --- sections ------------------------------------------------------------
    def tools(self):
        out = []
        v = sys.version_info
        out.append(r(OK if v >= (3, 10) else FAIL, f"Python {v.major}.{v.minor}",
                     fix="" if v >= (3, 10) else "install Python 3.10 or newer"))
        git = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "--is-inside-work-tree"],
                             capture_output=True, text=True)
        out.append(r(OK if git.returncode == 0 else FAIL, "git repository",
                     fix="" if git.returncode == 0 else "run the wizard from your fleet repo clone"))
        if not GitHub.available():
            out.append(r(FAIL, "GitHub CLI (gh) installed",
                         fix="install it: https://cli.github.com, then `gh auth login`"))
        elif self.gh:
            ok, msg = self.gh.auth_status()
            out.append(r(OK if ok else FAIL, "gh is logged in",
                         "" if ok else msg.splitlines()[0] if msg else "",
                         "" if ok else "run `gh auth login`"))
        return out

    def configuration(self):
        out = []
        rings, ringpol = self._rings()
        if not rings:
            return [r(FAIL, "promotion path", "fleet/policies.yml has no promotion_order",
                      "run `python scripts/setup_wizard.py`")]
        topo = next((k for k, v in config.TOPOLOGIES.items() if v == rings), None)
        out.append(r(OK if topo else WARN, f"promotion path: {' → '.join(rings)}",
                     "" if topo else "not one of the wizard's standard paths (still supported)"))

        chain_ok = all(r_ in ringpol for r_ in rings) and \
            (ringpol.get(rings[0]) or {}).get("source") is None and \
            all((ringpol.get(rings[i]) or {}).get("source") == rings[i - 1] for i in range(1, len(rings)))
        out.append(r(OK if chain_ok else FAIL, "each ring promotes from the one before it",
                     "" if chain_ok else "rings.<ring>.source doesn't match promotion_order",
                     "" if chain_ok else "fix fleet/policies.yml (or re-run the wizard's step 2)"))

        all_rings = set(ringpol)
        pin_files = {f.stem for f in (self.repo / "fleet" / "pins").glob("*.yml")}
        if pin_files == all_rings:
            out.append(r(OK, "one pin file per ring"))
        else:
            out.append(r(FAIL, "one pin file per ring",
                         f"policies: {sorted(all_rings)}; pin files: {sorted(pin_files)}",
                         "add/remove fleet/pins/<ring>.yml to match fleet/policies.yml"))

        wf = config.workflow_choices(self.repo)
        want_conv = rings + [x for x in sorted(all_rings) if x not in rings]
        conv_ok = set(wf.get("converge") or []) == set(want_conv)
        prom_ok = list(wf.get("promote") or []) == rings[1:]
        out.append(r(OK if conv_ok and prom_ok else FAIL, "workflow ring choices match",
                     "" if conv_ok and prom_ok else
                     f"converge: {wf.get('converge')} (want {want_conv}); "
                     f"promote: {wf.get('promote')} (want {rings[1:]})",
                     "" if conv_ok and prom_ok else
                     "re-run the wizard's step 2, or edit the options in converge.yml / promote.yml"))

        tenants = config.tenants(self.repo)
        for ring in sorted(all_rings):
            members = [t for t, p in tenants.items() if p.get("ring") == ring]
            out.append(r(OK if members else FAIL, f"ring '{ring}' has a tenant",
                         ", ".join(members), "" if members else
                         f"add one: python scripts/setup_wizard.py add-tenant (ring {ring})"))
        for t, p in tenants.items():
            problems = []
            if config.tenant_name_error(t):
                problems.append("name: " + config.tenant_name_error(t))
            if p.get("tenant") != t:
                problems.append(f"`tenant:` is '{p.get('tenant')}', file is {t}.yml")
            if p.get("ring") not in all_rings:
                problems.append(f"ring '{p.get('ring')}' isn't in policies.yml")
            ref = p.get("credentials_ref") or ""
            if not SECRET_NAME.match(ref) or ref.startswith("GITHUB_"):
                problems.append(f"credentials_ref '{ref}' isn't a valid GitHub secret prefix")
            out.append(r(FAIL if problems else OK, f"tenant {t}", "; ".join(problems),
                         "fix fleet/tenants/%s.yml" % t if problems else ""))

        for ring in sorted(all_rings):
            code, msg = self._script("resolve.py", "--ring", ring, "--json")
            out.append(r(OK if code == 0 else FAIL, f"ring '{ring}' resolves (every pack pinned)",
                         "" if code == 0 else msg.splitlines()[-1],
                         "" if code == 0 else f"pin the missing pack(s) in fleet/pins/{ring}.yml"))
        for title, args, fix in [
            ("namespace contract", ["namespace_lint.py"], "see the message above"),
            ("Config Overlays", ["config_overlay_lint.py"], "see the message above"),
            ("MSSP pack catalog in sync", ["mssp_catalog.py", "--check"],
             "python scripts/mssp_catalog.py --write"),
            ("artifact lock", ["lock.py", "--check"], "python scripts/lock.py --write"),
        ]:
            code, msg = self._script(*args)
            out.append(r(OK if code == 0 else FAIL, title, "" if code == 0 else msg, "" if code == 0 else fix))

        rb = ((config.defaults(self.repo).get("mssp_catalog") or {}).get("release_base") or "")
        if "YOUR-ORG" in rb or not rb:
            out.append(r(FAIL, "release_base points at this repo", rb,
                         "re-run the wizard's step 2 (it sets fleet/defaults.yml mssp_catalog.release_base)"))
        elif self.repo_meta and self.repo_meta["name_with_owner"].lower() not in rb.lower():
            out.append(r(WARN, "release_base points at this repo",
                         f"{rb} isn't {self.repo_meta['name_with_owner']} (fine if packs release elsewhere)"))
        else:
            out.append(r(OK, "release_base points at this repo"))

        left = config.example_leftovers(self.repo)
        out.append(r(WARN if left else OK, "template examples removed", ", ".join(left),
                     "re-run the wizard's step 2 to remove them" if left else ""))

        last = (ringpol.get(rings[-1]) or {})
        out.append(r(OK if last.get("require_lock") else WARN,
                     f"{rings[-1]} deploys only locked (sha256-verified) artifacts",
                     "" if last.get("require_lock") else "require_lock is off",
                     "" if last.get("require_lock") else
                     f"python scripts/lock.py --write, then set rings.{rings[-1]}.require_lock: true"))
        out.append(r(INFO, f"{rings[-1]} change window",
                     "on" if last.get("require_change_window") else "off (deploys any time)"))
        return out

    def content(self):
        if self.offline:
            return [r(SKIP, "upstream catalog", "skipped (--offline)")]
        loader = self.catalog_loader
        if loader is None:
            sys.path.insert(0, str(self.repo / "scripts"))
            from catalog import load_catalog
            d = config.defaults(self.repo).get("catalog") or {}
            loader = lambda: load_catalog(d.get("url"), d.get("pinned_commit"))  # noqa: E731
        try:
            cat = loader()
        except Exception as e:  # noqa: BLE001
            return [r(WARN, "upstream catalog reachable", str(e).splitlines()[0],
                      "check network access to raw.githubusercontent.com")]
        published = {p.get("id"): p.get("version") for p in cat.get("packs", [])}
        mssp = {d.name for d in (self.repo / "Packs").glob("*") if (d / "pack_metadata.json").exists()}
        sys.path.insert(0, str(self.repo / "scripts"))
        import lock as lockmod
        import resolve as resolvemod
        resolvemod.FLEET = self.repo / "fleet"
        locked = lockmod.load_lock()
        out = [r(OK, "upstream catalog reachable", f"{len(published)} packs")]
        seen = set()
        for ring in sorted({f.stem for f in (self.repo / "fleet" / "pins").glob("*.yml")}):
            for pack, ver in config.pins(self.repo, ring).items():
                ver = str(ver)
                if (pack, ver) in seen or ver == "local" or pack in mssp:
                    continue
                seen.add((pack, ver))
                is_locked = bool(lockmod.lock_entry(locked, pack, ver))
                if pack not in published and not is_locked:
                    out.append(r(FAIL, f"{pack} {ver} deployable",
                                 "not in the upstream catalog and not locked — converge can't fetch it",
                                 "pick a pack from the catalog, or lock a known zip URL"))
                elif published.get(pack) != ver and not is_locked:
                    out.append(r(WARN, f"{pack} {ver} deployable",
                                 f"catalog publishes {published.get(pack)}; the URL for {ver} is guessed",
                                 f"lock it (python scripts/lock.py --write --pack {pack} --allow-retag) "
                                 f"after checking, or move to {published.get(pack)}"))
        if len(out) == 1:
            out.append(r(OK, "every pinned pack is fetchable"))
        return out

    def github(self):
        if not self.gh or not self.repo_meta:
            return [r(SKIP, "GitHub settings", "gh not logged in / repo not found",
                      "run `gh auth login` in this repo")]
        out = []
        meta = self.repo_meta
        if meta["name_with_owner"].lower() == config.TEMPLATE_REPO.lower():
            out.append(r(FAIL, "using your own repository",
                         "this clone points at the shared template",
                         "create your own repo from the template (the wizard's step 1 offers to)"))
        admin = meta.get("permission") == "ADMIN"
        out.append(r(OK if admin else WARN, "you have admin rights on the repo",
                     meta.get("permission") or "", "" if admin else
                     "some checks below need admin; ask a repo admin to run doctor"))

        rings, ringpol = self._rings()
        try:
            envs = self.gh.environments()
        except GhError as e:
            envs = None
            out.append(r(WARN, "Environments readable", str(e).splitlines()[0]))
        tenants = config.tenants(self.repo)
        if envs is not None:
            needed = sorted({(ringpol.get(x) or {}).get("environment") for x in ringpol} - {None, ""})
            for env in needed:
                out.append(r(OK if env in envs else FAIL, f"Environment {env}",
                             fix="" if env in envs else "run the wizard's protections step"))
            last_env = (ringpol.get(rings[-1]) or {}).get("environment") if rings else None
            if last_env in envs:
                revs = self.gh.environment_reviewers(envs[last_env])
                out.append(r(OK if revs else WARN, f"{last_env} requires an approver",
                             ", ".join((x.get("reviewer") or {}).get("login") or
                                       (x.get("reviewer") or {}).get("slug") or "?" for x in revs),
                             "" if revs else "add required reviewers (wizard protections step)"))
            if not admin:
                out.append(r(SKIP, "per-tenant secrets", "GitHub only shows secrets to repo admins"))
            repo_secrets = self.gh.secret_names() if admin else set()
            env_secrets = {}
            for t, p in (tenants.items() if admin else []):
                ref = p.get("credentials_ref") or ""
                env = (ringpol.get(p.get("ring")) or {}).get("environment") or ""
                if env and env not in env_secrets:
                    env_secrets[env] = self.gh.secret_names(env) if env in envs else set()
                names = env_secrets.get(env, set())
                want = {f"{ref}_CREDS"}
                flat = {f"{ref}_BASE_URL", f"{ref}_API_KEY", f"{ref}_AUTH_ID"}
                if want & names or flat <= names:
                    out.append(r(OK, f"secret for {t}", f"{ref}_CREDS in {env}"))
                elif want & repo_secrets or flat <= repo_secrets:
                    out.append(r(WARN, f"secret for {t}",
                                 f"{ref}_CREDS is a REPO secret — every job could read it",
                                 f"move it into the {env} Environment (add-tenant re-stores it)"))
                else:
                    out.append(r(FAIL, f"secret for {t}", f"{ref}_CREDS not found in {env}",
                                 f"python scripts/setup_wizard.py add-tenant  (re-enter {t}'s key)"))

        labels = self.gh.label_names()
        miss = [x for x in ("rollback", "promotion") if x not in labels]
        out.append(r(WARN if miss else OK, "labels rollback / promotion",
                     f"missing: {', '.join(miss)}" if miss else "", "run the wizard's protections step" if miss else ""))

        prot = self.gh.branch_protection(meta["default_branch"]) if admin else None
        if not admin:
            out.append(r(SKIP, f"{meta['default_branch']} protection",
                         "GitHub only shows branch protection to repo admins"))
        elif not prot:
            out.append(r(FAIL, f"{meta['default_branch']} is protected",
                         "anyone with write access can push straight to it, skipping ring-gate",
                         "run the wizard's protections step"))
        else:
            checks = set((prot.get("required_status_checks") or {}).get("contexts") or [])
            checks |= {c.get("context") for c in (prot.get("required_status_checks") or {}).get("checks") or []}
            out.append(r(OK if "gate" in checks else FAIL, "ring-gate is a required check",
                         "" if "gate" in checks else f"required checks: {sorted(checks) or 'none'}",
                         "" if "gate" in checks else "run the wizard's protections step"))
            prs = prot.get("required_pull_request_reviews")
            out.append(r(OK if prs is not None else WARN, "changes go through pull requests",
                         fix="" if prs is not None else "run the wizard's protections step"))
        mq = any(any(rule.get("type") == "merge_queue" for rule in (rs.get("rules") or []))
                 for rs in self.gh.rulesets())
        out.append(r(OK if mq else INFO, "merge queue",
                     "on" if mq else "off — converge still re-checks change windows"))

        vars_ = self.gh.variables()
        app = "FLEET_APP_ID" in vars_ and "FLEET_APP_PRIVATE_KEY" in self.gh.secret_names()
        out.append(r(OK if app else WARN, "promote workflow can trigger ring-gate (GitHub App)",
                     "" if app else "promote PRs will need ring-gate re-triggered by hand",
                     "" if app else "wizard step 'Who approves changes' sets up the App"))
        live = vars_.get("FLEET_LIVE") == "true"
        out.append(r(INFO, "live deployments", "ON — merges deploy to tenants" if live else
                     "off — converge is dry-run only (set FLEET_LIVE=true to go live)"))
        out.append(r(INFO, "orphan deletion", "ON" if vars_.get("FLEET_ALLOW_DELETE") == "true"
                     else "off (FLEET_ALLOW_DELETE) — prove it on a lab tenant first"))
        return out

    # --- run -----------------------------------------------------------------
    def run(self):
        if self.gh:
            try:
                self.repo_meta = self.gh.repo_info()
                self.gh.repo = self.repo_meta["name_with_owner"]
            except GhError:
                self.repo_meta = None
        return [("Tools", self.tools()), ("Configuration", self.configuration()),
                ("Content", self.content()), ("GitHub", self.github())]


def render(sections, ui):
    from .ui import bold, dim, green, red, yellow, cyan
    icon = {OK: green("✓"), WARN: yellow("!"), FAIL: red("✗"), INFO: cyan("·"), SKIP: dim("-")}
    counts = {OK: 0, WARN: 0, FAIL: 0}
    for name, results in sections:
        ui.say()
        ui.say(bold(name))
        for res in results:
            counts[res.status] = counts.get(res.status, 0) + 1
            ui.say(f"  {icon[res.status]} {res.title}" + (dim(f" — {res.detail}") if res.detail and res.status != FAIL else ""))
            if res.status in (FAIL, WARN):
                if res.detail and res.status == FAIL:
                    for line in res.detail.splitlines()[:6]:
                        ui.say(dim(f"      {line}"))
                if res.fix:
                    ui.say(f"      → {res.fix}")
    ui.say()
    summary = f"{counts[OK]} ok, {counts[WARN]} warning(s), {counts[FAIL]} problem(s)"
    ui.say(bold(red(summary) if counts[FAIL] else yellow(summary) if counts[WARN] else green(summary)))
    return counts[FAIL] == 0
