"""Terminal prompts for the setup wizard — interactive, or answered from a file.

Every prompt has an ANSWER KEY (a dotted path such as "org.topology" or
"tenants.0.name"). Interactively the key is ignored; with --answers FILE the
value is taken from that YAML file instead of asking, so the wizard can run
unattended (CI, repeatable environments, tests). A key missing from the file
falls back to the prompt's default, or fails if there is none — an unattended
run never blocks on stdin.

Secrets are never read from the answers file: a key ending in `_env` names an
environment variable holding the value (e.g. tenants.0.api_key_env: ACME_KEY).
"""
import getpass
import os
import sys

_TTY = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(code, s):
    return f"\033[{code}m{s}\033[0m" if _TTY else s


def bold(s):
    return _c("1", s)


def dim(s):
    return _c("2", s)


def green(s):
    return _c("32", s)


def yellow(s):
    return _c("33", s)


def red(s):
    return _c("31", s)


def cyan(s):
    return _c("36", s)


OK, WARN, FAIL, INFO = green("✓"), yellow("!"), red("✗"), cyan("·")


class AnswerError(Exception):
    """An unattended run needed an answer the file doesn't provide."""


_MISSING = object()


class UI:
    def __init__(self, answers=None, out=None, inp=None, secret_inp=None):
        self.answers = answers            # dict, or None for interactive
        self.out = out or sys.stdout
        self._input = inp or input
        self._secret = secret_inp or getpass.getpass

    @property
    def unattended(self):
        return self.answers is not None

    # --- output --------------------------------------------------------------
    def say(self, text=""):
        print(text, file=self.out)

    def step(self, n, total, title):
        self.say()
        self.say(bold(f"━━ Step {n}/{total} · {title} ") + bold("━" * max(0, 50 - len(title))))

    def explain(self, text):
        for line in text.strip("\n").splitlines():
            self.say(dim("  " + line) if line.strip() else "")

    def ok(self, text):
        self.say(f"  {OK} {text}")

    def warn(self, text):
        self.say(f"  {WARN} {text}")

    def fail(self, text):
        self.say(f"  {FAIL} {text}")

    def info(self, text):
        self.say(f"  {INFO} {text}")

    # --- answers file --------------------------------------------------------
    def lookup(self, key):
        node = self.answers
        for part in key.split("."):
            if isinstance(node, list) and part.isdigit() and int(part) < len(node):
                node = node[int(part)]
            elif isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return _MISSING
        return node

    def _answered(self, key, default):
        v = self.lookup(key) if self.unattended else _MISSING
        if v is _MISSING:
            if default is None:
                raise AnswerError(f"answers file has no value for '{key}'")
            return default
        return v

    # --- prompts -------------------------------------------------------------
    def ask(self, key, prompt, default=None, validate=None, help=None):
        """Free text. `validate(value)` returns an error string or None."""
        if self.unattended:
            value = str(self._answered(key, default))
            err = validate(value) if validate else None
            if err:
                raise AnswerError(f"'{key}': {err}")
            return value
        if help:
            self.explain(help)
        suffix = f" [{default}]" if default not in (None, "") else ""
        while True:
            value = self._input(f"  {prompt}{suffix}: ").strip()
            if not value and default is not None:
                value = str(default)
            err = validate(value) if validate else (None if value else "a value is required")
            if not err:
                return value
            self.fail(err)

    def confirm(self, key, prompt, default=True):
        if self.unattended:
            v = self._answered(key, default)
            return v if isinstance(v, bool) else str(v).lower() in ("y", "yes", "true", "1")
        hint = "Y/n" if default else "y/N"
        while True:
            v = self._input(f"  {prompt} [{hint}]: ").strip().lower()
            if not v:
                return default
            if v in ("y", "yes"):
                return True
            if v in ("n", "no"):
                return False
            self.fail("please answer y or n")

    def choose(self, key, prompt, options, default=None):
        """Pick one of [(value, label)]. Returns the value."""
        values = [v for v, _ in options]
        if self.unattended:
            v = self._answered(key, default)
            if v not in values:
                raise AnswerError(f"'{key}': {v!r} is not one of {values}")
            return v
        self.say(f"  {prompt}")
        for i, (_v, label) in enumerate(options, 1):
            self.say(f"    {bold(str(i))}) {label}")
        dflt = str(values.index(default) + 1) if default in values else None
        while True:
            raw = self._input(f"  Choose 1-{len(options)}" + (f" [{dflt}]" if dflt else "") + ": ").strip()
            raw = raw or (dflt or "")
            if raw.isdigit() and 1 <= int(raw) <= len(options):
                return values[int(raw) - 1]
            self.fail(f"enter a number from 1 to {len(options)}")

    def choose_many(self, key, prompt, options, default=()):
        """Pick any of [(value, label)] by comma-separated numbers. Returns values."""
        values = [v for v, _ in options]
        if self.unattended:
            picked = self._answered(key, list(default))
            bad = [p for p in picked if p not in values]
            if bad:
                raise AnswerError(f"'{key}': unknown option(s) {bad}")
            return list(picked)
        self.say(f"  {prompt}")
        for i, (v, label) in enumerate(options, 1):
            mark = green("*") if v in default else " "
            self.say(f"   {mark}{bold(str(i)):>3}) {label}")
        dflt = ",".join(str(values.index(v) + 1) for v in default if v in values)
        while True:
            raw = self._input("  Numbers, comma-separated (blank = the * ones, '-' = none)"
                              + (f" [{dflt}]" if dflt else "") + ": ").strip()
            if raw == "-":
                return []
            raw = raw or dflt
            try:
                idx = [int(x) for x in raw.replace(" ", "").split(",") if x]
            except ValueError:
                idx = [0]
            if idx and all(1 <= i <= len(options) for i in idx) or not raw:
                return [values[i - 1] for i in dict.fromkeys(idx)]
            self.fail(f"enter numbers from 1 to {len(options)}, e.g. 1,3")

    def secret(self, key, prompt):
        """A secret value: typed without echo, or read from the env var named
        by `<key>_env` in the answers file."""
        if self.unattended:
            var = self.lookup(f"{key}_env")
            if var is _MISSING or not os.environ.get(str(var)):
                raise AnswerError(f"set '{key}_env' in the answers file to an environment "
                                  f"variable that holds the value")
            return os.environ[str(var)]
        while True:
            v = self._secret(f"  {prompt} (input hidden): ").strip()
            if v:
                return v
            self.fail("a value is required")
