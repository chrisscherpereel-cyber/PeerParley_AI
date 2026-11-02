"""What one rerun costs, measured. Run as a script, not collected by pytest.

The complaint this exists for: "the application still seems very slow when I am
doing small tasks like changing the schedule." The cause was not any single
slow function — it was that Streamlit re-executes the whole script on every
click, and the script asked storage for 52 objects each time: the survey, the
roster, every one of forty responses, the account list. On a laptop that is a
fifth of a second and invisible. On Microsoft 365 or Dropbox, where each read is
an HTTPS round trip, the same click costs several seconds.

So the number this measures is **backend round trips per rerun** — counted at
the backend, below the read cache, because that is what a cloud store charges
for. It is the number that predicts the instructor's experience, and a regression
in it would not show up in any timing taken on a local disk.

Usage:  python tests/_rerun_cost.py <working-dir> [--budget N]
Prints the counts and exits non-zero if the budget is exceeded.
"""
from __future__ import annotations

import collections
import hashlib
import os
import random
import sys
import traceback

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

TEAMS, PER_TEAM = 8, 5          # 40 students, a full section
WORDS = ("model spreadsheet deadline interviews slides analysis meetings "
         "rehearsal formatting sources timeline budget research").split()


def main(workdir: str, budget: int = 4) -> int:
    os.makedirs(workdir, exist_ok=True)
    os.chdir(workdir)

    from cryptography.fernet import Fernet
    key = Fernet.generate_key().decode()
    os.environ.update(FERNET_KEY=key, VAULT_BACKEND="local",
                      VAULT_FOLDER="rerun-cost")

    import pandas as pd
    from peerparley import survey
    from peerparley.vault import LocalBackend, Vault

    rows = [{"First Name": f"S{t}{i}", "Last Name": f"L{t}{i}",
             "Email": f"s{t}{i}@example.edu", "Team": str(t)}
            for t in range(TEAMS) for i in range(PER_TEAM)]
    slug, teams = survey.save_setup("PERF101", "1", pd.DataFrame(rows),
                                    dict(survey.DEFAULT_SURVEY),
                                    vault=Vault(), owner="admin")

    random.seed(11)
    def sentence() -> str:
        return " ".join(random.choice(WORDS)
                        for _ in range(random.randint(14, 26))) + "."

    vault = Vault()
    for team, members in teams.items():
        for pos, me in enumerate(members):
            answers = {}
            for i, _m in enumerate(members):
                entry = {"ratings": [5, 5, 6, 5], "rank": "Adequate performer",
                         "contribution": sentence(), "improve": sentence()}
                if i != pos:
                    entry["alloc"] = 100 // (len(members) - 1)
                answers[str(i)] = entry
            survey.save_response(vault, slug, team, pos, {
                "slug": slug, "team": team, "pos": pos,
                "evaluator": me["name"],
                "evaluator_key": survey.name_key(me["name"]),
                "submitted": "2026-11-02T12:00:00+00:00",
                "members": answers, "self_contribution": sentence(),
                "confidential": ""})

    # Count what reaches the store, and remember who asked.
    hits: collections.Counter = collections.Counter()
    askers: collections.Counter = collections.Counter()

    def counted(label, fn):
        def inner(self, *a, **kw):
            hits[label] += 1
            for frame in reversed(traceback.extract_stack()[:-1]):
                if "PeerParley" in frame.filename and "vault.py" not in frame.filename:
                    askers[f"{os.path.basename(frame.filename)}:{frame.lineno}"] += 1
                    break
            return fn(self, *a, **kw)
        return inner

    for name in ("get", "put", "list", "delete"):
        setattr(LocalBackend, name, counted(name, getattr(LocalBackend, name)))

    from streamlit.testing.v1 import AppTest
    app = AppTest.from_file(os.path.join(REPO, "app.py"), default_timeout=300)
    app.secrets["fernet_key"] = key
    app.secrets["app_password_sha256"] = hashlib.sha256(b"pw").hexdigest()
    app.secrets["vault"] = {"backend": "local", "folder": "rerun-cost"}
    for k, v in {
            "pp_user": {"user": "admin", "name": "A", "role": "admin",
                        "source": "secrets"},
            "new_course": "PERF101", "new_eval": "1",
            "inv_url": "https://pp.example",
            "long_df": survey.responses_to_long(vault, slug),
            "self_evals": survey.self_evaluations(vault, slug),
            "roster": survey.roster_for_matching(
                survey.load_roster_snapshot(vault, slug))}.items():
        app.session_state[k] = v

    app.run()                                   # first paint: warms the caches
    if app.exception:
        print("FAIL - the app raised:", [str(e.value)[:200] for e in app.exception])
        return 1
    hits.clear(); askers.clear()

    app.run()                                   # the rerun being measured
    steady = sum(hits.values())
    print(f"storage round trips for one steady-state rerun: {steady}")
    for where, n in askers.most_common(8):
        print(f"   {n:3d}  {where}")
    for latency_ms in (50, 150, 300):
        print(f"   at {latency_ms}ms per call: "
              f"{steady * latency_ms / 1000:.1f}s of waiting per click")

    # The interaction the complaint named.
    hits.clear(); askers.clear()
    boxes = [c for c in app.checkbox
             if str(getattr(c, "label", "")) == "Set a CLOSE date"]
    if boxes:
        boxes[0].check().run()
        schedule = sum(hits.values())
        print(f"storage round trips for a schedule change: {schedule}")
    else:
        schedule = 0
        print("note: the close-date control was not on the page")

    worst = max(steady, schedule)
    ok = worst <= budget and not app.exception
    print(("PASS" if ok else "FAIL"),
          f"- one rerun stays within {budget} storage round trip(s) (worst: {worst})")
    return 0 if ok else 1


if __name__ == "__main__":
    args = sys.argv[1:]
    budget = 4
    if "--budget" in args:
        budget = int(args[args.index("--budget") + 1])
        args = args[:args.index("--budget")]
    raise SystemExit(main(args[0] if args else ".", budget))
