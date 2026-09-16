"""Headless QA regression harness using Streamlit's AppTest.

Rewritten for v2. The previous version was written against a step-wizard UI
("Next"/"Previous" buttons, a ``step`` session key) that the app replaced with
tabs some time ago, so it failed on its second check against v1 as well as v2 —
it had stopped testing anything. The checks below target what the tabbed app
actually exposes, plus the v2 AI path.

The AI checks run entirely offline. No API key is needed and none is read: a
fake client supplies the model's side, which is the only way to assert what the
app does when a model cites a comment nobody wrote.

Run with:  python qa_regression.py
"""
import pandas as pd
from cryptography.fernet import Fernet
from streamlit.testing.v1 import AppTest


# Two teams of three. Distinct, substantive comments per teammate so the AI
# evidence checks have something real to match against, and one non-submitter
# (Fay Oh) so grade blanking is still covered.
TEAMS = {"A": ["Ann Lee", "Bob Kim", "Cara Ng"],
         "B": ["Dan Ray", "Eve Sun", "Fay Oh"]}
NON_SUBMITTER = "Fay Oh"

CONTRIB = {
    "Ann Lee": "Ann led the regression analysis and walked the team through it twice.",
    "Bob Kim": "Bob rebuilt the slide deck the night before the presentation.",
    "Cara Ng": "Cara kept the shared notes current after every single meeting.",
    "Dan Ray": "Dan wrote the literature review section and sourced every citation.",
    "Eve Sun": "Eve ran the interviews and transcribed all of them herself.",
    "Fay Oh": "Fay built the budget model and caught two errors in our figures.",
}
IMPROVE = {
    "Ann Lee": "Ann sometimes posts updates late at night so we see them next morning.",
    "Bob Kim": "Bob went quiet for most of week three without telling anyone.",
    "Cara Ng": "Cara could speak up more in meetings; she has good ideas she keeps quiet.",
    "Dan Ray": "Dan's drafts arrive close to the deadline, which leaves no review time.",
    "Eve Sun": "Eve took on more than she could finish and had to hand two tasks back.",
    "Fay Oh": "Fay's spreadsheets are hard for the rest of us to follow without her.",
}


def make_qualtrics_export() -> bytes:
    """Build a synthetic raw Qualtrics export.

    Written to the real schema parse_qualtrics_export expects — code row, label
    row, then dated data rows — so the harness exercises the actual ingest path
    rather than hand-placing a long frame the app would never have produced that
    way.
    """
    max_k = max(len(v) for v in TEAMS.values())
    codes = ["StartDate", "RecipientFirstName", "RecipientLastName",
             "RecipientEmail", "Team"]
    codes += [f"Team Member {k}" for k in range(1, max_k + 1)]
    for k in range(1, max_k + 1):
        codes += [f"Q{2 * k}.1_{j}" for j in range(1, 5)]   # four ratings
        codes += [f"Q22.1_{k}", f"Q23.1_{k}"]               # rank, $100 alloc
        codes += [f"Q{2 * k + 1}.1", f"Q{2 * k + 1}.2"]     # improve, contribution
    codes += ["Q24.1", "Q24.2"]                             # self-contrib, confidential

    rows = [codes, ["Question label row"] * len(codes)]
    for team, members in TEAMS.items():
        for evaluator in members:
            if evaluator == NON_SUBMITTER:
                continue                      # never submitted
            rec = {c: "" for c in codes}
            first, last = evaluator.split(" ", 1)
            rec["StartDate"] = "2026-03-02 10:15:00"
            rec["RecipientFirstName"] = first
            rec["RecipientLastName"] = last
            rec["RecipientEmail"] = f"{first.lower()}@nau.edu"
            rec["Team"] = team
            for k, mate in enumerate(members, start=1):
                rec[f"Team Member {k}"] = mate
                is_self = mate == evaluator
                # Non-uniform allocations so peer_ratio != 1 and B can move grades.
                rec[f"Q23.1_{k}"] = 0 if is_self else (60 if k == 1 else 40)
                rec[f"Q22.1_{k}"] = k
                for j in range(1, 5):
                    rec[f"Q{2 * k}.1_{j}"] = 3 if is_self else (4 if k == 1 else 3)
                if not is_self:
                    rec[f"Q{2 * k + 1}.2"] = CONTRIB[mate]
                    rec[f"Q{2 * k + 1}.1"] = IMPROVE[mate]
            rec["Q24.1"] = "I contributed steadily throughout the project."
            rec["Q24.2"] = "Confidential note."
            rows.append([rec[c] for c in codes])

    return pd.DataFrame(rows).to_csv(index=False, header=False).encode("utf-8")


def boot():
    """Boot the app with responses already ingested, the way tab 2 leaves it."""
    from peerparley import ingest
    long_df, self_evals, roster, report = ingest.parse_qualtrics_export(
        make_qualtrics_export(), "qa_export.csv")
    if long_df.empty:
        raise SystemExit("harness error: synthetic export produced no rows")

    at = AppTest.from_file("app.py", default_timeout=60)
    at.secrets["fernet_key"] = Fernet.generate_key().decode()
    at.secrets["app_password_sha256"] = "x"   # unused; login is bypassed below
    at.session_state["pp_authenticated"] = True
    at.session_state["long_df"] = long_df
    at.session_state["self_evals"] = self_evals
    at.session_state["roster"] = roster
    at.run()
    return at, report


results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), "-", name)


def ss(at, key):
    """Safe read of AppTest session_state (its .get is a key lookup)."""
    return at.session_state[key] if key in at.session_state else None


# --------------------------------------------------------------------------- #
# Boot
# --------------------------------------------------------------------------- #
at, ingest_report = boot()
check("app boots without exception", not at.exception)
check("version reports 2.x", __import__("peerparley").__version__.startswith("2."))

# --------------------------------------------------------------------------- #
# Grading still works, and the AI feature has not disturbed it
# --------------------------------------------------------------------------- #
from peerparley.grading import GradeSettings, compute, results_to_frame  # noqa: E402

long_df = ss(at, "long_df")
check("responses ingested into a long frame",
      long_df is not None and len(long_df) > 0)
check("the non-submitter left no evaluation rows",
      NON_SUBMITTER not in set(long_df["evaluator"]))

teams = compute(long_df, GradeSettings(sensitivity_B=0.5),
                self_evals=ss(at, "self_evals"))
check("teams computed", len(teams) == 2)

teams_zero = compute(long_df, GradeSettings(sensitivity_B=0.0),
                     self_evals=ss(at, "self_evals"))
check("B=0 gives multiplier 1.0 (no peer effect)",
      abs(teams_zero[0].members[0].multiplier - 1.0) < 1e-9)
check("changing B changes the multiplier",
      teams[0].members[0].multiplier != teams_zero[0].members[0].multiplier)

frame = results_to_frame(teams)
fay = frame[frame["Name"] == NON_SUBMITTER].iloc[0]
check("non-submitter flagged Self-eval = No", fay["Self-eval?"] == "No")
submitters = frame[frame["Self-eval?"] == "Yes"]
check("submitters get a numeric grade",
      submitters["Grade Δ"].str.contains("%").all())

# NOTE — open question for the instructor, not a v2 regression.
# The previous harness asserted that a non-submitter's "Grade Δ" renders as an
# em dash. It does not: results_to_frame formats signed_pct for everyone, so
# Fay Oh shows a real adjustment despite never having submitted. Because that
# old harness stopped at its second check (it targeted a step-wizard UI the app
# no longer has), this expectation went unrun for a long time and the two drifted
# apart. Which one is right is a grading-policy decision — should a student who
# skipped the evaluation still be scored by their teammates, or held out? — so
# this records the behaviour rather than changing it. See GRADING.md.
check("non-submitter is still scored (documents current behaviour)",
      fay["Grade Δ"].endswith("%"))

# --------------------------------------------------------------------------- #
# PDFs — with and without a narrative
# --------------------------------------------------------------------------- #
from peerparley import pdfgen  # noqa: E402

member = teams[0].members[0]
plain = pdfgen.build_individual_pdf(member, "1", "QA101")
check("individual PDF builds", plain.startswith(b"%PDF"))

narr = ("Your teammates highlighted the design work you led and the meetings "
        "you organized. More than one mentioned the report content you wrote.")
withn = pdfgen.build_individual_pdf(member, "1", "QA101", narrative=narr)
check("narrative enlarges the PDF", len(withn) > len(plain))

off = pdfgen.build_individual_pdf(member, "1", "QA101",
                                  report={"narrative": False}, narrative=narr)
check("narrative can be switched off in the report settings", len(off) < len(withn))

long_narr = "\n\n".join(["A long paragraph of peer feedback. " * 25] * 8)
check("a long narrative paginates without crashing",
      pdfgen.build_individual_pdf(member, "1", "QA101",
                                  narrative=long_narr).startswith(b"%PDF"))

check("section summary builds",
      pdfgen.build_section_summary_pdf(teams, "QA101", "1").startswith(b"%PDF"))
check("confidential report builds",
      pdfgen.build_confidential_pdf(teams, "QA101", "1").startswith(b"%PDF"))

# --------------------------------------------------------------------------- #
# The AI feature is genuinely optional
# --------------------------------------------------------------------------- #
from peerparley.aiconfig import AISettings  # noqa: E402
from peerparley import feedback_ai as fai  # noqa: E402

off_settings = AISettings()
check("AI is off by default", not off_settings.enabled)
ready, why = AISettings(enabled=True, provider="anthropic",
                        model="claude-sonnet-4-5", api_key="").ready()
check("a missing key is reported, not raised", (not ready) and "API key" in why)
check("a local model needs no key",
      AISettings(enabled=True, provider="local", model="llama3").ready()[0])

# --------------------------------------------------------------------------- #
# Evidence boundaries
# --------------------------------------------------------------------------- #
target = teams[0].members[0]
src = fai.build_source(target)
evidence = src.evidence_text() + " ".join(src.comments)
check("confidential comments never enter the evidence",
      "Confidential note." not in evidence)
check("public comments do enter the evidence", len(src.comments) > 0)
check("ratings can be withheld",
      fai.build_source(target, include_ratings=False).ratings == [])

prompt = fai.build_user_prompt(src, AISettings(enabled=True))
check("every comment appears in the prompt", all(c in prompt for c in src.comments))
check("confidential comments never enter the prompt",
      "Confidential note." not in prompt)

# --------------------------------------------------------------------------- #
# Grounding: the fabricated-citation guard, offline
# --------------------------------------------------------------------------- #
from peerparley.llm import Usage  # noqa: E402


class FakeClient:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.usage = Usage()

    def complete_json(self, system, user, max_tokens=None):
        self.usage.add(Usage(400, 150, 1))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


real_quote = src.comments[0]
honest = {
    "strengths": "Your teammates credited you with leading the design work and "
                 "organizing the team's meetings.",
    "strength_points": [{"point": "You led the design and organized meetings.",
                         "source": real_quote, "raised_by": 1}],
    "focus": "", "focus_points": [], "disagreement": "",
    "insufficient_evidence": "",
}
settings = AISettings(enabled=True, provider="openai", model="gpt-4.1",
                      api_key="qa", verify=False)

d_ok = fai.generate_draft(FakeClient(honest), src, settings)
check("an honestly-cited draft passes both checks", d_ok.clean and not d_ok.flags)
check("its citation validates", d_ok.strength_points[0].quote_ok)

invented = dict(honest)
invented["focus_points"] = [{
    "point": "You should set up a shared task board.",
    "source": "We need a shared task board to track deadlines.",
    "raised_by": 2,
}]
d_bad = fai.generate_draft(FakeClient(invented), src, settings)
check("a fabricated citation is caught with no API call for the check",
      bool(d_bad.high_severity))
check("the fabricated point is marked unverified",
      not d_bad.focus_points[0].quote_ok)
check("a flagged draft is excluded from bulk approval", not d_bad.clean)

verify_settings = AISettings(enabled=True, provider="openai", model="gpt-4.1",
                             api_key="qa", verify=True)
audit = {"grounded": False, "score": 0.5,
         "unsupported": [{"text": "You should delegate more.",
                          "problem": "advice no comment asked for",
                          "severity": "high"}]}
d_audit = fai.generate_draft(FakeClient(honest, audit), src, verify_settings)
check("the grounding audit runs as a second call", d_audit.usage.calls == 2)
check("the audit's finding lands on the draft", bool(d_audit.high_severity))

from peerparley.llm import LLMError  # noqa: E402

d_failed_audit = fai.generate_draft(
    FakeClient(honest, LLMError("503 unavailable")), src, verify_settings)
check("an audit that could not run is not reported as passed",
      (not d_failed_audit.verified) and not d_failed_audit.clean)

# --------------------------------------------------------------------------- #
# The approval gate
# --------------------------------------------------------------------------- #
d_ok.approved = False
check("an unapproved draft yields no narrative",
      fai.approved_narratives({target.key: d_ok}) == {})
d_ok.approved = True
check("an approved draft yields its narrative",
      target.key in fai.approved_narratives({target.key: d_ok}))
d_ok.edited = "The instructor's own wording."
check("an instructor edit overrides the model",
      fai.approved_narratives({target.key: d_ok})[target.key]
      == "The instructor's own wording.")

# --------------------------------------------------------------------------- #
# Every delivery path honours the same approvals
# --------------------------------------------------------------------------- #
from peerparley import emailpack  # noqa: E402
from peerparley.ui_helpers import build_messages  # noqa: E402

narratives = fai.approved_narratives({target.key: d_ok})
msgs = build_messages(teams, ss(at, "roster"), "S", "B", False, "QA101", "1",
                      report=None, narratives=narratives)
check("emails build with narratives attached", len(msgs) == 6)
sizes = {m.to_name: len(m.attachments[0].content) for m in msgs}
plain_msgs = build_messages(teams, ss(at, "roster"), "S", "B", False, "QA101", "1",
                            report=None, narratives={})
plain_sizes = {m.to_name: len(m.attachments[0].content) for m in plain_msgs}
check("the approved student's attachment grew",
      sizes[target.name] > plain_sizes[target.name])
check("nobody else's attachment changed",
      all(sizes[n] == plain_sizes[n] for n in sizes if n != target.name))

parts = emailpack.results_parts(teams, ss(at, "roster"), "S", "B", False,
                                "QA101", "1", None, narratives)
part_sizes = {p["name"]: len(p["attachments"][0][1]) for p in parts}
check("the .eml / auto-send packs honour the same approvals",
      part_sizes[target.name] > plain_sizes[target.name])

# --------------------------------------------------------------------------- #
# Thin evidence costs nothing
# --------------------------------------------------------------------------- #
thin = teams[1].members[0]
thin_src = fai.build_source(thin)
thin_src.valued, thin_src.focus, thin_src.other = ["Good."], [], []
empty_client = FakeClient()  # any call would IndexError
d_thin = fai.generate_draft(empty_client, thin_src, settings)
check("too-thin evidence skips the API entirely",
      bool(d_thin.insufficient_evidence) and empty_client.usage.calls == 0)
check("a thin draft contributes no narrative", d_thin.text() == "")

# --------------------------------------------------------------------------- #
# Partial results survive a session ending
# --------------------------------------------------------------------------- #
class _MemVault:
    def __init__(self): self.store = {}
    def put_bytes(self, name, data): self.store[name] = bytes(data); return name
    def get_bytes(self, name): return self.store[name]
    def delete(self, name): self.store.pop(name, None)


mv = _MemVault()
part = {}
part[target.key] = d_ok                      # reviewed, edited, approved above
part["broken"] = fai.Draft(key="broken", name="Failed Student", team="9",
                           error="401 rejected")
ok_save, save_err = fai.save_drafts(mv, "qa-slug", part)
check("partial results save to the vault", ok_save)

reloaded, load_err = fai.load_drafts(mv, "qa-slug")
check("they load back after a sign-out", load_err == "" and len(reloaded) == 2)
check("the approval survives", reloaded[target.key].approved)
check("the instructor's edit survives",
      reloaded[target.key].text() == "The instructor's own wording.")
check("the failure is still marked failed", not reloaded["broken"].ok)
check("the approval gate means the same after reload",
      set(fai.approved_narratives(reloaded)) == {target.key})
check("nothing saved yet is not an error",
      fai.load_drafts(mv, "no-such-survey") == ({}, ""))
check("drafts are keyed per survey",
      fai.drafts_key("qa-slug") != fai.drafts_key("other-slug"))
check("the fingerprint suppresses a no-op re-save",
      fai.fingerprint(reloaded) == fai.fingerprint(reloaded))

# Selective retry: only outstanding students are regenerated.
member_keys = [m.key for t in teams for m in t.members]
_missing = [k for k in member_keys if k not in part]
_failed = [k for k, d in part.items() if not d.ok]
_done = [k for k, d in part.items() if d.ok and d.text().strip()]
check("retry targets exclude finished drafts", target.key not in (_missing + _failed))
check("retry targets include the failed one", "broken" in _failed)
check("finished work is counted as done", _done == [target.key])

# --------------------------------------------------------------------------- #
# The whole working set survives a sign-out, course name included
# --------------------------------------------------------------------------- #
from peerparley import survey  # noqa: E402
from peerparley import workspace as pws  # noqa: E402

wv = _MemVault()
wv.delete = lambda n: wv.store.pop(n, None)
ok_ws, ws_err = pws.save(wv, "cms89", long_df=long_df,
                         self_evals=ss(at, "self_evals"),
                         roster=ss(at, "roster"), course="Testing2", eval_no="1")
check("the working set autosaves", ok_ws)

restored, rerr = pws.load(wv, "cms89")
check("it loads back after a sign-out", rerr == "" and restored is not None)
check("the responses come back", len(restored["long_df"]) == len(long_df))
check("the self-evaluations come back (the .ppx used to drop these)",
      len(restored["self_evals"]) == len(ss(at, "self_evals") or {}))
check("the roster comes back (so email still works)",
      restored["roster"].match("Ann Lee") is not None)
check("the course name comes back", restored["course"] == "Testing2")

# The actual bug: drafts were filed under the course slug, and the course box
# resets on sign-in, so the app looked under the wrong key.
_written = fai.drafts_key(survey.slugify("Testing2", "1"))
_old_lookup = fai.drafts_key(survey.slugify("", "1"))
_new_lookup = fai.drafts_key(survey.slugify(restored["course"], restored["eval_no"]))
check("the old lookup missed the saved drafts", _written != _old_lookup)
check("restoring the course reconnects them", _written == _new_lookup)

# Named bundles now carry everything, and old ones still load.
_bundle = pws.bundle_bytes(long_df, ss(at, "self_evals"), ss(at, "roster"),
                           "Testing2", "1",
                           {k: d.to_dict() for k, d in part.items()})
_bstate, _bnote = pws.read_bundle(_bundle)
check("a bundle carries the responses", len(_bstate["long_df"]) == len(long_df))
check("a bundle carries the self-evaluations", bool(_bstate["self_evals"]))
check("a bundle carries the roster", _bstate["roster"] is not None)
check("a bundle carries the AI drafts", len(_bstate["drafts"]) == 2)
check("a bundle carries the course name", _bstate["course"] == "Testing2")
_rebuilt = {k: fai.Draft.from_dict(v) for k, v in _bstate["drafts"].items()}
check("approvals survive a bundle round trip",
      set(fai.approved_narratives(_rebuilt)) == {target.key})

import io as _bio
_legacy = _bio.BytesIO()
long_df.to_parquet(_legacy, index=False)
_lstate, _lnote = pws.read_bundle(_legacy.getvalue())
check("legacy bundles still load", len(_lstate["long_df"]) == len(long_df))
check("and say what they could not carry", "responses only" in _lnote)

print("\n== SUMMARY ==")
passed = sum(1 for _, ok in results if ok)
print(f"{passed}/{len(results)} checks passed")
if passed != len(results):
    print("FAILURES:", [n for n, ok in results if not ok])
    raise SystemExit(1)
print("ALL REGRESSION CHECKS PASSED")
