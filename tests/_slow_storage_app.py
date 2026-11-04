"""Does the app still paint when storage is slow to answer?

Reported: "all that I am getting is the app spinner when I try to run the app."
A permanent spinner means the script never finishes, and 2.14.1 had put a
storage round trip in front of the first paint — so a provider that answers
slowly, or a credential that makes the SDK retry, meant nothing appeared at
all. Measured here with a backend that sleeps.

The rule this holds: **nothing before the sign-in screen may touch the
network.** Whether storage is reachable is a question for the sidebar, after
the app is on screen, answered from the work the app actually did.

Usage:  python tests/_slow_storage_app.py <working-dir> [seconds]
"""
from __future__ import annotations

import hashlib
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

FAILURES: list = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS" if ok else "FAIL"), "-", name, ("| " + detail) if detail else "")
    if not ok:
        FAILURES.append(name)


def main(workdir: str, delay: float = 6.0) -> int:
    os.makedirs(workdir, exist_ok=True)
    os.chdir(workdir)

    from cryptography.fernet import Fernet
    key = Fernet.generate_key().decode()
    os.environ.update(FERNET_KEY=key, VAULT_BACKEND="local",
                      VAULT_FOLDER="slow-storage")

    from peerparley.vault import LocalBackend
    calls = {"n": 0}
    for name in ("list", "get", "put"):
        real = getattr(LocalBackend, name)

        def slow(self, *a, _real=real, **kw):
            calls["n"] += 1
            time.sleep(delay)
            return _real(self, *a, **kw)

        setattr(LocalBackend, name, slow)

    from streamlit.testing.v1 import AppTest

    def boot(**state):
        app = AppTest.from_file(os.path.join(REPO, "app.py"), default_timeout=600)
        app.secrets["fernet_key"] = key
        app.secrets["app_password_sha256"] = hashlib.sha256(b"pw").hexdigest()
        app.secrets["vault"] = {"backend": "local", "folder": "slow-storage"}
        for k, v in state.items():
            app.session_state[k] = v
        started = time.perf_counter()
        app.run()
        return app, time.perf_counter() - started

    # ---- before sign-in -------------------------------------------------
    calls["n"] = 0
    app, elapsed = boot()
    page = " ".join(str(getattr(e, "value", "")) for e in app.markdown)
    check("the sign-in screen paints without waiting for storage",
          elapsed < delay, f"{elapsed:.1f}s with a {delay:.0f}s backend")
    check("and it is the sign-in screen", "Instructor sign-in" in page)
    check("no storage call was made before it", calls["n"] == 0,
          f"{calls['n']} call(s)")
    check("the app did not error", not app.exception,
          str([str(e.value)[:160] for e in app.exception]))

    # ---- after sign-in --------------------------------------------------
    # Here storage is genuinely needed, so the page waits for it — but it must
    # finish, and it must not multiply the wait by asking many times over.
    calls["n"] = 0
    app, elapsed = boot(pp_user={"user": "admin", "name": "Admin",
                                 "role": "admin", "source": "secrets"})
    check("the signed-in page finishes", not app.exception,
          str([str(e.value)[:160] for e in app.exception]))
    # Five distinct objects on a cold page — the saved session, the account
    # list, the AI settings, the survey list, the roster snapshot — each needed
    # once and cached afterwards.
    check("a cold page reads only what it needs", calls["n"] <= 6,
          f"{calls['n']} call(s) in {elapsed:.1f}s")

    # A second run is served from the read cache, so clicking around an app
    # whose storage is slow does not pay the cost again.
    calls["n"] = 0
    started = time.perf_counter()
    app.run()
    again = time.perf_counter() - started
    check("a second interaction costs no storage calls at all", calls["n"] == 0,
          f"{calls['n']} call(s) in {again:.1f}s")

    print("FAILURES:", FAILURES or "none")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    args = sys.argv[1:]
    raise SystemExit(main(args[0] if args else ".",
                          float(args[1]) if len(args) > 1 else 6.0))
