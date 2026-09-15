# Changelog

## 2.3.0 — the model flexibility TransQ had, restored

v2 shipped OpenRouter with seven hardcoded model slugs and a free-text box.
TransQ fetched the whole catalogue live; trimming it to a curated handful was
the wrong call, and it also quietly dropped two other things TransQ did. All
three are ported back.

### Every model OpenRouter carries

`peerparley/openrouter_catalog.py`, ported from TransQ essentially unchanged:
`GET /api/v1/models` fetched live (public, no key), cached an hour, sorted A–Z.
A hardcoded list would be wrong within a month and would go on being
confidently wrong.

- Free-only toggle and a vendor filter, with a type-to-search dropdown.
- 🆓 marks models priced at $0 — decided by the **price**, not by a `:free`
  suffix.
- **↻ Refresh list** busts the cache without waiting out the hour; **Or a slug**
  accepts anything at all.
- Live prices are merged into the cost meter via a newly ported
  `llm.register_pricing`, so it quotes today's rates. A model whose price
  OpenRouter doesn't publish reads "price not known" rather than $0.00, which
  would understate a real bill.
- Image/video generators and `:batch` variants are filtered out — they're in the
  same catalogue and cannot answer.
- A failed fetch falls back to a bundled snapshot **and says so on screen**, so
  a stale list is never shown as current.

### Local models read from the machine

`peerparley/localmodels.py`, also from TransQ. The previous free-text box asked
the instructor to remember what they'd downloaded; the server already knows.
Probes the address, names what answered, lists its models, and tolerates both
the OpenAI `/models` shape and Ollama's native one. Unreachable servers get the
actual cause — nothing listening, wrong port, still loading — not an errno.

On Streamlit Cloud the picker says plainly that a local model cannot be reached
and stops, rather than offering an address that can never work.

### Settings remembered per instructor

Provider, model, tone, length and grounding options save to the vault per user,
so they aren't re-picked on every sign-in. **The API key is deliberately
excluded**: its whole security story is that it dies with the session, and
persisting it for convenience would make that promise false. Asserted by a test.

21 new tests (84 total).

## 2.2.0 — drafts persist, and retry means retry

Three problems from a live 40-student run, all of which cost real work.

### Partial results survive the session

Drafts lived only in `st.session_state`, so signing out — or Streamlit
recycling an idle session — discarded them. That was the worst of the three: a
draft costs an API call, but the edits and approvals layered on top cost the
instructor's judgement, which is the expensive part.

Drafts now save to the **same encrypted vault as everything else**, keyed per
survey (`aidrafts__<slug>.json`), and are restored when the panel opens. Edits,
approvals, flags, citations and per-draft token counts all round-trip.

- Vault-stored rather than local, because a draft contains a student's name and
  their teammates' comments; `Vault.put_bytes` Fernet-encrypts before the bytes
  leave the process, so the provider holds ciphertext.
- Saving is best-effort and never raises — a storage hiccup must not cost the
  in-memory work it was protecting. If it fails, the panel says so, naming
  durability rather than claiming data loss.
- A fingerprint guards the write, so an idle rerun doesn't re-upload forty
  drafts on every keystroke.
- Keyed per survey, so one cohort's narratives can never load under another's.
- An *unreadable* save (changed Fernet key) reports itself, rather than looking
  like "you never drafted anything".

### Retry only touches what's outstanding

The panel offered "Draft the 40 remaining" next to "38 failed". Two causes:

- The button label was rendered *before* the batch ran, so it showed pre-batch
  counts. The panel now reruns after a batch, so labels and metrics agree.
- The retry is now the **primary** action whenever finished drafts exist, since
  it is the one that cannot destroy them, and it says exactly what it will do
  ("Draft only the N not yet done"). "Draft feedback for all" is relabelled
  **"Redo all N"** and its help text states plainly that it discards finished
  drafts along with their edits and approvals.

### The free router's empty replies

38 of 40 students failed with "stopped at its output limit after 0 characters".
An empty completion is not a truncated one, whatever `finish_reason` says — and
reporting it that way sends the instructor to shorten a draft that was never
written. OpenRouter's free router assigns a different upstream model per call and
some answer with nothing under load, so an empty reply is now a **transient**
failure that retries automatically, with a message saying that pinning a model
beats the free router for a whole section. A genuinely partial reply is still
treated as truncation and still salvaged.

12 new tests (63 total) and 12 new end-to-end checks (53 total).

## 2.1.2 — the panel no longer reports failures as drafts

A second live run made the reporting problem plain: the header read
**"Drafted 40"** while forty requests had failed and nothing had been written.
That is worse than an error, because it looks like success.

- **"Written" replaces "Drafted"** as the headline metric, counting only drafts
  that produced usable text. `batch_stats` gains `written`; `total` still counts
  Draft objects, and a failure is still a Draft — which is exactly why it was
  the wrong number to show.
- **A "Failed" metric** sits beside it, so failures are visible rather than
  inferred from the absence of drafts.
- **The post-batch summary is an error, not a success, when nothing was
  written.**
- **Repeated failures collapse into one banner** (`error_groups`) naming the
  count and the affected students. Auth failures already abort the batch, but a
  wrong model name fails per-request and would still have filled the panel with
  identical rows.
- 3 new tests (51 total).

## 2.1.1 — credential failures report once, not forty times

Found by a live run against a 40-student section: an OpenRouter key that
returned `401 - User not found` produced forty identical failures, one per
student. Every one of them was the same fact, and the fortieth was no more
informative than the first.

- **`AuthLLMError`** distinguishes a rejected credential from a per-request
  failure. A rate limit on student fourteen says nothing about student fifteen;
  a bad key says everything about all of them.
- **The batch stops at the first auth failure** (`BatchAborted`), keeping every
  draft that finished before the stop, and the review panel shows one message
  instead of a wall of them.
- **Auth is classified before the transient check.** Some providers phrase a 401
  with wording that also matches a transient marker, which would have retried a
  dead key four times with backoff.
- **Provider-specific guidance** replaces the raw JSON dump — including that
  OpenRouter says "User not found" for a key it doesn't recognise, and that the
  free router still needs a valid key (free models, not an anonymous account).
- **Wrong-vendor keys are caught before any request.** A key whose prefix
  belongs to another provider (`sk-ant-` under OpenRouter, say) now fails
  `ready()` with a message naming both, rather than costing a full batch to
  discover. Deliberately conservative: a bare `sk-` is ambiguous, so it isn't
  guessed.
- **Keys are stripped on every read path.** A trailing newline in the Streamlit
  secrets box is a classic silent 401.
- **"Test the key"** in the sidebar: one tiny request that confirms the provider
  accepts the key and model, and distinguishes "key rejected" from "key fine,
  model wrong".
- 8 new tests (48 total), and a troubleshooting section in `docs/AI_FEEDBACK.md`.

## 2.1.0 — AI feedback writer

Adds an optional step between grading and delivery: each student's written peer
comments are rewritten into a readable narrative, checked against those same
comments, and shown to the instructor for approval. **Off by default.** With it
off, every part of the app behaves exactly as before.

The AI access layer is ported from **TransQ**, a lecture-quiz builder with the
same requirement — one instructor-facing Streamlit app that must talk to
whichever vendor the department has a key for.

### The constraint

The model may **reword and expand** what teammates wrote. It may not add
anything they did not say: no invented recommendations, no inferred causes, no
generic teamwork advice, no extrapolating one remark into a pattern, no claims
about ability or attitude, no mention of grades, nothing identifying who said
what. Four mechanisms enforce it, in descending order of reliability:

1. **Structural.** Every point the model makes must carry the verbatim comment
   it came from. A claim with no source has nowhere to live in the response
   schema.
2. **Deterministic citation check** — no API call. A quote that does not appear
   in the real comments was fabricated, and string matching is a better test of
   that than model self-assessment. Also catches overstated agreement ("several
   teammates" behind one comment). Lightly reworded quotes still pass, so it
   doesn't cry wolf.
3. **Grounding audit** — one extra call, fresh context, given the draft and the
   same evidence, asked what goes beyond it. Catches what string matching can't:
   a real quote attached to a point it doesn't support, an invented cause,
   advice smuggled into a paragraph. An audit that *fails to run* is recorded as
   unverified, not as passed.
4. **The instructor.** Nothing reaches a student without approval. Flagged
   drafts cannot be swept up by bulk approval; they must be opened and read.
   Instructor edits are what ship.

### New

- `peerparley/aiconfig.py` — provider registry (OpenRouter, Anthropic, OpenAI,
  Gemini, xAI, local Ollama/LM Studio), key resolution, tone presets,
  `AISettings`.
- `peerparley/llm.py` — provider-agnostic client over three SDK paths, with
  retry/backoff on transient failures, truncation salvage, and a token/cost
  meter that reports "no published price" rather than inventing a figure.
- `peerparley/ai_prompts.py` — every prompt in one editable file.
- `peerparley/feedback_ai.py` — evidence assembly, generation, the citation
  check, the audit, and `approved_narratives()`: the single gate to
  student-visible text.
- `peerparley/ai_ui.py` — sidebar settings and the review panel.
- `tests/test_feedback_ai.py` — 40 tests, fully offline via a fake client.
- `docs/AI_FEEDBACK.md` — rules, setup, cost, privacy, audit trail.
- `.streamlit/config.toml` and `.streamlit/secrets.toml.example` — referenced by
  the README and DEPLOYMENT but previously absent from the repo.
- **Audit trail export** (JSON): every draft, its citations and match scores,
  its flags, what was edited, what was approved, and the model and settings
  used.

### Changed

- `pdfgen.build_individual_pdf` takes `narrative=`, rendered as "Summary of your
  peer feedback" **above** the raw comment bullets, which are never replaced — a
  student is entitled to their teammates' actual words, and a summary they can't
  check against the source is worth less, not more. New `_page_guard` keeps
  section headings from being orphaned now that the document can run to two
  pages, and stamps the confidentiality footer on every page rather than only
  the last.
- `ui_helpers.build_messages` and `emailpack.results_parts` / `results_items`
  take `narratives=`, so the direct send, the `.eml` pack, the auto-send pack
  and the PDF zip all honour the same approvals. Choosing a different delivery
  method cannot change what a student receives.
- `survey.py` report settings gain a `narrative` flag, so the section can be
  hidden even for approved drafts.
- The survey-switch guard in `app.py` clears AI drafts, so one cohort's
  narratives can never attach to another's students.
- `requirements.txt` adds `tenacity` and the three optional provider SDKs. Every
  SDK import is lazy: a missing package produces a message naming it, not a
  traceback.

### Fixed

- **`qa_regression.py` had stopped testing anything.** It was written against a
  step-wizard UI (`Next`/`Previous` buttons, a `step` session key) that the app
  replaced with tabs, so it raised `KeyError: 'step'` on its second check — on
  v1 as well as v2 — and everything after that never ran. Rewritten against the
  tabbed app, now 41 checks covering grading, PDFs, the evidence boundaries, the
  grounding guards, the approval gate, and every delivery path. It builds a
  synthetic Qualtrics export and runs it through the real `ingest` path rather
  than hand-placing a frame the app would never have produced.

### Open question, not a change

The old harness asserted that a non-submitter's "Grade Δ" renders as an em dash.
It does not — `results_to_frame` formats a signed percentage for everyone, so a
student who never submitted still shows a peer-driven adjustment. Because that
check hadn't run in a long time, code and expectation drifted apart unnoticed.
Which is correct is a grading-policy decision (should a student who skipped the
evaluation still be scored by their teammates, or held out?), so v2 records the
current behaviour in the harness and changes nothing. Worth a deliberate
decision before the next live round.

## 2.0.0

Built-in survey (students no longer need Qualtrics), per-instructor accounts,
encrypted vault with pluggable backends, Graph device-code email, comparison
across evaluation rounds.
