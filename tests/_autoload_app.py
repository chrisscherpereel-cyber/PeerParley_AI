"""Driver: an evaluation's responses are loaded when it is opened.

Reported: *"I should not have to reload responses in step 2 if I have done it
already when I reenter the application"*.

Not named test_* on purpose: pytest must not collect it. It runs in a fresh
process, because Streamlit's secrets are a process-wide singleton.

Usage:  python tests/_autoload_app.py <working-dir>
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
                      VAULT_FOLDER="autoload")

    import pandas as pd
    from peerparley import survey
    from peerparley import vault as vmod
    from peerparley.vault import Vault

    vault = Vault()
    rows = [{"First Name": f, "Last Name": "Lee", "Email": f"{f.lower()}@x.edu",
             "Team": "1"} for f in ("Ann", "Bob", "Cid", "Dan")]

    def seed(course, owner, answer=3):
        slug, saved = survey.save_setup(course, "1", pd.DataFrame(rows),
                                        dict(survey.DEFAULT_SURVEY),
                                        vault=vault, owner=owner)
        members = saved["1"]
        for me in members[:answer]:
            survey.save_response_by_id(vault, slug, str(me["id"]), {
                "slug": slug, "team": "1", "evaluator": me["name"],
                "members": {str(m["id"]): {"alloc": 33, "ratings": [5] * 4,
                                           "contribution": "ok"}
                            for m in members if m["id"] != me["id"]}})
        return slug, members

    s1, members = seed("AL101", "admin")
    s_other, _ = seed("AL999", "jdoe")
    empty, _ = seed("AL555", "admin", answer=0)

    from streamlit.testing.v1 import AppTest

    def boot(user=None, **state):
        vmod.clear_cache()
        app = AppTest.from_file(os.path.join(REPO, "app.py"), default_timeout=600)
        app.secrets["fernet_key"] = key
        app.secrets["app_password_sha256"] = hashlib.sha256(b"pw").hexdigest()
        app.secrets["vault"] = {"backend": "local", "folder": "autoload"}
        app.session_state["pp_user"] = user or {
            "user": "admin", "name": "Admin", "role": "admin", "source": "secrets"}
        app.session_state["inv_url"] = "https://pp.example"
        for k, v in state.items():
            app.session_state[k] = v
        app.run()
        return app

    def text(app):
        return " ".join(str(getattr(m, "value", "")) for m in
                        list(app.info) + list(app.success) + list(app.markdown))

    # ---- a new visit: open the evaluation, and it is ready to grade -------
    app = boot(new_course="AL101", new_eval="1")
    check("the page renders", not app.exception,
          str([str(e.value)[:200] for e in app.exception]))
    check("responses are loaded without pressing anything",
          "long_df" in app.session_state and
          app.session_state["loaded_slug"] == s1)
    check("and graded", bool(app.session_state["teams"]))
    check("it says so on tab ②", "Loaded all 3 submission(s)" in text(app),
          text(app)[:300])
    labels = [str(b.label) for b in app.button]
    check("the button becomes Reload", "🔄 Reload responses" in labels,
          str(labels)[:300])
    check("and the load is remembered with the evaluation",
          survey.load_grading(vault, s1).get("loaded", {}).get("count") == 3)

    # ---- a late submission, then the instructor comes back ----------------
    me = members[3]
    survey.save_response_by_id(vault, s1, str(me["id"]), {
        "slug": s1, "team": "1", "evaluator": me["name"],
        "members": {str(m["id"]): {"alloc": 33, "ratings": [5] * 4,
                                   "contribution": "ok"}
                    for m in members if m["id"] != me["id"]}})
    app = boot(new_course="AL101", new_eval="1")
    shown = text(app)
    check("coming back loads the new submission too",
          app.session_state["loaded_submissions"] == 4)
    check("and says it arrived after the last load",
          "1 arrived after you last loaded" in shown, shown[:400])

    # ---- what must not be loaded automatically -----------------------------
    app = boot(user={"user": "admin2", "name": "Other", "role": "instructor",
                     "source": "accounts"}, new_course="AL999", new_eval="1")
    check("someone else's evaluation is not loaded", "long_df" not in app.session_state)
    app = boot(new_course="AL555", new_eval="1")
    check("an evaluation with no submissions loads nothing",
          "long_df" not in app.session_state and not app.exception)
    import pandas as _pd
    preset = _pd.DataFrame([{"evaluator": "X", "evaluator_key": "x",
                             "evaluator_team": "9", "evaluatee": "Y",
                             "evaluatee_key": "y", "points": 50,
                             "public_comment": ""}])
    app = boot(new_course="AL101", new_eval="1", long_df=preset)
    check("data brought in another way is never replaced",
          app.session_state["long_df"].equals(preset))

    print("FAILURES:", FAILURES or "none")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
