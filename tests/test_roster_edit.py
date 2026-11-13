"""Editing a saved roster: permanent ids, and what an edit is allowed to touch.

Asked for: *"I would like to add the capability to easily add, move, and remove
students from peer evaluation teams in this application."*

Until 2.16.0 that was the one thing an instructor was warned off. A student was
identified by their **position** — their index in their team's list in the
stored snapshot — which is what the link token carried and what the answers
inside a submission referred to. Adding a student named "Adams" to a team
renumbered everyone after them: the links already in students' inboxes pointed
at the wrong people and the stored answers described the wrong people. Nothing
errored; the grades were just wrong, and `roster_shift` existed to say so.

Every student now carries a permanent id, so positions are presentational.
These tests hold the three things that makes true:

* **ids do not churn** — not when the contact list is uploaded again, not when
  a name is corrected, and not when anyone is added, moved or removed;
* **an edit disturbs only the answers that stopped describing anything** — a
  move clears what the mover wrote about the team they left and what that team
  wrote about them, and nothing else, anywhere;
* **a survey saved under the old scheme still grades identically**, its links
  included, because migration rewrites the answers rather than reinterpreting
  them.

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
from peerparley.grading import (  # noqa: E402
    GradeSettings,
    compute,
    results_to_frame,
)
from peerparley.tokens import make_token, read_token  # noqa: E402
from peerparley.vault import Vault  # noqa: E402

NAMES = {"1": ["Ann Apple", "Bob Brown", "Cid Cole"],
         "2": ["Dee Diaz", "Eve Ellis", "Finn Ford"]}
TEXT = "{who} ran the customer interviews and wrote up every transcript."


@pytest.fixture()
def vault(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FERNET_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("VAULT_BACKEND", "local")
    monkeypatch.setenv("VAULT_FOLDER", "roster-edit")
    monkeypatch.delenv("TOKEN_SECRET", raising=False)
    return Vault()


def contacts(names=None, email_of=None) -> pd.DataFrame:
    names = names or NAMES
    return pd.DataFrame([
        {"First Name": n.split()[0], "Last Name": n.split()[1],
         "Email": (email_of or (lambda x: f"{x.split()[0].lower()}@x.edu"))(n),
         "Team": t}
        for t, ns in names.items() for n in ns])


def set_up(vault, names=None):
    slug, teams = survey.save_setup("RE101", "1", contacts(names),
                                    dict(survey.DEFAULT_SURVEY),
                                    vault=vault, owner="admin")
    return slug, teams


def submit_everyone(vault, slug, alloc=50):
    """Every student evaluates every teammate, so edits have consequences."""
    snap = survey.roster_now(vault, slug)
    for team, members in snap["teams"].items():
        for pos, me in enumerate(members):
            answers = {}
            for i, m in enumerate(members):
                entry = {"ratings": [5, 6, 5, 6],
                         "rank": ["High performer", "Adequate performer",
                                  "Low performer"][i % 3],
                         "contribution": TEXT.format(who=m["name"]),
                         "improve": ""}
                if i != pos:
                    entry["alloc"] = alloc
                answers[str(m["id"])] = entry
            survey.save_response_by_id(vault, slug, str(me["id"]), {
                "slug": slug, "team": team, "pos": pos,
                "evaluator": me["name"],
                "evaluator_key": survey.name_key(me["name"]),
                "submitted": "2026-11-13T12:00:00+00:00",
                "members": answers,
                "self_contribution": "I led the analysis.", "confidential": ""})


def ids_of(vault, slug):
    snap = survey.roster_now(vault, slug)
    return {m["name"]: str(m["id"])
            for _t, _p, m in survey.snapshot_members(snap)}


def links_of(vault, slug):
    snap = survey.roster_now(vault, slug)
    return {l["name"]: l["link"] for l in survey.student_links(
        "https://pp.example", slug, snap["teams"], survey.token_secret())}


def graded_frame(vault, slug):
    return results_to_frame(compute(
        survey.responses_to_long(vault, slug), GradeSettings(),
        self_evals=survey.self_evaluations(vault, slug)))


# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #
def test_every_student_gets_an_id(vault):
    slug, _ = set_up(vault)
    snap = survey.roster_now(vault, slug)
    got = [str(m.get("id", "")) for _t, _p, m in survey.snapshot_members(snap)]
    assert all(got), "a student with no id cannot be addressed safely"
    assert len(set(got)) == len(got), "ids must be unique within a survey"


def test_re_uploading_the_same_contact_list_does_not_mint_new_students(vault):
    slug, _ = set_up(vault)
    before = ids_of(vault, slug)
    survey.save_setup("RE101", "1", contacts(), dict(survey.DEFAULT_SURVEY),
                      vault=vault, owner="admin")
    assert ids_of(vault, slug) == before


def test_correcting_a_misspelt_name_keeps_that_student_their_id(vault):
    """The reason ids are random rather than derived from the name: every
    derivable thing about a student is also a thing an instructor fixes."""
    slug, _ = set_up(vault)
    before = ids_of(vault, slug)
    fixed = {"1": ["Ann Apple", "Bob Browne", "Cid Cole"],
             "2": NAMES["2"]}
    # Same email, corrected surname — matched on the email.
    survey.save_setup("RE101", "1",
                      contacts(fixed, email_of=lambda n: f"{n.split()[0].lower()}@x.edu"),
                      dict(survey.DEFAULT_SURVEY), vault=vault, owner="admin")
    after = ids_of(vault, slug)
    assert after["Bob Browne"] == before["Bob Brown"]
    assert after["Ann Apple"] == before["Ann Apple"]


def test_a_link_carries_the_id_and_resolves_to_that_student(vault):
    slug, _ = set_up(vault)
    snap = survey.roster_now(vault, slug)
    secret = survey.token_secret()
    for link in survey.student_links("https://pp.example", slug,
                                     snap["teams"], secret):
        payload = read_token(link["link"].split("t=", 1)[1], secret)
        assert payload == {"s": slug, "i": link["id"]}
        assert survey.resolve_token(payload, snap) == link["id"]


def test_a_link_issued_before_ids_existed_still_works(vault):
    """Links are already in students' inboxes; they have to keep working."""
    slug, _ = set_up(vault)
    snap = survey.roster_now(vault, slug)
    # Simulate migration having recorded which seat each link was issued for.
    bob = snap["teams"]["1"][1]
    payload = {"s": slug, "t": "1", "p": 1}
    assert survey.resolve_token(payload, snap) == str(bob["id"])


def test_an_old_link_whose_seat_has_been_renumbered_resolves_to_nobody(vault):
    """Never to whoever is standing in that position now. Handing a student
    someone else's form is the failure the id scheme exists to prevent, and the
    compatibility path must not reintroduce it."""
    slug, _ = set_up(vault)
    snap = survey.roster_now(vault, slug)
    for pos, m in enumerate(snap["teams"]["1"]):
        m["issued_pos"] = pos
    survey._save_json(vault, survey.key_roster(slug), snap)
    # "Adams" sorts to the front, so everyone behind him shifts by one and
    # position 1 — the seat Bob's link was issued against — now holds Ann.
    survey.add_student(vault, slug, "Abe Adams", "1", "abe@x.edu")
    snap = survey.roster_now(vault, slug)
    assert [m["name"] for m in snap["teams"]["1"]][:2] == ["Abe Adams",
                                                           "Ann Apple"]
    assert survey.resolve_token({"s": slug, "t": "1", "p": 1}, snap) == "", \
        "Bob's old link must not open Ann's form"


# --------------------------------------------------------------------------- #
# Migrating a survey stored under the old scheme
# --------------------------------------------------------------------------- #
def make_legacy(vault, slug):
    """Rewrite a saved survey as a pre-2.16.0 vault: no ids anywhere."""
    snap = survey.roster_now(vault, slug)
    for team, members in snap["teams"].items():
        for pos, me in enumerate(members):
            answers = {}
            for i, m in enumerate(members):
                entry = {"ratings": [5, 6, 5, 6],
                         "rank": ["High performer", "Adequate performer",
                                  "Low performer"][i % 3],
                         "contribution": TEXT.format(who=m["name"]),
                         "improve": ""}
                if i != pos:
                    entry["alloc"] = 50
                answers[str(i)] = entry          # positional, as it was
            survey._save_json(vault, survey.key_response_legacy(slug, team, pos), {
                "slug": slug, "team": team, "pos": pos,
                "evaluator": me["name"],
                "evaluator_key": survey.name_key(me["name"]),
                "submitted": "2026-11-13T12:00:00+00:00", "members": answers,
                "self_contribution": "I led the analysis.", "confidential": ""})
    stripped = {k: v for k, v in snap.items() if k not in ("schema", "unassigned")}
    stripped["teams"] = {t: [{k: v for k, v in m.items() if k != "id"}
                             for m in ms] for t, ms in snap["teams"].items()}
    survey._save_json(vault, survey.key_roster(slug), stripped)


def test_a_legacy_survey_grades_identically_after_migration(vault):
    slug, _ = set_up(vault)
    make_legacy(vault, slug)
    assert int(survey.load_roster_snapshot(vault, slug).get("schema", 1)) == 1
    before = graded_frame(vault, slug)
    survey.roster_now(vault, slug)              # migrates
    after = graded_frame(vault, slug)
    assert before.equals(after), before.compare(after).to_string()


def test_migration_assigns_ids_and_is_idempotent(vault):
    slug, _ = set_up(vault)
    make_legacy(vault, slug)
    snap = survey.roster_now(vault, slug)
    assert snap["schema"] == survey.ROSTER_SCHEMA
    first = ids_of(vault, slug)
    assert all(first.values())
    assert survey.roster_now(vault, slug)["schema"] == survey.ROSTER_SCHEMA
    assert ids_of(vault, slug) == first, "a second pass must change nothing"


def test_migration_does_not_leave_every_answer_counted_twice(vault):
    """Both naming schemes share the `resp__<slug>__` prefix `all_responses`
    lists, so a migrated submission left in both places was read twice — which
    silently doubled the ratings every student appeared to have received."""
    slug, _ = set_up(vault)
    make_legacy(vault, slug)
    survey.roster_now(vault, slug)
    teams = compute(survey.responses_to_long(vault, slug), GradeSettings())
    for t in teams:
        for m in t.members:
            assert len(m.received_breakdown) == len(t.members) - 1, (
                f"{m.name} was rated {len(m.received_breakdown)} times by "
                f"{len(t.members) - 1} teammates")
    assert survey.response_count(vault, slug) == 6


def test_migration_carries_an_old_link_onto_the_students_id(vault):
    slug, _ = set_up(vault)
    make_legacy(vault, slug)
    snap = survey.roster_now(vault, slug)
    secret = survey.token_secret()
    token = make_token({"s": slug, "t": "1", "p": 2}, secret)
    resolved = survey.resolve_token(read_token(token, secret), snap)
    assert survey.member_by_id(snap, resolved)["name"] == "Cid Cole"


# --------------------------------------------------------------------------- #
# Moving a student
# --------------------------------------------------------------------------- #
def test_moving_a_student_says_what_it_would_discard_before_doing_it(vault):
    slug, _ = set_up(vault)
    submit_everyone(vault, slug)
    cid = ids_of(vault, slug)["Cid Cole"]
    impact = survey.edit_impact(vault, slug, "move", cid, team="2")
    assert not impact["safe"] and impact["responses"] == 3
    joined = " ".join(impact["drops"])
    assert "Cid Cole" in joined
    assert "wrote about their 2 teammate(s)" in joined
    assert "What 2 teammate(s) wrote about" in joined
    # Nothing has happened yet.
    assert survey.member_by_id(survey.roster_now(vault, slug), cid)["team"] == "1"


def test_moving_a_student_before_anyone_submits_costs_nothing(vault):
    slug, _ = set_up(vault)
    cid = ids_of(vault, slug)["Cid Cole"]
    impact = survey.edit_impact(vault, slug, "move", cid, team="2")
    assert impact["safe"] and impact["responses"] == 0


def test_a_move_leaves_every_other_students_link_alone(vault):
    slug, _ = set_up(vault)
    submit_everyone(vault, slug)
    before = links_of(vault, slug)
    cid = ids_of(vault, slug)["Cid Cole"]
    assert survey.move_student(vault, slug, cid, "2")["ok"]
    after = links_of(vault, slug)
    assert after == before, "a move must not reissue anybody's link, not even the mover's"


def test_a_move_clears_only_the_answers_that_stopped_applying(vault):
    slug, _ = set_up(vault)
    submit_everyone(vault, slug)
    ids = ids_of(vault, slug)
    survey.move_student(vault, slug, ids["Cid Cole"], "2")

    ann = survey.load_response_by_id(vault, slug, ids["Ann Apple"])
    assert ids["Cid Cole"] not in {str(k) for k in ann["members"]}
    assert ids["Bob Brown"] in {str(k) for k in ann["members"]}, \
        "Ann's answers about Bob have nothing to do with Cid's move"
    assert ids["Ann Apple"] in {str(k) for k in ann["members"]}, \
        "and neither has her own self-evaluation"

    mine = survey.load_response_by_id(vault, slug, ids["Cid Cole"])
    assert {str(k) for k in mine["members"]} == {ids["Cid Cole"]}, \
        "the mover keeps their self-evaluation and nothing else"
    assert mine["team"] == "2"

    # Team 2's own answers are untouched.
    dee = survey.load_response_by_id(vault, slug, ids["Dee Diaz"])
    assert {str(k) for k in dee["members"]} == {
        ids["Dee Diaz"], ids["Eve Ellis"], ids["Finn Ford"]}


def test_a_moved_student_is_listed_as_no_data_rather_than_disappearing(vault):
    """Their new teammates have not written about them and the answers they
    wrote were cleared with the move, so not one row of the long frame mentions
    them — which used to drop them out of the results entirely."""
    slug, _ = set_up(vault)
    submit_everyone(vault, slug)
    survey.move_student(vault, slug, ids_of(vault, slug)["Cid Cole"], "2")
    frame = graded_frame(vault, slug)
    assert len(frame) == 6
    row = frame[frame["Name"] == "Cid Cole"]
    assert len(row) == 1
    assert row.iloc[0]["Performance"] == "No data"
    assert row.iloc[0]["Grade Δ"] == "+0%", "no data is not a deduction"
    assert row.iloc[0]["Team"] == "2"


def test_a_presence_row_does_not_cost_the_student_feedback_points(vault):
    """The row that keeps them visible must contribute nothing else — above
    all not a phantom teammate to the denominator of Response Pts."""
    slug, _ = set_up(vault)
    submit_everyone(vault, slug)
    ids = ids_of(vault, slug)
    before = {m.name: m.response_points
              for t in compute(survey.responses_to_long(vault, slug),
                               GradeSettings()) for m in t.members}
    survey.move_student(vault, slug, ids["Cid Cole"], "2")
    after = {m.name: m.response_points
             for t in compute(survey.responses_to_long(vault, slug),
                              GradeSettings()) for m in t.members}
    for name in ("Dee Diaz", "Eve Ellis", "Finn Ford"):
        assert after[name] == before[name], f"{name} is on another team entirely"


def test_moving_a_student_to_no_team_is_allowed(vault):
    slug, _ = set_up(vault)
    cid = ids_of(vault, slug)["Cid Cole"]
    assert survey.move_student(vault, slug, cid, "")["ok"]
    snap = survey.roster_now(vault, slug)
    assert survey.member_by_id(snap, cid)["team"] == ""
    assert "Cid Cole" in [m["name"] for m in snap["unassigned"]]


def test_moving_a_student_to_the_team_they_are_on_is_refused(vault):
    slug, _ = set_up(vault)
    cid = ids_of(vault, slug)["Cid Cole"]
    out = survey.move_student(vault, slug, cid, "1")
    assert not out["ok"] and "already there" in out["detail"]


# --------------------------------------------------------------------------- #
# Adding a student
# --------------------------------------------------------------------------- #
def test_adding_a_student_gives_them_a_link_and_moves_nobody_elses(vault):
    slug, _ = set_up(vault)
    submit_everyone(vault, slug)
    before = links_of(vault, slug)
    out = survey.add_student(vault, slug, "Abe Adams", "1", "abe@x.edu")
    assert out["ok"]
    after = links_of(vault, slug)
    assert "Abe Adams" in after
    assert all(after[n] == before[n] for n in before), (
        "'Adams' sorts to the front of team 1 — under the positional scheme "
        "that renumbered everyone behind him")


def test_a_newly_added_student_grades_as_no_data(vault):
    slug, _ = set_up(vault)
    submit_everyone(vault, slug)
    survey.add_student(vault, slug, "Abe Adams", "1", "abe@x.edu")
    row = graded_frame(vault, slug)
    abe = row[row["Name"] == "Abe Adams"]
    assert len(abe) == 1 and abe.iloc[0]["Performance"] == "No data"


def test_adding_a_student_who_is_already_there_is_refused(vault):
    slug, _ = set_up(vault)
    out = survey.add_student(vault, slug, "Ann Apple", "2", "ann@x.edu")
    assert not out["ok"] and "already on this roster" in out["detail"]


def test_a_student_with_no_email_is_added_and_the_consequence_is_stated(vault):
    slug, _ = set_up(vault)
    out = survey.add_student(vault, slug, "Abe Adams", "1")
    assert out["ok"] and "no email" in out["detail"]


def test_adding_a_student_needs_a_name(vault):
    slug, _ = set_up(vault)
    assert not survey.add_student(vault, slug, "   ", "1")["ok"]


# --------------------------------------------------------------------------- #
# Removing a student
# --------------------------------------------------------------------------- #
def test_removing_a_student_deletes_their_submission_and_every_reference(vault):
    slug, _ = set_up(vault)
    submit_everyone(vault, slug)
    ids = ids_of(vault, slug)
    bob = ids["Bob Brown"]
    impact = survey.edit_impact(vault, slug, "remove", bob)
    assert "own submission is deleted" in " ".join(impact["drops"])

    assert survey.remove_student(vault, slug, bob)["ok"]
    assert survey.member_by_id(survey.roster_now(vault, slug), bob) is None
    assert survey.load_response_by_id(vault, slug, bob) is None
    ann = survey.load_response_by_id(vault, slug, ids["Ann Apple"])
    assert bob not in {str(k) for k in ann["members"]}
    assert ids["Cid Cole"] in {str(k) for k in ann["members"]}
    assert "Bob Brown" not in set(graded_frame(vault, slug)["Name"])


def test_removing_a_student_leaves_other_links_alone(vault):
    slug, _ = set_up(vault)
    before = links_of(vault, slug)
    survey.remove_student(vault, slug, ids_of(vault, slug)["Ann Apple"])
    after = links_of(vault, slug)
    assert all(after[n] == before[n] for n in after)


# --------------------------------------------------------------------------- #
# Teams
# --------------------------------------------------------------------------- #
def test_a_new_team_survives_long_enough_to_be_filled(vault):
    """The first version of this dissolved any team below two members, so
    moving the first student into a new team removed the team under you."""
    slug, _ = set_up(vault)
    assert survey.create_team(vault, slug, "3")["ok"]
    ids = ids_of(vault, slug)
    assert survey.move_student(vault, slug, ids["Cid Cole"], "3")["ok"]
    snap = survey.roster_now(vault, slug)
    assert "3" in snap["teams"], "the team must still be there for the second student"
    assert survey.undersized_teams(snap) == {"3": 1}
    assert survey.move_student(vault, slug, ids["Finn Ford"], "3")["ok"]
    snap = survey.roster_now(vault, slug)
    assert [m["name"] for m in snap["teams"]["3"]] == ["Cid Cole", "Finn Ford"]
    assert survey.undersized_teams(snap) == {}


def test_a_team_that_cannot_grade_yet_is_named_rather_than_dissolved(vault):
    slug, _ = set_up(vault)
    ids = ids_of(vault, slug)
    survey.move_student(vault, slug, ids["Bob Brown"], "2")
    survey.move_student(vault, slug, ids["Cid Cole"], "2")
    snap = survey.roster_now(vault, slug)
    assert survey.undersized_teams(snap) == {"1": 1}
    assert [m["name"] for m in snap["teams"]["1"]] == ["Ann Apple"]


def test_renaming_a_team_keeps_links_and_relabels_its_submissions(vault):
    slug, _ = set_up(vault)
    submit_everyone(vault, slug)
    before = links_of(vault, slug)
    assert survey.rename_team(vault, slug, "1", "Alpha")["ok"]
    assert links_of(vault, slug) == before
    snap = survey.roster_now(vault, slug)
    assert "Alpha" in snap["teams"] and "1" not in snap["teams"]
    ann = survey.load_response_by_id(vault, slug, ids_of(vault, slug)["Ann Apple"])
    assert ann["team"] == "Alpha", "or the grader files it under a team that is gone"
    frame = graded_frame(vault, slug)
    assert set(frame[frame["Name"] == "Ann Apple"]["Team"]) == {"Alpha"}


def test_renaming_onto_an_existing_team_is_refused_rather_than_merging(vault):
    slug, _ = set_up(vault)
    out = survey.rename_team(vault, slug, "1", "2")
    assert not out["ok"] and "already exists" in out["detail"]


def test_dissolving_a_team_keeps_its_students_and_their_submissions(vault):
    slug, _ = set_up(vault)
    submit_everyone(vault, slug)
    ids = ids_of(vault, slug)
    assert survey.delete_team(vault, slug, "1")["ok"]
    snap = survey.roster_now(vault, slug)
    assert "1" not in snap["teams"]
    assert {m["name"] for m in snap["unassigned"]} == {
        "Ann Apple", "Bob Brown", "Cid Cole"}
    assert survey.load_response_by_id(vault, slug, ids["Ann Apple"]) is not None
    # And they grade again as soon as they are on a team.
    survey.create_team(vault, slug, "9")
    survey.move_student(vault, slug, ids["Ann Apple"], "9")
    survey.move_student(vault, slug, ids["Bob Brown"], "9")
    assert "Ann Apple" in set(graded_frame(vault, slug)["Name"])


def test_deleting_a_team_that_does_not_exist_is_refused(vault):
    slug, _ = set_up(vault)
    assert not survey.delete_team(vault, slug, "nope")["ok"]
    assert not survey.rename_team(vault, slug, "nope", "x")["ok"]


# --------------------------------------------------------------------------- #
# Students with no team
# --------------------------------------------------------------------------- #
def test_a_student_with_no_team_stays_on_the_roster(vault):
    """They used to be dropped silently, so an instructor fixing a blank Team
    cell had no way to see who needed one."""
    rows = contacts()
    rows.loc[len(rows)] = {"First Name": "Lone", "Last Name": "Ranger",
                           "Email": "lone@x.edu", "Team": ""}
    slug, _ = survey.save_setup("RE101", "1", rows,
                                dict(survey.DEFAULT_SURVEY), vault=vault,
                                owner="admin")
    snap = survey.roster_now(vault, slug)
    assert "Lone Ranger" in [m["name"] for m in snap["unassigned"]]
    assert survey.find_member(snap, "Lone Ranger")["id"]


def test_a_team_of_one_in_the_upload_goes_to_the_no_team_list(vault):
    names = dict(NAMES)
    names["9"] = ["Solo Singleton"]
    slug, _ = set_up(vault, names)
    snap = survey.roster_now(vault, slug)
    assert "9" not in snap["teams"]
    assert "Solo Singleton" in [m["name"] for m in snap["unassigned"]]


def test_an_unassigned_student_is_not_counted_as_not_having_replied(vault):
    slug, _ = set_up(vault)
    survey.move_student(vault, slug, ids_of(vault, slug)["Cid Cole"], "")
    rows = survey.response_status(vault, slug)
    cid = [r for r in rows if r["name"] == "Cid Cole"]
    assert len(cid) == 1 and cid[0].get("unassigned") is True


# --------------------------------------------------------------------------- #
# Re-uploading a changed contact list
# --------------------------------------------------------------------------- #
def test_re_uploading_a_changed_list_moves_students_without_renumbering(vault):
    """`roster_shift` used to block this save: re-saving renumbered positions
    and handed answers to a teammate. It is a caution now, not a stop."""
    slug, _ = set_up(vault)
    submit_everyone(vault, slug)
    before_ids, before_links = ids_of(vault, slug), links_of(vault, slug)
    moved = {"1": ["Ann Apple", "Bob Brown"],
             "2": ["Cid Cole", "Dee Diaz", "Eve Ellis", "Finn Ford"]}

    shift = survey.roster_shift(vault, slug, contacts(moved))
    assert not shift["safe"] and shift["responses"]

    survey.save_setup("RE101", "1", contacts(moved),
                      dict(survey.DEFAULT_SURVEY), vault=vault, owner="admin")
    assert ids_of(vault, slug) == before_ids, "ids must survive a re-upload"
    assert links_of(vault, slug) == before_links, "and so must every link"
    snap = survey.roster_now(vault, slug)
    assert survey.member_by_id(snap, before_ids["Cid Cole"])["team"] == "2"
    # Ann's stored answer about Cid is now about a student on another team. It
    # is ignored rather than misattributed, which is the guarantee that matters.
    frame = graded_frame(vault, slug)
    assert len(frame) == 6
    assert set(frame[frame["Name"] == "Cid Cole"]["Team"]) == {"2"}


def test_an_unchanged_list_re_saves_without_a_caution(vault):
    slug, _ = set_up(vault)
    submit_everyone(vault, slug)
    assert survey.roster_shift(vault, slug, contacts())["safe"]


# --------------------------------------------------------------------------- #
# The whole page
# --------------------------------------------------------------------------- #
def test_the_editor_works_in_the_real_app(tmp_path):
    """The point of the feature is the page: an edit has to show its
    consequences, wait for a yes, and leave every other student alone. A
    separate process, because Streamlit's secrets are a process-wide singleton.
    The driver is tests/_roster_edit_app.py.
    """
    done = subprocess.run(
        [sys.executable, os.path.join(REPO, "tests", "_roster_edit_app.py"),
         str(tmp_path / "edit")],
        capture_output=True, text=True, timeout=900, cwd=REPO)
    assert "FAILURES: none" in done.stdout, done.stdout[-5000:]
    assert done.returncode == 0, done.stdout[-5000:]
    for line in ("PASS - the Teams editor is offered",
                 "PASS - a move that touches answers does not just happen",
                 "PASS - nothing moved while the question was on screen",
                 "PASS - cancelling leaves the student where they were",
                 "PASS - every other student's link is unchanged",
                 "PASS - the moved student is listed, not dropped",
                 "PASS - nobody else's link moved",
                 "PASS - their submission is gone"):
        assert line in done.stdout, done.stdout[-5000:]
