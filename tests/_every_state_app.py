"""Drive every tab, in every state, pressing everything that can be pressed.

The release question is "how likely is this app to crash in front of an
instructor?", and for a Streamlit app that has a concrete answer. Every tab's
body runs on every rerun, so a crash anywhere — including on a tab the
instructor is not looking at — takes the whole page with it. The way to find
those is not to read the code but to put the app in each state an instructor
can reach and then press, tick and choose everything on the page.

States:
  A  signed in, nothing chosen
  B  a course chosen whose roster is saved, nothing loaded
  C  B with the responses loaded into grading — Results and Send come alive
  D  C with every preview switched on
  E  a Qualtrics export imported instead of the built-in survey
  F  an instructor looking at a survey that belongs to someone else

The data is deliberately untidy: a team where nobody answered, a student on no
team, a student with no email, a name with accents and an apostrophe, partial
self-evaluations, and confidential notes.

In each state, from a fresh boot each time so failures are attributable:
  * every button is pressed, except the ones that would genuinely send email
    or call a paid AI provider — and those are pressed too in the states where
    nothing is configured, because "nothing configured" must produce a
    message, not a traceback;
  * every checkbox is ticked;
  * every dropdown is set to each of its options in turn — this is where the
    branches nobody has looked at live, such as the six delivery methods on
    each of three email panels.

Not named test_* on purpose: pytest must not collect it. Fresh process,
because Streamlit's secrets are a process-wide singleton.

Usage:  python tests/_every_state_app.py <working-dir> [--quick]
Prints one line per state and per failure; exits non-zero on any failure.
"""
from __future__ import annotations

import hashlib
import os
import sys
import time
import traceback

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

FAILURES: list = []
COUNTS = {"runs": 0, "buttons": 0, "checkboxes": 0, "choices": 0}

# Pressed only where no mail or AI provider is configured — which in this
# harness is everywhere, so they are all pressed. Listed so that, if someone
# ever runs this against real credentials, these are the ones to worry about.
EXTERNAL = ("Send ", "Prepare ", "Draft ", "Redraft", "Test the connection",
            "I've completed sign-in")

# Buttons whose whole job is to leave this page or change who is signed in.
SKIP = ("Sign out", "Log out", "Delete this survey", "Yes, delete",
        "Delete it", "Discard", "Start over")


def fail(state: str, what: str, app=None, exc: BaseException = None) -> None:
    detail = ""
    if app is not None and app.exception:
        detail = " | ".join(str(e.value)[:300] for e in app.exception)
    elif exc is not None:
        detail = "".join(traceback.format_exception_only(type(exc), exc))[:300]
    FAILURES.append(f"[{state}] {what}: {detail}")
    print(f"FAIL [{state}] {what}: {detail}")


def seed(vault, survey, pd):
    """A section with every awkward case in it."""
    rows = []
    teams = {
        "1": ["Ann Apple", "Bob Brown", "Cid Cole", "Dee Diaz"],
        "2": ["Zoë O'Brien-Núñez", "Eve Ellis", "Finn Ford"],
        "3": ["Gail Gray", "Hugo Hale", "Ivy Irwin"],      # nobody answers
    }
    for team, names in teams.items():
        for n in names:
            first, last = n.split(" ", 1)
            rows.append({"First Name": first, "Last Name": last,
                         "Email": ("" if n == "Finn Ford"
                                   else f"{first.lower()}@example.edu"),
                         "Team": team})
    rows.append({"First Name": "Lone", "Last Name": "Wolf",
                 "Email": "lone@example.edu", "Team": ""})   # no team
    slug, saved = survey.save_setup("ST101", "1", pd.DataFrame(rows),
                                    dict(survey.DEFAULT_SURVEY),
                                    vault=vault, owner="admin")
    TEXT = ["{w} rebuilt the model after the data changed and walked us "
            "through every assumption in Tuesday's meeting.",
            "{w} wrote the slides.", "fine", ""]
    for team, members in saved.items():
        if team == "3":
            continue
        for pos, me in enumerate(members):
            if team == "1" and pos == 3:
                continue                      # one non-responder
            answers = {}
            others = [m for m in members if m["id"] != me["id"]]
            share = 100 // max(1, len(others))
            for i, m in enumerate(members):
                rated_self = pos % 2 == 0
                e = {"ratings": ([((i + pos) % 5) + 3] * 4
                                 if (m["id"] != me["id"] or rated_self) else []),
                     "rank": ["High performer", "Adequate performer",
                              "Low performer"][(i + pos) % 3],
                     "contribution": TEXT[pos % 4].format(w=m["name"]),
                     "improve": "Speak up sooner." if pos == 0 else ""}
                if m["id"] != me["id"]:
                    e["alloc"] = share + (5 if i == 0 else 0)
                answers[str(m["id"])] = e
            survey.save_response_by_id(vault, slug, str(me["id"]), {
                "slug": slug, "team": team, "pos": pos,
                "evaluator": me["name"],
                "evaluator_key": survey.name_key(me["name"]),
                "submitted": "2026-11-13T12:00:00+00:00",
                "members": answers,
                "self_contribution": "I led the analysis." if pos else "",
                "confidential": (f"Please look at team {team}'s split."
                                 if pos == 0 else "")})
    return slug


def main(workdir: str, quick: bool = False) -> int:
    os.makedirs(workdir, exist_ok=True)
    os.chdir(workdir)
    from cryptography.fernet import Fernet
    key = Fernet.generate_key().decode()
    os.environ.update(FERNET_KEY=key, VAULT_BACKEND="local",
                      VAULT_FOLDER="every-state")

    import pandas as pd
    from peerparley import survey
    from peerparley.vault import Vault
    from streamlit.testing.v1 import AppTest

    vault = Vault()
    slug = seed(vault, survey, pd)

    # Every press starts from the same stored data. Without this, a press of
    # Remove or Dissolve in the Teams editor genuinely edits the roster, and
    # every state after it runs against a section that is no longer the one
    # it was meant to test.
    import shutil
    from peerparley import vault as vault_mod
    store = os.path.abspath(os.path.join("vault_cache", "every-state"))
    pristine = store + ".pristine"
    shutil.copytree(store, pristine)

    def restore():
        shutil.rmtree(store, ignore_errors=True)
        shutil.copytree(pristine, store)
        vault_mod.clear_cache()

    # Only the export builder from qa_regression.py. Importing that module
    # runs its whole check suite as a side effect, inside this process.
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _fuzz_inputs import load_export_builder
    qa_regression = load_export_builder()
    from peerparley import ingest
    q_long, q_self, q_roster, _rep = ingest.parse_qualtrics_export(
        qa_regression.make_qualtrics_export(), "qa_export.csv")

    admin = {"user": "admin", "name": "Admin", "role": "admin",
             "source": "secrets"}
    other = {"user": "jdoe", "name": "J Doe", "role": "instructor",
             "source": "accounts"}

    ERRORS_SEEN: dict = {}

    def note_errors(app, where):
        """Handled errors are bugs too, if they should never appear."""
        for e in app.error:
            text = str(getattr(e, "value", ""))[:220]
            ERRORS_SEEN.setdefault(text, where)

    def boot(user=admin, course="", **state):
        restore()
        app = AppTest.from_file(os.path.join(REPO, "app.py"),
                                default_timeout=600)
        app.secrets["fernet_key"] = key
        app.secrets["app_password_sha256"] = hashlib.sha256(b"pw").hexdigest()
        app.secrets["vault"] = {"backend": "local", "folder": "every-state"}
        app.session_state["pp_user"] = user
        if course:
            app.session_state["new_course"] = course
            app.session_state["new_eval"] = "1"
            app.session_state["inv_url"] = "https://pp.example"
        for k, v in state.items():
            app.session_state[k] = v
        app.run()
        COUNTS["runs"] += 1
        return app

    def loaded_state():
        """What 'Load responses into grading' leaves behind."""
        snap = survey.roster_now(vault, slug)
        return {"long_df": survey.responses_to_long(vault, slug),
                "self_evals": survey.self_evaluations(vault, slug),
                "roster": survey.roster_for_matching(snap),
                "loaded_slug": slug,
                "loaded_submissions": survey.response_count(vault, slug)}

    previews = {"prev_instructor": True, "prev_student": True}

    STATES = {
        "A nothing chosen": dict(),
        "B roster saved": dict(course="ST101"),
        "C responses loaded": dict(course="ST101", **loaded_state()),
        "D every preview on": dict(course="ST101", **loaded_state(),
                                   **previews),
        "E Qualtrics import": dict(course="QX101", long_df=q_long,
                                   self_evals=q_self, roster=q_roster,
                                   qualtrics_sig="pre-loaded"),
        "F someone else's survey": dict(user=other, course="ST101"),
        # The Teams editor sits behind a toggle, so with it off its thirty-odd
        # controls are not on the page at all. Opened here, so the sweep
        # presses every Move, Remove, Rename and Dissolve too.
        "G Teams editor open": dict(course="ST101", **loaded_state(),
                                    roster_open=True),
    }
    if quick:
        # The richest single state: everything is loaded, so every tab is live
        # and every send panel, preview and dropdown is on the page. The full
        # matrix is the release gate (RELEASE.md); this is what every run of
        # the suite can afford.
        STATES = {k: v for k, v in STATES.items() if k[0] in "CG"}

    for name, state in STATES.items():
        t0 = time.perf_counter()
        try:
            app = boot(**state)
        except Exception as exc:  # noqa: BLE001
            fail(name, "boot", exc=exc)
            continue
        if app.exception:
            fail(name, "first render", app)
            continue
        note_errors(app, f"[{name}] first render")

        # ---- every button -------------------------------------------------
        targets = []
        for b in app.button:
            label = str(getattr(b, "label", ""))
            if any(label.startswith(s) for s in SKIP):
                continue
            targets.append((b.key, label))
        for bkey, label in targets:
            try:
                fresh = boot(**state)
                found = [b for b in fresh.button if b.key == bkey]
                if not found:
                    continue
                if found[0].disabled:
                    continue
                found[0].click().run()
                COUNTS["buttons"] += 1
                note_errors(fresh, f"[{name}] button {label!r}")
                if fresh.exception:
                    fail(name, f"button {label!r}", fresh)
            except Exception as exc:  # noqa: BLE001
                fail(name, f"button {label!r}", exc=exc)

        # ---- every checkbox -----------------------------------------------
        for c in [c for c in app.checkbox if not c.value]:
            label = str(getattr(c, "label", ""))
            try:
                fresh = boot(**state)
                found = [x for x in fresh.checkbox if x.key == c.key]
                if not found or found[0].disabled:
                    continue
                found[0].check().run()
                COUNTS["checkboxes"] += 1
                note_errors(fresh, f"[{name}] checkbox {label!r}")
                if fresh.exception:
                    fail(name, f"checkbox {label!r}", fresh)
            except Exception as exc:  # noqa: BLE001
                fail(name, f"checkbox {label!r}", exc=exc)

        # ---- every toggle ---------------------------------------------------
        for c in [c for c in app.toggle if not c.value]:
            label = str(getattr(c, "label", ""))
            try:
                fresh = boot(**state)
                found = [x for x in fresh.toggle if x.key == c.key]
                if not found or found[0].disabled:
                    continue
                found[0].set_value(True).run()
                COUNTS["checkboxes"] += 1
                note_errors(fresh, f"[{name}] toggle {label!r}")
                if fresh.exception:
                    fail(name, f"toggle {label!r}", fresh)
            except Exception as exc:  # noqa: BLE001
                fail(name, f"toggle {label!r}", exc=exc)

        # ---- every option of every dropdown --------------------------------
        for s in app.selectbox:
            label = str(getattr(s, "label", ""))
            # `options` holds the *displayed* labels (after format_func), which
            # set_value does not accept — so choose by position instead.
            shown = list(getattr(s, "options", []) or [])
            indexes = list(range(len(shown)))
            if len(indexes) > 12:                 # long pickers: ends only
                indexes = indexes[:2] + indexes[-2:]
            current = getattr(s, "index", None)
            for i in indexes:
                if i == current:
                    continue
                opt = shown[i]
                try:
                    fresh = boot(**state)
                    found = [x for x in fresh.selectbox if x.key == s.key]
                    if not found or found[0].disabled:
                        continue
                    if i >= len(list(found[0].options or [])):
                        continue
                    found[0].select_index(i).run()
                    COUNTS["choices"] += 1
                    note_errors(fresh, f"[{name}] dropdown {label!r} = {opt!r}")
                    if fresh.exception:
                        fail(name, f"dropdown {label!r} = {opt!r}", fresh)
                except Exception as exc:  # noqa: BLE001
                    fail(name, f"dropdown {label!r} = {opt!r}", exc=exc)

        print(f"STATE [{name}] {len(targets)} buttons, "
              f"{len(app.checkbox)} checkboxes, {len(app.selectbox)} dropdowns "
              f"in {time.perf_counter() - t0:.0f}s", flush=True)

    print(f"ERRORS SHOWN TO THE INSTRUCTOR: {len(ERRORS_SEEN)} distinct")
    for text, where in sorted(ERRORS_SEEN.items(), key=lambda kv: kv[1]):
        print(f"  ERRBOX {where}: {text}")
    print(f"TOTAL runs={COUNTS['runs']} buttons={COUNTS['buttons']} "
          f"checkboxes={COUNTS['checkboxes']} choices={COUNTS['choices']}")
    print("FAILURES:", len(FAILURES) if FAILURES else "none")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    raise SystemExit(main(args[0] if args else ".",
                          quick="--quick" in sys.argv))
