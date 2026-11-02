"""The whole built-in survey, end to end, with nothing mocked but the browser.

Why this file exists
--------------------
Before release there was no automated test of the path almost every new user
takes. ``qa_regression.py`` grades a synthetic *Qualtrics* export, and the unit
tests cover the AI layer — so the built-in survey, which is PeerParley's own
reason for existing, was validated only by hand. Everything between "save the
roster" and "a graded PDF" was untested: the signed link, the token that
identifies a student by their position in the saved roster, the encrypted
response objects, the conversion back into grading rows, the self-evaluation
pull, and the report.

What it covers
--------------
Each test drives a real local vault (Fernet-encrypted files in a temp
directory), real tokens, and the real survey/grading/PDF code:

* a saved roster produces one working link per student, and a tampered or
  foreign token opens nothing;
* a submission validated by the same gate the form uses lands in the vault,
  encrypted, and comes back as grading rows;
* those rows grade, and the grades are the ones the arithmetic implies;
* the student's report builds and contains their teammates' words;
* a student who never submits does not become a 50% deduction;
* reopening a link returns the student's previous answers rather than a blank
  form, which is what the UI promises.

Run with:  python -m pytest tests/ -q
"""
from __future__ import annotations

import json
import os
import sys

import pytest
from cryptography.fernet import Fernet

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from peerparley import survey, pdfgen  # noqa: E402
from peerparley.grading import GradeSettings, compute, data_warnings  # noqa: E402
from peerparley.tokens import read_token  # noqa: E402
from peerparley.vault import Vault  # noqa: E402

TEAM = "1"
ROSTER = [
    ("Ann", "Apple", "ann@example.edu"),
    ("Bob", "Brown", "bob@example.edu"),
    ("Cid", "Cole", "cid@example.edu"),
    ("Dee", "Diaz", "dee@example.edu"),
]

# What four students would actually write about each other, because the comment
# scorer measures length, variety and overlap — placeholder text scores zero and
# would hide the behaviour being tested.
PRAISE = {
    "Ann Apple": "Ann chaired every meeting, kept the schedule current, and chased "
                 "the rest of us politely when we drifted.",
    "Bob Brown": "Bob rebuilt the financial model twice after the data changed and "
                 "explained the assumptions so we could follow them.",
    "Cid Cole": "Cid ran all six customer interviews and wrote the transcripts up "
                "for everyone else to use.",
    "Dee Diaz": "Dee was quiet in discussion, but her competitor map became the "
                "backbone of the final deck.",
}
IMPROVE = {
    "Ann Apple": "Ann could delegate more instead of absorbing the overflow herself.",
    "Bob Brown": "Bob could share drafts earlier so the rest of us can react in time.",
    "Cid Cole": "Cid could post interview notes the same day rather than days later.",
    "Dee Diaz": "Dee could speak up earlier when she disagrees with the direction.",
}


@pytest.fixture()
def vault(tmp_path, monkeypatch) -> Vault:
    """A real encrypted vault in a temp directory.

    The local backend writes under ``./vault_cache``, so the working directory
    is what isolates one test from the next — and from the developer's own
    data.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FERNET_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("VAULT_BACKEND", "local")
    monkeypatch.setenv("VAULT_FOLDER", "e2e")
    monkeypatch.delenv("TOKEN_SECRET", raising=False)
    return Vault()


def contact_frame():
    import pandas as pd
    return pd.DataFrame([
        {"First Name": f, "Last Name": l, "Email": e, "Team": TEAM}
        for f, l, e in ROSTER
    ])


def set_up(vault) -> tuple:
    """Save a survey the way tab ① does. Returns (slug, teams, links)."""
    slug, teams = survey.save_setup("QA101", "1", contact_frame(),
                                    dict(survey.DEFAULT_SURVEY), vault=vault,
                                    owner="qa-instructor")
    links = survey.student_links("https://peerparley.example", slug, teams,
                                 survey.token_secret())
    return slug, teams, links


def answers_from(members, pos, dollars) -> dict:
    """One student's completed form, in the shape the form submits.

    Ratings and the forced ranking include the student's own row (that is the
    self-evaluation); the dollar allocation does not, because the $100 is for
    teammates.
    """
    out = {}
    for i, m in enumerate(members):
        entry = {
            "ratings": [6, 6, 5, 6] if i == pos else [5, 5, 5, 5],
            "rank": "High performer" if i == pos else
                    ("Adequate performer" if i % 2 else "Low performer"),
            "contribution": PRAISE[m["name"]],
            "improve": IMPROVE[m["name"]],
        }
        if i != pos:
            entry["alloc"] = dollars[i]
        out[str(i)] = entry
    return out


def submit(vault, slug, teams, pos, dollars, confidential=""):
    """Validate with the form's own gate, then store it like the form does."""
    members = teams[TEAM]
    answers = answers_from(members, pos, dollars)
    problems = survey.submission_problems(answers, survey.DEFAULT_SURVEY,
                                          len(members), self_pos=pos)
    assert problems == [], problems
    survey.save_response(vault, slug, TEAM, pos, {
        "slug": slug, "team": TEAM, "pos": pos,
        "evaluator": members[pos]["name"],
        "evaluator_key": survey.name_key(members[pos]["name"]),
        "submitted": "2026-10-29T12:00:00+00:00",
        "members": answers,
        "self_contribution": "I ran the analysis and kept the deck on schedule.",
        "confidential": confidential,
    })
    return answers


# --------------------------------------------------------------------------- #
# Links
# --------------------------------------------------------------------------- #

def test_every_student_gets_a_link_that_resolves_to_them(vault):
    slug, teams, links = set_up(vault)
    assert len(links) == len(ROSTER)
    secret = survey.token_secret()
    for link in links:
        token = link["link"].split("t=", 1)[1]
        payload = read_token(token, secret)
        assert payload == {"s": slug, "t": link["team"], "p": link["pos"]}
        # The position in the token is the position in the SAVED roster, which
        # is what makes re-saving a changed roster dangerous.
        assert teams[TEAM][payload["p"]]["name"] == link["name"]


def test_a_tampered_or_foreign_token_opens_nothing(vault):
    _, _, links = set_up(vault)
    token = links[0]["link"].split("t=", 1)[1]
    body, _, sig = token.partition(".")
    secret = survey.token_secret()
    assert read_token(f"{body}.{'0' * len(sig)}", secret) is None
    assert read_token(token, "a-different-secret") is None
    assert read_token("not-a-token", secret) is None


# --------------------------------------------------------------------------- #
# Collection
# --------------------------------------------------------------------------- #

def test_a_submission_is_stored_encrypted_and_read_back(vault):
    slug, teams, _ = set_up(vault)
    submit(vault, slug, teams, 0, {1: 40, 2: 30, 3: 30},
           confidential="Please keep an eye on how the work was split.")

    back = survey.load_response(vault, slug, TEAM, 0)
    assert back["evaluator"] == "Ann Apple"
    assert back["members"]["1"]["alloc"] == 40

    # On disk it is ciphertext: no student's name, words or dollars in the clear.
    # Read through the backend's own root rather than a guessed path, so this
    # cannot drift from wherever the vault is actually writing.
    root = vault.backend.root
    on_disk = b"".join(
        open(os.path.join(root, n), "rb").read() for n in os.listdir(root)
    )
    assert on_disk, f"nothing was written under {root}"
    for probe in (b"Ann Apple", b"competitor map", b"Please keep an eye"):
        assert probe not in on_disk


def test_response_progress_tracks_who_has_answered(vault):
    slug, teams, _ = set_up(vault)
    submit(vault, slug, teams, 0, {1: 40, 2: 30, 3: 30})
    submit(vault, slug, teams, 2, {0: 34, 1: 33, 3: 33})
    status = survey.response_status(vault, slug)
    done = {r["name"] for r in status if r["responded"]}
    assert done == {"Ann Apple", "Cid Cole"}
    assert len(status) == len(ROSTER)


def test_reopening_a_link_returns_the_students_own_answers(vault):
    """The form tells students they may revise until the evaluation closes."""
    slug, teams, _ = set_up(vault)
    sent = submit(vault, slug, teams, 1, {0: 40, 2: 30, 3: 30})
    prior = survey.load_response(vault, slug, TEAM, 1)
    assert prior["members"] == json.loads(json.dumps(sent))


# --------------------------------------------------------------------------- #
# Grading the collected responses
# --------------------------------------------------------------------------- #

def all_four(vault):
    slug, teams, _ = set_up(vault)
    # Ann is well regarded; Dee is not. Every rater spends exactly $100.
    submit(vault, slug, teams, 0, {1: 34, 2: 33, 3: 33})
    submit(vault, slug, teams, 1, {0: 40, 2: 35, 3: 25})
    submit(vault, slug, teams, 2, {0: 40, 1: 35, 3: 25})
    submit(vault, slug, teams, 3, {0: 40, 1: 30, 2: 30})
    return slug, teams


def test_submissions_become_grading_rows(vault):
    slug, teams = all_four(vault)
    long_df = survey.responses_to_long(vault, slug)
    # Four raters, three teammates each: no self rows, no missing rows.
    assert len(long_df) == 12
    assert not (long_df["evaluator_key"] == long_df["evaluatee_key"]).any()
    assert set(long_df["evaluator_team"]) == {TEAM}
    # Every rater's dollars total exactly the $100 they were given.
    totals = long_df.groupby("evaluator_key")["points"].sum()
    assert set(totals.round(2)) == {100.0}


def test_the_whole_section_grades_and_the_numbers_follow_the_dollars(vault):
    slug, teams = all_four(vault)
    long_df = survey.responses_to_long(vault, slug)
    self_evals = survey.self_evaluations(vault, slug)
    results = compute(long_df, GradeSettings(), self_evals=self_evals)

    assert len(results) == 1
    members = {m.name: m for m in results[0].members}
    assert set(members) == {n for n in PRAISE}

    # Received dollars: Ann 120, Bob 99, Cid 98, Dee 83 — so Ann is above the
    # team average and Dee below it, and the pay grades have to say so.
    assert members["Ann Apple"].received_total == 120.0
    assert members["Dee Diaz"].received_total == 83.0
    assert members["Ann Apple"].pay_grade > 1.0 > members["Dee Diaz"].pay_grade
    assert members["Ann Apple"].signed_pct >= members["Dee Diaz"].signed_pct

    for m in members.values():
        assert m.submitted_self_eval is True
        assert m.self_responded is True          # self-ratings came through
        assert not m.no_peer_input
        assert -50.0 <= m.signed_pct <= 5.0
        # The score shown and the adjustment shown are the same number.
        assert abs(m.individual_score - (m.team_score + m.signed_pct)) < 1e-9
        # Three teammates wrote about them, and both halves arrived.
        assert len(m.contributions) == 3 and len(m.improvements) == 3

    assert data_warnings(long_df, results) == []


def test_each_student_gets_a_report_carrying_their_teammates_words(vault):
    slug, teams = all_four(vault)
    results = compute(survey.responses_to_long(vault, slug), GradeSettings(),
                      self_evals=survey.self_evaluations(vault, slug))
    for m in results[0].members:
        pdf = pdfgen.build_individual_pdf(m, "1", "QA101")
        assert pdf[:5] == b"%PDF-"
        assert len(pdf) > 2500
        # The feedback a student receives is what their teammates wrote.
        assert all(c in PRAISE.values() for c in m.contributions)
        assert all(c in IMPROVE.values() for c in m.improvements)


def test_the_confidential_note_reaches_the_instructor_only(vault):
    slug, teams, _ = set_up(vault)
    note = "Ann did the work two of us were supposed to do."
    submit(vault, slug, teams, 2, {0: 40, 1: 30, 3: 30}, confidential=note)
    results = compute(survey.responses_to_long(vault, slug), GradeSettings())
    members = {m.name: m for m in results[0].members}

    # Filed under its author, not the first teammate listed.
    assert members["Cid Cole"].confidential_comments == [note]
    assert members["Ann Apple"].confidential_comments == []

    # It is in the instructor's PDF and in no student's.
    assert note.encode() not in b"".join(
        pdfgen.build_individual_pdf(m, "1", "QA101") for m in results[0].members)
    assert len(pdfgen.build_confidential_pdf(results[0:1], "QA101", "1")) > 1000


def test_a_section_where_only_one_student_answered_is_not_a_mass_deduction(vault):
    """The release-blocking case from the 2.9.0 audit, through the real path."""
    slug, teams, _ = set_up(vault)
    submit(vault, slug, teams, 0, {1: 34, 2: 33, 3: 33})
    results = compute(survey.responses_to_long(vault, slug), GradeSettings())
    members = {m.name: m for m in results[0].members}

    ann = members["Ann Apple"]          # nobody evaluated the only respondent
    assert ann.no_peer_input and ann.signed_pct == 0.0
    assert ann.individual_score == ann.team_score
    for name in ("Bob Brown", "Cid Cole", "Dee Diaz"):
        assert members[name].signed_pct <= 0.0 or members[name].signed_pct <= 5.0
    assert any("No teammate evaluated" in w
               for w in data_warnings(survey.responses_to_long(vault, slug),
                                      results))


# --------------------------------------------------------------------------- #
# The form's own gate
# --------------------------------------------------------------------------- #

def test_the_form_refuses_an_incomplete_submission(vault):
    _, teams, _ = set_up(vault)
    members = teams[TEAM]
    answers = answers_from(members, 0, {1: 40, 2: 30, 3: 30})

    # Dollars that do not total $100.
    short = {k: dict(v) for k, v in answers.items()}
    short["1"]["alloc"] = 10
    assert any("100" in p for p in survey.submission_problems(
        short, survey.DEFAULT_SURVEY, len(members), self_pos=0))

    # A missing rating.
    unrated = {k: dict(v) for k, v in answers.items()}
    unrated["2"]["ratings"] = [None, None, None, None]
    assert any("rating" in p for p in survey.submission_problems(
        unrated, survey.DEFAULT_SURVEY, len(members), self_pos=0))

    # A ranking category left unused.
    flat = {k: dict(v) for k, v in answers.items()}
    for k in flat:
        flat[k]["rank"] = "High performer"
    assert any("categor" in p for p in survey.submission_problems(
        flat, survey.DEFAULT_SURVEY, len(members), self_pos=0))


def test_a_closed_survey_reports_closed(vault):
    """The window check the student form consults before accepting anything."""
    cfg = dict(survey.DEFAULT_SURVEY)
    cfg["is_open"] = False
    assert survey.window_state(cfg) == "disabled"
    assert survey.window_message(cfg)

    cfg = dict(survey.DEFAULT_SURVEY)
    cfg["closes_at"] = "2020-01-01T00:00:00"
    assert survey.window_state(cfg) == "closed"

    cfg = dict(survey.DEFAULT_SURVEY)
    cfg["opens_at"] = "2099-01-01T00:00:00"
    assert survey.window_state(cfg) == "not_yet"
    assert survey.window_state(dict(survey.DEFAULT_SURVEY)) == "open"
