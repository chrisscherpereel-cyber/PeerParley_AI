# PeerParley

**Peer evaluation, made clear.** A single instructor/administrator application
that runs the entire peer-evaluation workflow — set up → collect → grade → PDF →
email — from one Streamlit app you can deploy on **Streamlit Community Cloud**,
while keeping all student PII **encrypted and stored behind your university's
firewall**.

The public cloud host never persists plaintext student data. Everything
sensitive is AES-encrypted in-process and written only to a
university-controlled storage vault (Microsoft 365, Dropbox, or pCloud).

**New in v2:** an optional **AI feedback writer** turns each student's raw peer
comments into a readable narrative — grounded in those comments and nothing
else, checked automatically for invented claims, and released only after you
approve it. Off by default, and off again every time you sign in. See
**`docs/AI_FEEDBACK.md`**.

### If someone has just handed you this app

Sign in, and open **Start here** at the top of the page. It is a five-step
checklist ticked off from what you have actually done, and each unfinished step
names the one button that completes it. You need a contact list — names, emails
and teams, one row per student — and nothing else; the template is on tab ①.
Three things are worth knowing before you click:

* **Save the survey before you email anybody.** Until you do, a student opening
  their link is told the evaluation is not available. The app blocks sending
  until then, and tells you.
* **Don't change the student list after answers start arriving.** Each link
  points at a student's place in the saved list, so adding or removing someone
  would hand their answers to a teammate. PeerParley refuses the save and says
  so; use a new evaluation number for a changed roster.
* **Nothing reaches a student until you click a button that says it will.**
  Every send button names its audience and how many; the ones that discard work
  ask first. The AI writer, if you turn it on, drafts for your review and
  nothing it writes is sent without your approval.

---

## What it does

One app, five steps (tabs), plus Compare and Vault:

1. **Set up survey** — upload the **contact list** (the only input). PeerParley
   builds a private evaluation link for every student; their answers become the
   grading input directly. Download the built-in contact-list template and fill
   the yellow cells only. (Optional: import a Qualtrics raw export instead.)
2. **Collect responses** — watch who has responded, send reminders, then load
   the responses into grading.
3. **Grading rules** — sensitivity, separate increase/decrease caps, feedback
   points, rounding, and the peer-adjustment method (allocation / rating /
   ranking / combined). Sensible defaults; skippable your first time.
4. **Results & reports** — results table, instructor summary + confidential
   PDFs, per-student feedback PDFs, and control over what students see. Also
   where the optional **AI feedback writer** drafts and you approve each
   student's narrative.
5. **Send feedback** — email each student their PDF via Microsoft 365 / SMTP,
   or download a ready-to-run auto-send pack (double-click on Mac or Windows).

**Compare** shows the same students across evaluation rounds. **Vault**
encrypts the working dataset to your firewall-side storage.

### The deliverables
- Individual anonymous feedback PDF (student-facing) — with an approved AI
  narrative above the raw comment bullets, when you've enabled and approved one
- AI audit trail JSON (instructor-only): every draft, citation, flag and approval
- Team self-reported contribution PDF (team-facing)
- Instructor section-summary PDF (instructor-only)
- Instructor confidential feedback PDF (instructor-only)

---

## Privacy model (read this)

| Concern | How it's handled |
|---|---|
| App is on a public host | Per-instructor accounts + shared-password admin (SHA-256 in secrets). |
| PII on the cloud disk | Never persisted in plaintext. In-session memory only; any cache is Fernet-encrypted. |
| Where PII actually lives | Encrypted `.ppx` bundles in **your** M365 / Dropbox / pCloud folder. |
| Who can decrypt | Only holders of the Fernet key (kept university-side in secrets). |
| Cross-student leakage | Each student PDF is built alone; email delivery validates attachment ↔ recipient before sending. |
| Secrets & data in git | `.gitignore` blocks `secrets.toml`, all CSV/XLSX/PDF, and `vault_cache/`. |
| Comments sent to an AI vendor | Only when you enable it, and only the public peer comments plus (optionally) the four numeric ratings. **Never** confidential comments, emails, IDs, grades or scores. Note that a comment a teammate wrote may itself mention a name, so treat the comment text as the unit of disclosure. Pick the **On this computer** provider and nothing leaves the room. |
| AI inventing feedback | Every point must cite a real comment; citations are verified locally, a second pass audits the prose, and nothing reaches a student without your approval. See `docs/AI_FEEDBACK.md`. |
| AI keys | Read from secrets, or pasted into the sidebar for that session only — never written to disk, the vault, or the audit trail. |

**FERPA note:** with `backend = "m365"` and your NAU tenant, student data stays
under university identity, DLP, and retention governance. See `ARCHITECTURE.md`.

---

## Quick start (local)

**You need Python 3.10 or newer.** Check with `python3 --version`.

```bash
python3 -m venv .venv && source .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 -m streamlit run app.py
```

That is the whole installation. The app opens on a **setup page**: choose an
administrator password, and PeerParley generates the encryption key that
protects every student record it stores. It will write
`.streamlit/secrets.toml` for you, or hand you the text to paste if you would
rather do it yourself. Restart the app, sign in as **admin**, and the first tab
tells you what to do next.

Keep a copy of that encryption key somewhere safe — a password manager. It is
not recoverable, and without it everything already saved is unreadable.

```bash
python3 selfcheck.py     # "is this installation usable?" — run it any time
```

`selfcheck.py` reports on Python, packages, your settings, storage, encryption,
email and the AI writer, and finishes with a plain verdict. It changes nothing.
Run it first whenever something looks wrong.

> On macOS, if `pip` and `streamlit` disagree about the interpreter, always use
> `python3 -m pip …` and `python3 -m streamlit run …`.

### Where your data lives

Out of the box, storage is **local**: encrypted files under `vault_cache/`
beside the app. That is fine for trying PeerParley out, but on a hosted server
those files can be wiped at any restart — so before collecting real responses,
point it at your university's storage (Microsoft 365, Dropbox or pCloud). One
setting, covered in **`DEPLOYMENT.md`**.

### Optional extras

```bash
python3 -m pip install -r requirements-optional.txt   # all of them
```

Nothing here is required. The list covers Dropbox storage, in-app PDF previews,
a formatting toolbar for emails, and the AI feedback writer's provider SDKs —
install one of `openai` (OpenAI / xAI / OpenRouter / a model on your own
computer), `anthropic` (Claude) or `google-genai` (Gemini). Choosing a feature
whose package is missing produces a message naming the package, not a crash.

For the AI writer, add the matching key to `secrets.toml`
(`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `XAI_API_KEY`,
`OPENROUTER_API_KEY`) or paste one into the sidebar for a single session, then
switch it on there. It is off every time you sign in, by design. Full guide:
**`docs/AI_FEEDBACK.md`**.

### Checks

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m pytest tests/ -q     # 233 tests, fully offline
python3 qa_regression.py        # 93 checks driving the real app, headless
python3 selfcheck.py            # is this installation usable?
```

Nothing in there touches the network, emails anybody, or spends an AI request:
the tests run against fakes and synthetic sections. The end-to-end tests drive
the real survey — signed link, student form, encrypted response, grading, PDF —
so a broken install fails loudly rather than in week three of your course.

## Deploy to Streamlit Cloud
See **`DEPLOYMENT.md`** — push this repo to GitHub, point Streamlit Cloud at
`app.py`, and paste your secrets into the app's **Settings → Secrets** box
(never commit them).

---

## Repo layout

```
├── app.py                     # single Streamlit application (entry point)
├── requirements.txt           # what the app needs
├── requirements-optional.txt  # extras: AI writer, Dropbox, previews
├── requirements-dev.txt       # pytest, for running the checks
├── selfcheck.py               # "is this installation usable?"
├── .gitignore                 # blocks secrets + all student data
├── .streamlit/
│   ├── config.toml
│   └── secrets.toml.example   # template — copy, fill, never commit
├── assets/
│   ├── ContactList_Template.xlsx
│   ├── peerparley_logo.svg
│   ├── peerparley_mark.svg
│   └── peerparley_mark.png    # browser/app favicon
├── peerparley/
│   ├── __init__.py            # version
│   ├── config.py              # secrets/env loader
│   ├── accounts.py            # per-instructor accounts + roles
│   ├── auth.py                # sign-in gate
│   ├── security.py            # Fernet encryption
│   ├── vault.py               # M365 / Dropbox / pCloud / local backends
│   ├── survey.py              # built-in survey: setup, links, collection
│   ├── ingest.py              # Qualtrics + roster parsing, QA
│   ├── comments.py            # comment-support score Q
│   ├── grading.py             # allocation grading engine
│   ├── pdfgen.py              # branded ReportLab PDFs
│   ├── emailpack.py           # .eml + auto-send script packs
│   ├── email_delivery.py      # Graph device-code + SMTP
│   ├── tokens.py              # signed student-link tokens
│   ├── ui_helpers.py          # app-side glue
│   ├── branding.py
│   ├── aiconfig.py            # v2: LLM provider registry + AI settings
│   ├── llm.py                 # v2: provider-agnostic client (ported from TransQ)
│   ├── ai_prompts.py          # v2: every prompt, in one editable file
│   ├── feedback_ai.py         # v2: evidence, generation, grounding, approval gate
│   ├── openrouter_catalog.py  # v2: live OpenRouter model list (from TransQ)
│   ├── localmodels.py         # v2: local server probe + model discovery (from TransQ)
│   ├── workspace.py           # v2: session autosave + complete .ppx bundles
│   ├── model_advisor.py       # v2: model verdicts, recommendations, track record
│   ├── safety.py              # v2: abusive-language screen for comments + summaries
│   └── ai_ui.py               # v2: sidebar settings, model pickers, review panel
│   └── setup_wizard.py        # first-run setup: password + encryption key
├── tests/
│   ├── test_feedback_ai.py    # the AI writer, offline; no API key needed
│   ├── test_survey_end_to_end.py  # link → form → response → grade → PDF
│   └── test_first_run.py      # a brand-new installation
├── qa_regression.py           # headless AppTest harness (90 checks)
├── docs/
│   └── AI_FEEDBACK.md         # the AI writer: rules, setup, cost, audit trail
├── GRADING.md
├── SURVEY_FEATURE.md
├── DEPLOYMENT.md
└── ARCHITECTURE.md
```
