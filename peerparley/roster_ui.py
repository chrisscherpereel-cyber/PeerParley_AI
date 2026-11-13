"""The Teams editor: add, move and remove students after the roster is saved.

Why it is safe to offer
-----------------------
Until 2.16.0 editing a saved roster was the one thing an instructor was warned
off doing. A student was identified by their *position* — their index in their
team's list in the stored snapshot — so adding a student named "Adams" to a
team renumbered everyone after them: the links already in students' inboxes
pointed at the wrong people, and the answers already stored described the wrong
people. Nothing errored. The grades were simply wrong, and `roster_shift`
existed to tell the instructor not to try.

Every student now carries a permanent id (see `survey.py`). Positions are
presentational, so a student can be added, moved or removed and nobody else is
disturbed: their teammates' links keep working, and their teammates' answers
keep meaning what they meant.

What an edit is allowed to discard
----------------------------------
Only answers that have stopped describing anything. Moving a student clears
what they wrote about the team they are leaving and what that team wrote about
them; it keeps their own self-evaluation, and it keeps everything their old
teammates wrote about each *other*. Removing a student deletes their submission
and every reference to them.

So an edit that touches stored answers is never applied on the first click.
`survey.edit_impact` is shown first, in plain English, naming the students
affected — and an edit that costs nothing (the ordinary case, before links go
out) is applied straight away rather than asking a pointless question.
"""
from __future__ import annotations

from typing import Dict, List, Optional

import streamlit as st

from . import survey
from .vault import Vault

UNASSIGNED = "— no team —"
PENDING = "roster_pending"       # the edit awaiting confirmation


def _label(member: dict) -> str:
    mail = member.get("email", "")
    return f"{member.get('name', '?')}" + (f"  ·  {mail}" if mail else "")


def _after_write(result: dict) -> None:
    """Apply the result of an edit: tell the instructor, drop stale caches."""
    st.session_state.pop(PENDING, None)
    if not result.get("ok"):
        st.session_state["roster_error"] = result.get("detail", "That didn't work.")
        return
    st.session_state["roster_note"] = result.get("detail", "Done.")
    # Everything downstream of the roster is now stale: the grades, the
    # progress table, the loaded long-format frame and the vault's own read
    # cache. Cleared here rather than at each call site, because a half-cleared
    # view of an edited roster is how an instructor ends up grading the
    # arrangement that existed a minute ago.
    for key in ("long_df", "self_evals", "teams", "roster", "pdf_zip",
                "ai_narratives", "ai_reports"):
        st.session_state.pop(key, None)
    st.cache_data.clear()


def _render_pending(vault: Vault, slug: str) -> bool:
    """The confirmation step. True when something is waiting on the person."""
    pending = st.session_state.get(PENDING)
    if not pending:
        return False
    snap = survey.roster_now(vault, slug)
    who = survey.member_by_id(snap, pending.get("member_id", ""))
    if not who:
        st.session_state.pop(PENDING, None)
        return False
    action = pending.get("action")
    target = pending.get("team", "")
    impact = survey.edit_impact(vault, slug, action, who["id"], team=target)

    if impact["safe"]:
        # Nothing stored is affected, so there is nothing to confirm.
        _apply(vault, slug, pending)
        return False

    verb = ("Remove" if action == "remove" else
            f"Move to {target or 'no team'}")
    with st.container(border=True):
        st.warning(f"**{verb}: {who['name']}** — this changes answers already "
                   f"collected ({impact['responses']} submission(s)).",
                   icon="⚠️")
        for line in impact["drops"]:
            st.markdown(f"- {line}")
        st.caption("Everything else is untouched: every other student keeps "
                   "their link, their submission and the answers their "
                   "teammates wrote about them.")
        c1, c2, _ = st.columns([1, 1, 3])
        if c1.button("Yes, do it", type="primary", key="roster_confirm"):
            _apply(vault, slug, pending)
            st.rerun()
        if c2.button("Cancel", key="roster_cancel"):
            st.session_state.pop(PENDING, None)
            st.rerun()
    return True


def _apply(vault: Vault, slug: str, pending: dict) -> None:
    action = pending.get("action")
    mid = pending.get("member_id", "")
    if action == "remove":
        _after_write(survey.remove_student(vault, slug, mid))
    elif action == "move":
        _after_write(survey.move_student(vault, slug, mid,
                                         pending.get("team", "")))
    else:
        st.session_state.pop(PENDING, None)


def _request(action: str, member_id: str, team: str = "") -> None:
    st.session_state[PENDING] = {"action": action, "member_id": member_id,
                                 "team": team}


def render(vault: Vault, slug: str, read_only: bool = False) -> Optional[dict]:
    """Draw the editor. Returns the snapshot as it now stands, or None."""
    snap = survey.roster_now(vault, slug)
    if not snap:
        return None

    teams: Dict[str, List[dict]] = dict(snap.get("teams", {}) or {})
    loose: List[dict] = list(snap.get("unassigned", []) or [])
    n_students = sum(len(v) for v in teams.values()) + len(loose)

    note = st.session_state.pop("roster_note", "")
    err = st.session_state.pop("roster_error", "")
    if note:
        st.success(note, icon="✅")
    if err:
        st.error(err)

    if read_only:
        st.caption("This section belongs to another instructor, so its teams "
                   "can be seen but not changed.")

    short = survey.undersized_teams(snap)
    if short:
        st.warning(
            "These teams cannot be graded yet — a student needs at least one "
            "teammate to evaluate: "
            + ", ".join(f"**team {t}** (needs {n} more)"
                        for t, n in sorted(short.items())), icon="👥")
    if loose:
        st.info(f"**{len(loose)} student(s) are on the roster with no team.** "
                "They have a link, but it tells them there is nothing to fill "
                "in yet. Give them a team below and the same link starts "
                "working.", icon="🧍")

    if not read_only and _render_pending(vault, slug):
        return snap        # a confirmation is on screen; don't offer more edits

    options = [UNASSIGNED] + survey.all_team_names(snap)

    def member_row(member: dict, current: str) -> None:
        cols = st.columns([3.2, 1.5, 0.9, 0.9])
        cols[0].markdown(_label(member))
        mid = str(member.get("id", ""))
        if read_only:
            return
        choice = cols[1].selectbox(
            "Team", options, index=options.index(current if current in options
                                                 else UNASSIGNED),
            key=f"rt_{mid}", label_visibility="collapsed")
        want = "" if choice == UNASSIGNED else choice
        if cols[2].button("Move", key=f"rm_{mid}",
                          disabled=(want == (current if current != UNASSIGNED
                                             else "")),
                          help="Move this student to the team chosen beside "
                               "their name."):
            _request("move", mid, want)
            st.rerun()
        if cols[3].button("Remove", key=f"rx_{mid}",
                          help="Take this student off the evaluation "
                               "altogether. Their submission goes with them."):
            _request("remove", mid)
            st.rerun()

    st.caption(f"{len(teams)} team(s), {n_students} student(s). Every student "
               f"keeps the same personal link through any change here.")

    for team in sorted(teams, key=str):
        members = teams[team]
        head = f"**Team {team}** — {len(members)} student(s)"
        if team in short:
            head += "  ·  not gradeable yet"
        with st.container(border=True):
            tc = st.columns([3.2, 1.5, 0.9, 0.9])
            tc[0].markdown(head)
            if not read_only:
                new_name = tc[1].text_input(
                    "Rename", value=team, key=f"rn_{team}",
                    label_visibility="collapsed",
                    help="Rename this team. Links and answers are unaffected.")
                if tc[2].button("Rename", key=f"rnb_{team}",
                                disabled=(new_name.strip() == team)):
                    _after_write(survey.rename_team(vault, slug, team,
                                                    new_name))
                    st.rerun()
                if tc[3].button("Dissolve", key=f"rdb_{team}",
                                help="Break up this team. Its students move to "
                                     "the no-team list and keep everything "
                                     "they have submitted."):
                    _after_write(survey.delete_team(vault, slug, team))
                    st.rerun()
            if not members:
                st.caption("Nobody on this team yet.")
            for m in members:
                member_row(m, team)

    if loose:
        with st.container(border=True):
            st.markdown("**No team yet**")
            for m in loose:
                member_row(m, UNASSIGNED)

    if read_only:
        return snap

    # ---- add a student, add a team ----------------------------------------
    with st.expander("Add a student, or add a team"):
        st.caption("A student added here gets their own link straight away, "
                   "and nobody else's link or answers change.")
        a1, a2, a3 = st.columns([2, 2, 1.4])
        name = a1.text_input("Name", key="radd_name",
                             placeholder="Jordan Reyes")
        email = a2.text_input("Email (optional)", key="radd_email",
                              placeholder="jordan@university.edu")
        where = a3.selectbox("Team", options, key="radd_team")
        if st.button("Add student", key="radd_go",
                     disabled=not name.strip()):
            _after_write(survey.add_student(
                vault, slug, name, "" if where == UNASSIGNED else where,
                email))
            st.rerun()
        st.divider()
        t1, t2 = st.columns([2, 1])
        tname = t1.text_input("New team name", key="rnew_team",
                              placeholder="5")
        if t2.button("Add team", key="rnew_go", disabled=not tname.strip()):
            _after_write(survey.create_team(vault, slug, tname))
            st.rerun()
    return snap
