# Built-in survey — setup & collection

PeerParley can now **run the peer evaluation itself** instead of importing a
Qualtrics export. The instructor uploads only the **contact list** (names,
emails, teams); PeerParley serves each student a personal evaluation form and
turns their answers into the same grading input the rest of the app already
uses. The Qualtrics upload path is still there as a fallback.

## What was added

| File | Change |
|---|---|
| `peerparley/tokens.py` | **new** — signed, tamper-proof student-link tokens (HMAC-SHA256). |
| `peerparley/survey.py` | **new** — survey config + open/close schedule, roster snapshot with **owner**, encrypted storage of survey/roster/responses, link generation, `responses → long_df`, survey listing + access checks, and the public student form. |
| `peerparley/accounts.py` | **new** — multi-instructor accounts (PBKDF2 hashes in the vault), roles, and a shared-password break-glass admin. |
| `peerparley/auth.py` | **modified** — per-user username/password login (replaces the single shared-password gate; the old password still works as admin). |
| `peerparley/config.py` | **modified** — added optional `token_secret` and `public_url`. |
| `peerparley/grading.py` | **modified** — rating-matrix dimension grades, forced-ranking performance, pay grade, and self-evaluation (full `PeerParley_V2_3` parity); unchanged for allocation-only data. |
| `peerparley/pdfgen.py` | **modified** — V2_3-style student PDF (respects report toggles), landscape instructor summary with confidential comments, and a cross-round comparison PDF. |
| `peerparley/ui_helpers.py` | **modified** — `build_messages` passes the instructor's report display settings into each student PDF. |
| `app.py` | **modified** — student-link interceptor, two new tabs, user-aware sidebar with a survey picker + admin user panel, ownership enforcement, delivery-method pickers, and CSV downloads. |

Nothing in `grading.py`, `pdfgen.py`, `email_delivery.py`, `vault.py`, or
`security.py` changed. No new dependencies — `requirements.txt` is unchanged.

## The survey (matches the Qualtrics instrument)

The student form is modelled on the MGT 301 peer evaluation, block for block, and
every student evaluates **each teammate and themselves**:

1. **Header** — name, class, section, team, and the list of members being evaluated.
2. **Rating matrix** — four statements on a 7-point *Strongly agree → Strongly
   disagree* scale (team player · did their share · quality of work · team
   performed well because of them).
3. **Qualitative** — two anonymous questions per member: how to *increase* their
   contribution, and their *most significant* contribution.
4. **Forced ranking** — each member into High / Adequate / Low performer (every
   category used at least once).
5. **Pay allocation** — split $100 across the team.
6. **Your contribution** — free text, released to the team anonymously.
7. **Confidential note** — to the instructor, never released.

**The instructor turns any block on or off** in Survey setup → *Questions*, and
edits every prompt and the four statements in → *Wording*. Nothing is
pre-selected on the rating/ranking questions, so an unanswered item is caught
rather than silently recorded as neutral.

**Fast, self-checking form.** Ratings and the forced ranking use **radio buttons**
(not dropdowns), and the form validates **as the student answers** — a live
running total on the $100 allocation, inline "rank everyone / use every category"
checks, and a count of unanswered ratings. The **Submit** button stays disabled,
with a clear checklist of what's left, until everything required is complete, so
students can't submit an incomplete or mis-totalled evaluation.

**Full workbook parity.** The engine reproduces the original `PeerParley_V2_3`
calculations:

- **Grade adjustment** — `Team Score × [1 + B·A·Q·(peer ratio − 1)]`, agreement
  weight `A` banded at 10/20/30%.
- **Response-quality Points** — each student earns points for the **quality of the
  feedback they wrote** (author-side comment-support score `Q × max_points`),
  matching the workbook's feedback points.
- **Dimension letter grades** — Team Player / Quantity / Quality / Effect, each
  `mean(rating)/7 → letter` from the 4-statement matrix.
- **Performance** — from the **forced ranking** (High / Adequate / Low) when
  collected, otherwise the allocation ratio.
- **Pay grade** — average received allocation ÷ team average.
- **Self-evaluation** — the student's own ratings shown beside the peer grades.

**The reports:**

- **Instructor summary PDF** (landscape) — every student's dimension grades, pay
  grade, agreement `A`, support `Q`, **response-quality points**, multiplier,
  grade Δ, and performance, with a legend. Download it directly from Review &
  PDFs, or in the full zip.
- **Student feedback PDF** — styled like the original project: rating meters
  (teammates' average with your self-rating tick and peer/self letter badges),
  performance + pay grade + grade-adjustment meter, "What your teammates valued"
  and "Where to focus next", and "The feedback you gave" (your Q + points).

With only allocation + comments (a plain Qualtrics export), the engine degrades
gracefully — the dimension/ranking fields are simply blank.

**Default public URL.** `public_url` now defaults to
`https://peerparley.streamlit.app`, so student links prefill correctly without
any secret; set the `public_url` secret to override.

## Multiple instructors, per-section visibility

Several instructors share one deployment. Each signs in with their **own
username and password** (stored as salted PBKDF2 hashes in the vault, never in
plaintext). Every survey is stamped with an **owner**:

- **Instructors** see and administer only the sections they own — the sidebar
  survey picker lists just theirs, and another instructor's responses are
  blocked with a notice.
- **Administrators** see and administer **every** instructor's sections, and get
  a **👥 Manage instructors** panel in the sidebar to add accounts, reset
  passwords, change roles, deactivate, or remove them.

The app's original shared password (`app_password_sha256`) still works — it signs
you in as a built-in **admin** called `admin`, so nothing breaks and you can't be
locked out. Sign in with it first, then add instructors. New accounts get a
temporary password and must set their own on first sign-in.

## CSV everywhere (send it yourself)

Both sides can be exported instead of emailed from the app:

- **Invitations** — `links.csv` (every student's name, email, and personal link)
  in the Survey setup tab.
- **Results** — `recipients.csv` (name, email, rendered subject/body, and the
  individual PDF filename) in the Email tab, alongside the grades CSV in Review &
  PDFs and the PDF bundle. Download those and mail-merge them yourself.

## The two new tabs

**1 · Survey setup** — upload the contact list, edit the survey wording and the
points total, set an **open/close schedule** (or use the master on/off switch),
and **Save survey + roster to vault**. A status line shows whether the survey is
Open / Scheduled / Closed for students right now. Then generate every student's
personal link and either email it or download **links.csv** for your own mail
merge.

**Removing a finished survey.** The sidebar's survey list only ever grew — a few
semesters of one course and the dropdown is unusable. Selecting a survey now
offers **🗑 Delete this survey…**, which removes its questions, its schedule, the
roster snapshot that student links resolve against, every collected response and
its AI drafts. It states what will go before it does anything, and where
responses exist it asks for the survey's name to be typed, because the vault has
no trash: deletion is permanent. Save an encrypted bundle from the **🔒 Vault**
tab first if the responses might still be wanted. Only the survey's owner or an
administrator is offered the control, and an instructor never sees another
instructor's surveys in the list at all.

**Open / close dates.** The survey config carries optional `opens_at` /
`closes_at` datetimes, read against the **scheduling clock** you set in the same
panel (`clock_offset_minutes`, stored with the survey). That clock starts from
your browser's own time zone, so "closes Friday 5pm" means 5pm where you are —
not 5pm wherever the app happens to be running, which on Streamlit Cloud is UTC.
Surveys saved before 2.11.0 have no offset stored and keep using the server's
clock. Before the open date the student form says "opens on…"; after the close
date it says "closed"; submissions are blocked outside the window (re-checked on
submit, so a deadline that passes mid-session still holds). The master switch
closes it instantly regardless of dates.

**Email is not tied to Microsoft 365, and nothing sends by accident.** Every
send point (invitations, reminders, and the results Email tab) offers the same
menu, which opens on **Download auto-send scripts** — a zip that drives the
Outlook or Mail already signed in on your own computer, so no app registration
or SMTP password is needed and the mail comes from your address. *Microsoft 365
(Graph)* and *SMTP server* (credentials from `[email]` secrets) are there for
sending from the app itself, along with .eml files, mailto links and a CSV for
your own mail merge.

Every method that can send offers **"prepare them for me to look over first"**,
ticked by default. It is not cosmetic: Graph writes Outlook drafts, SMTP hands
back a zip of ready-to-send files, and the auto-send scripts call Outlook's
`.Save()` and the Mac apps' `save` instead of sending — so each message waits in
your Drafts folder. The zip is named for what it does (`Prepare drafts
(Windows).cmd`), because that folder is what you open days later.

**2 · Responses** — response counts overall and per team, a non-responder list
you can download or email a reminder to, and **Load responses into grading**,
which pulls every submission, builds the tidy grading input, and hands it to the
existing **Review & PDFs** and **Email** tabs. From there everything works
exactly as before.

## How students submit

Each link is `https://<your-app>/?t=<token>`. The token is a signed
`{slug, student id}`; the app detects it **before** the instructor password gate
and shows that student their form — no login, and no student can edit the URL to
reach anyone else (a bad signature is rejected). Students can reopen their link
to revise until the survey is closed.

The id is **permanent**, assigned the first time a student appears in a contact
list and kept for as long as the survey exists. That is what lets the roster be
edited after links have gone out: a student can be added, moved to another team
or removed, and everyone else's link keeps opening their own form. A student who
is moved follows the same link and finds their new teammates on it.

Until 2.16.0 the token carried a team and a **position** instead — the student's
index in their team's list — so adding one student named "Adams" renumbered
everyone behind them and the links already sent pointed at the wrong people.
Links issued under that scheme are still honoured, and a survey stored under it
is migrated the first time it is opened.

## Editing teams after the roster is saved

**① Set up survey → 👥 Teams — add, move or remove a student.** Every team is
listed with its members; each student has a *Move* (to any team, or to no team
at all) and a *Remove*, and teams can be created, renamed and dissolved.

An edit never silently changes a grade. The only answers it is allowed to
discard are the ones that have stopped describing anything:

| Edit | What goes | What stays |
|---|---|---|
| Move a student | What they wrote about the team they are leaving, and what that team wrote about them | Their own submission and self-evaluation; everything their old teammates wrote about each *other*; every link |
| Remove a student | Their submission, and every reference to them | Everybody else's submissions and links |
| Rename a team | Nothing | Everything; submissions are relabelled to the new name |
| Dissolve a team | Nothing | Its students move to the no-team list and keep their submissions — put them on a team and they grade again |

Any edit that would touch stored answers is shown first, in plain English and
naming the students affected, and waits for a yes. An edit that costs nothing —
the ordinary case before links go out — just happens.

A student with no team stays on the roster rather than disappearing (they used
to be dropped silently), and their link tells them there is nothing to fill in
yet. A team with only one member is kept and flagged **not gradeable yet**
rather than being dissolved, so a team can be built up one student at a time.

A moved or newly added student whom nobody has evaluated appears in the results
as **No data**, with their grade left at the team score — not as a low score,
and not missing from the report.

## Writing the emails

Every message the app sends — the invitation, the reminder and the results
email — is written with a formatting toolbar rather than as HTML, and each has a
preview showing the message as one real student receives it, with their name,
team and link filled in. The plain-text version a mail client without
formatting would show is there too.

The body that is stored and sent is still HTML, because that is what an email
is; **Edit the HTML directly** is a checkbox for pasting in markup you already
have. Placeholders — `{first_name}`, `{last_name}`, `{name}`, `{team}`,
`{class}`, `{eval_no}`, `{link}` — are substituted per student, and any braced
name that is *not* one of them is flagged above the send button, since it would
otherwise reach every student exactly as typed.

## Privacy / storage

Responses are written to the **same encrypted vault** as the rest of the app
(`survey__<slug>.ppj`, `roster__<slug>.ppj`, `resp__<slug>__m<id>.ppj`),
Fernet-encrypted before they leave the process. This matches the app's existing
single-key model — simpler than a split-key scheme, and consistent with how the
vault already protects PII.

> **Use a real vault backend for collection.** With `backend = "local"` the
> responses live only in the ephemeral Streamlit Cloud container and vanish on
> restart. Set `vault.backend` to `m365` / `dropbox` / `pcloud` so the student
> form and the instructor console share one durable, encrypted store.

## New secrets (both optional)

```toml
# .streamlit/secrets.toml
token_secret = "any long random string"   # signs student links
public_url   = "https://your-app.streamlit.app"   # used to build links
```

`token_secret` is optional — if omitted, a stable secret is derived from your
existing `fernet_key`, so links work out of the box. `public_url` just
pre-fills the link base in the Survey setup tab; you can also type it there.
Changing `token_secret` (or `fernet_key`, if you rely on the fallback)
invalidates links already sent.
