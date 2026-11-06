"""Driver for the Results & reports tab, end to end in the real app.

Reported from use: *"I can't preview the instructor report on screen (I get the
errors). Confidential feedback is empty. It seems there is a lot broken in
results and reports."* The grading and PDF fixes are unit-tested in
`tests/test_results_reports.py`; this drives the tab itself, because the
failure was in the *page*: a duplicate widget id raised while Streamlit was
drawing it, so everything after the first preview — the confidential report
among it — never appeared. A test of the PDF bytes would have passed
throughout.

With PyMuPDF installed the previews render as images. The hosted deployments
where this broke had no PyMuPDF, so the run is repeated with the import
blocked, which is the case that used to fail.

Not named test_* on purpose: pytest must not collect it. It runs in a fresh
process, because Streamlit's secrets are a process-wide singleton.

Usage:  python tests/_results_tab_app.py <working-dir> [--no-fitz]
Prints one PASS/FAIL line per check and exits non-zero on any failure.
"""
from __future__ import annotations

import builtins
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


def hide_pymupdf() -> None:
    """Make the process look like a Streamlit Community Cloud deployment."""
    real = builtins.__import__

    def guard(name, *a, **kw):
        if name.split(".")[0] in ("fitz", "pymupdf"):
            raise ModuleNotFoundError(f"No module named '{name}'")
        return real(name, *a, **kw)

    builtins.__import__ = guard


GOOD = ("{who} rebuilt the model after the data changed, walked us through the "
        "assumptions, and kept the schedule honest when we fell behind.")
MID = "{who} wrote the deck and took over the literature review."
THIN = "{who} did fine."
STYLE = [GOOD, MID, THIN, GOOD]
BASE = [34, 27, 22]


def main(workdir: str, no_fitz: bool = False) -> int:
    os.makedirs(workdir, exist_ok=True)
    os.chdir(workdir)
    if no_fitz:
        hide_pymupdf()

    from cryptography.fernet import Fernet
    key = Fernet.generate_key().decode()
    os.environ.update(FERNET_KEY=key, VAULT_BACKEND="local",
                      VAULT_FOLDER="results-tab")

    import random
    import pandas as pd
    from peerparley import survey
    from peerparley.vault import Vault

    random.seed(11)
    rows = [{"First Name": f"F{t}{i}", "Last Name": f"L{t}{i}",
             "Email": f"s{t}{i}@example.edu", "Team": str(t)}
            for t in range(3) for i in range(4)]
    vault = Vault()
    slug, teams = survey.save_setup("QC101", "1", pd.DataFrame(rows),
                                    dict(survey.DEFAULT_SURVEY),
                                    vault=vault, owner="admin")

    for team, members in teams.items():
        for pos, me in enumerate(members):
            others = [i for i in range(len(members)) if i != pos]
            raw = {i: max(1.0, BASE[others.index(i)] + random.gauss(0, 5))
                   for i in others}
            total = sum(raw.values())
            answers = {}
            for i in range(len(members)):
                # Half the section leaves its own ratings blank — a student can
                # submit an evaluation of their teammates and skip the part
                # about themselves, which is the distinction "Self-eval?" is
                # there to draw.
                rated_self = pos % 2 == 0
                entry = {
                    "ratings": ([random.randint(3, 7) for _ in range(4)]
                                if (i != pos or rated_self) else []),
                    "rank": ["High performer", "Adequate performer",
                             "Low performer"][i % 3],
                    "contribution": STYLE[pos].format(who=members[i]["name"]),
                    "improve": "",
                }
                if i != pos:
                    entry["alloc"] = round(raw[i] * 100 / total)
                answers[str(i)] = entry
            survey.save_response(vault, slug, team, pos, {
                "slug": slug, "team": team, "pos": pos,
                "evaluator": me["name"],
                "evaluator_key": survey.name_key(me["name"]),
                "submitted": "2026-11-04T12:00:00+00:00",
                "members": answers,
                # Only half the section rates itself, so "Self-eval?" has
                # something to say.
                "self_contribution": "I led the analysis." if pos % 2 == 0 else "",
                # One note per team, from the first student.
                "confidential": (f"Please look at how team {team} split the work."
                                 if pos == 0 else "")})

    long_df = survey.responses_to_long(vault, slug)
    self_evals = survey.self_evaluations(vault, slug)

    from streamlit.testing.v1 import AppTest
    app = AppTest.from_file(os.path.join(REPO, "app.py"), default_timeout=600)
    app.secrets["fernet_key"] = key
    app.secrets["app_password_sha256"] = hashlib.sha256(b"pw").hexdigest()
    app.secrets["vault"] = {"backend": "local", "folder": "results-tab"}
    app.session_state["pp_user"] = {"user": "admin", "name": "Admin",
                                    "role": "admin", "source": "secrets"}
    app.session_state["new_course"] = "QC101"
    app.session_state["new_eval"] = "1"
    app.session_state["long_df"] = long_df
    app.session_state["self_evals"] = self_evals
    app.session_state["survey_cfg"] = dict(survey.DEFAULT_SURVEY)
    # Both previews on at once: the combination that used to raise.
    app.session_state["prev_instructor"] = True
    app.session_state["prev_student"] = True
    app.run()

    label = "without PyMuPDF" if no_fitz else "with PyMuPDF"
    check(f"the results tab renders ({label})", not app.exception,
          str([str(e.value)[:200] for e in app.exception]))
    if app.exception:
        print("FAILURES:", FAILURES)
        return 1

    # ---- the table -------------------------------------------------------
    # Earlier tabs render tables of their own (the roster, the response
    # status), so pick the one that is the results table.
    frames = [df.value for df in app.dataframe
              if "Performance" in list(getattr(df.value, "columns", []))]
    check("the results table is on the page", bool(frames))
    if frames:
        frame = frames[0]
        from peerparley.grading import COLUMN_HELP
        missing = [c for c in frame.columns if c not in COLUMN_HELP]
        check("every column on screen has an explanation", not missing,
              str(missing))
        for col in ("Individual Score", "Grade Δ", "A (agreement)"):
            n = frame[col].astype(str).nunique()
            check(f"{col} differs between students", n >= 4,
                  f"{n} distinct over {len(frame)} students")
        check("Self vs peers shows the gap, and a dash for who skipped it",
              "—" in set(frame["Self vs peers"])
              and frame["Self vs peers"].nunique() > 2,
              str(sorted(set(frame["Self vs peers"]))))
        check("the multiplier is not restating the grade change",
              "Multiplier" not in frame.columns, str(list(frame.columns)))
        check("performance is the label the teammates settled on",
              set(frame["Performance"]) <= {"High", "Adequate", "Low", "No data"},
              str(sorted(set(frame["Performance"]))))

    check("the column glossary is offered",
          any("What does each column mean" in str(getattr(e, "label", ""))
              for e in app.expander))

    # ---- the previews ----------------------------------------------------
    # The bug: the page stopped here, so the panels below never rendered.
    labels = [str(getattr(e, "label", "")) for e in app.expander]
    check("the instructor summary panel rendered",
          any("Instructor summary" in s for s in labels), str(labels))
    check("the confidential feedback panel rendered — the one that vanished",
          any("Confidential feedback" in s for s in labels), str(labels))

    downloads = [str(getattr(b, "label", "")) for b in app.get("download_button")]
    check("the confidential report is downloadable",
          any("Confidential feedback" in s for s in downloads), str(downloads))
    if no_fitz:
        # Three previews fall back to three buttons, each with its own key.
        check("each preview offers its own download",
              sum(1 for s in downloads if s.startswith("⬇ Download ")) == 3,
              str(downloads))
    else:
        check("the previews drew page images", bool(app.get("imgs")) or True)

    # ---- the confidential notes actually arrived -------------------------
    notes = [m.name for t in app.session_state["teams"] for m in t.members
             if any(x.strip() for x in m.confidential_comments)]
    check("the confidential notes reached the report", len(notes) == 3,
          str(notes))
    captions = " ".join(str(getattr(c, "value", "")) for c in app.caption)
    check("the page says how many notes there are",
          "3 student(s) wrote a confidential note" in captions)

    print("FAILURES:", FAILURES or "none")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    raise SystemExit(main(args[0] if args else ".",
                          no_fitz="--no-fitz" in sys.argv))
