# The AI feedback writer (v2)

PeerParley has always shown students the written comments their teammates left,
as bullets, in their feedback PDF. That is honest but rough: the bullets are the
raw words of five hurried classmates — terse, repetitive, sometimes blunter than
the writer meant. This feature offers a readable narrative built from those same
bullets and nothing else, which you read and approve before any student sees it.

It is **off by default** and entirely optional. Grading, PDFs, email, the vault
and the survey all behave exactly as in v1 with it switched off.

## The one rule

> The model may **reword and expand** what teammates wrote. It may not add
> anything they did not say.

No invented recommendations, no inferred causes, no generic teamwork advice, no
extrapolating one remark into a pattern, no claims about the student's ability
or attitude, no mention of grades or scores, nothing that identifies who said
what. If the comments are sparse, the narrative is short. If they are one-sided,
it is one-sided.

This matters more than the feature's convenience. A student who receives
invented criticism has been wronged in a way good phrasing does not offset, and
an instructor who cannot vouch for a sentence in a feedback report should not be
sending it.

## How the rule is enforced

Four things, in descending order of reliability.

**1. The output shape.** The model must return each point it makes *with the
verbatim comment it came from*. A claim with no source has nowhere to live in the
JSON — a stronger constraint than an instruction not to make one.

**2. The citation check** (`feedback_ai.check_quotes`). Every quote is matched
against the real comments locally, with **no API call**. A quote that does not
appear in the evidence was fabricated, and string matching is a better test of
that than any amount of model self-assessment. This catches the single worst
failure mode: advice the model invented, then attributed to a teammate. It also
catches overstated agreement — one comment cannot have been raised by three
people.

Lightly reworded quotes still pass. Flagging a trimmed clause would cry wolf on
every draft, so a citation counts when it shares at least 80% of its words with
a real comment (`QUOTE_MATCH_THRESHOLD`).

**3. The grounding audit** (`feedback_ai.verify_draft`). A second API call hands
the finished prose and the same evidence to a fresh context and asks what goes
beyond it. This catches what string matching cannot: a real quote attached to a
point it does not support, an invented cause, generic advice smuggled into a
paragraph. Asking the same model in the same breath "and is that grounded?"
reliably gets "yes"; auditing finished text against a fixed evidence list is a
much harder question to answer wrongly.

An audit that *fails to run* is not an audit that passed. The draft is marked
unverified, its score capped, and it is held out of bulk approval.

**4. You.** Nothing reaches a student without your approval. The three checks
above exist to make your review fast and tell you where to look — not to replace
it. Drafts with any flag cannot be swept up by *Approve the clean drafts*; they
have to be opened and read. Your edits are what ship.

## What the model sees

| Included | Why |
|---|---|
| Contribution comments ("what teammates valued") | The evidence |
| Improvement comments ("where to focus") | The evidence |
| Other public comments | The evidence, de-duplicated against the two above |
| The four dimension ratings + performance label | Optional. Lets the narrative match the magnitude of the round rather than treating every one as rosy. The figures are never quoted back to the student. |

**Never included: confidential comments.** They are written to you. A narrative
that echoed one back would leak both its content and the fact that it was meant
to be hidden. This is asserted by a test.

Students who received fewer than two comments, or under about fifteen words
total, are skipped with no API call at all. Asking a model to write from almost
nothing is an invitation to fill the gap, which is the exact failure this feature
exists to prevent. Their raw bullets still appear in the PDF.

## Setup

Pick one provider. All are optional; install only what you use.

```bash
pip install openai        # OpenAI, xAI, OpenRouter, local Ollama / LM Studio
pip install anthropic     # Claude
pip install google-genai  # Gemini
```

Then give the app a key, either in `.streamlit/secrets.toml`:

```toml
# Top level, or grouped under [ai] — both are read.
OPENROUTER_API_KEY = "sk-or-..."
ANTHROPIC_API_KEY  = "sk-ant-..."
OPENAI_API_KEY     = "sk-..."
GEMINI_API_KEY     = "..."
XAI_API_KEY        = "xai-..."
```

…or by pasting one into the sidebar, which keeps it in session memory only. It
is never written to disk, the vault, or the audit trail, and it dies with the
session. For a shared departmental deployment, prefer secrets; for one
instructor trying this out, the sidebar is fine.

| Provider | Notes |
|---|---|
| **OpenRouter** | Default. One key, **every model OpenRouter carries** — the list is fetched live, not hardcoded. |
| **Anthropic Claude** | Strongest at holding to the "invent nothing" rule. |
| **OpenAI** | `gpt-4.1-mini` is the cheap option that still follows the format. |
| **Google Gemini** | Free tier covers a course or two. |
| **xAI Grok** | OpenAI-compatible endpoint. |
| **On this computer** | Ollama or LM Studio, with the model list read from your machine. Free, and no student comment leaves the room — but only reachable when PeerParley runs on that same machine. |

### Choosing an OpenRouter model

OpenRouter carries several hundred models and the roster turns over weekly, so
the picker fetches `GET /api/v1/models` live (public, no key needed), caches it
for an hour, and sorts it A–Z. A hardcoded list would be wrong within a month
and would go on being confidently wrong — offering retired models and hiding
new ones.

- **Free models only** — filters to what OpenRouter serves at $0. 🆓 marks them
  in the list.
- **Filter by vendor** — narrows to `anthropic/`, `deepseek/`, and so on.
- The dropdown is **type-to-search**, so you can jump straight to a slug.
- **↻ Refresh list** re-fetches immediately rather than waiting out the hour.
- **Or a slug** takes anything at all — a model released this morning, or a
  variant not in the list.
- Live prices feed the cost meter, so it quotes what OpenRouter charges today.
  A model whose price OpenRouter doesn't publish shows "price not known" rather
  than a confident $0.00.

Two categories are filtered out because they cannot do this job: image and
video generators (which appear in the same catalog) and `:batch` variants
(asynchronous endpoints that accept a request without answering it).

If the fetch fails — no network, OpenRouter down — the picker falls back to a
bundled snapshot **and says so on screen**, so a stale list is never shown as
though it were current. You can still type any slug by hand.

A caveat specific to this task: free models are rate-limited and smaller, and
rewording peer comments *without adding to them* punishes a weak model in a way
that's easy to miss, because the output still reads fluently.

### Running on OpenRouter's free tier

It works, but the daily ceiling is the thing to plan around. OpenRouter's
published free-tier limits are **20 requests per minute** and **50 requests per
day** — rising to **1,000 per day** once an account has ever purchased 10
credits (a one-time $10; the credits themselves don't expire into the limit,
the higher ceiling just stays).

| Section size | Audit on (2 calls each) | Audit off (1 call each) |
|---|---|---|
| 20 students | 40 — fits in 50/day | 20 — fits |
| **40 students** | **80 — exceeds 50/day** | 40 — fits, no headroom |
| 40 students, after the $10 purchase | 80 — fits easily | 40 — fits easily |

So for a 40-student section on a never-funded account, either turn the grounding
audit off, or split the run across two days. The panel does this arithmetic for
you before the batch and warns when the request count exceeds the cap.

PeerParley also **paces requests automatically** when a free model is selected
(18/min, just under the 20 limit). Pacing is strictly cheaper than absorbing
429s, which cost a round trip, a backoff sleep, and possibly part of the daily
allowance.

Free models available as of September 2026 include
`nvidia/nemotron-3-ultra-550b:free`, `nvidia/nemotron-3.5-lightning:free` and
`thinkingmachines/inkling:free` — all with very large context windows. The
picker's **Free models only** toggle shows the current list, since the roster
changes.

**The genuinely free option with no limits at all** is the local one: Ollama or
LM Studio on your own machine, which also means no student comment ever leaves
the room. That requires running PeerParley locally rather than on Streamlit
Cloud — see "Choosing a local model" above.

### Choosing a local model

The list comes from the server itself — what's offered is what you've actually
downloaded, which is the only list that can be right. PeerParley probes the
address, reports what answered (Ollama on 11434, LM Studio on 1234), and reads
its models. A bare host or a missing `/v1` gets tidied up. **Check again**
re-probes after you start the server.

When the server isn't reachable, the message says *which* cause it is — nothing
listening, wrong port, still loading a model — rather than printing an errno.

If PeerParley is running on Streamlit Cloud, the picker says plainly that a
local model cannot be reached from there and stops, instead of offering an
address that can never work. `localhost` on Streamlit's servers means
Streamlit's container, not your computer.

### Remembering your choices

**💾 Remember these settings** saves your provider, model, tone, length and
grounding options to your account, so you don't re-pick them on every sign-in.

**Your API key is never saved.** Its whole security story is that it lives in
session memory and dies with the session; persisting it for convenience would
make that promise false. Put it in the app's secrets if you want it to persist.

## Using it

1. Sidebar → **🤖 AI feedback writer** → *Enable*. Choose provider, model, tone.
2. Load responses as usual, then open **④ Results & reports**.
3. **Draft feedback for all N.** One or two API calls per student depending on
   whether the audit is on. A 30-student section is well under a dollar on most
   models, and free on the OpenRouter free router or a local model.
4. Read the review panel. Work top-down: 🔴 unsupported claims first, then 🟡
   minor flags, then the rest. Each draft shows its source comments, its
   citations, and its flags side by side with the editable text.
5. Edit anything you would not have written yourself. Approve what you stand
   behind.
6. The PDFs and emails built below the panel include approved narratives under
   **"Summary of your peer feedback"**, above the raw bullets — which are never
   replaced. A student is entitled to their teammates' actual words, and a
   summary they cannot check against the source is worth less, not more.

Every delivery path honours the same approvals — direct send, `.eml` pack,
auto-send scripts, and the PDF zip — so changing send method cannot change what
a student receives.

## The audit trail

**⬇ Audit trail (JSON)** exports every draft, its citations and match scores, its
flags, what you edited, and what you approved, along with the provider, model and
settings used. That is the record of what an AI wrote and what a human signed off
on. Worth keeping with the round's grades, particularly the first time you run it
and if anyone ever asks how the feedback was produced.

## Cost

The sidebar meter counts tokens and estimates dollars as the batch runs. Models
with no published price show tokens and say so rather than inventing a figure;
local models show an exact `$0.00`.

Rough order of magnitude for a 30-student section with the audit on (60 calls):
a few cents on a mini/flash model, well under a dollar on a frontier one.

## Cut-off and empty replies

### A partial reply is never discarded

A model that overruns its token budget has still written most of a draft, and
that text is already paid for. Recovery works down four levels:

1. **Retry once with a much bigger ceiling.** The only level that produces a
   *complete* draft, so it goes first — it's the "resubmit to complete" you'd
   otherwise do by hand, once per student. On by default; switch it off under
   Writing options.
2. **Structural salvage** — the top-level fields that closed cleanly.
3. **Prose salvage** — text recovered from a field cut mid-sentence. This is the
   one that matters most: a rambling model hits the cap inside its first
   paragraph, where structural salvage finds nothing and thousands of readable
   characters are at stake.
4. **A readable fragment** of whatever arrived, even if no parser could touch it
   — a model that answered in prose instead of JSON still did the work.

A recovered draft is labelled as such ("Recovered from a cut-off reply — 7233
characters arrived"), carries a flag so it can't be bulk-approved, and keeps the
exact raw reply under a **Raw reply** tab. Even a draft that failed outright
shows what arrived under **What came back**, so nothing is invisible.

If drafts keep getting cut off, raise **Reply size limit** in the sidebar. You
pay for tokens produced, not for the cap, so a generous ceiling costs nothing.

### "Came back empty"

The request succeeded and the model returned no narrative at all. This is a
**model-quality** problem, not a grounding one, so it has its own metric rather
than counting as an unsupported claim. It shows as a loud flag, is excluded from
bulk approval, and says to retry or pin a stronger model — earlier versions
reported it as "1 minor flag(s)" beside an empty text box, which read as though
the draft were fine.

Free-router models cause most of these. Pinning a real model fixes it.

## When every student fails at once

**First: check which build is running.** The sidebar shows the version next to
the storage backend (`Storage: local · v2.1.2`). The improvements below only
exist from the version that introduced them, so an error whose wording doesn't
match this document usually means the running app is older than the fix — on
Streamlit Cloud, that means the new code hasn't been pushed and redeployed yet.
Locally, restart `streamlit run` after replacing the files.

A row of identical errors across the whole section is almost always one cause,
not forty. The batch now stops at the first rejected request rather than
repeating it, so you get one message instead of a wall of them, and whatever
finished before the stop is kept.

**Use "Test the key" in the sidebar first.** One tiny request, a few tokens,
and it tells you immediately whether the provider accepts the key and the model.

### `401 - User not found` (OpenRouter)

OpenRouter's wording for *this key is not recognised at all*. It reads like a
problem with the student; it is not. In rough order of likelihood:

1. **The key is not an OpenRouter key.** Pasting an `sk-ant-…` or `sk-proj-…`
   key into the OpenRouter slot gives exactly this. The app now checks the key's
   prefix and refuses before spending a single call, but a key created before
   that check may still be sitting in your secrets.
2. **The key was revoked or the account removed.** Check
   <https://openrouter.ai/keys> — if the key isn't listed, create a new one.
3. **You expected the free router to need no key.** It does need one.
   `openrouter/free` means the *models* cost nothing; the account is still what
   authenticates the request. Free is not anonymous.
4. **Whitespace.** A trailing newline in the Streamlit secrets box, or a space
   picked up when copying. Keys are now stripped on every read path, so this is
   fixed going forward.
5. **Credit.** A zero balance usually gives a 402 rather than a 401, but it is
   worth a glance at the dashboard if the key itself looks fine.

### Other providers

| Message | Usually means |
|---|---|
| `401 invalid_api_key` (OpenAI) | Revoked key, or a project key used against the wrong org. |
| `401 invalid x-api-key` (Anthropic) | Key deleted, or an OpenRouter key pasted in. |
| `API key not valid` (Gemini) | Key restricted by referrer/IP, or the Generative Language API not enabled on that project. |
| `403` anywhere | Sometimes region or model permission rather than the key — the message says the key was accepted if that's the case. |
| Connection refused (local) | Ollama or LM Studio isn't running, or the port differs (11434 vs 1234). |

### Not a key problem

If "Test the key" says the key was accepted but the request failed, the
credential is fine — check the model name. A custom model ID with a typo, or a
model your account has no access to, fails per-request rather than globally, so
those still appear per student and are worth retrying with **Draft the
remaining** after fixing.

## Your work is saved

**The whole session, not just the drafts.** The responses, the self-evaluations,
the roster and the course name autosave to the encrypted vault as you work. Sign
back in and the sidebar offers **↩ Resume it**, naming what it would bring
back. Resuming restores the course name too — which is what reconnects the
drafts, because they're filed under it.

If the panel says there are no drafts for this survey but some exist for
others, it lists those. Drafts are filed by course and evaluation number, so
reaching one means setting the course box to match, or resuming that session.

### Drafts specifically

Drafts, your edits, and your approvals are written to the encrypted vault as you
go, keyed to the survey you're working on. Sign out, come back tomorrow, open
the Results tab, and the panel restores what you'd done — it says so when it
does. Nothing is lost to a closed laptop or an idle session timing out.

Because they go to the vault, they are Fernet-encrypted like every other piece
of student data here; the storage provider holds ciphertext. Saving is
best-effort — if the vault is unreachable the panel warns you that the work
won't survive signing out, but it stays in the session meanwhile.

**If you change the Fernet key**, previously saved drafts become unreadable.
The panel tells you that explicitly rather than showing an empty review, since
the two mean very different things.

## Drafting only what's left

Two buttons, and the difference matters:

| Button | What it does |
|---|---|
| **↻ Draft only the N not yet done** | Students with no draft, plus any that failed. Leaves every finished draft — and your edits and approvals on them — untouched. This is the one to use after a partial run. |
| **Redo all N** | Starts over for everybody, discarding finished drafts along with their edits and approvals. |

The retry button is the primary (highlighted) one whenever finished drafts
exist, because it's the one that can't lose work. A caption beside them shows
the split — "31 done, 9 to go" — so you can see what a retry will actually
touch before clicking it.

## Turning it off

Untick *Enable* in the sidebar, or untick "Summary of your peer feedback" under
**① Set up survey → What students see in their feedback report** to hide the
section even for approved drafts. Drafts live in session state only — they are
cleared when you switch surveys and are not written to the vault. Nothing about
grading changes either way.

## Files

| File | Role |
|---|---|
| `peerparley/aiconfig.py` | Provider registry, key resolution, `AISettings` |
| `peerparley/llm.py` | Provider-agnostic client, retries, JSON recovery, pricing |
| `peerparley/ai_prompts.py` | Every prompt, in one file, meant to be edited |
| `peerparley/feedback_ai.py` | Evidence assembly, generation, citation check, audit, approval gate |
| `peerparley/ai_ui.py` | Sidebar settings, model pickers, and the review panel |
| `peerparley/openrouter_catalog.py` | The live OpenRouter catalog (ported from TransQ) |
| `peerparley/localmodels.py` | Local server probing and model discovery (ported from TransQ) |
| `peerparley/workspace.py` | Session autosave and the complete `.ppx` bundle format |
| `tests/test_feedback_ai.py` | 109 offline tests; no API key needed |

The provider layer is ported from **TransQ**, a lecture-quiz builder that solved
the same problem — one instructor-facing Streamlit app that has to talk to
whichever vendor the department has a key for. Adding a vendor is an entry in
`PROVIDERS`, not a branch in the client.

## Editing the prompts

All of them are in `peerparley/ai_prompts.py`, which exists as its own file
because prompt wording is the biggest lever on output quality and you should not
have to read the rest of the codebase to change it. The tone presets are there
too. If you tune the wording for your discipline, run `python -m pytest tests/ -q`
afterwards — several tests assert the evidence boundaries rather than the phrasing,
so they will still tell you if a change opened a hole.
