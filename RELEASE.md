# Release checklist

What was validated before 2.10.0, and what to re-run before any future
release. Everything here is a command, so the answer is never a matter of
opinion.

## 1. The checks (all offline — no network, no email, no AI request)

```bash
python3 -m pip install -r requirements.txt -r requirements-dev.txt
python3 -m pytest tests/ -q          # expect: 233 passed
python3 qa_regression.py             # expect: 93/93 checks passed
python3 selfcheck.py                 # expect: exit 0 on a configured install
```

| Suite | What it proves |
|---|---|
| `tests/test_feedback_ai.py` | the AI writer's grounding, approval gate, persistence and error handling, against fakes |
| `tests/test_survey_end_to_end.py` | the primary path: signed link → student form → encrypted response → grading → PDF, with a real vault in a temp folder |
| `tests/test_first_run.py` | a brand-new installation: the setup screen, the file it writes, signing in with the chosen password, the dependency bounds, and that no secret or student file is in the repository |
| `tests/test_delivery_and_clock.py` | the delivery defaults, that the review option is offered on every method and that the auto-send pack's review mode writes drafts instead of sending, the detected app address, and the scheduling clock |
| `qa_regression.py` | the real app driven headless through Streamlit's test runner: grading, PDFs, email packs, the AI review panel, the setup gate |

## 2. The install path a new user takes

Validated on a clean virtual environment containing **only**
`requirements.txt` — no optional extras:

- every module imports;
- the app boots with no exception;
- a feature whose optional package is absent names the package
  (`Anthropic Claude needs the anthropic package: pip install anthropic`)
  rather than raising;
- the app boots on two Streamlit versions (1.66 and current), which is how the
  deprecated `st.components.v1.html` call was caught — it is now `st.iframe`
  where available, with the old call as the fallback.

## 3. First contact, in every setup state

Checked with Streamlit's test harness, counting what is actually on the page:

| State | Before | Now |
|---|---|---|
| No secrets at all | a red box quoting a shell command, **0 fields, 0 buttons** | setup screen: password, generated key, written or pasteable settings |
| Password but no encryption key | signs in, then fails at the first save | setup screen explains and completes the setup |
| Fully configured | sign-in form | sign-in form — the setup screen never appears |

The setup screen renders **only** in the two states where the app cannot work
at all, refuses to overwrite an existing `secrets.toml`, and is not offered as
a file-write on a hosted server (where the file would not survive a restart).

## 4. Before you tag a release

- [ ] All three commands in §1 pass on a clean virtual environment.
- [ ] `python3 selfcheck.py` on a configured install exits 0.
- [ ] Version bumped in `peerparley/__init__.py`, with a `CHANGELOG.md` entry.
- [ ] Test counts in `README.md`, `DEPLOYMENT.md` and `RELEASE.md` match the
      actual suites (`test_the_python_floor_is_declared_and_enforced` and
      friends keep the documentation honest, but the counts are manual).
- [ ] `git status` is clean and `git ls-files` contains no `secrets.toml`, no
      `vault_cache/`, no `.ppx`/`.ppj`, and no real roster or export. The last
      test in `tests/test_first_run.py` asserts this; the repository is public.
- [ ] Dependency bounds still hold: if a dependency has released a new major
      version, raise the ceiling deliberately and re-run §1.

## 5. Known limits, stated rather than hidden

- **`local` storage is for trying it out.** The files are encrypted, but they
  sit beside the app and a hosted restart can wipe them. Real student records
  belong in Microsoft 365, Dropbox or pCloud (`DEPLOYMENT.md`).
- **The encryption key is not recoverable.** Lose it and everything already
  stored is unreadable, by anybody. The setup screen says so; keep a copy in a
  password manager.
- **Python 3.10 or newer.** Enforced at import with a readable message rather
  than a type error from a dataclass annotation.
- **Email sending needs either a Microsoft 365 app registration or SMTP
  credentials.** Without them PeerParley still builds ready-to-send email
  files, so a new user is never blocked — they send from their own mail client.
- **The AI writer is optional, off by default, and off again at every
  sign-in.** It needs a provider SDK and an API key; without them the feature
  is simply not offered.
- **Timezone.** Survey open and close times are read against the clock set in
  the schedule panel, which starts from the browser's own time zone and is
  stored with the survey. Set it once per evaluation; surveys saved before
  2.11.0 continue to use the server's clock, which on Streamlit Cloud is UTC.
- **Sending email.** The default method builds scripts that drive the Outlook
  or Mail on the instructor's own computer, and by default those scripts write
  drafts rather than sending. Every method offers that review step, so nothing
  reaches a student without a second, deliberate action.
