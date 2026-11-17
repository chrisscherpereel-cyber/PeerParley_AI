"""Driver: the Start-here panel before a survey is chosen, and web-mail sending.

Requested: *"when a user logs into peer parley they should not see progress in
the start here since no survey has been selected. Is there a way to setup a
send option that will use a web based mail tool?"*

Not named test_* on purpose: pytest must not collect it. It runs in a fresh
process, because Streamlit's secrets are a process-wide singleton.

Usage:  python tests/_webmail_app.py <working-dir>
Prints one PASS/FAIL line per check and exits non-zero on any failure.
"""
from __future__ import annotations

import hashlib
import os
import sys
import urllib.parse

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
                      VAULT_FOLDER="webmail")

    import pandas as pd
    from peerparley import survey
    from peerparley.vault import Vault

    rows = [{"First Name": f, "Last Name": l,
             "Email": f"{f.lower()}@example.edu", "Team": "1"}
            for f, l in [("Ann", "Apple"), ("Bob", "Brown"), ("Cid", "Cole")]]
    vault = Vault()
    survey.save_setup("WM101", "1", pd.DataFrame(rows),
                      dict(survey.DEFAULT_SURVEY), vault=vault, owner="admin")
    # A survey saved with no course at all: its slug is the one a blank
    # sidebar produces, which is how progress used to leak onto the panel.
    survey.save_setup("", "1", pd.DataFrame(rows),
                      dict(survey.DEFAULT_SURVEY), vault=vault, owner="admin")

    from streamlit.testing.v1 import AppTest

    def boot(**state):
        app = AppTest.from_file(os.path.join(REPO, "app.py"), default_timeout=600)
        app.secrets["fernet_key"] = key
        app.secrets["app_password_sha256"] = hashlib.sha256(b"pw").hexdigest()
        app.secrets["vault"] = {"backend": "local", "folder": "webmail"}
        app.session_state["pp_user"] = {"user": "admin", "name": "Admin",
                                        "role": "admin", "source": "secrets"}
        app.session_state["inv_url"] = "https://pp.example"
        for k, v in state.items():
            app.session_state[k] = v
        app.run()
        return app

    def errs(app):
        return [str(e.value)[:200] for e in app.exception]

    # ---- 1. Signed in, nothing chosen ----------------------------------------
    app = boot()
    check("the page renders with no survey chosen", not app.exception, str(errs(app)))
    labels = [str(getattr(e, "label", "")) for e in app.expander]
    start = [s for s in labels if "Start here" in s]
    check("Start here asks for a survey", start and "choose a survey" in start[0],
          str(start))
    shown = " ".join(str(getattr(m, "value", "")) for m in app.markdown)
    check("and shows no progress", "✅" not in shown, shown[:300])

    # ---- 2. A survey chosen: progress, numbered from the first open step -----
    app = boot(new_course="WM101", new_eval="1")
    labels = [str(getattr(e, "label", "")) for e in app.expander]
    start = [s for s in labels if "Start here" in s]
    check("with a survey, Start here shows its step", start and "step" in start[0],
          str(start))
    check("the step is the first unfinished one (responses)",
          start and "step 3 of 4" in start[0], str(start))
    shown_steps = " ".join(str(getattr(m, "value", "")) for m in app.markdown)
    check("the app's address, already known, is not a step",
          "Enter this app's web address" not in shown_steps)
    shown = " ".join(str(getattr(m, "value", "")) for m in app.markdown)
    check("and ticks what is done", "✅" in shown)

    # When the address really is unknown (no browser address, none typed),
    # the step comes back: it is then what stands between saving and links.
    app = boot(new_course="WM101", new_eval="1", inv_url="")
    shown_steps = " ".join(str(getattr(m, "value", "")) for m in app.markdown)
    labels = [str(getattr(e, "label", "")) for e in app.expander]
    start = [s for s in labels if "Start here" in s]
    if "pp.example" not in shown_steps:      # the test harness has no page URL
        check("an unknown address is listed as a step",
              "Enter this app's web address" in shown_steps
              and start and "of 5" in start[0], str(start))

    # ---- 3. Invitations through web mail -------------------------------------
    app = boot(new_course="WM101", new_eval="1",
               inv_m="Open in web mail — Outlook or Gmail",
               inv_subj="Due {close_date} — {class}")
    check("the web-mail method renders", not app.exception, str(errs(app)))
    btns = [b for b in app.get("link_button")]
    urls = [b.proto.url for b in btns]
    names = [b.proto.label for b in btns]
    check("one button per student", len(urls) == 3, str(names))
    check("Outlook on the web is the default",
          urls and all(u.startswith("https://outlook.office.com/mail/deeplink/compose?")
                       for u in urls), str(urls[:1])[:200])
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(urls[0]).query) if urls else {}
    check("addressed to the student", q.get("to") == ["ann@example.edu"], str(q.get("to")))
    check("subject filled in, dates included",
          q.get("subject") == ["Due TBA — WM101"], str(q.get("subject")))
    body = (q.get("body") or [""])[0]
    check("the student's own link is in the body",
          "https://pp.example?t=" in body and "Hi Ann" in body, body[:300])
    check("and no HTML in it", "<" not in body and "&lt;" not in body, body[:200])
    boxes = [str(getattr(c, "label", "")) for c in app.checkbox]
    check("a Sent tick per student", boxes.count("Sent") == 3, str(boxes))

    app = boot(new_course="WM101", new_eval="1",
               inv_m="Open in web mail — Outlook or Gmail", inv_wm_p="Gmail")
    urls = [b.proto.url for b in app.get("link_button")]
    check("Gmail opens Gmail's composer",
          urls and all(u.startswith("https://mail.google.com/mail/?view=cm")
                       for u in urls), str(urls[:1])[:200])

    # Ticking: the count follows.
    app = boot(new_course="WM101", new_eval="1",
               inv_m="Open in web mail — Outlook or Gmail")
    next(c for c in app.checkbox if str(c.label) == "Sent").check().run()
    caps = " ".join(str(getattr(c, "value", "")) for c in app.caption)
    check("ticking one counts it", "1 of 3" in caps, caps[-300:])

    print("FAILURES:", FAILURES or "none")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
