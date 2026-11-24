#!/usr/bin/env python3
"""
setup_wizard.py — set up (and later health-check) your XSIAM fleet repository.

    python scripts/setup_wizard.py               guided setup (resumable)
    python scripts/setup_wizard.py doctor        check everything, change nothing
    python scripts/setup_wizard.py doctor --remote   …and test every tenant's
                                                 stored key from GitHub Actions
    python scripts/setup_wizard.py add-tenant    connect one more tenant
    python scripts/setup_wizard.py --step tenants    re-run a single step

Unattended:  --answers answers.yml  (see docs/setup-wizard.md; secrets come
from environment variables named in the file, never from the file itself).

Needs: Python 3.10+, PyYAML, git, and the GitHub CLI logged in (`gh auth login`).
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import yaml  # noqa: E402

from fleet_setup.gh import GitHub  # noqa: E402
from fleet_setup.ui import UI, AnswerError, red  # noqa: E402
from fleet_setup.wizard import STEPS, Abort, Wizard  # noqa: E402

REPO = Path(__file__).resolve().parents[1]


def main(argv=None):
    ap = argparse.ArgumentParser(description="Set up and health-check your XSIAM fleet repo.")
    ap.add_argument("command", nargs="?", default="setup", choices=["setup", "doctor", "add-tenant"])
    ap.add_argument("--answers", help="YAML answers file for an unattended run")
    ap.add_argument("--step", choices=[s for s, _ in STEPS], help="run one setup step only")
    ap.add_argument("--remote", action="store_true", help="doctor: test tenant keys from GitHub Actions")
    ap.add_argument("--offline", action="store_true", help="doctor: skip network checks")
    args = ap.parse_args(argv)

    answers = None
    if args.answers:
        answers = yaml.safe_load(Path(args.answers).read_text()) or {}
    ui = UI(answers=answers)
    wiz = Wizard(REPO, ui, GitHub(cwd=str(REPO)))
    try:
        if args.command == "doctor":
            return 0 if wiz.doctor(offline=args.offline, remote=args.remote) else 1
        if args.command == "add-tenant":
            wiz.add_tenant_flow()
            return 0
        wiz.run(only=args.step)
        return 0
    except (Abort, AnswerError) as e:
        ui.say()
        ui.say(red("Stopped: ") + str(e))
        ui.say("Your answers so far are saved — re-run the same command to continue.")
        return 1
    except KeyboardInterrupt:
        ui.say()
        ui.say("Interrupted. Re-run the same command to continue where you left off.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
