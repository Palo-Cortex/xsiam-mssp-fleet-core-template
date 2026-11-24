"""ring_gate.py against REAL git history — the trust properties that can't be
shown with monkeypatched pins:

  * no-skip reads the source ring from the BASE branch, so one PR can't add a
    version to qa and prod at once;
  * soak is measured on the base branch's first-parent history, so a backdated
    or long-lived PR commit can't arrive "pre-soaked";
  * a return to a version the ring already ran needs the `rollback` label,
    and with it no-skip/soak/window are waived;
  * converge's window re-check (converge_window) blocks a windowed ring outside
    the window unless the push is rollback-only.

Each test builds a throwaway repo; commit/merge dates are pinned through
GIT_AUTHOR_DATE / GIT_COMMITTER_DATE.
"""
import os
import subprocess
from datetime import datetime, timezone

import pytest
import yaml

import ring_gate

POLICY = {
    "promotion_order": ["dev", "qa", "prod"],
    "rings": {
        "dev": {"source": None},
        "qa": {"source": "dev", "min_soak_days": 2},
        "prod": {"source": "qa", "min_soak_days": 5, "require_change_window": True,
                 "change_windows": [{"days": ["Tue"], "start": "14:00", "end": "22:00"}]},
    },
}
IN_WINDOW = datetime(2026, 7, 21, 15, 0, tzinfo=timezone.utc)      # a Tuesday
OUT_OF_WINDOW = datetime(2026, 7, 25, 15, 0, tzinfo=timezone.utc)  # a Saturday


class Repo:
    def __init__(self, root):
        self.root = root
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@example.invalid")
        self.git("config", "user.name", "t")
        self.git("config", "commit.gpgsign", "false")
        self.write("fleet/policies.yml", POLICY)
        for ring in ("dev", "qa", "prod"):
            self.write(f"fleet/pins/{ring}.yml", {"ring": ring, "pins": {}})
        self.commit("init", "2026-01-01T00:00:00Z")

    def git(self, *args, date=None):
        env = dict(os.environ)
        if date:
            env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
        return subprocess.run(["git", "-C", str(self.root), *args], env=env,
                              check=True, capture_output=True, text=True).stdout.strip()

    def write(self, rel, data):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(yaml.safe_dump(data, sort_keys=False))

    def set_pin(self, ring, pack, version):
        p = self.root / "fleet" / "pins" / f"{ring}.yml"
        d = yaml.safe_load(p.read_text())
        d["pins"] = d.get("pins") or {}
        if version is None:
            d["pins"].pop(pack, None)
        else:
            d["pins"][pack] = version
        self.write(f"fleet/pins/{ring}.yml", d)

    def commit(self, msg, date):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", msg, date=date)

    def merge_pr(self, branch, changes, commit_date, merge_date):
        """Branch, apply {ring: {pack: version}}, commit at commit_date, then
        merge --no-ff into main at merge_date (what GitHub's merge button does)."""
        self.git("checkout", "-q", "-b", branch)
        for ring, pins in changes.items():
            for pack, version in pins.items():
                self.set_pin(ring, pack, version)
        self.commit(branch, commit_date)
        self.git("checkout", "-q", "main")
        self.git("merge", "-q", "--no-ff", "-m", f"merge {branch}", branch, date=merge_date)

    def open_pr(self, branch, changes, commit_date):
        """Leave the working tree on a PR branch carrying `changes`."""
        self.git("checkout", "-q", "-b", branch)
        for ring, pins in changes.items():
            for pack, version in pins.items():
                self.set_pin(ring, pack, version)
        self.commit(branch, commit_date)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    r = Repo(tmp_path)
    monkeypatch.setattr(ring_gate, "REPO", ring_gate.REPO)  # restored after test
    monkeypatch.setattr(ring_gate, "FLEET", ring_gate.FLEET)
    ring_gate.set_repo(tmp_path)
    return r


def _gate(ring, now, labels=()):
    base = ring_gate.sh("merge-base", "main", "HEAD").strip()
    return ring_gate.evaluate(ring, POLICY, base, now, labels=labels)


def test_one_pr_cannot_fill_source_and_target_rings(repo):
    """Adding X to dev, qa and prod in ONE PR (even with a backdated commit)
    fails no-skip: the source ring is read from the base branch."""
    repo.open_pr("sneaky", {"dev": {"X": "1.0.0"}, "qa": {"X": "1.0.0"},
                            "prod": {"X": "1.0.0"}}, commit_date="2025-01-01T00:00:00Z")

    v_qa, _ = _gate("qa", IN_WINDOW)
    v_prod, _ = _gate("prod", IN_WINDOW)

    assert any("not present in source ring 'dev'" in v for v in v_qa)
    assert any("not present in source ring 'qa'" in v for v in v_prod)


def test_soak_counts_from_merge_not_from_pr_commit_date(repo):
    """A dev pin committed long ago on a branch but merged 1 day before the
    promotion has soaked 1 day, not months."""
    repo.merge_pr("bump", {"dev": {"X": "1.0.0"}},
                  commit_date="2026-01-01T00:00:00Z", merge_date="2026-07-20T15:00:00Z")
    repo.open_pr("promote", {"qa": {"X": "1.0.0"}}, commit_date="2026-07-21T14:00:00Z")

    violations, _ = _gate("qa", IN_WINDOW)

    assert violations and "soaked 1.0d" in violations[0]


def test_soak_satisfied_after_merge_plus_min_days(repo):
    repo.merge_pr("bump", {"dev": {"X": "1.0.0"}},
                  commit_date="2026-07-10T00:00:00Z", merge_date="2026-07-10T00:00:00Z")
    repo.open_pr("promote", {"qa": {"X": "1.0.0"}}, commit_date="2026-07-21T14:00:00Z")

    violations, notes = _gate("qa", IN_WINDOW)

    assert violations == []
    assert any("soaked" in n and "✓" in n for n in notes)


def test_soak_clock_resets_when_pin_moves_away_and_back(repo):
    repo.merge_pr("a", {"dev": {"X": "1.0.0"}}, "2026-07-01T00:00:00Z", "2026-07-01T00:00:00Z")
    repo.merge_pr("b", {"dev": {"X": "1.1.0"}}, "2026-07-05T00:00:00Z", "2026-07-05T00:00:00Z")
    repo.merge_pr("c", {"dev": {"X": "1.0.0"}}, "2026-07-20T15:00:00Z", "2026-07-20T15:00:00Z")
    repo.open_pr("promote", {"qa": {"X": "1.0.0"}}, commit_date="2026-07-21T14:00:00Z")

    violations, _ = _gate("qa", IN_WINDOW)

    assert violations and "soaked 1.0d" in violations[0]


def _prod_ran_then_moved_on(repo):
    """prod ran X 1.0.0, then moved to 2.0.0 (qa also on 2.0.0)."""
    repo.merge_pr("d1", {"dev": {"X": "1.0.0"}}, "2026-06-01T00:00:00Z", "2026-06-01T00:00:00Z")
    repo.merge_pr("q1", {"qa": {"X": "1.0.0"}}, "2026-06-04T00:00:00Z", "2026-06-04T00:00:00Z")
    repo.merge_pr("p1", {"prod": {"X": "1.0.0"}}, "2026-06-10T00:00:00Z", "2026-06-10T00:00:00Z")
    repo.merge_pr("d2", {"dev": {"X": "2.0.0"}}, "2026-07-01T00:00:00Z", "2026-07-01T00:00:00Z")
    repo.merge_pr("q2", {"qa": {"X": "2.0.0"}}, "2026-07-04T00:00:00Z", "2026-07-04T00:00:00Z")
    repo.merge_pr("p2", {"prod": {"X": "2.0.0"}}, "2026-07-14T15:00:00Z", "2026-07-14T15:00:00Z")


def test_rollback_without_label_is_rejected_with_a_hint(repo):
    _prod_ran_then_moved_on(repo)
    repo.open_pr("rb", {"prod": {"X": "1.0.0"}}, commit_date="2026-07-25T15:00:00Z")

    violations, _ = _gate("prod", OUT_OF_WINDOW)

    assert any("'rollback' label" in v for v in violations)


def test_rollback_with_label_waives_no_skip_soak_and_window(repo):
    _prod_ran_then_moved_on(repo)
    repo.open_pr("rb", {"prod": {"X": "1.0.0"}}, commit_date="2026-07-25T15:00:00Z")

    violations, notes = _gate("prod", OUT_OF_WINDOW, labels=["rollback"])

    assert violations == []
    assert any("ROLLBACK" in n for n in notes)


def test_rollback_label_does_not_allow_never_run_versions(repo):
    _prod_ran_then_moved_on(repo)
    repo.open_pr("rb", {"prod": {"X": "0.9.0"}}, commit_date="2026-07-21T15:00:00Z")

    violations, _ = _gate("prod", IN_WINDOW, labels=["rollback"])

    assert any("not present in source ring" in v for v in violations)


def test_converge_window_blocks_outside_window(repo):
    _prod_ran_then_moved_on(repo)
    before = repo.git("rev-parse", "HEAD~1")   # prod at 1.0.0 → 2.0.0 push
    ok, reason = ring_gate.converge_window("prod", POLICY, before, "HEAD", OUT_OF_WINDOW)
    assert not ok and "outside the change window" in reason

    ok, _ = ring_gate.converge_window("prod", POLICY, before, "HEAD", IN_WINDOW)
    assert ok


def test_converge_window_allows_rollback_only_push(repo):
    _prod_ran_then_moved_on(repo)
    repo.merge_pr("rb", {"prod": {"X": "1.0.0"}}, "2026-07-25T15:00:00Z", "2026-07-25T15:00:00Z")
    before = repo.git("rev-parse", "HEAD~1")

    ok, reason = ring_gate.converge_window("prod", POLICY, before, "HEAD", OUT_OF_WINDOW)

    assert ok and "rollback" in reason


def test_converge_window_ignores_unwindowed_rings(repo):
    ok, _ = ring_gate.converge_window("qa", POLICY, None, "HEAD", OUT_OF_WINDOW)
    assert ok
