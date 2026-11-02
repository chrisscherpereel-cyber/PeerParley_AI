"""Driver for the "open a saved survey a second time" test. Run as a script.

Not named test_* on purpose: pytest must not collect it. It runs in a fresh
process, because Streamlit's secrets are a process-wide singleton — an earlier
test in the same process that loads a `secrets.toml` leaves its encryption key
behind, and this test would then seed a vault with one key and read it with
another. That is a property of the test harness, not of the app, and the
cleanest way around it is a clean process.

Usage:  python tests/_second_visit_app.py <working-dir>
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


def main(workdir: str) -> int:
    os.makedirs(workdir, exist_ok=True)
    os.chdir(workdir)

    from cryptography.fernet import Fernet
    key = Fernet.generate_key().decode()
    os.environ.update(FERNET_KEY=key, VAULT_BACKEND="local",
                      VAULT_FOLDER="second-visit")
    os.environ.pop("TOKEN_SECRET", None)

    import pandas as pd
    from peerparley import survey
    from peerparley.vault import Vault

    roster = [("Ann", "Apple"), ("Bob", "Brown"), ("Cid", "Cole"), ("Dee", "Diaz")]
    cdf = pd.DataFrame([
        {"First Name": f, "Last Name": l, "Email": f"{f.lower()}@example.edu",
         "Team": "1"} for f, l in roster])

    # Visit one: the instructor uploads the list and saves.
    slug, teams = survey.save_setup("QA101", "1", cdf,
                                    dict(survey.DEFAULT_SURVEY),
                                    vault=Vault(), owner="admin")
    check("visit one saved the roster", sum(len(v) for v in teams.values()) == 4)

    # A schedule and a question choice the instructor made on visit one. The
    # page must come back showing these, not the factory defaults — otherwise
    # "Save these settings" writes defaults over them.
    first_cfg = dict(survey.DEFAULT_SURVEY)
    first_cfg["closes_at"] = "2026-12-05T17:00:00"
    first_cfg["ask_confidential"] = False
    first_cfg["title"] = "MGT 301 peer evaluation"
    first_cfg[survey.CLOCK_KEY] = -420
    survey.save_config("QA101", "1", first_cfg, Vault())

    # Visit two: a new session, nothing in the uploader.
    from streamlit.testing.v1 import AppTest
    app = AppTest.from_file(os.path.join(REPO, "app.py"), default_timeout=180)
    app.secrets["fernet_key"] = key
    app.secrets["app_password_sha256"] = hashlib.sha256(b"pw").hexdigest()
    app.secrets["vault"] = {"backend": "local", "folder": "second-visit"}
    app.session_state["pp_user"] = {"user": "admin", "name": "Admin",
                                    "role": "admin", "source": "secrets"}
    app.session_state["new_course"] = "QA101"
    app.session_state["new_eval"] = "1"
    app.session_state["inv_url"] = "https://pp.example"
    app.run()
    check("the page renders without an exception", not app.exception,
          str([str(e.value)[:160] for e in app.exception]))

    saves = [b for b in app.button if "Save" in str(getattr(b, "label", ""))]
    check("the save button is on the page", bool(saves))
    if saves:
        check("the save button is not greyed out", not saves[0].disabled,
              str(saves[0].label))

    sends = [s for s in app.selectbox
             if "send" in str(getattr(s, "label", "")).lower()]
    check("the send section rendered", bool(sends))
    check("the student links were rebuilt",
          any("Preview 4 links" in str(getattr(e, "label", ""))
              for e in app.expander))
    check("the saved roster is recognised on screen",
          any("Saved earlier" in str(getattr(e, "value", ""))
              for e in app.success))

    # Change the schedule and save, still with nothing uploaded.
    # What was saved is what is on screen.
    loaded = app.session_state["survey_cfg"] if "survey_cfg" in app.session_state else {}
    check("the saved closing date came back",
          loaded.get("closes_at") == "2026-12-05T17:00:00", str(loaded.get("closes_at")))
    check("the saved question choices came back",
          loaded.get("ask_confidential") is False)
    check("the saved wording came back",
          loaded.get("title") == "MGT 301 peer evaluation", str(loaded.get("title")))
    check("the saved scheduling clock came back",
          loaded.get(survey.CLOCK_KEY) == -420, str(loaded.get(survey.CLOCK_KEY)))

    closes = [c for c in app.checkbox
              if str(getattr(c, "label", "")) == "Set a CLOSE date"]
    check("the close-date control is available", bool(closes))
    if closes and saves and not saves[0].disabled:
        closes[0].check().run()
        saves = [b for b in app.button if "Save" in str(getattr(b, "label", ""))]
        saves[0].click().run()
        check("saving the schedule change raised nothing", not app.exception,
              str([str(e.value)[:160] for e in app.exception]))
        stored = survey.load_survey(Vault(), slug)
        check("the new closing date is stored", bool(stored.get("closes_at")),
              str(stored.get("closes_at")))
        check("saving kept the other settings too",
              stored.get("ask_confidential") is False
              and stored.get("title") == "MGT 301 peer evaluation"
              and stored.get(survey.CLOCK_KEY) == -420,
              f"confidential={stored.get('ask_confidential')} "
              f"title={stored.get('title')!r} clock={stored.get(survey.CLOCK_KEY)}")
        names = [m["name"] for m in survey.saved_teams(Vault(), slug).get("1", [])]
        check("the roster the links point at is untouched",
              names == sorted(f"{f} {l}" for f, l in roster), str(names))

    print("FAILURES:", FAILURES or "none")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
