# Changelog

## 2.14.1 — ModuleNotFoundError: dropbox, at sign-in

Reported with a screenshot: signing in produced a traceback ending in
`import dropbox`. My regression, from 2.10.0.

That release split the dependencies into `requirements.txt` and
`requirements-optional.txt`, and put the Dropbox SDK in the optional file.
Streamlit Community Cloud installs **only** `requirements.txt`. So a deployment
whose secrets said `backend = "dropbox"` — one that had been working for weeks —
could not import its own storage, and the failure landed on the instructor's
sign-in screen as a raw traceback.

**The storage backends are back in `requirements.txt`**, where they belong:
which one an app uses is a line in its secrets, not a code change, so every
backend the app offers has to be installed by default. `msal` was already
there; `dropbox` is again; pCloud needs nothing beyond `requests`. A test now
reads `requirements.txt` and fails if any backend's package is missing from it.

**And a backend that cannot be opened is no longer fatal.** `Vault()` is built
in a dozen places, including the sign-in path, so a configuration problem has
to be reportable rather than explosive:

* it now remembers the failure instead of raising during construction, and
  reports it as `VaultUnavailable` with a message an instructor can act on —
  which package to add, to which file, or which setting to change;
* the app checks storage before the sign-in box and shows that message on a
  screen of its own, with what usually causes it, rather than letting someone
  sign in "successfully" into an app that cannot save anything;
* it says plainly that nothing has been lost, because the app has not been able
  to write anything either;
* a student opening their link sees the ordinary "this evaluation isn't
  available right now" — never the institution's storage configuration;
* `selfcheck.py` now checks that the configured backend's package is installed.
  That is precisely the question it exists to ask, and it was not asking it.

9 new tests (261 total), including a driver that hides the `dropbox` module
from the import system and walks the whole failure: the library, the
instructor's screen, and what the student sees.

## 2.14.0 — the instructor summary's metrics were four copies of one bug

Reported: *"in the instructor summary the metrics don't seem to be reporting
correctly, they are same for all students regardless of input."* Measured, that
was true of four columns, and all four came from `score_comment`.

It was `passed / 5` over five all-or-nothing thresholds, so it could return only
six values — and on ten realistic comments **nine returned exactly 0.20**, a
one-word "fine" scoring the same as a thoughtful nineteen-word sentence, because
one word trivially passes the "variety" check. That constant was the **Q**
column; times ten it was the **Resp.Pts** column; and because Q multiplies the
grade adjustment it removed four fifths of every adjustment, which 5% rounding
then took to zero, flattening **Mult** and **Grade Δ** for the whole section.
The rendering was never at fault.

**The score now distinguishes comments.** Each component is a ramp, not a cliff,
weighted by what it says about whether a comment substantiates a rating:
substance 45%, naming something concrete 30%, more than one sentence 10%, varied
prose 15%. Length counts only in proportion to variety, so padding cannot buy a
score (200 repetitions of "good" scores 0.13, where it used to score 0.35). The
keyword list now covers the nouns of project work as well as the verbs, and
longer stems match inside a word — "rebuilt the financial model" was being
scored as naming nothing at all. On ten real comments the score runs 0.98 → 0.07
monotonically, with nine distinct values.

**Two aggregations changed, both chosen deliberately.** Q is the *average* of
the comments written about a student rather than the best one, which had made it
a property of the team — one articulate teammate lifted everybody's Q
identically. And Resp.Pts is the average across the teammates a student was
asked about, with a teammate they skipped counting as zero; one careful comment
plus three "fine"s used to score full marks. A survey with no written feedback at
all leaves the dampener inactive, so numbers-only sections still grade, and a
student whose teammates all left their boxes blank takes the team average rather
than a zero they did not earn.

**And when nothing moves, the app says why.** A section where every Grade Δ is
0% reads as a broken report. Usually nothing is broken: sensitivity, agreement,
written support and the rounding step multiply out to less than one step. The
Results tab and the summary PDF now state that arithmetic with the instructor's
own numbers — "the furthest anyone sat from their team's average was 4%, and
written support averaged 0.23, so at sensitivity 0.5 the largest adjustment this
section could produce was about 0.5%, which rounds to 0 in 5% steps" — and name
the two settings that change it.

Q still dampens the adjustment, which is the original design and is now worth
keeping because it finally varies.

15 new tests (258 total), including one that reads the finished PDF and asserts
that every column the complaint named differs between students.

## 2.13.0 — deleting a finished survey

The sidebar's survey list only ever grew. After a few semesters of one course it
is unusable, and there was no way to shorten it.

Selecting a survey now offers **🗑 Delete this survey…**. It removes everything
stored for that survey: the questions and schedule, the roster snapshot that
student links resolve against, every collected response, and its AI drafts.
Other surveys are untouched — including one whose name is a prefix of it, since
`QA101-eval1` sits next to `QA101-eval10` and a careless prefix match would take
both. That case is a test.

Because this destroys student records and the vault has no trash:

* the confirmation states what would go, counted — "2 student(s) in 1 team(s),
  **2 collected response(s)**, the questions and the schedule — 5 stored items
  in all";
* where responses exist it asks for the survey's name to be typed, and the
  delete button stays disabled until it matches;
* it points at the **🔒 Vault** tab first, so an encrypted backup can be kept
  if the responses might still be wanted;
* only the owner or an administrator is offered it, and an instructor never sees
  another instructor's surveys in the list to begin with;
* a survey with nothing collected asks once and deletes — no typing for an
  empty one.

Reading what a deletion would destroy costs no response reads: the counts come
from object names, because this runs while the instructor is looking at a
sidebar. Deleting clears the read cache, so the survey leaves the list
immediately rather than lingering for the cache's lifetime, and any state
carried for it is dropped from the session.

6 new tests (243 total), including a driver that works the sidebar through the
real app: the button's absence on "New survey", both confirmation shapes, a
mistyped name leaving everything in place, and the responses actually gone.

## 2.12.0 — why small clicks were slow

Reported: *"the application still seems very slow when I am doing small tasks
like changing the schedule."* Measured before changing anything, because the
cause was not what it felt like.

### What was actually happening

Streamlit re-executes the whole script on every click, keystroke and checkbox.
Instrumented with a full 40-student section, **one rerun asked storage for 52
objects**: the survey, the roster snapshot, all forty responses, the account
list. On a laptop with local files that is a fifth of a second and invisible —
which is why it survived the earlier performance pass, where I measured
computation and never counted round trips. On Microsoft 365, Dropbox or pCloud,
where every read is an HTTPS request of 100–300ms, the same click costs **five
to fifteen seconds**. Changing a closing date paid for forty student responses
to be fetched and decrypted.

Forty of those reads came from one line I added in 2.9.0: the Start here
checklist showed "Collect responses (N in so far)", and it got N by loading
every submission.

### A rerun now costs no storage round trips at all

Measured the same way: **52 → 0** for a steady-state rerun and for a schedule
change. At 150ms a call that is about eight seconds of waiting per click
removed.

* **Reads are remembered for fifteen seconds**, in `Vault` itself, so every
  caller benefits — surveys, rosters, drafts, accounts, the workspace. Two
  rules keep it honest: a write refreshes the entry it wrote, so a save is
  visible immediately and never costs a read; and nothing lives longer than the
  window, which is what keeps a student's submission — written by another
  process — from going unnoticed. **Check for new responses** clears it
  outright.
* The cache key includes the store's absolute path, not just the backend name
  and folder. The test suite caught that immediately: two local vaults with the
  same folder name under different working directories are different stores
  holding different students.
* **Counting responses no longer reads them.** `survey.response_count()`
  answers from the object names alone.
* The storage healthcheck in the sidebar, which runs on every rerun, goes
  through the cache too. A green badge up to fifteen seconds old is a fair
  trade for not charging a network call per click; a store that goes away is
  still reported inside that window.

### Two other things that ran on every rerun

* **A student's report was being built and rasterised** for a preview panel
  nobody had opened — the body of a `st.expander` executes whether or not it is
  open. It is now behind a checkbox, like the instructor previews.
* **The AI review panel rendered all forty students at once** — each row an
  expander holding a text box, a checkbox, a radio and three tabs, every
  element of it rebuilt on every keystroke. It now shows ten at a time, with
  edits and approvals kept when you move between groups.

### Kept honest

`magicEnabled = false` is now set, but **not** as a speed-up: Streamlit caches
the compiled script, so its syntax-tree rewrite happens once per process rather
than per rerun, and timing it showed no difference. It is set because this app
never relies on magic — a test asserts that — so turning it off removes a way
for a stray line to print something to an instructor. `fastReruns = true` does
affect how the app feels: a rerun already in flight is abandoned when the
instructor clicks something else.

### A test that holds the line

`tests/_rerun_cost.py` seeds a 40-student section, drives the real app, and
counts round trips **at the backend, below the cache** — the number a timing on
local disk cannot see. The budget is four; it currently measures zero, and any
change that reintroduces a per-student read will fail it.

4 new tests (237 total).

## 2.11.1 — opening a survey a second time

Reported from use: *"when I open a second time the save — and make student
links live is grayed out and can't be regenerated even after a schedule change,
and the send personal links can't be regenerated and sent."*

Correct, and my fault — introduced with the save guards in 2.9.0/2.10.0. Both
sections were gated on the *uploaded* contact list, and the file uploader is
empty when you come back to a survey you set up last week. So the page offered
no way to change a closing date and no way to re-send a link without
re-uploading a file that had not changed.

Three fixes, all on the same idea: **the saved roster is the student list.**

* **Saving works without an upload.** With a survey already stored, the button
  reads "Save these settings" and writes the questions, the schedule, the
  report choices and the scheduling clock while leaving the roster untouched.
  That is also the safer of the two operations: a student's identity in a
  submission is their position in the stored roster, so the roster is exactly
  the thing not to rewrite once answers exist — and here it is not touched at
  all. Uploading a list is needed only when the list itself has changed, and
  the existing guard against re-pointing collected answers still applies then.
* **Links are rebuilt from the saved list, always.** A link token carries a team
  and a position *in the stored roster*, so that is what a link resolves
  against; building links from a fresh upload could hand a student someone
  else's form if the two had drifted. As a consequence they are available on
  every visit, and the page says so when an uploaded list differs from the
  saved one.
* **The page comes back showing what was saved.** `survey_cfg` was rebuilt from
  the factory defaults on a new session, so a returning instructor saw default
  questions and an empty schedule — and "Save these settings" would then have
  written those defaults over their real ones. The saved configuration is now
  loaded once per survey, and dropped when you switch surveys.

The teams preview, the student count and the no-email warning now come from the
saved list too, so a second visit no longer looks like an unconfigured one.

4 new tests (233 total), one of them driving the real page with the uploader
empty — the reported case, which nothing covered.

## 2.11.0 — sending, and whose clock it is

Four changes to how an evaluation goes out, all of them defaults or wording
that an instructor meets before they understand the app.

### The default way to send is now the one that works for everybody

Both "how do you want to send?" menus open on **Download auto-send scripts
(Mac / Windows)**. It needs no app registration, no SMTP password and no
decision about this app's mail settings: it drives the Outlook or Mail already
signed in on the instructor's own computer, so the messages come from their own
address. The two "send now" transports are still there, one place down.

### Every send method offers a review step

"Prepare them for me to look over first — don't send yet" is now on every
method that can put mail in the world, ticked by default, and on the auto-send
pack it is real rather than reassuring: the generated scripts call Outlook's
`.Save()` instead of `.Send()` and `save` instead of `send` in both Mac mail
apps, so every message lands in the Drafts folder and nothing leaves.

The pack says so too — the launcher is named `Prepare drafts (Windows).cmd`,
and the README in the zip describes drafts rather than sending. That matters
because the instructor opens that folder days later, when the checkbox is long
forgotten and the files are the only thing left saying what will happen. The
two methods that cannot send anything (mailto, .eml files) now say that in
place of the checkbox, so the same question has an answer everywhere.

### This app's web address fills itself in

The address that builds every student link is taken from the page the
instructor is actually on, through the browser context. Until 2.9 the box
carried a plausible default belonging to a different deployment; since then it
has been empty and typed in by hand. Now it is right by default, and if the box
is later changed to something else the app says plainly that students will be
sent somewhere other than where you are.

The query string is dropped on purpose: it carries this app's own state, and on
a student's device it carries their personal token.

### The scheduling clock is something you set

Open and close times were compared against the *server's* clock, which on
Streamlit Community Cloud is UTC. An instructor in Arizona setting "closes
Friday 5pm" was setting 5pm UTC — 10am their time — so the survey shut
mid-morning, and the panel disclosed this by printing the server clock rather
than solving it.

The schedule panel now asks what time it is where you are, and every open and
close time, every status badge and every student-facing message is read against
that clock. The browser's own offset is adopted the first time the panel is
opened, so the usual case is a glance rather than arithmetic; it is stored with
the survey, so the schedule means the same thing whoever opens the app. A
survey saved before this release keeps behaving exactly as it did.

### Also

* `use_container_width=True`, whose removal date passed in December 2025, is
  gone from every call. One shim asks the installed Streamlit which spelling it
  takes, so the app works on both the versions `requirements.txt` allows and a
  routine upgrade cannot turn a layout argument into a traceback. The app now
  boots with no deprecation warnings at all.
* The browser-context readers live in `peerparley/webcontext.py` as plain
  functions over a context object, rather than inside `app.py` — a helper
  buried in the Streamlit script cannot be tested, and the last defect of that
  shape took a whole page down.

17 new tests (229 total).

## 2.10.0 — first public release

Release validation, wearing a quality-control hat rather than a feature one:
what does a *new* user meet, and does it work? Four things were release
blockers, and all four were found by driving the app rather than by reading it.

### A new installation had no way to start

With no secrets configured — the state every new user is in — the app showed a
red box quoting a shell one-liner and, measured with Streamlit's own test
harness, **zero fields and zero buttons**. There was nothing to click. An
instructor handed this app by a colleague was stopped on the first screen.

There is now a setup screen: it takes an administrator password, generates the
encryption key itself, and either writes `.streamlit/secrets.toml` or hands
over the exact text to paste into Streamlit Cloud's Secrets box. No terminal,
no `hashlib` one-liner. It also covers the second broken state — a password set
but no encryption key — which used to let someone type in forty students and
discover only at the first save that nothing could be stored.

It appears **only** in those two states, refuses to overwrite an existing
secrets file, and offers no file-write on a hosted server where the file would
not survive a restart. A configured deployment never sees it; that is asserted
in both test suites.

### Nothing tested the path almost everyone takes

The regression harness graded a synthetic *Qualtrics* export, and the unit
tests covered the AI writer. The built-in survey — PeerParley's own reason for
existing — had no automated test at all. Everything between "save the roster"
and "a graded PDF" was checked by hand only.

`tests/test_survey_end_to_end.py` now drives it for real, through a live
encrypted vault in a temp folder: a signed link per student, tokens that resolve
to the right person and fail when tampered with, a submission validated by the
same gate the form uses, responses that are ciphertext on disk (asserted by
searching the files for the students' own words), grading, the self-evaluation
pull, the report, the confidential note reaching the instructor and no student,
and a section where only one student answered.

### A first install could break later through no fault of its own

Every dependency was `>=` with no ceiling, so a major release of pandas or
Streamlit lands in a *fresh* install months from now and breaks it for someone
who changed nothing. All core requirements now have both bounds, and a test
fails if one loses its ceiling.

The one file also mixed what the app needs with what it does not: a new user
installed three AI SDKs and the Dropbox client to run a peer evaluation. It is
now `requirements.txt` (the app), `requirements-optional.txt` (AI writer,
Dropbox, PDF previews, the rich email editor) and `requirements-dev.txt`
(pytest). Validated on a clean virtual environment with only the core file
installed: every module imports, the app boots, and a feature whose package is
missing names that package instead of raising.

### A deprecated call already past its removal date

`st.components.v1.html` is deprecated with a removal date that has passed, and
the app called it on every page load. Found by booting against two Streamlit
versions. It is now `st.iframe` where available — which rejects the `height=0`
the old call accepted, so the two could not simply be swapped — with the old
call as the fallback and both wrapped, because the only thing at stake is
whether the page scrolls itself.

### Also in this release

* **`selfcheck.py`** — "is this installation usable?" in one command. Python
  version, packages (required and optional, with what each is for),
  configuration, an encryption round trip, a real write-read-delete against
  storage, email and AI readiness, then a plain verdict and an exit code. It
  changes nothing, and it never prints an API key.
* **`RELEASE.md`** — what was validated, the commands that prove it, and the
  limits stated rather than hidden.
* Python 3.10 is now declared and enforced at import with a readable message.
  Several dataclasses use `X | None` annotations that are evaluated at class
  creation, so 3.9 failed with "unsupported operand type(s) for |" — which
  tells a new user nothing.
* `VAULT_FOLDER` is honoured as an environment variable. The backend already
  was, so configuration this module documents as working only half worked.
* The licence names a person rather than a GitHub handle, and two
  how-to-push-this-to-GitHub notes left over from development no longer ship.
* README and DEPLOYMENT lead with an install that is three commands and a
  setup screen; the manual secret-generation route is kept for the hosted
  deployment, where it is still useful.

26 new tests (212 total) and 3 new regression checks (93 total).

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
