"""Every workflow pins each action to a full commit SHA, and the SAME SHA as
every other workflow. Dependabot bumps the files it knows about; a workflow
added later with older pins (doctor.yml once ran on deprecated Node 20 this
way) fails here instead of drifting silently."""
import re
from collections import defaultdict
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
USES = re.compile(r"^\s*-?\s*uses:\s*([\w.-]+/[\w.-]+)@(\S+)", re.M)


def _pins():
    pins = defaultdict(dict)          # action -> {file: ref}
    for f in sorted(WORKFLOWS.glob("*.yml")):
        for action, ref in USES.findall(f.read_text()):
            pins[action][f.name] = ref
    return pins


def test_actions_are_pinned_to_full_shas():
    loose = [(a, f, ref) for a, files in _pins().items() for f, ref in files.items()
             if not re.fullmatch(r"[0-9a-f]{40}", ref)]
    assert loose == [], f"pin these to a commit SHA: {loose}"


def test_each_action_uses_one_sha_across_workflows():
    drift = {a: files for a, files in _pins().items() if len(set(files.values())) > 1}
    assert drift == {}, f"workflows disagree on action versions: {drift}"
