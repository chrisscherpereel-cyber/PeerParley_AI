# Release checklist

What was validated before 2.17.0, and what to re-run before any future
release. Everything here is a command, so the answer is never a matter of
opinion.

## 1. The checks (all offline — no network, no email, no AI request)

```bash
python3 -m pip install -r requirements.txt -r requirements-dev.txt
python3 -m pytest tests/ -q          # expect: 427 passed
python3 qa_regression.py             # expect: 95/95 checks passed
python3 selfcheck.py                 # expect: exit 0 on a configured install
```

### 1b. The release gate — before tagging, not on every commit

```bash
python3 tests/_every_state_app.py /tmp/ppstate   # ~4 min; expect FAILURES: none
```

The full crash matrix: seven states an instructor can reach, and in each one
every button pressed, every checkbox and toggle switched on, and every option
of every dropdown chosen — each from a freshly restored copy of the same data,
so one press cannot leak into the next. The suite runs the two richest states
of it on every commit; this runs all seven. At 2.17.0: 320 renders, 67 buttons,
51 checkboxes and toggles, 168 dropdown choices, no failures, on both the dev
environment and a clean install of `requirements.txt` (which pulled Streamlit
1.67 and pandas 3.1, newer than the dev environment).

| Suite | What it proves |
|---|---|
| `tests/test_feedback_ai.py` | the AI writer's grounding, approval gate, persistence and error handling, against fakes |
| `tests/test_survey_end_to_end.py` | the primary path: signed link → student form → encrypted response → grading → PDF, with a real vault in a temp folder |
| `tests/test_first_run.py` | a brand-new installation: the setup screen, the file it writes, signing in with the chosen password, the dependency bounds, and that no secret or student file is in the repository |
| `tests/test_delivery_and_clock.py` | the delivery defaults, that the review option is offered on every method and that the auto-send pack's review mode writes drafts instead of sending, the detected app address, and the scheduling clock |
| `qa_regression.py` | the real app driven headless through Streamlit's test runner: grading, PDFs, email packs, the AI review panel, the setup gate |
| `tests/test_results_reports.py` | the results table and the reports: that every column has an explanation and no explanation outlives its column, that the grade columns actually differentiate students on default settings, that a flat column names its own cause, and that three report previews on one page do not collide |
| `tests/_results_tab_app.py` | the real Results tab with both preview boxes ticked, run twice — with PyMuPDF and without, which is how a Streamlit Cloud deployment is configured. The duplicate-widget-id bug was in the *page*: the PDF bytes were always correct |
| `tests/_restore_defaults_app.py` | pressing **Restore defaults** on the Grading rules tab, and failing unless the controls themselves come back — none of them carries a Streamlit `key`, so widget identity comes from each one's own arguments, which is a property of the framework and has to be exercised through it |
| `tests/test_roster_edit.py` | permanent student ids: that they survive a re-upload and a corrected name, that migrating a positional survey grades identically and leaves nothing double-counted, that an old link resolves to its own student and a renumbered seat resolves to nobody, and that each edit discards only the answers that stopped applying |
| `tests/_roster_edit_app.py` | adding, moving and removing students in the real app, failing unless the confirmation appears before a costly edit and every other student's link is byte-for-byte unchanged afterwards |
| `tests/test_email_editor.py` | that the email body is written with a toolbar rather than as HTML, that every send path previews the message rendered with a real student's details, that a placeholder nothing fills in is flagged, and that reminders carry the student's permanent id |
| `tests/_email_editor_app.py` | the editor and its previews in the real app, run twice — with the toolbar package and without it, which is how every Streamlit Cloud deployment was configured |
| `tests/test_release_hygiene.py` | pyflakes over the whole tree; that every stateful widget inside a function carries a key (the duplicate-id class, which has reached a release twice); that every `icon=` is one Streamlit accepts (an invalid one shipped in the AI writer); and that every module compiles |
| `tests/test_contact_lists.py` | contact lists, including `assets/ContactList_Template.xlsx` as shipped and as Excel saves it once filled in; the formats spreadsheets really save in (cp1252, UTF-16, semicolons, a roster on sheet 2); that a missing value is never a student called "nan"; that team labels read "1", not "1.0"; and that the team column is never a count or a member column |
| `tests/test_crash_matrix.py` | runs the crash matrix (two richest states), the student-form matrix, and the upload fuzz from the suite |
| `tests/_every_state_app.py` | the full crash matrix described in §1b |
| `tests/_student_form_app.py` | the public student page, opened with every kind of link a student can arrive with — fresh, pre-2.16.0, tampered, garbage, for a survey that does not exist, for a student since moved or removed, for a closed or not-yet-open evaluation — and filled in and submitted where it should be |
| `tests/_fuzz_inputs.py` | thirty malformed and awkward contact lists and Qualtrics exports through every path an upload takes, to the PDFs: a bad file may produce a message, never a traceback |
| `tests/test_accounts_and_reports.py` | instructor accounts (sign-in, wrong and short passwords, reset, deactivate, remove, reserved names, salted hashes, no published default password), the team and comparison PDFs, and that the setup screen can never hand out a replacement encryption key |
| `tests/_compare_app.py` | the Compare tab with three real rounds — a student who changes team, one who joins late, one written "Last, First" — and that the comparison is built once, not on every click |
| `tests/_rerun_cost.py` | what one rerun costs in storage round trips, with a 40-student section loaded — the number that decides whether the app feels fast on a cloud store, and the one a local timing cannot see |

## 1a. Performance, measured rather than felt

```bash
python3 tests/_rerun_cost.py /tmp/ppcost     # expect: 0–4 round trips, PASS
```

Streamlit re-runs the whole script on every click, so the question is not how
fast a function is but how much work a rerun repeats. The number that matters is
**storage round trips per rerun**: on a laptop each one is free, and on Microsoft
365 or Dropbox each is an HTTPS request of 100–300ms. One rerun of a 40-student
section cost 52 of them before 2.12.0 and costs 0 now. If a change makes the app
feel slow again, run this first.

## 1c. Every advertised feature, and what proves it

| Feature (README) | Proved by |
|---|---|
| Contact list upload, incl. the shipped template | `test_contact_lists.py`, `_fuzz_inputs.py`, `test_survey_end_to_end.py` |
| Personal links, signed and permanent | `test_survey_end_to_end.py`, `test_roster_edit.py`, `_student_form_app.py` |
| Teams editor: add, move, remove, rename, dissolve | `test_roster_edit.py`, `_roster_edit_app.py`, matrix state G |
| Qualtrics import | `qa_regression.py`, `_fuzz_inputs.py`, matrix state E |
| Response status, reminders, load into grading | matrix states B–C, `test_email_editor.py` |
| Grading rules, restore defaults | `test_metrics.py`, `test_results_reports.py`, `_restore_defaults_app.py` |
| Results table, column help, previews | `test_results_reports.py`, `_results_tab_app.py` |
| Instructor summary, confidential, student, team, comparison PDFs | `test_results_reports.py`, `test_accounts_and_reports.py`, `qa_regression.py` |
| AI feedback writer | `test_feedback_ai.py`, `qa_regression.py` |
| Email: editor, previews, every delivery method | `test_email_editor.py`, `_email_editor_app.py`, `test_delivery_and_clock.py`, matrix (every method on every panel) |
| Compare | `_compare_app.py`, `test_accounts_and_reports.py` |
| Vault save / resume | `qa_regression.py` |
| Survey deletion | `_delete_survey_app.py` |
| Instructor accounts | `test_accounts_and_reports.py` |
| First-run setup, storage problems | `test_first_run.py`, `_missing_backend_app.py`, `_slow_storage_app.py` |
| The student form | `_student_form_app.py`, `test_survey_end_to_end.py` |

What cannot be proved offline: delivery through a real Microsoft 365 tenant or
SMTP server, and a real AI provider. Both are exercised against fakes, and with
nothing configured the matrix presses every send button and requires a message
rather than a traceback.

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

- **A package any user-facing feature needs must be in `requirements.txt`,**
  not in `requirements-optional.txt`: Streamlit Community Cloud installs only
  the first file. An optional *storage* package meant a deployment that could
  not open its own storage (2.10.0); an optional **PyMuPDF** meant every hosted
  deployment silently lost the on-screen report previews it offers, and fell
  back to a code path with a duplicate-widget-id bug in it that took the whole
  Results tab down (2.15.0). Tests enforce both; do not "tidy" them back out.
  `requirements-optional.txt` is for features the app *offers to install* — the
  AI provider SDKs — not for features it just offers.
- **The same mistake has now been made three times**: the storage backends
  (2.10.0), PyMuPDF (2.15.0) and `streamlit-quill` (2.16.1). The test
  `test_selfcheck_requires_everything_requirements_txt_declares` keeps
  `selfcheck.py` honest about the list, but it cannot tell you that a package
  is in the wrong file. Before moving anything into
  `requirements-optional.txt`, ask whether a hosted deployment would still
  offer the feature — if it would, the package is core.
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
