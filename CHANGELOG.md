# Changelog

## 2.9.0 — the whole-application audit

A review of all 13,000 lines, prompted by one request ("have AI off by default,
and validate the code"). Four reviews ran in parallel — the grading pipeline,
the AI layer, performance, and the app as a first-time instructor meets it —
and every finding below was reproduced before it was fixed. The grading changes
alter numbers, so they were decided deliberately rather than by me.

### The AI writer is off by default, and starts every session off

`enabled` is no longer saved with the other AI preferences, and signing out
clears the toggle. An instructor who tried the writer once in March does not
find it armed in September, spending on an API account and sending student
comments to a vendor because of a click they have forgotten. Everything around
the switch — provider, model, tone, length — is still remembered, so turning it
back on is one click rather than a re-setup. The API key is still never stored,
and is now kept out of `repr()` as well.

### Grades: four defects that produced wrong numbers

**A student nobody evaluated was given the maximum deduction.** An empty list of
received ratings averaged to `0.0`, so a student whose raters all failed to
submit scored 0% of the team average: multiplier 0.50, −50% on the team score —
and their zero dragged the team average down for everyone else too. Missing data
is now missing: they keep the team score, they are left out of their team's
average, their performance reads "No data", and the instructor gets a named list
of who they are.

**The $100 was collected for the whole team and then spent on nobody.** The form
asked each student to split $100 across their team *including themselves* and
required the total to be exactly $100 — then grading discarded the
self-allocation. A student who kept $40 distributed only $60, lowering every
teammate at no cost to themselves. The form now has no box for yourself and the
total is required across teammates, which is what the grader always assumed (an
even share is 100/(n−1)).

**"Round to the nearest 5%" rounded only the label.** A student could be told
"+5%" while the gradebook held 104.44. The rounded adjustment is now the one
applied — the report and the score are the same number — and tab ③ says what a
5% step means in practice, since most adjustments in a typical section are
smaller than ±2.5% and now round to no change. `-0.0` also stopped rendering as
"+-0%".

**"Correct for rater leniency" had a unit bug.** It fed a standard score where a
ratio was expected, so turning it on could swing one student from +0% to −35%
and reorder the team. Each rater's dollars are now standardised and mapped back
onto the team's own mean and spread, so the correction removes easy-grader and
harsh-grader differences while the result stays in dollars on the same scale.
It is a no-op when every rater spreads their money the same way.

Also: the confidential note to the instructor is one box per submission, and
both intake paths park it on the first teammate's row — so it used to read as
evidence *about* that teammate. It is now filed under its author, and both
instructor PDFs say so. The B−/B grade boundary was 0.78, which made that band
twice as wide as every other and B a sliver; it is 0.71, restoring the uniform
0.07 bands. An allocation column that parsed to nothing (a renamed Qualtrics
question) crashed the results tab with a `TypeError` and now reads as missing
data. A contact list saved from Excel in cp1252 crashed the upload on the first
accented name.

### Problems that changed the grades silently now say so

One function, shown above the results table: a name spelled two ways is being
graded as two students; a student listed on two teams has had both teams merged;
nobody evaluated these students. When nearly every name differs, it says the two
files are written in different formats rather than listing forty "typos".

### The AI layer

* A citation that wrapped a real comment in invented words scored a perfect
  match — "Always on time" is real, "Always on time, and she told us to set up a
  shared Gantt chart" is a recommendation nobody made. Containment in that
  direction is now a high-severity flag that names what happened.
* A verbatim copy of a terse comment ("On time") was reported as citing a
  comment nobody wrote. Short citations that match a whole comment pass; short
  ones that do not are flagged as unverifiable rather than accused of invention,
  because crying wolf teaches a reviewer to ignore the real flags.
* A draft of paragraphs with no sourced points gave the citation check nothing
  to check, raised no flags, scored 1.0, and read as "Grounded". Bulk approval
  now requires that something was actually checked.
* A failed draft was never screened for abusive language — yet the student's
  report falls back to their teammates' comments, so those comments reach them
  either way. Now screened.
* Clearing the draft box meant "use the generated text", so clearing a draft and
  approving it shipped the model's original words. It now means what it says.
* The per-student delivery override — the control that withholds an abusive
  comment from one student — was outside the autosave guard, so it was never
  written and was gone by the next sign-in.
* `402`, `401` and `403` were matched as text anywhere in a provider's message.
  "You requested 14025 tokens" contains "402", so an ordinary bad-request error
  aborted the batch telling the instructor to buy credits they already had.
  Status codes are now read as codes.
* A spent free daily allowance arrives as a 429, indistinguishable from the
  per-minute limit that *is* worth retrying, so it was retried four times per
  student: 160 doomed requests and nine minutes of backoff for a 40-student
  batch. It is now its own global failure that stops the batch and says when the
  allowance resets.
* The `_nonce` cache-busters never worked — Streamlit leaves underscore-prefixed
  arguments out of the cache key — so the key-status panel kept showing a stale
  error after the key was fixed. Removed, with `.clear()` doing the work.

### Speed

A click anywhere used to regrade all forty students: measured 1.1–10 seconds for
teams of five, 44 seconds for teams of ten, and 12 more for three rounds on the
Compare tab. Grades are now computed when the data or the settings change;
comment-similarity is computed once per pair rather than three or four times
(40 students: 1.19s → 0.42s); the instructor PDFs are built once per result set
instead of twice per rerun; previews rasterise only when asked for; the response
list is read at most every 30 seconds with a refresh button; the Compare tab
waits for a button; and one vault instance is shared instead of ten per rerun,
each acquiring its own auth token. The Qualtrics upload is parsed once per file
rather than re-parsed on every rerun, where it had been quietly overwriting
responses loaded from elsewhere.

### For an instructor meeting this app for the first time

* **Start here** — a five-step checklist above the tabs, ticked off from what is
  actually true, each unmet step naming the one button that completes it.
* The storage check now verifies the encryption key as well as the connection,
  so "not finished being set up" is caught at sign-in rather than after forty
  students have been typed in. Both setup messages lost their Python one-liners.
* **Saving now refuses to re-point collected answers.** A student's identity in
  a submission is their position in the saved roster; adding one student shifted
  everyone after them, so answers already collected described the wrong people.
  Nothing errored. Saving is blocked, with the reason, when it would do that.
* Links cannot be sent before the survey is saved or without the app's address —
  each used to send forty dead links with no error anywhere on screen. The
  default address (a real one) is gone: it made the "enter your address" check
  pass.
* "Prepare them for me to look over first" now exists for SMTP, where one click
  used to send forty real emails. Asking for drafts over SMTP previously *sent*
  them; it now hands back a zip of ready-to-send emails.
* Buttons that send name their audience and count ("Send 40 emails to students").
  Discard, un-approve-everything and redraft-everyone ask first and name what
  would be lost. "Approve the 38 clean drafts" now reads "Approve 38 drafts
  without reading them", which is what the click means.
* The review panel no longer opens with 38 of 40 drafts hidden.
* The report settings on tab ① apply to the PDFs you build now, rather than only
  after a save — which was impossible in a resumed session, where the save
  button was disabled.
* Grading settings are saved with the session, so resuming no longer regrades at
  the defaults with nothing on screen saying so.
* An unreadable saved session is reported as unreadable rather than as absent,
  and the autosave is held off so it cannot be overwritten.
* A jargon pass over the worst of it: tokens, slugs, nonces, z-scores, "Q", the
  WebPA weighting fraction, Fernet, and "structured outputs" are gone from the
  instructor's screen. A new "Check for new responses" button, and a notice when
  more responses have arrived since the grades were loaded.

### Removed

Five unused encryption helpers, a `Roster.from_df` that nothing called and that
picked the wrong column anyway, and an uncalled `delete_drafts`.

30 new tests (185 total) and 10 new regression checks (90 total).

## 2.8.2 — AttributeError on "On this computer"

Choosing the local provider raised
`AttributeError: 'LocalServer' object has no attribute 'status'` and the page
went with it. `LocalServer` exposes `status_line()`, a method; the picker read
`server.status`, a field that has never existed.

**Why it shipped.** The mapping from a probe result to the dict the picker
renders lived inside a `@st.cache_data` wrapper, where no test could call it.
Every other state in that picker is covered; this one line was unreachable.
It is now `_local_server_state()`, a plain function with no network and no
Streamlit in it, covered by five tests across all three states a probe can
return — unreachable, running with nothing downloaded, and ready.

**Why it was fatal rather than annoying.** The sidebar renders before every
tab, so an exception raised while drawing a picker loses the whole page, not
the feature. Both rich pickers — OpenRouter and local — are now wrapped, and
fall back to a plain text box with the error shown above it. The local probe's
own fallback returns the same keys as a successful probe, which a test now
asserts, because a fallback missing a key the picker reads would have been the
same bug in a new costume.

6 new tests (155 total).

## 2.8.1 — "Should handle this" next to a 402

A live run showed **✅ Should handle this** beside four identical failures:
`402 — This request requires more credits, or fewer max_tokens. You requested
up to 4000 tokens, but can only afford 680.` The verdict was true about
capability and useless in practice, which is the exact failure mode
`model_advisor` exists to prevent. Three defects.

**402 was not classified.** It fell through to a generic per-student error, so
one exhausted balance produced one failure per student. `BillingLLMError` now
sits alongside `AuthLLMError` — both are global rather than per-student, so the
batch aborts at the first one. Checked *before* the auth and transient markers,
because some 401 bodies also mention credits and the remedies differ, and
because retrying a billing failure cannot succeed.

**The message now names the remedies.** It explains that OpenRouter
pre-authorises the reply cap before generating (so a smaller request might have
gone through), states what the balance actually covers, and points at adding
credits or switching to a free model. It suggests lowering the cap *only* when
the remaining balance would still fund a usable narrative — at 680 tokens it
says plainly that lowering it will not rescue the run.

**Affordability is now part of the verdict.** `assess()` takes
`credits_remaining`, and a model the account cannot pay for is **🚫 Not enough
credit to run this** rather than suitable — capable or not. `llm.affordable_tokens`
does the arithmetic, and reproduces OpenRouter's own figure exactly: a
`$0.000272` balance at `$0.40/M` gives 680, matching the API's number. Free
models are never judged on the balance, since they draw on the daily allowance
instead.

The pre-flight also reports, for a paid model with a known balance, how many
requests it can afford — and refuses to let a doomed batch start quietly.

Fixed along the way: `affordable_tokens` floored `679.9999999999999` to 679,
disagreeing with OpenRouter's 680 for a reason no instructor could have
diagnosed.

8 new tests (149 total).

## 2.8.0 — per-student delivery, one-student regenerate, and an abuse screen

### The delivery decision moves into review

The survey-wide setting is the right starting point and the wrong place to
finish: a cohort-wide rule cannot know that *one* student's comments contain
something that should not be forwarded. Each draft now has **What this student
receives** — default, summary and comments, summary only, comments only, or
nothing — overriding it for that student alone.

`feedback_ai.report_overrides` turns those choices into per-student report
flags, threaded through every delivery path (PDF zip, preview, direct send,
`.eml` pack, auto-send pack), so switching send method still cannot change what
a student gets.

The approval gate is untouched. Summary-only with nothing approved means no
written feedback, and the panel says so at the point of choosing rather than
letting it be discovered later. Comments-only suppresses the narrative whether
or not it was approved — the choice beats the approval, which is the right
precedence.

### Regenerate one student

**↻ Regenerate** on any draft redrafts that student alone, discarding their
draft, edits and approval. It routes through the same generate → check → screen
path as a batch rather than a parallel shortcut, so a regenerated draft is never
held to a lower standard.

### Abusive language is screened

Peer evaluation invites anonymous criticism, and anonymity occasionally produces
something that is not criticism. An abusive remark previously had a clear path
from one student to another with nobody in between.

- **`peerparley/safety.py`** — a local, deterministic screen. No API call,
  always runs, covers profanity, slurs, threats and direct personal attacks.
- **The grounding audit also flags abuse**, at no extra cost, catching what a
  word list cannot: contempt in clean language, "shouldn't be in this major".
- **Both surfaces**, and the raw comments matter more than the narrative — they
  are unedited, and in comments-only mode they are the whole report. A student
  with too few comments to draft from still gets their comments screened, which
  a narrative-only screen would have missed entirely.
- **Severity suggests, it does not act.** Severe (slurs, threats, harassment)
  blocks bulk approval and proposes summary-only for that student — which is
  exactly what the override above is for. Moderate is the instructor's call.
  Mild is noted only, because "he contributed nothing" may be the honest and
  useful truth and flagging it as abuse would make the flag meaningless.
- **Nothing is censored automatically.** Quietly deleting a teammate's blunt but
  genuine criticism would be its own failure. `EXTRA_PATTERNS` lets an
  institution add its own vocabulary.

Neither screen is a guarantee — a word list is evadable and blind to tone, a
model is inconsistent — and the docs say so. They direct attention; the
instructor is the control.

17 new tests (141 total) and 11 new end-to-end checks (80 total).

## 2.7.0 — know before you spend, and choose what students receive

Three requests, all of which pointed at the same gap: the app knew nothing about
a model before committing a whole section to it.

### The key is checked first

`GET /api/v1/key` verifies the credential and reports today's remaining free
allowance (`free_model_daily_requests`). A rejected key is caught before a batch
rather than as forty identical 401s, and the budget line now uses the actual
remaining count instead of assuming a fresh 50 — a second batch on the same day
used to look affordable when it wasn't.

### Models are assessed on published capability

`ORModel` now parses `supported_parameters` and
`top_provider.max_completion_tokens`, and `peerparley/model_advisor.py` turns
them into a verdict: **✅ Should handle this**, **⚠️ Might struggle**, or
**🚫 Cannot do this job**, with reasons. A reply ceiling under ~2,000 tokens is
disqualifying — that is exactly the failure that cost 7,233 characters. No
declared structured-output support is a warning rather than a ban, because
salvage exists.

**Only models that can do this job** filters the picker, on by default and
switchable off, since the metadata is occasionally missing or wrong.

### A track record, reported honestly

Outcomes are recorded per model across runs — attempts, usable drafts, empty
replies, truncations, failures — and fed back into selection.

- **A rate is withheld below 5 attempts.** One success out of one is not 100%,
  and saying so would be the most misleading thing here; counts are shown
  instead.
- Observed performance is scored **against a baseline** rather than added to the
  score. Adding it meant a model with a 5% success rate outranked an untried
  one, which is backwards: a demonstrated failure is worse news than no news.
- There is deliberately **no blended "probability of success"**. It would look
  authoritative while resting on an invented weighting between capability and
  history.

### Recommendations, paid and free

**💡 Recommended for this task** names one of each, with reasons and a
select button. Ties break toward the cheaper model. The free router is never
recommended — it picks a different model per call, so advising it advises a
lottery.

### What students receive is now a choice

**Written feedback** under the report settings: summary *and* comments (the
default), summary only, comments only, or neither. The approval gate holds in
every mode — "summary only" never ships an unapproved draft; a student without
one simply gets no written feedback, which is the honest cost of that choice and
is stated in the UI.

15 new tests (124 total).

## 2.6.0 — the free tier, made workable

Prompted by a direct question: is there a free OpenRouter option that works for
this? Yes, but only if you plan around the daily ceiling — which the app was
doing nothing to help with.

OpenRouter's published free-tier limits are 20 requests/minute and 50
requests/day, rising to 1,000/day once an account has ever purchased 10
credits. A 40-student section with the grounding audit on is **80 requests**,
so it silently exceeded the cap. That is very likely a contributing cause of
the earlier mass failures, alongside the rejected key.

- **A pre-flight budget** in the panel: the request count against the free cap,
  shown before the batch rather than discovered at student 25, with the
  arithmetic for turning the audit off.
- **Automatic pacing** when a free model is selected — 18/min, just under the
  limit. `LLMClient.min_interval` waits between calls; pacing is strictly
  cheaper than absorbing a 429, which costs a round trip, a backoff sleep, and
  possibly part of the daily allowance. Paid models are never slowed.
- `llm.is_free_model`, `FREE_TIER_RPM`, `FREE_TIER_DAILY` and
  `FREE_TIER_DAILY_WITH_CREDITS` state the policy in one place so the UI can do
  arithmetic against it.
- **Refreshed free-model seeds** to the current roster
  (`nvidia/nemotron-3-ultra-550b:free`, `nvidia/nemotron-3.5-lightning:free`,
  `thinkingmachines/inkling:free`); TransQ's snapshot had gone stale. The live
  catalog remains the authority.
- `docs/AI_FEEDBACK.md` gains a free-tier section with the section-size table.

5 new tests (109 total).

## 2.5.0 — partial replies are recovered, and empty ones say so

Two problems from the same screenshot, both mine.

### 7233 characters were being thrown away

A student failed with "stopped at its output limit after 7233 characters". Those
characters were a nearly-complete narrative, and the code kept none of them:
`salvage_object_fields` needs a comma at depth one to close the object, so a
reply cut off *inside its first field* salvaged nothing. A rambling model hits
the cap mid-paragraph, which is exactly where the most text is at stake and
exactly where the old salvage was blind.

Recovery is now a four-level ladder:

1. **Retry once with a much bigger ceiling** (`retry_truncated`, on by default).
   The only level that yields a *complete* draft, so it goes first — it is the
   "resubmit to complete" an instructor would otherwise do by hand forty times.
   A retry that comes back whole clears the truncation state entirely; a
   completed draft is not a salvaged one.
2. **Structural salvage** — the fields that closed cleanly (unchanged).
3. **`salvage_partial_strings`** — prose recovered from a field cut mid-sentence.
4. **`readable_fragment`** — whatever prose can be pulled from a reply no parser
   could touch. A model that answered in prose instead of JSON still did the
   work.

Every draft now keeps `raw_partial`: the exact bytes that arrived before the
cut, shown under a **Raw reply** tab, and under **What came back** even when the
draft failed outright. Nothing is discarded silently.

The default reply cap rose from 1600 to 4000 tokens, and is now adjustable in
the sidebar. You pay for tokens produced, not for the cap, so a tight ceiling
bought nothing and cost truncated drafts.

### An empty draft no longer reads as a good one

One student showed an empty text box beside "🟡 1 minor flag(s)" — the call had
succeeded with every narrative field blank, and the only flag was the
truncation note. That is the most misleading thing the panel could say.

`Draft.empty` is now an explicit state, distinct from both "failed" and "too
thin to write". It carries a high-severity flag, scores 0.0, is excluded from
bulk approval and from `approved_narratives`, summarises as "came back empty —
retry this student", and gets its **own metric** rather than inflating
"Unsupported claims" — an empty reply is a model-quality problem, not a
grounding one. A "Recovered from a cut-off reply" count sits alongside it.

9 new tests (104 total).

## 2.4.0 — the session actually persists now

In 2.2.0 I made the AI drafts durable and called the persistence problem
solved. It wasn't, and a live run showed why: an uploaded survey plus its
generated output disappeared on sign-out. Two defects, one of them mine twice
over.

### The drafts were never lost — they were unaddressable

Drafts are keyed by survey slug, and the slug is built from the course box,
which resets to empty on sign-in. Drafts written under `Testing2-eval1` were
then looked for under `section-eval1`. They were sitting in the vault the whole
time under a name the app had forgotten how to ask for.

Persistence keyed to transient UI state is not persistence. So the fix isn't a
better key: the **workspace** is now durable and restores as a unit, course
name included, which is what reconnects the drafts.

### The responses were never persisted at all

`long_df` lived in `st.session_state` alone. Worse, the `.ppx` bundle — the one
deliberate save available — held *only* `long_df`. `self_evals` (every
self-rating) and `roster` (every name-to-email mapping) were not in it, so even
a diligent manual save came back without the self-evaluation column and without
the ability to email anyone.

New in this release:

- **`peerparley/workspace.py`** — autosaves the responses, self-evaluations,
  roster, course and evaluation number per instructor, to the encrypted vault.
  Fingerprint-guarded, best-effort, never raises.
- **A "Resume it" card in the sidebar** naming what it would bring back
  ("Testing2 · Eval 1 · 40 evaluation rows · saved Sep 15 at 11:08 PM"), plus
  Discard. Resuming pre-sets the slug so the cross-survey guard doesn't read the
  restored course as a survey switch and wipe what was just loaded.
- **`.ppx` bundles now carry everything**: responses, self-evaluations, roster,
  course name, and the AI drafts with their edits and approvals. Bundles written
  by earlier versions still load, and say plainly what that format could not
  carry rather than handing back a thinner session in silence.
- **Orphaned drafts are visible.** When the panel finds none for the current
  survey but some exist for others, it lists those slugs instead of leaving them
  invisible — the state that produced "my info was gone".

### Known behaviour, not a bug

A Qualtrics/raw upload doesn't create a *survey record*, which is why an
uploaded course never appears in the sidebar dropdown — that list is built from
saved survey setups. The resume card covers it, so the dropdown is no longer
the only way back to an uploaded section.

11 new tests (95 total) and 16 new end-to-end checks (69 total).

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
