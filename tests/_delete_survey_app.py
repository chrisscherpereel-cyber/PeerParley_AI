"""Deleting a survey from the sidebar list, driven through the real app.

Run as a script (not collected by pytest), in a fresh process, for the same
reason as the other app drivers here: Streamlit's secrets are a process-wide
singleton, so a test that loaded a secrets file earlier in the same run leaves
its encryption key behind.

Usage:  python tests/_delete_survey_app.py <working-dir>
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


def labels_of(app, kind: str) -> list:
    return [str(getattr(w, "label", "")) for w in getattr(app, kind)]


def main(workdir: str) -> int:
    os.makedirs(workdir, exist_ok=True)
    os.chdir(workdir)

    from cryptography.fernet import Fernet
    key = Fernet.generate_key().decode()
    os.environ.update(FERNET_KEY=key, VAULT_BACKEND="local",
                      VAULT_FOLDER="delete-survey")

    import pandas as pd
    from peerparley import survey
    from peerparley.vault import Vault

    cdf = pd.DataFrame([
        {"First Name": "Ann", "Last Name": "Apple", "Email": "a@x.edu", "Team": "1"},
        {"First Name": "Bob", "Last Name": "Brown", "Email": "b@x.edu", "Team": "1"},
    ])
    vault = Vault()
    # Three surveys: one with responses, one empty, one owned by somebody else.
    for ev, owner in (("1", "admin"), ("2", "admin"), ("3", "someone-else")):
        slug, _ = survey.save_setup("QA101", ev, cdf, dict(survey.DEFAULT_SURVEY),
                                    vault=vault, owner=owner)
        if ev == "1":
            for pos in range(2):
                survey.save_response(vault, slug, "1", pos,
                                     {"pos": pos, "team": "1", "members": {}})
    vault.refresh()
    check("three surveys to start with",
          len(survey.list_surveys(vault)) == 3)

    from streamlit.testing.v1 import AppTest

    def boot(pick: str | None = None, state: dict | None = None):
        app = AppTest.from_file(os.path.join(REPO, "app.py"), default_timeout=180)
        app.secrets["fernet_key"] = key
        app.secrets["app_password_sha256"] = hashlib.sha256(b"pw").hexdigest()
        app.secrets["vault"] = {"backend": "local", "folder": "delete-survey"}
        app.session_state["pp_user"] = {"user": "admin", "name": "Admin",
                                        "role": "admin", "source": "secrets"}
        if pick:
            app.session_state["survey_pick"] = pick
        for k, v in (state or {}).items():
            app.session_state[k] = v
        app.run()
        return app

    # ---- a new survey: nothing to delete, so no button ------------------
    app = boot()
    check("the page renders", not app.exception,
          str([str(e.value)[:160] for e in app.exception]))
    check("no delete button while 'New survey' is selected",
          not any("Delete this survey" in l for l in labels_of(app, "button")))

    # ---- an empty survey: one confirmation, no typing -------------------
    app = boot(pick="QA101 · Eval 2 — admin")
    check("the delete button appears for a saved survey",
          any("Delete this survey" in l for l in labels_of(app, "button")))
    [b for b in app.button if "Delete this survey" in str(b.label)][0].click().run()
    check("the confirmation names the survey",
          any("Delete QA101 · Eval 2?" in str(getattr(w, "value", ""))
              for w in app.warning),
          str([str(getattr(w, "value", ""))[:90] for w in app.warning]))
    check("no typed confirmation is demanded when nothing was collected",
          not any("Type the survey's name" in l for l in labels_of(app, "text_input")))
    go = [b for b in app.button if str(b.label) == "Delete it"]
    check("the delete action is available", bool(go) and not go[0].disabled)
    go[0].click().run()
    check("deleting raised nothing", not app.exception,
          str([str(e.value)[:160] for e in app.exception]))
    remaining = {s["slug"] for s in survey.list_surveys(Vault())}
    check("the empty survey is gone", "QA101-eval2" not in remaining, str(remaining))
    check("the others are untouched",
          {"QA101-eval1", "QA101-eval3"} <= remaining, str(remaining))
    check("the deletion is reported back",
          any("Deleted" in str(getattr(e, "value", "")) for e in app.success))

    # ---- a survey with responses: the typed confirmation ----------------
    app = boot(pick="QA101 · Eval 1 — admin")
    [b for b in app.button if "Delete this survey" in str(b.label)][0].click().run()
    check("the confirmation counts the collected responses",
          any("2 collected response(s)" in str(getattr(w, "value", ""))
              for w in app.warning),
          str([str(getattr(w, "value", ""))[:120] for w in app.warning]))
    check("it points at the backup first",
          any("encrypted backup" in str(getattr(e, "value", ""))
              for e in app.caption))
    typed = [w for w in app.text_input if "Type the survey's name" in str(w.label)]
    check("it asks for the name to be typed", bool(typed))
    go = [b for b in app.button if str(b.label) == "Delete it"]
    check("and refuses until it matches", bool(go) and go[0].disabled)

    # The wrong name does not unlock it.
    typed[0].input("QA101 · Eval 9").run()
    go = [b for b in app.button if str(b.label) == "Delete it"]
    check("a mistyped name keeps it locked", bool(go) and go[0].disabled)
    check("nothing was deleted while locked",
          "QA101-eval1" in {s["slug"] for s in survey.list_surveys(Vault())})

    # The right name does.
    typed = [w for w in app.text_input if "Type the survey's name" in str(w.label)]
    typed[0].set_value("QA101 · Eval 1").run()
    go = [b for b in app.button if str(b.label) == "Delete it"]
    check("the correct name unlocks it", bool(go) and not go[0].disabled)
    go[0].click().run()
    check("deleting with responses raised nothing", not app.exception,
          str([str(e.value)[:160] for e in app.exception]))
    left = {s["slug"] for s in survey.list_surveys(Vault())}
    check("the survey and its responses are gone",
          "QA101-eval1" not in left and not [
              n for n in Vault().list() if n.startswith("resp__QA101-eval1__")],
          str(sorted(Vault().list())))

    # ---- somebody else's survey: not even offered ----------------------
    # An instructor sees only what they own, so the stronger guarantee is that
    # another instructor's survey is not in their list at all — there is nothing
    # to click. (The control also refuses on access, which is belt and braces if
    # the visibility rule ever loosens; that is asserted as a unit test.)
    app = boot(pick="QA101 · Eval 3 — someone-else",
               state={"pp_user": {"user": "chris", "name": "Chris",
                                  "role": "instructor", "source": "vault"}})
    picker = [w for w in app.selectbox if str(getattr(w, "label", "")) == "Survey"]
    options = list(getattr(picker[0], "options", [])) if picker else []
    check("another instructor's survey is not in their list",
          not any("Eval 3" in str(o) for o in options), str(options))
    check("so there is no delete button to press",
          not any("Delete this survey" in l for l in labels_of(app, "button")))
    check("it is still there",
          "QA101-eval3" in {s["slug"] for s in survey.list_surveys(Vault())})

    print("FAILURES:", FAILURES or "none")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
