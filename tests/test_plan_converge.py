"""plan_converge.py — the converge planner, against a real throwaway git repo.

  * a multi-commit push converges every ring changed ANYWHERE in it (not just
    the last commit's);
  * a windowed ring outside its window is dropped and reported as blocked —
    unless the push is rollback-only, or a dispatch sets --break-glass;
  * canary tenants are split out to deploy first; entries carry the tenant's
    credentials_ref and the ring's Environment; max_parallel comes from policy.
"""
import os
import subprocess
from datetime import datetime, timezone

import pytest
import yaml

import plan_converge
import ring_gate

IN_WINDOW = datetime(2026, 7, 21, 15, 0, tzinfo=timezone.utc)      # Tuesday
OUT_OF_WINDOW = datetime(2026, 7, 25, 15, 0, tzinfo=timezone.utc)  # Saturday

POLICY = {"rings": {
    "dev": {"source": None, "environment": "fleet-nonprod"},
    "qa": {"source": "dev", "environment": "fleet-nonprod", "max_parallel": 10},
    "prod": {"source": "qa", "environment": "fleet-prod", "max_parallel": 3,
             "require_change_window": True,
             "change_windows": [{"days": ["Tue"], "start": "14:00", "end": "22:00"}]},
}}


@pytest.fixture
def fleet_repo(tmp_path, monkeypatch):
    def git(*a):
        env = {**os.environ, "GIT_AUTHOR_DATE": "2026-07-01T00:00:00Z",
               "GIT_COMMITTER_DATE": "2026-07-01T00:00:00Z"}
        return subprocess.run(["git", "-C", str(tmp_path), *a], env=env, check=True,
                              capture_output=True, text=True).stdout.strip()

    def write(rel, data):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(yaml.safe_dump(data, sort_keys=False))

    def commit(msg):
        git("add", "-A")
        git("commit", "-q", "-m", msg)
        return git("rev-parse", "HEAD")

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.invalid")
    git("config", "user.name", "t")
    git("config", "commit.gpgsign", "false")
    write("fleet/policies.yml", POLICY)
    for ring in ("dev", "qa", "prod"):
        write(f"fleet/pins/{ring}.yml", {"ring": ring, "pins": {"X": "1.0.0"}})
    write("fleet/tenants/devlab.yml", {"tenant": "devlab", "ring": "dev", "credentials_ref": "DEVLAB"})
    write("fleet/tenants/qa1.yml", {"tenant": "qa1", "ring": "qa", "credentials_ref": "QA1"})
    write("fleet/tenants/prodcanary.yml", {"tenant": "prodcanary", "ring": "prod",
                                           "credentials_ref": "PRODCANARY", "canary": True})
    write("fleet/tenants/prod1.yml", {"tenant": "prod1", "ring": "prod", "credentials_ref": "PROD1"})
    root = commit("init")

    monkeypatch.setattr(plan_converge, "REPO", tmp_path)
    monkeypatch.setattr(ring_gate, "REPO", ring_gate.REPO)
    monkeypatch.setattr(ring_gate, "FLEET", ring_gate.FLEET)

    class F:
        pass
    f = F()
    f.root, f.write, f.commit, f.git, f.first = tmp_path, write, commit, git, root
    return f


def _names(entries):
    return [e["tenant"] for e in entries]


def test_multi_commit_push_covers_every_changed_ring(fleet_repo):
    before = fleet_repo.first
    fleet_repo.write("fleet/pins/dev.yml", {"ring": "dev", "pins": {"X": "2.0.0"}})
    fleet_repo.commit("dev bump")
    fleet_repo.write("fleet/pins/qa.yml", {"ring": "qa", "pins": {"X": "2.0.0"}})
    after = fleet_repo.commit("qa bump")

    p = plan_converge.plan("push", before=before, after=after, now=IN_WINDOW)

    assert sorted(_names(p["fleet"])) == ["devlab", "qa1"]


def test_entries_carry_ref_and_environment(fleet_repo):
    before = fleet_repo.first
    fleet_repo.write("fleet/pins/qa.yml", {"ring": "qa", "pins": {"X": "2.0.0"}})
    after = fleet_repo.commit("qa bump")

    p = plan_converge.plan("push", before=before, after=after, now=IN_WINDOW)

    assert p["fleet"] == [{"tenant": "qa1", "ring": "qa", "credentials_ref": "QA1",
                           "environment": "fleet-nonprod"}]
    assert p["max_parallel"] == 10


def test_prod_in_window_splits_canary_first(fleet_repo):
    before = fleet_repo.first
    fleet_repo.write("fleet/pins/prod.yml", {"ring": "prod", "pins": {"X": "2.0.0"}})
    after = fleet_repo.commit("prod bump")

    p = plan_converge.plan("push", before=before, after=after, now=IN_WINDOW)

    assert _names(p["canary"]) == ["prodcanary"]
    assert _names(p["fleet"]) == ["prod1"]
    assert p["max_parallel"] == 3
    assert p["blocked"] == []


def test_prod_outside_window_is_blocked(fleet_repo):
    before = fleet_repo.first
    fleet_repo.write("fleet/pins/prod.yml", {"ring": "prod", "pins": {"X": "2.0.0"}})
    after = fleet_repo.commit("prod bump")

    p = plan_converge.plan("push", before=before, after=after, now=OUT_OF_WINDOW)

    assert p["canary"] == [] and p["fleet"] == []
    assert p["blocked"] and "outside the change window" in p["blocked"][0]


def test_prod_rollback_push_outside_window_is_allowed(fleet_repo):
    fleet_repo.write("fleet/pins/prod.yml", {"ring": "prod", "pins": {"X": "2.0.0"}})
    before = fleet_repo.commit("prod bump")
    fleet_repo.write("fleet/pins/prod.yml", {"ring": "prod", "pins": {"X": "1.0.0"}})
    after = fleet_repo.commit("rollback")

    p = plan_converge.plan("push", before=before, after=after, now=OUT_OF_WINDOW)

    assert sorted(_names(p["canary"] + p["fleet"])) == ["prod1", "prodcanary"]


def test_dispatch_outside_window_needs_break_glass(fleet_repo):
    blocked = plan_converge.plan("workflow_dispatch", ring="prod", now=OUT_OF_WINDOW)
    glass = plan_converge.plan("workflow_dispatch", ring="prod", break_glass=True,
                               now=OUT_OF_WINDOW)

    assert blocked["fleet"] == [] and blocked["blocked"]
    assert _names(glass["fleet"]) == ["prod1"]


def test_zero_before_sha_falls_back_to_parent(fleet_repo):
    fleet_repo.write("fleet/pins/dev.yml", {"ring": "dev", "pins": {"X": "3.0.0"}})
    after = fleet_repo.commit("dev bump")

    p = plan_converge.plan("push", before="0" * 40, after=after, now=IN_WINDOW)

    assert _names(p["fleet"]) == ["devlab"]


def test_dry_run_plans_windowed_ring_with_a_note(fleet_repo):
    """FLEET_LIVE off: nothing deploys, so the window must not withhold (and
    paint the run red) — it becomes a note, and prod gets its dry-run plan."""
    before = fleet_repo.first
    fleet_repo.write("fleet/pins/prod.yml", {"ring": "prod", "pins": {"X": "2.0.0"}})
    after = fleet_repo.commit("prod bump")

    p = plan_converge.plan("push", before=before, after=after, now=OUT_OF_WINDOW, dry_run=True)

    assert p["blocked"] == []
    assert sorted(_names(p["canary"] + p["fleet"])) == ["prod1", "prodcanary"]
    assert p["notes"] and "dry-run only" in p["notes"][0]


def test_live_run_still_withholds_outside_window(fleet_repo):
    before = fleet_repo.first
    fleet_repo.write("fleet/pins/prod.yml", {"ring": "prod", "pins": {"X": "2.0.0"}})
    after = fleet_repo.commit("prod bump")

    p = plan_converge.plan("push", before=before, after=after, now=OUT_OF_WINDOW, dry_run=False)

    assert p["blocked"] and p["fleet"] == [] and p["notes"] == []
