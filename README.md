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
approve it. Off by default. See **`docs/AI_FEEDBACK.md`**.

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

```bash
python3 -m venv .venv && source .venv/bin/activate
python3 -m pip install -r requirements.txt

# create secrets
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
# generate an app password hash:
python3 -c "import hashlib,getpass;print(hashlib.sha256(getpass.getpass().encode()).hexdigest())"
# generate an encryption key:
python3 -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"
# paste both into secrets.toml, choose a vault backend, fill its credentials

python3 -m streamlit run app.py
```

> On macOS, if `pip` and `streamlit` disagree about the interpreter, always use
> `python3 -m pip …` and `python3 -m streamlit run …`.

### Optional: the AI feedback writer

```bash
# install only the provider you'll use
python3 -m pip install openai        # OpenAI / xAI / OpenRouter / local Ollama
python3 -m pip install anthropic     # Claude
python3 -m pip install google-genai  # Gemini
```

Add the matching key to `secrets.toml` (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`,
`GEMINI_API_KEY`, `XAI_API_KEY`, `OPENROUTER_API_KEY`) or paste one into the
sidebar, then switch it on there. Full guide: **`docs/AI_FEEDBACK.md`**.

### Tests

```bash
python3 -m pytest tests/ -q     # 40 unit tests, fully offline
python3 qa_regression.py        # 41 end-to-end checks via Streamlit AppTest
```

## Deploy to Streamlit Cloud
See **`DEPLOYMENT.md`** — push this repo to GitHub, point Streamlit Cloud at
`app.py`, and paste your secrets into the app's **Settings → Secrets** box
(never commit them).

---

## Repo layout

```
├── app.py                     # single Streamlit application (entry point)
├── requirements.txt
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
│   └── ai_ui.py               # v2: sidebar settings + review panel
├── tests/
│   └── test_feedback_ai.py    # 40 offline tests; no API key needed
├── qa_regression.py           # headless AppTest harness (41 checks)
├── docs/
│   └── AI_FEEDBACK.md         # the AI writer: rules, setup, cost, audit trail
├── GRADING.md
├── SURVEY_FEATURE.md
├── UPLOAD_TO_GITHUB.md
├── DEPLOYMENT.md
└── ARCHITECTURE.md
```
