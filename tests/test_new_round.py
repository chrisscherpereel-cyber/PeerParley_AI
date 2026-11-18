"""Starting a new round (Evaluation 2) from an earlier one.

Requested: *"If I want to send a second round of peer evaluations built on the
current student team setup can I just change the evaluation number from 1 to 2
and everything will work?"* It did not — Eval 2 opened as an empty survey, so
the spreadsheet had to be uploaded again (losing every 👥 Teams edit) and the
questions went back to the defaults. Then: *"create a cleaner fix"*.

Run with:  python -m pytest tests/ -q
"""
from __future__ import annotations

import os
import subprocess
import sys

import pandas as pd
import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from peerparley import survey  # noqa: E402
from peerparley.vault import Vault  # noqa: E402


@pytest.fixture()
def vault(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FERNET_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("VAULT_BACKEND", "local")
    monkeypatch.setenv("VAULT_FOLDER", "rounds")
    return Vault()


ROWS = [{"First Name": f, "Last Name": "Lee", "Email": f"{f.lower()}@x.edu",
         "Team": t} for f, t in [("Ann", "1"), ("Bob", "1"), ("Cat", "2"),
                                  ("Dan", "2"), ("Lone", "")]]


def _s(course, ev, students=4, slug=None):
    return {"slug": slug or survey.slugify(course, ev), "course": course,
            "eval_no": ev, "owner": "admin", "teams": 2, "students": students}


def test_the_same_course_latest_round_is_suggested_first():
    pool = [_s("ACC 200", "1"), _s("MGT 301", "1"), _s("MGT 301", "3"),
            _s("MGT 301", "2"), _s("MGT 301", "9", students=0)]
    got = survey.round_candidates(pool, "mgt 301",
                                  exclude_slug=survey.slugify("MGT 301", "4"))
    assert [(c["course"], c["eval_no"]) for c in got] == [
        ("MGT 301", "3"), ("MGT 301", "2"), ("MGT 301", "1"), ("ACC 200", "1")]


def test_the_round_being_started_is_never_its_own_source():
    pool = [_s("MGT 301", "1"), _s("MGT 301", "2")]
    got = survey.round_candidates(pool, "MGT 301",
                                  exclude_slug=survey.slugify("MGT 301", "2"))
    assert [c["eval_no"] for c in got] == ["1"]


def test_copy_carries_teams_unassigned_and_settings_but_not_dates(vault):
    cfg = dict(survey.DEFAULT_SURVEY, ask_ratings=False,
               opens_at="2026-09-01T08:00:00", closes_at="2026-09-05T17:00:00",
               is_open=False, clock_offset_minutes=-420)
    slug1, _ = survey.save_setup("MGT 301", "1", pd.DataFrame(ROWS), cfg,
                                 vault=vault, owner="jdoe")
    slug2, summary = survey.start_round_from(vault, slug1, "MGT 301", "2",
                                             owner="jdoe")
    assert summary == {"teams": 2, "students": 4, "unassigned": 1}
    snap = survey.roster_now(vault, slug2)
    assert snap["owner"] == "jdoe"
    assert [m["name"] for m in snap["unassigned"]] == ["Lone Lee"]
    assert {m["email"] for ms in snap["teams"].values() for m in ms} == {
        "ann@x.edu", "bob@x.edu", "cat@x.edu", "dan@x.edu"}
    new = survey.load_survey(vault, slug2)
    assert new["ask_ratings"] is False
    assert new["clock_offset_minutes"] == -420     # the instructor's clock
    assert new["opens_at"] == "" and new["closes_at"] == ""
    assert new["is_open"] is True
    # A link from round 1 does not open round 2.
    links1 = survey.student_links("https://x", slug1, survey.saved_teams(vault, slug1), "s")
    links2 = survey.student_links("https://x", slug2, survey.saved_teams(vault, slug2), "s")
    assert not {l["link"] for l in links1} & {l["link"] for l in links2}


def test_copy_never_overwrites_a_saved_round(vault):
    slug1, _ = survey.save_setup("MGT 301", "1", pd.DataFrame(ROWS),
                                 dict(survey.DEFAULT_SURVEY), vault=vault)
    survey.save_setup("MGT 301", "2", pd.DataFrame(ROWS[:2]),
                      dict(survey.DEFAULT_SURVEY), vault=vault)
    with pytest.raises(ValueError, match="already has a saved student list"):
        survey.start_round_from(vault, slug1, "MGT 301", "2")
    with pytest.raises(ValueError, match="same survey"):
        survey.start_round_from(vault, slug1, "MGT 301", "1")
    with pytest.raises(ValueError, match="no saved student list"):
        survey.start_round_from(vault, "nope-eval1", "MGT 301", "3")
    # Eval 2 still holds exactly what was saved there.
    kept = survey.saved_teams(vault, survey.slugify("MGT 301", "2"))
    assert sorted(m["name"] for ms in kept.values() for m in ms) == [
        "Ann Lee", "Bob Lee"]


def test_the_page(tmp_path):
    """Offer, copy and the result, in the real app: tests/_new_round_app.py."""
    done = subprocess.run(
        [sys.executable, os.path.join(REPO, "tests", "_new_round_app.py"),
         str(tmp_path / "nr")], capture_output=True, text=True, timeout=900,
        cwd=REPO)
    assert "FAILURES: none" in done.stdout, done.stdout[-5000:]
    assert done.returncode == 0, done.stdout[-5000:]
    for line in ("PASS - an earlier round is offered",
                 "PASS - the teams come across as edited, not as uploaded",
                 "PASS - the dates did not",
                 "PASS - Eval 1 is untouched"):
        assert line in done.stdout, done.stdout[-5000:]
