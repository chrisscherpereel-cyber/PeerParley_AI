"""Driver: start Evaluation 2 from Evaluation 1 in the real app.

Requested: *"If I want to send a second round of peer evaluations built on the
current student team setup can I just change the evaluation number from 1 to 2
and everything will work?"* — it did not: Eval 2 opened empty. Then: *"create a
cleaner fix"*.

Not named test_* on purpose: pytest must not collect it. It runs in a fresh
process, because Streamlit's secrets are a process-wide singleton.

Usage:  python tests/_new_round_app.py <working-dir>
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
                      VAULT_FOLDER="new-round")

    import pandas as pd
    from peerparley import survey
    from peerparley.vault import Vault

    rows = [{"First Name": f, "Last Name": "Lee", "Email": f"{f.lower()}@x.edu",
             "Team": t} for f, t in [("Ann", "1"), ("Bob", "1"), ("Cat", "1"),
                                      ("Dan", "2"), ("Eve", "2"), ("Fay", "2")]]
    vault = Vault()
    custom = dict(survey.DEFAULT_SURVEY, ask_confidential=False,
                  intro="Our own intro.", closes_at="2026-10-01T17:00:00")
    slug1, _ = survey.save_setup("MGT 301", "1", pd.DataFrame(rows), custom,
                                 vault=vault, owner="admin")
    # A change made in the app after the spreadsheet: Cat moves to team 2.
    cat = next(m for m in survey.roster_now(vault, slug1)["teams"]["1"]
               if m["name"].startswith("Cat"))
    survey.move_student(vault, slug1, cat["id"], "2")
    survey.save_response_by_id(vault, slug1, cat["id"], {
        "slug": slug1, "team": "2", "evaluator": "Cat Lee", "members": {}})

    from streamlit.testing.v1 import AppTest

    def boot(**state):
        app = AppTest.from_file(os.path.join(REPO, "app.py"), default_timeout=600)
        app.secrets["fernet_key"] = key
        app.secrets["app_password_sha256"] = hashlib.sha256(b"pw").hexdigest()
        app.secrets["vault"] = {"backend": "local", "folder": "new-round"}
        app.session_state["pp_user"] = {"user": "admin", "name": "Admin",
                                        "role": "admin", "source": "secrets"}
        app.session_state["inv_url"] = "https://pp.example"
        for k, v in state.items():
            app.session_state[k] = v
        app.run()
        return app

    def text(app):
        return " ".join(str(getattr(m, "value", "")) for m in
                        list(app.markdown) + list(app.caption) + list(app.success))

    # ---- the offer -------------------------------------------------------
    app = boot(new_course="MGT 301", new_eval="2")
    check("Eval 2 renders", not app.exception,
          str([str(e.value)[:200] for e in app.exception]))
    check("an earlier round is offered", "from an earlier round" in text(app))
    pick = [s for s in app.selectbox if s.key == "round_src"]
    check("Eval 1 of the same course is the default",
          bool(pick) and "MGT 301 · Eval 1" in str(pick[0].options[0]),
          str(pick[0].options if pick else None))

    # Saved surveys and other courses: no offer where it makes no sense.
    app1 = boot(new_course="MGT 301", new_eval="1")
    check("not offered on a survey that is already saved",
          "from an earlier round" not in text(app1))
    app0 = boot()
    check("not offered before a course is typed",
          "from an earlier round" not in text(app0))

    # ---- the copy --------------------------------------------------------
    next(b for b in app.button if b.key == "round_copy").click().run()
    check("the copy runs cleanly", not app.exception,
          str([str(e.value)[:200] for e in app.exception]))
    shown = text(app)
    check("it says what it did", "Started **MGT 301 · Evaluation 2**" in shown
          and "2 team(s), 6 student(s)" in shown, shown[:400])

    boxes = {str(c.label): c.value for c in app.checkbox}
    check("the same page switches to the copied settings",
          boxes.get("Confidential note to the instructor") is False)

    slug2 = survey.slugify("MGT 301", "2")
    snap2 = survey.roster_now(vault, slug2)
    t2 = {t: sorted(m["name"] for m in ms) for t, ms in snap2["teams"].items()}
    check("the teams come across as edited, not as uploaded",
          t2 == {"1": ["Ann Lee", "Bob Lee"],
                 "2": ["Cat Lee", "Dan Lee", "Eve Lee", "Fay Lee"]}, str(t2))
    ids1 = {m["id"] for ms in survey.roster_now(vault, slug1)["teams"].values()
            for m in ms}
    ids2 = {m["id"] for ms in snap2["teams"].values() for m in ms}
    check("every student has a new id, so new links", not (ids1 & ids2))
    cfg2 = survey.load_survey(vault, slug2)
    check("the questions came across",
          cfg2["ask_confidential"] is False and cfg2["intro"] == "Our own intro.")
    check("the dates did not", cfg2["closes_at"] == "" and cfg2["opens_at"] == "")
    check("no responses came across", survey.response_count(vault, slug2) == 0)
    check("Eval 1 is untouched", survey.response_count(vault, slug1) == 1)

    # The page now shows the new round as saved, ready for its dates.
    app = boot(new_course="MGT 301", new_eval="2")
    labels = [str(getattr(b, "label", "")) for b in app.button]
    check("the new round is ready to save its settings",
          "💾 Save these settings" in labels, str(labels)[:300])
    check("and the offer is gone", "from an earlier round" not in text(app))
    boxes = {str(c.label): c.value for c in app.checkbox}
    check("its question settings are on the page",
          boxes.get("Confidential note to the instructor") is False, str(boxes)[:300])

    print("FAILURES:", FAILURES or "none")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
