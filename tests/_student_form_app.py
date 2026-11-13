"""Drive the student form — the only public page — through every link a student
can arrive with.

It runs before the instructor's password gate, it is opened by forty people who
have never seen the app, and they have no way to report a traceback except
"the link didn't work". So every way a link can arrive is opened here, and the
form is filled in and submitted where it should be.

Links:
  fresh        the link from the invitation, today's id scheme
  legacy       a link issued before 2.16.0 (team + position)
  revisit      the same student coming back to revise
  tampered     a signature that does not match
  foreign      a valid signature for a survey that does not exist
  unassigned   a student on the roster with no team
  removed      a student taken off the roster after their link was sent
  moved        a student moved to another team after their link was sent
  closed       the evaluation has been closed
  not yet      the evaluation has not opened
  incomplete   a form with the allocation not adding up — submit stays off

Not named test_* on purpose: pytest must not collect it. Fresh process,
because Streamlit's secrets are a process-wide singleton.

Usage:  python tests/_student_form_app.py <working-dir>
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
                      VAULT_FOLDER="student-form")
    os.environ.pop("TOKEN_SECRET", None)

    import pandas as pd
    from peerparley import survey
    from peerparley.tokens import make_token
    from peerparley.vault import Vault
    from streamlit.testing.v1 import AppTest

    rows = [{"First Name": f, "Last Name": l, "Email": f"{f.lower()}@x.edu",
             "Team": t}
            for f, l, t in [("Ann", "Apple", "1"), ("Bob", "Brown", "1"),
                            ("Cid", "Cole", "1"), ("Zoë", "O'Brien", "2"),
                            ("Eve", "Ellis", "2"), ("Finn", "Ford", "2"),
                            ("Lone", "Wolf", "")]]
    vault = Vault()
    slug, teams = survey.save_setup("SF101", "1", pd.DataFrame(rows),
                                    dict(survey.DEFAULT_SURVEY),
                                    vault=vault, owner="admin")

    from peerparley.config import load_config
    secret = survey.token_secret(load_config())

    def boot(token: str):
        app = AppTest.from_file(os.path.join(REPO, "app.py"),
                                default_timeout=300)
        app.secrets["fernet_key"] = key
        app.secrets["app_password_sha256"] = hashlib.sha256(b"pw").hexdigest()
        app.secrets["vault"] = {"backend": "local", "folder": "student-form"}
        app.query_params["t"] = token
        app.run()
        return app

    def ok(app, what):
        check(f"{what}: no crash", not app.exception,
              str([str(e.value)[:200] for e in app.exception]))

    def text(app):
        bits = []
        for kind in ("markdown", "caption", "error", "info", "warning",
                     "success", "header"):
            for e in getattr(app, kind, []):
                bits.append(str(getattr(e, "value", "")))
        return " ".join(bits)

    def link(name):
        snap = survey.roster_now(vault, slug)
        mid = survey.find_member(snap, name)["id"]
        return make_token({"s": slug, "i": mid}, secret)

    def fill_and_submit(app, members, me_index):
        """Answer everything, split the $100, and press Submit."""
        cats = survey.DEFAULT_SURVEY.get("ranking_categories") \
            or survey.RANK_CATEGORIES
        for i, m in enumerate(members):
            wid = survey._wid(m, i)
            for si in range(4):
                r = [x for x in app.radio if x.key == f"r_{wid}_{si}"]
                if r:
                    r[0].set_value(r[0].options[1])
            rk = [x for x in app.radio if x.key == f"rank_{wid}"]
            if rk:
                rk[0].set_value(cats[i % len(cats)])
            for k in (f"con_{wid}", f"imp_{wid}"):
                ta = [x for x in app.text_area if x.key == k]
                if ta:
                    ta[0].input(f"{m['name']} rebuilt the model and kept us "
                                f"on schedule.")
        others = [i for i in range(len(members)) if i != me_index]
        share = 100 // len(others)
        for n, i in enumerate(others):
            wid = survey._wid(members[i], i)
            ni = [x for x in app.number_input if x.key == f"alloc_{wid}"]
            if ni:
                ni[0].set_value(share + (100 - share * len(others)
                                         if n == 0 else 0))
        app.run()
        sub = [b for b in app.button if b.key == "pp_student_submit"]
        if not sub or sub[0].disabled:
            return app, False
        sub[0].click().run()
        return app, True

    # ---- fresh link: fill in and submit --------------------------------------
    app = boot(link("Ann Apple"))
    ok(app, "fresh link")
    check("the form names the right student", "Ann Apple" in text(app))
    members = teams["1"]
    me = [i for i, m in enumerate(members) if m["name"] == "Ann Apple"][0]
    app, pressed = fill_and_submit(app, members, me)
    ok(app, "submitting")
    check("the completed form can be submitted", pressed)
    saved = survey.load_response_by_id(
        vault, slug, survey.find_member(survey.roster_now(vault, slug),
                                        "Ann Apple")["id"])
    check("the submission is stored", bool(saved))
    if saved:
        ids = {str(m["id"]) for m in members}
        check("its answers are keyed by teammate id",
              set(map(str, saved["members"])) <= ids,
              str(list(saved["members"])[:3]))
        allocs = [v.get("alloc") for k, v in saved["members"].items()
                  if v.get("alloc") is not None]
        check("the allocation totals 100", sum(allocs) == 100, str(allocs))

    # ---- revisit -------------------------------------------------------------
    app = boot(link("Ann Apple"))
    ok(app, "revisiting")
    vals = [x.value for x in app.number_input if x.key.startswith("alloc_")]
    check("a revisit shows the answers already given", sum(vals) == 100,
          str(vals))

    # ---- legacy positional link ---------------------------------------------
    pos_bob = [i for i, m in enumerate(teams["1"]) if m["name"] == "Bob Brown"][0]
    app = boot(make_token({"s": slug, "t": "1", "p": pos_bob}, secret))
    ok(app, "a pre-2.16.0 link")
    check("an old link still opens its own student's form",
          "Bob Brown" in text(app), text(app)[:160])

    # ---- tampered / foreign --------------------------------------------------
    good = link("Bob Brown")
    body, _, sig = good.partition(".")
    app = boot(f"{body}.{'0' * len(sig)}")
    ok(app, "a tampered link")
    check("a tampered link is refused politely",
          "invalid or has expired" in text(app), text(app)[:160])

    app = boot(make_token({"s": "no-such-eval1", "i": "abc"}, secret))
    ok(app, "a link for a survey that does not exist")
    check("it says the evaluation is unavailable",
          "isn't available" in text(app), text(app)[:160])

    app = boot("not-even-a-token")
    ok(app, "garbage in the link")

    # ---- unassigned ----------------------------------------------------------
    app = boot(link("Lone Wolf"))
    ok(app, "a student with no team")
    check("they are told they are not on a team yet",
          "haven't been assigned to a team" in text(app), text(app)[:200])
    check("and are not shown a form",
          not [b for b in app.button if b.key == "pp_student_submit"])

    # ---- moved after the link was sent ---------------------------------------
    eve_link = link("Eve Ellis")
    eve = survey.find_member(survey.roster_now(vault, slug), "Eve Ellis")["id"]
    survey.move_student(vault, slug, eve, "1")
    from peerparley import vault as vault_mod
    vault_mod.clear_cache()
    app = boot(eve_link)
    ok(app, "a student moved after their link was sent")
    t = text(app)
    check("the same link opens their new team's form",
          "Team 1" in t and "Ann Apple" in t, t[:200])

    # ---- removed after the link was sent -------------------------------------
    finn_link = link("Finn Ford")
    finn = survey.find_member(survey.roster_now(vault, slug), "Finn Ford")["id"]
    survey.remove_student(vault, slug, finn)
    vault_mod.clear_cache()
    app = boot(finn_link)
    ok(app, "a removed student's link")
    check("it does not open anybody's form",
          "doesn't match the current roster" in text(app), text(app)[:200])

    # ---- a team of one -------------------------------------------------------
    # Team 2 is now just Zoë: Eve moved, Finn removed.
    app = boot(link("Zoë O'Brien"))
    ok(app, "the last student on a team of one")
    check("they are told there is nobody to evaluate yet",
          "haven't been assigned to a team" in text(app), text(app)[:200])

    # ---- closed / not yet open -----------------------------------------------
    cfg = survey.load_survey(vault, slug)
    survey._save_json(vault, survey.key_survey(slug), {**cfg, "is_open": False})
    vault_mod.clear_cache()
    app = boot(link("Bob Brown"))
    ok(app, "a closed evaluation")
    check("a closed evaluation shows its closing note and no form",
          "closed" in text(app).lower()
          and not [b for b in app.button if b.key == "pp_student_submit"])

    survey._save_json(vault, survey.key_survey(slug),
                      {**cfg, "is_open": True,
                       "opens_at": "2099-01-01T09:00:00"})
    vault_mod.clear_cache()
    app = boot(link("Bob Brown"))
    ok(app, "an evaluation not yet open")
    check("it says when it opens", "opens on" in text(app), text(app)[:200])

    # ---- incomplete ----------------------------------------------------------
    survey._save_json(vault, survey.key_survey(slug), {**cfg, "is_open": True,
                                                        "opens_at": ""})
    vault_mod.clear_cache()
    app = boot(link("Cid Cole"))
    ok(app, "an untouched form")
    sub = [b for b in app.button if b.key == "pp_student_submit"]
    check("submit stays off until the form is complete",
          bool(sub) and sub[0].disabled)
    check("and it says what is missing", "Before you can submit" in text(app))

    print("FAILURES:", FAILURES or "none")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
