"""Driver for the Teams editor: add, move and remove students in the real app.

Asked for: *"I would like to add the capability to easily add, move, and remove
students from peer evaluation teams in this application."*

Driven rather than unit-tested because the point of the feature is the page: an
edit has to show its consequences, wait for a yes, and then leave every other
student's link and answers exactly as they were. The operations themselves are
unit-tested in tests/test_roster_edit.py.

Not named test_* on purpose: pytest must not collect it. It runs in a fresh
process, because Streamlit's secrets are a process-wide singleton.

Usage:  python tests/_roster_edit_app.py <working-dir>
Prints one PASS/FAIL line per check and exits non-zero on any failure.
"""
from __future__ import annotations

import hashlib
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

FAILURES: list = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS" if ok else "FAIL"), "-", name, ("| " + detail) if detail else "")
    if not ok:
        FAILURES.append(name)


TEXT = "{who} ran the customer interviews and wrote up every transcript."


def main(workdir: str) -> int:
    os.makedirs(workdir, exist_ok=True)
    os.chdir(workdir)

    from cryptography.fernet import Fernet
    key = Fernet.generate_key().decode()
    os.environ.update(FERNET_KEY=key, VAULT_BACKEND="local",
                      VAULT_FOLDER="roster-edit")

    import pandas as pd
    from peerparley import survey
    from peerparley.vault import Vault

    names = {"1": ["Ann Apple", "Bob Brown", "Cid Cole"],
             "2": ["Dee Diaz", "Eve Ellis", "Finn Ford"]}
    rows = [{"First Name": n.split()[0], "Last Name": n.split()[1],
             "Email": f"{n.split()[0].lower()}@example.edu", "Team": t}
            for t, ns in names.items() for n in ns]
    vault = Vault()
    slug, teams = survey.save_setup("RE101", "1", pd.DataFrame(rows),
                                    dict(survey.DEFAULT_SURVEY),
                                    vault=vault, owner="admin")

    # Everybody submits, so every edit below has consequences to report.
    for team, members in teams.items():
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
                answers[str(m["id"])] = entry
            survey.save_response_by_id(vault, slug, str(me["id"]), {
                "slug": slug, "team": team, "pos": pos,
                "evaluator": me["name"],
                "evaluator_key": survey.name_key(me["name"]),
                "submitted": "2026-11-13T12:00:00+00:00",
                "members": answers,
                "self_contribution": "I led the analysis.",
                "confidential": ""})

    def ids():
        snap = survey.roster_now(vault, slug)
        return {m["name"]: str(m["id"])
                for _t, _p, m in survey.snapshot_members(snap)}

    secret = survey.token_secret()

    def links():
        snap = survey.roster_now(vault, slug)
        return {l["name"]: l["link"] for l in survey.student_links(
            "https://pp.example", slug, snap["teams"], secret)}

    before_ids, before_links = ids(), links()

    from streamlit.testing.v1 import AppTest

    def boot(**state):
        app = AppTest.from_file(os.path.join(REPO, "app.py"),
                                default_timeout=600)
        app.secrets["fernet_key"] = key
        app.secrets["app_password_sha256"] = hashlib.sha256(b"pw").hexdigest()
        app.secrets["vault"] = {"backend": "local", "folder": "roster-edit"}
        app.session_state["pp_user"] = {"user": "admin", "name": "Admin",
                                        "role": "admin", "source": "secrets"}
        app.session_state["new_course"] = "RE101"
        app.session_state["new_eval"] = "1"
        app.session_state["roster_open"] = state.pop("_closed", False) is False
        for k, v in state.items():
            app.session_state[k] = v
        app.run()
        return app

    # ---- closed, it costs nothing -----------------------------------------
    # It sat in a collapsed expander, whose body Streamlit runs whether or not
    # it is open — five widgets per student on every click anywhere in the app.
    app = boot(_closed=True)
    check("closed, the editor builds no widgets",
          not [b for b in app.button if str(getattr(b, "label", "")) == "Move"],
          f"{len(app.button)} buttons")
    check("but the switch to open it is there",
          any("Teams — add, move or remove" in str(getattr(t, "label", ""))
              for t in app.toggle))

    # ---- the editor is on the page --------------------------------------
    app = boot()
    check("the setup tab renders", not app.exception,
          str([str(e.value)[:200] for e in app.exception]))
    if app.exception:
        print("FAILURES:", FAILURES)
        return 1
    labels = [str(getattr(t, "label", "")) for t in app.toggle]
    check("the Teams editor is offered",
          any("Teams — add, move or remove" in s for s in labels), str(labels))
    buttons = [str(getattr(b, "label", "")) for b in app.button]
    for want in ("Move", "Remove", "Add student", "Add team", "Rename",
                 "Dissolve"):
        check(f"the editor offers {want}", want in buttons, str(buttons[:14]))

    # ---- a move that costs answers asks first ---------------------------
    cid = before_ids["Cid Cole"]
    app = boot(roster_pending={"action": "move", "member_id": cid, "team": "2"})
    check("a move that touches answers does not just happen",
          not app.exception, str([str(e.value)[:200] for e in app.exception]))
    warned = " ".join(str(getattr(w, "value", "")) for w in app.warning)
    check("it warns before moving", "Cid Cole" in warned and "already collected" in warned,
          warned[:200])
    shown = " ".join(str(getattr(m, "value", "")) for m in app.markdown)
    check("it names what would be cleared",
          "wrote about their 2 teammate(s)" in shown, shown[-400:])
    check("and it says who wrote about them",
          "What 2 teammate(s) wrote about" in shown, shown[-400:])
    confirm = [b for b in app.button
               if str(getattr(b, "label", "")) == "Yes, do it"]
    cancel = [b for b in app.button
              if str(getattr(b, "label", "")) == "Cancel"]
    check("a yes and a no are both offered", bool(confirm) and bool(cancel))
    snap = survey.roster_now(vault, slug)
    check("nothing moved while the question was on screen",
          survey.member_by_id(snap, cid)["team"] == "1")

    # Cancel leaves everything alone.
    if cancel:
        cancel[0].click().run()
        snap = survey.roster_now(vault, slug)
        check("cancelling leaves the student where they were",
              survey.member_by_id(snap, cid)["team"] == "1")

    # ---- confirm it -----------------------------------------------------
    app = boot(roster_pending={"action": "move", "member_id": cid, "team": "2"})
    confirm = [b for b in app.button
               if str(getattr(b, "label", "")) == "Yes, do it"]
    if not confirm:
        check("the confirm button is there to press", False)
        print("FAILURES:", FAILURES)
        return 1
    confirm[0].click().run()
    check("confirming raises nothing", not app.exception,
          str([str(e.value)[:200] for e in app.exception]))
    snap = survey.roster_now(vault, slug)
    check("the student is on their new team",
          survey.member_by_id(snap, cid)["team"] == "2")

    # The whole point: everyone else is untouched.
    now_links = links()
    unchanged = [n for n in before_links
                 if n != "Cid Cole" and before_links[n] == now_links.get(n)]
    check("every other student's link is unchanged",
          len(unchanged) == len(before_links) - 1,
          f"{len(unchanged)} of {len(before_links) - 1}")
    check("the moved student's own link is unchanged too",
          before_links["Cid Cole"] == now_links.get("Cid Cole"),
          "it carries their id, not their seat")
    check("ids did not churn", ids() == before_ids)

    # Their old teammates no longer refer to them; their mutual answers stand.
    ann = survey.load_response_by_id(vault, slug, before_ids["Ann Apple"])
    check("Ann's submission no longer mentions Cid",
          cid not in {str(k) for k in ann["members"]})
    check("Ann still rates Bob",
          before_ids["Bob Brown"] in {str(k) for k in ann["members"]})
    mine = survey.load_response_by_id(vault, slug, cid)
    check("the mover keeps only their self-evaluation",
          {str(k) for k in mine["members"]} == {cid}, str(list(mine["members"])))

    # ---- it still grades, and the moved student reads as No data --------
    from peerparley.grading import compute, GradeSettings, results_to_frame
    frame = results_to_frame(compute(
        survey.responses_to_long(vault, slug), GradeSettings(),
        self_evals=survey.self_evaluations(vault, slug)))
    check("every student is still in the results", len(frame) == 6,
          f"{len(frame)} rows")
    row = frame[frame["Name"] == "Cid Cole"]
    check("the moved student is listed, not dropped", len(row) == 1)
    if len(row):
        check("and reads as No data rather than a low score",
              row.iloc[0]["Performance"] == "No data",
              str(row.iloc[0]["Performance"]))
        check("their grade is the team score",
              row.iloc[0]["Grade Δ"] == "+0%", str(row.iloc[0]["Grade Δ"]))

    # ---- add a student ---------------------------------------------------
    app = boot()
    app.text_input(key="radd_name").set_value("Ivy Irwin").run()
    app.text_input(key="radd_email").set_value("ivy@example.edu").run()
    app.selectbox(key="radd_team").set_value("1").run()
    [b for b in app.button if str(getattr(b, "label", "")) == "Add student"][0]\
        .click().run()
    check("adding a student raises nothing", not app.exception,
          str([str(e.value)[:200] for e in app.exception]))
    snap = survey.roster_now(vault, slug)
    ivy = survey.find_member(snap, "Ivy Irwin")
    check("the new student is on the team", bool(ivy) and ivy["team"] == "1")
    added_links = links()
    check("the new student has their own link", "Ivy Irwin" in added_links)
    check("nobody else's link moved",
          all(added_links.get(n) == now_links[n] for n in now_links),
          "adding a student renumbered nothing")

    # ---- remove a student ------------------------------------------------
    bob = before_ids["Bob Brown"]
    app = boot(roster_pending={"action": "remove", "member_id": bob})
    confirm = [b for b in app.button
               if str(getattr(b, "label", "")) == "Yes, do it"]
    check("removing asks first", bool(confirm))
    if confirm:
        confirm[0].click().run()
        check("removing raises nothing", not app.exception,
              str([str(e.value)[:200] for e in app.exception]))
        snap = survey.roster_now(vault, slug)
        check("the student is off the roster",
              survey.member_by_id(snap, bob) is None)
        check("their submission is gone",
              survey.load_response_by_id(vault, slug, bob) is None)
        ann = survey.load_response_by_id(vault, slug, before_ids["Ann Apple"])
        check("nobody still refers to them",
              bob not in {str(k) for k in ann["members"]})
        gone_links = links()
        check("still nobody else's link changed",
              all(gone_links.get(n) == added_links[n] for n in gone_links),
              "removal renumbered nothing")

    # ---- and the section still grades end to end -------------------------
    frame = results_to_frame(compute(
        survey.responses_to_long(vault, slug), GradeSettings(),
        self_evals=survey.self_evaluations(vault, slug)))
    check("the section grades after every edit", len(frame) >= 5,
          f"{len(frame)} students")
    check("the removed student is not in the results",
          "Bob Brown" not in set(frame["Name"]))

    print("FAILURES:", FAILURES or "none")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
