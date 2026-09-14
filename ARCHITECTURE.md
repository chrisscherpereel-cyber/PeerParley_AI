# Architecture

```
        ┌──────────────────────── PUBLIC TIER ────────────────────────┐
        │  Streamlit Community Cloud (from GitHub repo)                │
        │                                                             │
        │  app.py  ── shared-password gate (SHA-256)                   │
        │     │                                                       │
        │     ├─ ingest ─ grading ─ pdfgen   (all IN-MEMORY only)      │
        │     │                                                       │
        │     ├─ feedback_ai ─ llm  ──► LLM vendor (opt-in) ──────────┐│
        │     │   public comments only; instructor approves output    ││
        │     ├─ security.py  ──► Fernet AES encryption of any PII    │
        │     │                                                       │
        │     ├─ vault.py  ── encrypt-then-upload ──┐                 │
        │     └─ email_delivery.py ── Graph/SMTP ──┐│                 │
        └──────────────────────────────────────────┼┼────────────────┘
                                                    │││ HTTPS/443 only
                                    ciphertext .ppx │││ OAuth tokens
                                                    ▼▼▼
        ┌──────────────────── FIREWALL / UNIVERSITY TIER ─────────────┐
        │                                                             │
        │  Storage vault (choose one):                                │
        │    • Microsoft 365 OneDrive/SharePoint  (NAU tenant)        │
        │    • Dropbox (app folder)                                   │
        │    • pCloud                                                 │
        │  → holds ONLY encrypted bundles; provider sees ciphertext   │
        │                                                             │
        │  Microsoft 365 mailbox (course account)                     │
        │  → sends student emails via Graph                           │
        │                                                             │
        │  Secrets custody:                                           │
        │  → Fernet master key + app registration secrets held        │
        │    university-side (password manager / Entra)               │
        │  → LLM API keys, if used (or none: the "On this computer"   │
        │    provider keeps comments entirely inside the room)        │
        └─────────────────────────────────────────────────────────────┘
```

The AI path is the only egress that carries student *text* rather than
ciphertext, which is why it is opt-in, restricted to the public comment channel,
and gated behind instructor approval. It is also the only one you can remove
entirely: choose the local provider and no comment leaves the machine.

## Design principles

**Single application.** One Streamlit app (`app.py`) drives the whole workflow
in five tabs. The `peerparley/` package is internal structure, not separate
apps — you deploy and run exactly one process.

**PII never rests in plaintext on the public host.** Uploaded files are parsed
into an in-memory tidy frame. Anything written anywhere durable goes through
`security.encrypt_*` first. The `.gitignore` guarantees data files can't be
committed.

**Encryption is separate from transport.** Even though M365/Dropbox/pCloud all
use TLS in transit, the payload is *also* Fernet-encrypted at the application
layer, so the storage provider never holds decryptable student data. Confiden-
tiality reduces to custody of one Fernet key.

**Pluggable storage.** `vault.py` exposes a 4-method interface
(`put/get/list/delete`) with interchangeable backends, so moving from Dropbox to
your NAU M365 tenant is a secrets change, not a code change.

**Firewall-friendly by protocol.** All egress is HTTPS/443 (Graph, Dropbox,
pCloud REST). The email path uses the OAuth device-code flow specifically so it
works from networks that block SMTP and non-standard ports.

**Privacy validation at the last mile.** `email_delivery.validate_message`
checks that each outgoing message's attachments belong to its recipient before
anything is sent, preventing cross-student leakage.

## Grading model (summary)

```
individual = team_score × clamp(1 + B·A·Q·(peer_ratio − 1), min_mult, max_mult)
```

- `peer_ratio` = student's avg received allocation ÷ the team average
  (1.0 = the team average, so above → bonus, below → deduction).
- `A` = agreement weight from SD of received points, banded 10/20/30% →
  1.00/0.75/0.50/0.25.
- `Q` = comment-support score (0–1) from `comments.py` completeness +
  repetition + cross-comment similarity checks.
- `B`, the separate increase/decrease caps, rounding, performance method, and
  the **agreement guard** (softens a forced "Low" when evaluators themselves
  disagree) are all instructor-set.

See `peerparley/grading.py` and `peerparley/comments.py` for the exact bands.
Swap in your production formula there without touching the rest of the app.

## AI feedback narrative (v2, optional)

```
StudentResult
   │
   ├─ build_source ──► FeedbackSource     public comments (+ ratings, optionally)
   │                   • confidential comments are NEVER included
   │                   • <2 comments or <15 words → skipped, no API call
   ▼
generate_draft ──► Draft {prose + points, each carrying its verbatim source}
   │
   ├─ check_quotes    deterministic, no API call: does each cited comment
   │                  actually exist? overstated agreement counts?
   ├─ verify_draft    second call, fresh context: what in this prose goes
   │                  beyond the evidence? (a failed audit ≠ a passed audit)
   ▼
instructor review ──► approved_narratives()  ── the ONLY gate to a student
   │
   ▼
build_individual_pdf(narrative=...)   and every email path, from the same map
```

**Design principles specific to this tier.**

*Grounding is structural, not just instructed.* The model must return the
verbatim comment behind every point it makes. A claim with no source has nowhere
to live in the response schema — a harder constraint than an instruction not to
make one — and the citation can then be checked by string matching rather than
trust.

*The cheap check runs first.* A fabricated citation is both the worst outcome and
the easiest thing to detect, so it costs zero tokens. The paid audit handles only
what string matching cannot see.

*A check that didn't run is not a check that passed.* An audit failing on a 503
marks the draft unverified and holds it out of bulk approval, rather than
defaulting to clean.

*One gate, not several.* `approved_narratives()` is the single function producing
student-visible text, and the PDF builder, the direct send, the `.eml` pack and
the auto-send pack all read from it. There is no route to a student PDF that
bypasses the instructor's approval by choosing a different delivery method.

*Provider-agnostic by registry.* `aiconfig.PROVIDERS` maps a vendor to one of
three SDK code paths in `llm.py`. Adding a vendor is an entry in that dict;
nothing above `llm.py` knows which one is in use. Ported from the TransQ
lecture-quiz app, which had the same requirement.

*Prompts are a config file.* `ai_prompts.py` contains every prompt and nothing
else, because wording is the biggest lever on quality and an instructor tuning it
for their discipline shouldn't have to read the codebase.
```
