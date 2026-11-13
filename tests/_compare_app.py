"""Driver for the Compare tab, with three real evaluation rounds.

Compare had no test of any kind before the 2.17.0 audit. It reads, decrypts
and regrades every response of every round, so it was put behind a button — but
the flag the button set was never cleared, so after the first press it ran again
on every click anywhere in the app for the rest of the session.

This seeds three rounds of the same course, with a student who changes team,
one who joins in round 2 and one whose name is written "Last, First" in one
round, and checks the comparison is right and that it is built once.

Not named test_* on purpose. Fresh process, for Streamlit's secrets.

Usage:  python tests/_compare_app.py <working-dir>
"""
from __future__ import annotations

import hashlib
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

FAILURES: list = []


def check(name, ok, detail=""):
    print(("PASS" if ok else "FAIL"), "-", name, ("| " + detail) if detail else "")
    if not ok:
        FAILURES.append(name)


def main(workdir):
    os.makedirs(workdir, exist_ok=True)
    os.chdir(workdir)
    from cryptography.fernet import Fernet
    key = Fernet.generate_key().decode()
    os.environ.update(FERNET_KEY=key, VAULT_BACKEND="local",
                      VAULT_FOLDER="compare")
    import pandas as pd
    from peerparley import survey
    from peerparley.vault import Vault
    from streamlit.testing.v1 import AppTest

    vault = Vault()
    rounds = {
        "1": {"1": ["Ann Apple", "Bob Brown", "Cid Cole"],
              "2": ["Dee Diaz", "Eve Ellis", "Finn Ford"]},
        "2": {"1": ["Ann Apple", "Bob Brown", "Eve Ellis"],      # Eve moved
              "2": ["Dee Diaz", "Cid Cole", "Finn Ford", "Gus Gold"]},
        "3": {"1": ["Apple, Ann", "Bob Brown", "Eve Ellis"],     # written L, F
              "2": ["Dee Diaz", "Cid Cole", "Finn Ford", "Gus Gold"]},
    }
    for en, teams in rounds.items():
        rows = []
        for t, names in teams.items():
            for n in names:
                rows.append({"Name": n, "Email": "", "Team": t})
        slug, saved = survey.save_setup("CMP101", en, pd.DataFrame(rows),
                                        dict(survey.DEFAULT_SURVEY),
                                        vault=vault, owner="admin")
        for t, members in saved.items():
            for pos, me in enumerate(members):
                ans = {}
                for i, m in enumerate(members):
                    e = {"ratings": [5, 6, 5, 6],
                         "rank": ["High performer", "Adequate performer",
                                  "Low performer"][(i + int(en)) % 3],
                         "contribution": f"{m['name']} did the analysis well.",
                         "improve": ""}
                    if m["id"] != me["id"]:
                        # Round by round, the first-listed teammate gets more.
                        e["alloc"] = (100 // (len(members) - 1)
                                      + (10 * int(en) if i == 0 else 0))
                    ans[str(m["id"])] = e
                survey.save_response_by_id(vault, slug, str(me["id"]), {
                    "slug": slug, "team": t, "pos": pos,
                    "evaluator": me["name"],
                    "evaluator_key": survey.name_key(me["name"]),
                    "submitted": "2026-11-13T12:00:00+00:00", "members": ans,
                    "self_contribution": "", "confidential": ""})

    # Count how often the comparison actually reads every round's responses.
    reads = {"n": 0}
    real = survey.responses_to_long

    def counting(v, slug):
        reads["n"] += 1
        return real(v, slug)

    survey.responses_to_long = counting

    def boot(**state):
        app = AppTest.from_file(os.path.join(REPO, "app.py"), default_timeout=600)
        app.secrets["fernet_key"] = key
        app.secrets["app_password_sha256"] = hashlib.sha256(b"pw").hexdigest()
        app.secrets["vault"] = {"backend": "local", "folder": "compare"}
        app.session_state["pp_user"] = {"user": "admin", "name": "Admin",
                                        "role": "admin", "source": "secrets"}
        app.session_state["new_course"] = "CMP101"
        app.session_state["new_eval"] = "1"
        for k, v in state.items():
            app.session_state[k] = v
        app.run()
        return app

    app = boot()
    check("the Compare tab renders", not app.exception,
          str([str(e.value)[:200] for e in app.exception]))
    picks = [m for m in app.multiselect
             if "Evaluations to compare" in str(getattr(m, "label", ""))]
    check("all three rounds are offered",
          bool(picks) and list(picks[0].value) == ["1", "2", "3"],
          str(picks[0].value if picks else None))

    reads["n"] = 0
    app.run()
    check("nothing is read before the button is pressed", reads["n"] == 0,
          f"{reads['n']} round read(s)")

    go = [b for b in app.button if b.key == "cmp_go"]
    check("the build button is there", bool(go))
    if not go:
        print("FAILURES:", FAILURES)
        return 1
    reads["n"] = 0
    go[0].click().run()
    check("pressing it raises nothing", not app.exception,
          str([str(e.value)[:200] for e in app.exception]))
    check("pressing it reads each round once", reads["n"] == 3,
          f"{reads['n']} read(s)")

    # The trap: after the first press, every rerun anywhere rebuilt it all.
    reads["n"] = 0
    app.run()
    app.run()
    check("clicking elsewhere afterwards does not rebuild it",
          reads["n"] == 0, f"{reads['n']} round read(s) over two reruns")

    frames = [d.value for d in app.dataframe
              if "Student" in list(getattr(d.value, "columns", []))]
    check("the comparison table is shown", bool(frames))
    if frames:
        df = frames[0]
        students = list(df["Student"])
        check("one row per student, not per spelling",
              sum(1 for s in students if "Apple" in s) == 1,
              f"{[s for s in students if 'Apple' in s]}")
        check("a student who joined in round 2 is there",
              "Gus Gold" in students)
        gus = df[df["Student"] == "Gus Gold"].iloc[0]
        check("with nothing for the round before they joined",
              pd.isna(gus["E1 Score"]) and not pd.isna(gus["E2 Score"]),
              f"E1={gus['E1 Score']} E2={gus['E2 Score']}")
        for en in ("1", "2", "3"):
            check(f"round {en} has scores", df[f"E{en} Score"].notna().sum() >= 6)

    downloads = [str(getattr(b, "label", "")) for b in app.get("download_button")]
    check("the comparison PDF is offered",
          any("Comparison report" in s for s in downloads), str(downloads))

    # Choosing a different set of rounds asks to be rebuilt, rather than
    # silently showing the old result for the new choice.
    picks = [m for m in app.multiselect
             if "Evaluations to compare" in str(getattr(m, "label", ""))]
    picks[0].set_value(["1", "3"]).run()
    check("changing the rounds raises nothing", not app.exception)
    infos = " ".join(str(getattr(e, "value", "")) for e in app.info)
    check("it says the comparison needs building again",
          "press **Build the comparison**" in infos, infos[:160])
    frames = [d.value for d in app.dataframe
              if "Student" in list(getattr(d.value, "columns", []))]
    stale = bool(frames) and "E2 Score" in frames[0].columns
    check("a stale table is not shown for a different choice of rounds",
          not stale, "the old three-round table is still on screen" if stale else "")

    # No rounds chosen at all.
    picks = [m for m in app.multiselect
             if "Evaluations to compare" in str(getattr(m, "label", ""))]
    picks[0].set_value([]).run()
    check("choosing no rounds raises nothing", not app.exception)

    survey.responses_to_long = real
    print("FAILURES:", FAILURES or "none")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
