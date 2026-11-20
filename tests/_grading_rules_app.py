"""Driver: each evaluation's own grading rules, and overrides on a report.

Not named test_* on purpose: pytest must not collect it. It runs in a fresh
process, because Streamlit's secrets are a process-wide singleton.

Usage:  python tests/_grading_rules_app.py <working-dir>
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


def main(workdir: str) -> int:
    os.makedirs(workdir, exist_ok=True)
    os.chdir(workdir)
    from cryptography.fernet import Fernet
    key = Fernet.generate_key().decode()
    os.environ.update(FERNET_KEY=key, VAULT_BACKEND="local",
                      VAULT_FOLDER="grading-rules")

    import pandas as pd
    from peerparley import survey
    from peerparley.grading import settings_from
    from peerparley.vault import Vault

    vault = Vault()
    rows = [{"First Name": f, "Last Name": "Lee", "Email": f"{f.lower()}@x.edu",
             "Team": "1"} for f in ("Ann", "Bob", "Cid", "Dan")]
    loaded = {}
    for ev in ("1", "2"):
        slug, saved = survey.save_setup("GR101", ev, pd.DataFrame(rows),
                                        dict(survey.DEFAULT_SURVEY),
                                        vault=vault, owner="admin")
        members = saved["1"]
        tasks = ["rebuilt the financial model after the data changed",
                 "drafted the market research section and the survey",
                 "designed the slides and rehearsed the presentation",
                 "kept the schedule and ran Tuesday's planning meeting"]
        for pos, me in enumerate(members):
            answers = {}
            for i, m in enumerate(members):
                # Ann: "fine" and an even split (5). Bob: vague (7).
                # Cid and Dan: specific, different for each teammate (10).
                text = ("fine" if pos == 0 else
                        "was a good team member overall and did their part"
                        if pos == 1 else
                        f"{m['name'].split()[0]} {tasks[i]}, which kept the "
                        f"project on track.")
                e = {"ratings": [5, 5, 5, 5], "rank": "Adequate performer",
                     "contribution": text, "improve": ""}
                if m["id"] != me["id"]:
                    e["alloc"] = (34 if pos == 0 else
                                  [50, 30, 20, 25][(i + pos) % 4])
                answers[str(m["id"])] = e
            survey.save_response_by_id(vault, slug, str(me["id"]), {
                "slug": slug, "team": "1", "evaluator": me["name"],
                "members": answers})
        snap = survey.roster_now(vault, slug)
        loaded[ev] = {"long_df": survey.responses_to_long(vault, slug),
                      "self_evals": survey.self_evaluations(vault, slug),
                      "roster": survey.roster_for_matching(snap),
                      "loaded_slug": slug,
                      "loaded_submissions": survey.response_count(vault, slug)}
    s1, s2 = survey.slugify("GR101", "1"), survey.slugify("GR101", "2")

    from streamlit.testing.v1 import AppTest
    app = AppTest.from_file(os.path.join(REPO, "app.py"), default_timeout=600)
    app.secrets["fernet_key"] = key
    app.secrets["app_password_sha256"] = hashlib.sha256(b"pw").hexdigest()
    app.secrets["vault"] = {"backend": "local", "folder": "grading-rules"}
    app.session_state["pp_user"] = {"user": "admin", "name": "Admin",
                                    "role": "admin", "source": "secrets"}
    app.session_state["inv_url"] = "https://pp.example"
    app.session_state["new_course"] = "GR101"
    app.session_state["new_eval"] = "1"
    app.session_state["_active_slug"] = s1
    for k, v in loaded["1"].items():
        app.session_state[k] = v
    app.run()
    check("Eval 1 renders", not app.exception,
          str([str(e.value)[:200] for e in app.exception]))
    st_ = app.session_state["settings"]
    check("a fresh evaluation starts on the new defaults",
          (st_.sensitivity_B, st_.max_multiplier, st_.rounding_step,
           st_.rounding_mode, st_.response_scoring)
          == (1.0, 1.0, 5, "nearest", "rough"), repr(st_)[:200])
    teams = app.session_state["teams"]
    pts = sorted({m.response_points for t in teams for m in t.members})
    check("rough tiers reach the results by default", set(pts) == {5, 7, 10},
          str(pts))

    # ---- change a rule in Eval 1: quality scoring ------------------------
    radio = next(r for r in app.radio
                 if "score the feedback" in str(r.label))
    radio.set_value("quality").run()
    check("the change applies", app.session_state["settings"].response_scoring
          == "quality")
    stored = survey.load_grading(vault, s1)
    check("and is saved for Eval 1",
          settings_from(stored["settings"]).response_scoring == "quality",
          str(stored)[:200])
    check("but not for Eval 2",
          survey.load_grading(vault, s2).get("settings") in ({}, None))
    teams = app.session_state["teams"]
    pts = sorted({m.response_points for t in teams for m in t.members})
    check("quality points are no longer the tiers", set(pts) != {5, 7, 10},
          str(pts))

    # ---- open Eval 2: its own (default) rules ----------------------------
    # As if Eval 2 were picked and its responses loaded (setting the active
    # survey too, so the switch guard does not clear what is loaded here).
    app.session_state["new_eval"] = "2"
    app.session_state["_active_slug"] = s2
    for k, v in loaded["2"].items():
        app.session_state[k] = v
    app.run()
    check("Eval 2 renders", not app.exception,
          str([str(e.value)[:200] for e in app.exception]))
    check("Eval 2 has its own rules, not Eval 1's",
          app.session_state["settings"].response_scoring == "rough",
          f"{app.session_state['settings'].response_scoring} loaded_for="
          f"{app.session_state['_grading_loaded_for']} active="
          f"{app.session_state['_active_slug']} stored2={survey.load_grading(vault, s2)}")

    # ---- back to Eval 1: its rules came back -----------------------------
    app.session_state["new_eval"] = "1"
    app.session_state["_active_slug"] = s1
    for k, v in loaded["1"].items():
        app.session_state[k] = v
    app.run()
    check("Eval 1's rules come back with it",
          app.session_state["settings"].response_scoring == "quality")

    # ---- override one student's adjustment and points --------------------
    pick = next(s for s in app.selectbox if s.key == "report_pick")
    first = app.session_state["teams"][0].members[pick.value or 0]
    adj = next(n for n in app.number_input if str(n.label).startswith("Grade adjustment override"))
    adj.set_value(-20.0).run()
    pts = next(n for n in app.number_input if str(n.label).startswith("Response points override"))
    pts.set_value(4).run()
    check("overrides render cleanly", not app.exception,
          str([str(e.value)[:200] for e in app.exception]))
    m = next(mm for t in app.session_state["teams"] for mm in t.members
             if mm.key == first.key)
    check("the override reaches the grade", m.individual_score == 80
          and m.adjustment_overridden, f"{m.individual_score}")
    check("and the points", m.response_points == 4 and m.points_overridden)
    stored = survey.load_grading(vault, s1)
    check("overrides are saved with Eval 1",
          stored["overrides"].get(first.key) == {"adjust": -20.0, "points": 4},
          str(stored["overrides"]))
    frames = [df.value for df in app.dataframe
              if "Overridden" in list(getattr(df.value, "columns", []))]
    check("the results table marks it",
          bool(frames) and "adjustment, points" in set(frames[0]["Overridden"]),
          str(set(frames[0]["Overridden"])) if frames else "no table")
    warned = " ".join(str(w.value) for w in app.warning)
    check("an override past the caps is not warned (−20% is inside −50%)",
          "outside the caps" not in warned)
    adj = next(n for n in app.number_input if str(n.label).startswith("Grade adjustment override"))
    adj.set_value(10.0).run()
    warned = " ".join(str(w.value) for w in app.warning)
    check("an override above the 0% bonus cap is applied, with a warning",
          "outside the caps" in warned)

    # ---- the preview shows the overridden report -------------------------
    next(c for c in app.checkbox if c.key == "prev_student").check().run()
    check("the student's report builds with the override", not app.exception,
          str([str(e.value)[:200] for e in app.exception]))

    # ---- clearing goes back to the computed figures ----------------------
    next(b for b in app.button if b.key == "ov_clear_all").click().run()
    m = next(mm for t in app.session_state["teams"] for mm in t.members
             if mm.key == first.key)
    check("Clear all restores the computed figures",
          not m.adjustment_overridden and not m.points_overridden)
    check("and the saved overrides are gone",
          survey.load_grading(vault, s1)["overrides"] == {})
    check("Eval 2 never had any", survey.load_grading(vault, s2)["overrides"] == {})

    print("FAILURES:", FAILURES or "none")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
