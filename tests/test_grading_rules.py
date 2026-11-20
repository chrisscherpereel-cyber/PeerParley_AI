"""Per-evaluation grading rules, rough response scoring, and overrides.

Requested (2.21.0): *"I would like the grading settings to be unique to each
evaluation - so the defaults are changed in one evaluation they are not
changed in all evaluations. There should be alternative methods for response
scoring - one would be rough scoring - 0 points for no submission, 5 points
for a submission will little distinction between members and very little
feedback. 7 points for some feedback but not considered constructive, and 10
points for constructive feedback. I would like the instructor to be able to
override the grade adjustment values on individual feedback reports. I would
like the instructor to be able to override the response points in the
individual feedback report. Se the default sensitivity to 1.0, Max increase to
0%, the rounding to 5% and the rounding down."*

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
from peerparley import vault as vmod  # noqa: E402
from peerparley.grading import (  # noqa: E402
    GradeSettings, apply_overrides, compute, results_to_frame, rough_response,
    settings_from,
)
from peerparley.vault import Vault  # noqa: E402

GOOD = ("{who} rebuilt the financial model after the data changed and walked "
        "us through every assumption in Tuesday's meeting.")
VAGUE = "{who} was a good team member overall and did their part on the work."


def team(written: dict, dollars: dict | None = None, ranks: dict | None = None,
         non_submitters=("Eve",)):
    """One team. `written[author]` is a template for what they wrote about
    each teammate; authors in `non_submitters` submitted nothing."""
    names = list(written) + list(non_submitters)
    rows = []
    for ev in written:
        for ee in names:
            if ev == ee:
                continue
            even = 100 / (len(names) - 1)
            rows.append({
                "evaluator": ev, "evaluator_key": ev.lower(),
                "evaluator_team": "1", "evaluatee": ee,
                "evaluatee_key": ee.lower(),
                "points": (dollars or {}).get(ev, {}).get(ee, even),
                "public_comment": written[ev].format(who=ee),
                "rank": (ranks or {}).get(ev, {}).get(ee, 2),
                "r0": 5, "r1": 5, "r2": 5, "r3": 5})
    return pd.DataFrame(rows)


ROUGH = GradeSettings(response_scoring="rough")


def _by_name(teams):
    return {m.name: m for t in teams for m in t.members}


# --------------------------------------------------------------------------- #
# Rough scoring
# --------------------------------------------------------------------------- #
def test_the_four_rough_tiers():
    df = team({"Ann": "fine", "Bob": VAGUE, "Cid": GOOD,
               "Dan": "ok"},
              dollars={"Dan": {"Ann": 10, "Bob": 40, "Cid": 40, "Eve": 10}})
    m = _by_name(compute(df, ROUGH))
    assert m["Eve"].response_points == 0           # no submission
    assert m["Ann"].response_points == 5           # "fine", even split
    assert m["Bob"].response_points == 7           # wrote, but vague
    assert m["Cid"].response_points == 10          # specific, for everyone
    # Wrote almost nothing, but clearly told teammates apart in the $100:
    # that is some feedback, not the minimal tier.
    assert m["Dan"].response_points == 7
    assert "no submission" in m["Eve"].response_note
    assert "constructive" in m["Cid"].response_note


def test_a_different_ranking_counts_as_telling_teammates_apart():
    df = team({"Ann": "ok", "Bob": "ok"},
              ranks={"Ann": {"Bob": 1, "Eve": 3}})
    m = _by_name(compute(df, ROUGH))
    assert m["Ann"].response_points == 7
    assert m["Bob"].response_points == 5


def test_constructive_needs_at_least_half_the_teammates():
    names = ["Bob", "Cid", "Dan", "Eve"]
    given = [{"comment": GOOD.format(who=n) if i < 2 else "fine",
              "points": 25, "rating": 5, "rank": 2} for i, n in enumerate(names)]
    assert rough_response(given, [], 25)[0] == "constructive"
    given[1]["comment"] = "fine"
    assert rough_response(given, [], 25)[0] != "constructive"


def test_rough_points_scale_with_the_maximum():
    df = team({"Ann": "fine", "Bob": VAGUE, "Cid": GOOD})
    m = _by_name(compute(df, GradeSettings(response_scoring="rough",
                                           max_comment_points=20)))
    assert (m["Ann"].response_points, m["Bob"].response_points,
            m["Cid"].response_points, m["Eve"].response_points) == (10, 14, 20, 0)


def test_quality_scoring_is_unchanged_and_still_the_default():
    assert GradeSettings().response_scoring == "quality"
    df = team({"Ann": "fine", "Bob": VAGUE, "Cid": GOOD})
    q = _by_name(compute(df, GradeSettings()))
    assert q["Ann"].response_note == ""
    assert q["Ann"].response_points < q["Bob"].response_points < q["Cid"].response_points


def test_rough_scoring_does_not_touch_the_grade():
    df = team({"Ann": "fine", "Bob": VAGUE, "Cid": GOOD},
              dollars={"Ann": {"Bob": 50, "Cid": 30, "Eve": 20}})
    a = _by_name(compute(df, GradeSettings()))
    b = _by_name(compute(df, ROUGH))
    assert {k: v.individual_score for k, v in a.items()} == \
        {k: v.individual_score for k, v in b.items()}


# --------------------------------------------------------------------------- #
# Overrides
# --------------------------------------------------------------------------- #
def test_an_override_replaces_the_adjustment_and_the_points():
    df = team({"Ann": "fine", "Bob": VAGUE, "Cid": GOOD})
    teams = compute(df, GradeSettings())
    before = _by_name(teams)
    out = _by_name(apply_overrides(teams, {"bob": {"adjust": -10},
                                           "cid": {"points": 3}}))
    assert out["Bob"].signed_pct == -10
    assert out["Bob"].individual_score == 90
    assert out["Bob"].adjustment_overridden and not out["Bob"].points_overridden
    assert out["Bob"].computed_signed_pct == before["Bob"].signed_pct
    assert out["Cid"].response_points == 3 and out["Cid"].points_overridden
    assert out["Cid"].computed_response_points == before["Cid"].response_points
    # Untouched students, and the computed results themselves, are unchanged.
    assert out["Ann"].individual_score == before["Ann"].individual_score
    assert before["Bob"].adjustment_overridden is False
    frame = results_to_frame(apply_overrides(teams, {"bob": {"adjust": -10}}))
    assert dict(zip(frame["Name"], frame["Overridden"]))["Bob"] == "adjustment"


def test_an_override_is_not_held_to_the_caps():
    df = team({"Ann": GOOD, "Bob": GOOD})
    teams = compute(df, GradeSettings())          # no bonus by default
    out = _by_name(apply_overrides(teams, {"ann": {"adjust": 5}}))
    assert out["Ann"].individual_score == 105


def test_overrides_for_students_no_longer_there_are_ignored():
    df = team({"Ann": GOOD, "Bob": GOOD})
    teams = compute(df, GradeSettings())
    out = apply_overrides(teams, {"zed": {"adjust": -50}, "ann": {}})
    assert [m.individual_score for t in out for m in t.members] == \
        [m.individual_score for t in teams for m in t.members]


# --------------------------------------------------------------------------- #
# Each evaluation keeps its own rules
# --------------------------------------------------------------------------- #
@pytest.fixture()
def vault(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FERNET_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("VAULT_BACKEND", "local")
    monkeypatch.setenv("VAULT_FOLDER", "rules")
    vmod.clear_cache()
    yield Vault()
    vmod.clear_cache()


def test_grading_rules_are_stored_per_evaluation(vault):
    import dataclasses
    a, b = survey.slugify("MGT 301", "1"), survey.slugify("MGT 301", "2")
    survey.save_grading(vault, a, dataclasses.asdict(ROUGH),
                        {"ann": {"adjust": -10, "points": None}})
    got_a, got_b = survey.load_grading(vault, a), survey.load_grading(vault, b)
    assert settings_from(got_a["settings"]).response_scoring == "rough"
    assert got_a["overrides"] == {"ann": {"adjust": -10}}
    assert got_b == {"settings": {}, "overrides": {}}
    assert settings_from(got_b["settings"]) == GradeSettings()


def test_stored_rules_from_another_version_still_load():
    assert settings_from({"sensitivity_B": 0.7, "retired_field": 1}) == \
        GradeSettings(sensitivity_B=0.7)
    assert settings_from(None) == GradeSettings()


def test_a_new_round_takes_the_rules_but_not_the_overrides(vault):
    import dataclasses
    rows = [{"First Name": f, "Last Name": "Lee", "Email": f"{f}@x.edu",
             "Team": "1"} for f in ("Ann", "Bob", "Cid")]
    s1, _ = survey.save_setup("MGT 301", "1", pd.DataFrame(rows),
                              dict(survey.DEFAULT_SURVEY), vault=vault)
    survey.save_grading(vault, s1, dataclasses.asdict(ROUGH),
                        {"ann lee": {"adjust": -10}})
    s2, _ = survey.start_round_from(vault, s1, "MGT 301", "2")
    got = survey.load_grading(vault, s2)
    assert settings_from(got["settings"]).response_scoring == "rough"
    assert got["overrides"] == {}


def test_deleting_an_evaluation_takes_its_rules_with_it(vault):
    import dataclasses
    rows = [{"First Name": f, "Last Name": "Lee", "Email": f"{f}@x.edu",
             "Team": "1"} for f in ("Ann", "Bob")]
    s1, _ = survey.save_setup("MGT 301", "1", pd.DataFrame(rows),
                              dict(survey.DEFAULT_SURVEY), vault=vault)
    survey.save_grading(vault, s1, dataclasses.asdict(ROUGH), {})
    assert survey.key_grading(s1) in survey.survey_objects(vault, s1)


def test_the_page(tmp_path):
    """Rules per evaluation, and overrides on a student's report, in the real
    app: tests/_grading_rules_app.py."""
    done = subprocess.run(
        [sys.executable, os.path.join(REPO, "tests", "_grading_rules_app.py"),
         str(tmp_path / "gr")], capture_output=True, text=True, timeout=900,
        cwd=REPO)
    assert "FAILURES: none" in done.stdout, done.stdout[-5000:]
    assert done.returncode == 0, done.stdout[-5000:]
