"""Driver: team edits must survive "Save", and a new survey starts empty.

Reported: *"when I save and make links live the changes I made to teams do not
take (it goes back to the original teams from the spreadsheet). Also when I
create a new survey it keeps the existing uploaded teams spreadsheet and that
should be blank for a new survey."*

One cause for both. After the first save the spreadsheet stayed in the
uploader, so every later press of Save rebuilt the teams from it — undoing every
move, addition and removal made in the Teams editor. And the uploader kept the
same widget key across surveys, so the previous survey's file was still in it
when the instructor started a new one.

The upload goes through the real `file_uploader`, using the test harness's
own upload support, so this exercises exactly what the instructor clicks.

Not named test_* on purpose. Fresh process, for Streamlit's secrets.

Usage:  python tests/_save_after_edit_app.py <working-dir>
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
                      VAULT_FOLDER="save-after-edit")
    import pandas as pd
    from peerparley import survey
    from peerparley.vault import Vault
    from peerparley import vault as vault_mod
    from streamlit.testing.v1 import AppTest

    def uploader(app):
        found = [u for u in app.get("file_uploader")
                 if str(getattr(u, "key", "") or "").startswith("survey_roster")]
        return found[0] if found else None

    sheet = pd.DataFrame([
        {"First Name": f, "Last Name": l, "Email": f"{f.lower()}@x.edu",
         "Team": t}
        for f, l, t in [("Ann", "Apple", "1"), ("Bob", "Brown", "1"),
                        ("Cid", "Cole", "1"), ("Dee", "Diaz", "2"),
                        ("Eve", "Ellis", "2"), ("Finn", "Ford", "2")]])

    app = AppTest.from_file(os.path.join(REPO, "app.py"), default_timeout=600)
    app.secrets["fernet_key"] = key
    app.secrets["app_password_sha256"] = hashlib.sha256(b"pw").hexdigest()
    app.secrets["vault"] = {"backend": "local", "folder": "save-after-edit"}
    app.session_state["pp_user"] = {"user": "admin", "name": "Admin",
                                    "role": "admin", "source": "secrets"}
    app.session_state["new_course"] = "SV101"
    app.session_state["new_eval"] = "1"
    app.session_state["inv_url"] = "https://pp.example"
    app.run()
    check("the page renders", not app.exception,
          str([str(e.value)[:200] for e in app.exception]))

    # ---- the instructor uploads the spreadsheet ------------------------------
    csv = sheet.to_csv(index=False).encode()
    up = uploader(app)
    check("the contact-list uploader is on the page", up is not None)
    up.upload("teams.csv", csv, "text/csv").run()
    check("the upload raises nothing", not app.exception,
          str([str(e.value)[:200] for e in app.exception]))

    def save_button():
        return [b for b in app.button
                if str(getattr(b, "label", "")).startswith("💾 Save")]

    b = save_button()
    check("the save button is offered", bool(b) and not b[0].disabled,
          str([x.label for x in b]))
    b[0].click().run()
    check("the first save raises nothing", not app.exception,
          str([str(e.value)[:200] for e in app.exception]))
    vault = Vault()
    vault_mod.clear_cache()
    snap = survey.roster_now(vault, "SV101-eval1")
    check("the spreadsheet's teams are saved",
          bool(snap) and sorted(snap["teams"]) == ["1", "2"])

    # ---- after saving, the uploader is empty ---------------------------------
    up = uploader(app)
    check("after saving, the spreadsheet no longer sits in the uploader",
          up is not None and not up.value, f"uploader holds {up.value!r}"
          if up is not None else "no uploader")
    shown = " ".join(str(getattr(e, "value", "")) for e in app.success)
    check("and the save is confirmed on screen after the page refreshes",
          "Saved" in shown, shown[:200])

    # ---- the instructor edits the teams ---------------------------------------
    ids = {m["name"]: m["id"] for _t, _p, m in survey.snapshot_members(snap)}
    survey.move_student(vault, "SV101-eval1", ids["Cid Cole"], "2")
    survey.add_student(vault, "SV101-eval1", "Gus Gold", "1", "gus@x.edu")
    survey.remove_student(vault, "SV101-eval1", ids["Finn Ford"])
    vault_mod.clear_cache()
    app.run()

    # ---- and presses Save again -------------------------------------------------
    b = save_button()
    check("Save is still offered after the edits", bool(b) and not b[0].disabled)
    label = str(b[0].label) if b else ""
    check("it is the save that keeps the saved teams",
          label == "💾 Save these settings", label)
    b[0].click().run()
    check("saving again raises nothing", not app.exception,
          str([str(e.value)[:200] for e in app.exception]))
    vault_mod.clear_cache()
    snap = survey.roster_now(vault, "SV101-eval1")
    names = {t: [m["name"] for m in ms] for t, ms in snap["teams"].items()}
    check("the move survives the save", "Cid Cole" in names.get("2", []),
          str(names))
    check("the added student survives the save", "Gus Gold" in names.get("1", []),
          str(names))
    check("the removed student stays removed",
          not any("Finn Ford" in ns for ns in names.values()), str(names))

    # ---- a new survey starts with an empty uploader ---------------------------
    # The instructor uploads a spreadsheet (and does not save it), then starts
    # a new survey by changing the evaluation number.
    uploader(app).upload("teams.csv", csv, "text/csv").run()
    check("a second upload is held", bool(uploader(app).value))
    app.text_input(key="new_eval").set_value("2").run()
    check("switching to a new survey raises nothing", not app.exception,
          str([str(e.value)[:200] for e in app.exception]))
    up = uploader(app)
    check("the new survey's uploader is empty",
          up is not None and not up.value,
          f"it still holds {up.value!r}" if up is not None else "no uploader")
    left = (app.session_state["survey_contact_df"]
            if "survey_contact_df" in app.session_state else None)
    check("no spreadsheet is carried over into the new survey", left is None)
    teams_now = [b for b in app.button
                 if str(getattr(b, "label", "")).startswith("💾 Save")]
    check("and the new survey cannot be saved until a list is uploaded",
          bool(teams_now) and teams_now[0].disabled)

    print("FAILURES:", FAILURES or "none")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
