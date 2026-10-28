"""Tests for the AI feedback writer.

Every test here runs offline against a fake client. That is not only for speed:
the behaviour worth testing is what happens when a model misbehaves — cites a
comment nobody wrote, invents a recommendation, overstates how many teammates
agreed, gets cut off mid-reply — and a real model, asked nicely, mostly behaves.
Fakes let those cases be asserted rather than hoped for.

Run with:  python -m pytest tests/ -q
"""
from __future__ import annotations

import json
import math
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from peerparley import feedback_ai as fai  # noqa: E402
from peerparley.aiconfig import AISettings, PROVIDERS, get_provider  # noqa: E402
from peerparley.grading import StudentResult  # noqa: E402
from peerparley.llm import (  # noqa: E402
    LLMError,
    TruncatedResponseError,
    Usage,
    estimate_cost,
    parse_json_object,
    salvage_object_fields,
)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

def make_student(**kw) -> StudentResult:
    """A student with the fields build_source actually reads."""
    base = dict(
        name="Dana Ruiz", key="dana-ruiz", team="B", team_score=100.0,
        received_total=105.0, expected_share=100.0, peer_ratio=1.05,
        A=1.0, Q=0.8, multiplier=1.02, individual_score=102.0, signed_pct=2.0,
        performance="Adequate", comment_points=8, submitted_self_eval=True,
    )
    # Constructed by keyword so the dataclass defaults cover everything
    # build_source does not read.
    base.update(kw)
    return StudentResult(**base)


@pytest.fixture
def student() -> StudentResult:
    s = make_student()
    s.contributions = [
        "Dana built the regression model and walked us through it twice.",
        "Took over the slide deck when I ran out of time.",
    ]
    s.improvements = [
        "Sometimes posts updates late at night so we see them next morning.",
    ]
    s.public_comments = ["Reliable.", "Good teammate overall."]
    s.confidential_comments = ["Carried the whole project honestly."]
    s.peer_vals = [3.4, 3.8, 3.6, 3.5]
    s.team_player = "B+"
    s.quantity = "A-"
    s.quality = "A-"
    s.effect = "B+"
    return s


@pytest.fixture
def settings() -> AISettings:
    return AISettings(enabled=True, provider="openai", model="gpt-4.1",
                      api_key="test-key", verify=False)


class FakeClient:
    """Stands in for LLMClient. Replies come from a queue of payloads.

    A payload may be a dict (returned as the parsed JSON), a string (parsed the
    way a real reply would be), or an exception instance (raised).
    """

    def __init__(self, *replies):
        self.replies = list(replies)
        self.usage = Usage()
        self.prompts = []
        self.spec = get_provider("openai")

    def complete_json(self, system, user, max_tokens=None):
        self.prompts.append((system, user))
        self.usage.add(Usage(500, 200, 1))
        if not self.replies:
            raise AssertionError("FakeClient ran out of replies")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, str):
            return parse_json_object(reply)
        return reply


GOOD_PAYLOAD = {
    "strengths": "Your teammates singled out the regression model you built, and "
                 "noted that you explained it to the group more than once. One "
                 "teammate also mentioned you picked up the slide deck when they "
                 "ran short of time.",
    "strength_points": [
        {"point": "You built the regression model and explained it to the team.",
         "source": "Dana built the regression model and walked us through it twice.",
         "raised_by": 1},
        {"point": "You took on the slide deck when a teammate ran out of time.",
         "source": "Took over the slide deck when I ran out of time.",
         "raised_by": 1},
    ],
    "focus": "One teammate noted that your updates sometimes arrive late at "
             "night, which means the team does not see them until the following "
             "morning.",
    "focus_points": [
        {"point": "Updates posted late at night are not seen until the next day.",
         "source": "Sometimes posts updates late at night so we see them next morning.",
         "raised_by": 1},
    ],
    "disagreement": "",
    "insufficient_evidence": "",
}


# --------------------------------------------------------------------------- #
# Evidence assembly
# --------------------------------------------------------------------------- #

def test_source_excludes_confidential_comments(student):
    """The instructor-only channel must never reach the model.

    This is the single most consequential boundary in the feature: a student who
    reads back a confidential comment learns both its content and that it was
    meant to be hidden.
    """
    src = fai.build_source(student)
    blob = " ".join(src.comments) + src.evidence_text()
    assert "Carried the whole project" not in blob
    for c in student.confidential_comments:
        assert c not in blob


def test_source_dedupes_public_against_contributions(student):
    student.public_comments = [
        "Dana built the regression model and walked us through it twice.",
        "Something new entirely.",
    ]
    src = fai.build_source(student)
    # The duplicate is dropped, so the model can't read one comment as two
    # teammates agreeing.
    assert src.comments.count(
        "Dana built the regression model and walked us through it twice.") == 1
    assert "Something new entirely." in src.other


def test_ratings_optional(student):
    assert fai.build_source(student, include_ratings=True).ratings
    assert fai.build_source(student, include_ratings=False).ratings == []
    assert fai.build_source(student, include_ratings=False).performance == ""


def test_nan_ratings_are_dropped(student):
    student.peer_vals = [float("nan")] * 4
    assert fai.build_source(student).ratings == []


def test_has_material_gate(student):
    assert fai.build_source(student).has_material()

    thin = make_student()
    thin.contributions = ["Good."]
    thin.improvements = []
    thin.public_comments = []
    assert not fai.build_source(thin).has_material()


def test_thin_evidence_costs_no_api_call(settings):
    thin = make_student()
    thin.contributions = ["Fine."]
    client = FakeClient()  # empty queue: any call would raise
    draft = fai.generate_draft(client, fai.build_source(thin), settings)
    assert draft.insufficient_evidence
    assert draft.text() == ""
    assert client.usage.calls == 0


# --------------------------------------------------------------------------- #
# Citation checking — the deterministic guard
# --------------------------------------------------------------------------- #

def test_clean_draft_passes(student, settings):
    client = FakeClient(GOOD_PAYLOAD)
    draft = fai.generate_draft(client, fai.build_source(student), settings)
    assert draft.ok
    assert draft.flags == []
    assert draft.clean
    assert all(p.quote_ok for p in draft.strength_points)
    assert "regression model" in draft.text()


def test_fabricated_citation_is_caught(student, settings):
    """An invented recommendation wearing a quotation is the worst case."""
    payload = dict(GOOD_PAYLOAD)
    payload["focus_points"] = [{
        "point": "You should set up a shared task board to track deadlines.",
        "source": "We really need a shared task board to keep track of deadlines.",
        "raised_by": 2,
    }]
    client = FakeClient(payload)
    draft = fai.generate_draft(client, fai.build_source(student), settings)

    assert not draft.clean
    assert draft.high_severity
    flag = draft.high_severity[0]
    assert flag.origin == "quote-check"
    assert "does not appear" in flag.problem
    assert not draft.focus_points[0].quote_ok


def test_reworded_citation_still_counts(student, settings):
    """Flagging a lightly-trimmed quote would cry wolf on every draft."""
    payload = dict(GOOD_PAYLOAD)
    payload["strength_points"] = [{
        "point": "You built the regression model.",
        # Trailing clause dropped, capitalization changed — still plainly a copy.
        "source": "Dana built the regression model and walked us through it",
        "raised_by": 1,
    }]
    payload["focus_points"] = []
    client = FakeClient(payload)
    draft = fai.generate_draft(client, fai.build_source(student), settings)
    assert draft.strength_points[0].quote_ok
    assert not draft.high_severity


def test_overstated_agreement_is_flagged_but_only_mildly(student, settings):
    payload = json.loads(json.dumps(GOOD_PAYLOAD))
    payload["strength_points"][0]["raised_by"] = 4
    client = FakeClient(payload)
    draft = fai.generate_draft(client, fai.build_source(student), settings)

    assert draft.flags
    assert not draft.high_severity            # honest quote, dishonest count
    assert any("teammates raised this" in f.problem for f in draft.flags)
    assert not draft.clean                    # still keeps it out of bulk approve


def test_too_short_citation_is_not_evidence(student, settings):
    """A four-letter citation is flagged — but as unverifiable, not invented.

    It used to be reported as "cites a comment that does not appear in this
    student's feedback", which is an accusation the check cannot support: the
    word may well be in there. The honest flag is that there is too little text
    to check. It still keeps the draft out of bulk approval, which is what the
    flag is for.
    """
    payload = dict(GOOD_PAYLOAD)
    payload["strength_points"] = [
        {"point": "You were helpful.", "source": "good", "raised_by": 1}]
    payload["focus_points"] = []
    client = FakeClient(payload)
    draft = fai.generate_draft(client, fai.build_source(student), settings)
    assert not draft.clean
    assert any("too little text" in f.problem for f in draft.flags)
    assert not draft.high_severity


# --------------------------------------------------------------------------- #
# The grounding audit
# --------------------------------------------------------------------------- #

def test_verifier_flags_invented_advice(student, settings):
    settings.verify = True
    audit = {
        "grounded": False,
        "score": 0.6,
        "unsupported": [{
            "text": "Consider blocking out focus time earlier in the day.",
            "problem": "recommendation no comment asked for",
            "severity": "high",
        }],
        "note": "One sentence gives advice the comments do not support.",
    }
    client = FakeClient(GOOD_PAYLOAD, audit)
    draft = fai.generate_draft(client, fai.build_source(student), settings)

    assert draft.verified
    assert draft.high_severity
    assert draft.score <= 0.5
    assert not draft.clean
    assert client.usage.calls == 2


def test_verifier_clean_run_reports_verified(student, settings):
    settings.verify = True
    client = FakeClient(GOOD_PAYLOAD,
                        {"grounded": True, "score": 1.0, "unsupported": [], "note": ""})
    draft = fai.generate_draft(client, fai.build_source(student), settings)
    assert draft.verified and draft.clean and draft.score == 1.0
    assert "Grounded" in draft.summary()


def test_failed_audit_is_not_a_passed_audit(student, settings):
    """A check that could not run must not read as a check that passed."""
    settings.verify = True
    client = FakeClient(GOOD_PAYLOAD, LLMError("502 upstream error"))
    draft = fai.generate_draft(client, fai.build_source(student), settings)

    assert not draft.verified
    assert not draft.clean
    assert draft.score <= 0.5
    assert any("could not run" in f.problem for f in draft.flags)


def test_unverified_draft_is_not_reported_as_grounded(student, settings):
    settings.verify = False
    client = FakeClient(GOOD_PAYLOAD)
    draft = fai.generate_draft(client, fai.build_source(student), settings)
    assert not draft.verified
    assert "not verified" in draft.summary()


def test_verifier_receives_only_real_evidence(student, settings):
    settings.verify = True
    client = FakeClient(GOOD_PAYLOAD,
                        {"grounded": True, "score": 1.0, "unsupported": []})
    fai.generate_draft(client, fai.build_source(student), settings)

    _, audit_user = client.prompts[1]
    assert "regression model" in audit_user
    assert "Carried the whole project" not in audit_user   # confidential stays out


# --------------------------------------------------------------------------- #
# Failure handling
# --------------------------------------------------------------------------- #

def test_api_failure_becomes_a_draft_not_an_exception(student, settings):
    client = FakeClient(LLMError("401 invalid api key"))
    draft = fai.generate_draft(client, fai.build_source(student), settings)
    assert not draft.ok
    assert "401" in draft.error
    assert draft.text() == ""
    assert draft.summary().startswith("⚠")


def test_truncated_reply_is_salvaged(student, settings):
    """A reply cut off after two good fields is worth two fields, not zero."""
    settings.retry_truncated = False          # exercise salvage, not the retry
    partial = (
        '{"strengths": "Your teammates valued the regression model you built.",\n'
        ' "strength_points": [{"point": "You built the regression model.",'
        ' "source": "Dana built the regression model and walked us through it twice.",'
        ' "raised_by": 1}],\n'
        ' "focus": "One teammate mentioned tha'
    )
    client = FakeClient(TruncatedResponseError("cut off", raw=partial))
    draft = fai.generate_draft(client, fai.build_source(student), settings)

    assert draft.ok
    assert draft.truncated
    assert "regression model" in draft.strengths
    assert draft.strength_points and draft.strength_points[0].quote_ok
    assert any("cut off" in f.problem for f in draft.flags)
    assert not draft.clean          # a partial draft still wants a human read
    assert draft.raw_partial == partial     # nothing discarded


def test_truncation_retries_once_with_a_bigger_ceiling(student, settings):
    """"Resubmit to complete" — done automatically rather than by hand x40."""
    settings.retry_truncated = True
    settings.max_tokens = 1600

    class CutThenSucceed(FakeClient):
        def __init__(self, payload):
            super().__init__()
            self.payload = payload
            self.caps = []

        def complete_json(self, system, user, max_tokens=None):
            self.prompts.append((system, user))
            self.caps.append(max_tokens)
            self.usage.add(Usage(500, 200, 1))
            if len(self.caps) == 1:
                raise TruncatedResponseError("cut off", raw='{"strengths": "half')
            return self.payload

    client = CutThenSucceed(GOOD_PAYLOAD)
    draft = fai.generate_draft(client, fai.build_source(student), settings)

    assert len(client.caps) == 2
    assert client.caps[1] > client.caps[0]          # real headroom, not a nudge
    assert client.caps[1] <= fai.MAX_RETRY_TOKENS
    # A completed retry is a clean draft, not a salvaged one.
    assert draft.ok and not draft.truncated
    assert "regression model" in draft.text()
    assert draft.raw_partial == ""


def test_retry_can_be_switched_off(student, settings):
    settings.retry_truncated = False
    client = FakeClient(TruncatedResponseError("cut off", raw='{"strengths": "half'))
    fai.generate_draft(client, fai.build_source(student), settings)
    assert len(client.prompts) == 1          # asked once, salvaged, moved on


def test_prose_cut_mid_first_field_is_recovered(student, settings):
    """The live failure: 7233 characters, and the old code kept none of it.

    Structural salvage needs a comma at depth one to close the object, so a
    reply cut inside its first field yielded nothing at all.
    """
    settings.retry_truncated = False
    long_prose = ("Your teammates highlighted the VP role you played in the "
                  "simulation, and more than one singled out your supply-chain "
                  "decisions as sound. " * 40)
    raw = '{"strengths": "' + long_prose
    assert len(raw) > 4000

    assert salvage_object_fields(raw) == {}      # the old path: total loss

    client = FakeClient(TruncatedResponseError(
        f"stopped at its output limit after {len(raw)} characters", raw=raw))
    draft = fai.generate_draft(client, fai.build_source(student), settings)

    assert draft.ok                               # not thrown away
    assert draft.truncated
    assert "VP role" in draft.text()
    assert len(draft.text()) > 1000               # thousands of chars rescued
    assert draft.raw_partial == raw
    assert not draft.clean                        # still needs a read
    assert any("incomplete" in f.problem for f in draft.flags)


def test_a_prose_only_reply_is_still_offered_to_the_instructor(student, settings):
    """A model that answered in prose instead of JSON still did the work."""
    settings.retry_truncated = False
    prose = ("Kyle played the VP role in the simulation and his teammates "
             "credited his supply-chain decisions. One asked for better "
             "communication going forward. " * 3)
    client = FakeClient(TruncatedResponseError("cut off", raw=prose))
    draft = fai.generate_draft(client, fai.build_source(student), settings)

    assert draft.ok
    assert "VP role" in draft.text()
    assert draft.raw_partial == prose


def test_truncation_with_nothing_usable_still_keeps_the_raw(student, settings):
    settings.retry_truncated = False
    client = FakeClient(TruncatedResponseError("cut off", raw="{"))
    draft = fai.generate_draft(client, fai.build_source(student), settings)
    assert not draft.ok and draft.truncated
    assert draft.raw_partial == "{"          # even here, kept for inspection


def test_an_empty_reply_is_not_reported_as_a_minor_flag(student, settings):
    """Kyle Bartnik: an empty text box beside "1 minor flag(s)".

    The call succeeded and every narrative field came back blank. That used to
    read as a finished draft with a nitpick, which is the most misleading thing
    the panel could say.
    """
    settings.retry_truncated = False
    client = FakeClient({"strengths": "", "strength_points": [], "focus": "",
                         "focus_points": [], "disagreement": "",
                         "insufficient_evidence": ""})
    draft = fai.generate_draft(client, fai.build_source(student), settings)

    assert draft.ok                  # the request itself did not fail
    assert draft.empty               # but it produced nothing
    assert not draft.clean           # so it cannot be bulk-approved
    assert draft.score == 0.0
    assert draft.high_severity       # and it is flagged loudly, not mildly
    assert "came back empty" in draft.summary()
    assert "retry" in draft.summary()


def test_an_empty_draft_contributes_no_narrative_even_if_approved():
    d = fai.Draft(key="a", name="A", team="1", approved=True)
    assert d.empty
    assert fai.approved_narratives({"a": d}) == {}


def test_a_real_draft_is_not_flagged_empty(student, settings):
    settings.retry_truncated = False
    client = FakeClient(GOOD_PAYLOAD)
    draft = fai.generate_draft(client, fai.build_source(student), settings)
    assert not draft.empty and draft.clean


def test_raw_partial_survives_the_vault_round_trip():
    """The rescued text has to still be there after signing out."""
    vault = MemoryVault()
    d = fai.Draft(key="a", name="A", team="1", strengths="Recovered prose.",
                  truncated=True, raw_partial='{"strengths": "Recovered prose')
    fai.save_drafts(vault, "s", {"a": d})
    back, _ = fai.load_drafts(vault, "s")
    assert back["a"].raw_partial == '{"strengths": "Recovered prose'
    assert back["a"].truncated


# --------------------------------------------------------------------------- #
# The approval gate
# --------------------------------------------------------------------------- #

def test_only_approved_drafts_produce_narratives():
    a = fai.Draft(key="a", name="A", team="1", strengths="Solid work.")
    b = fai.Draft(key="b", name="B", team="1", strengths="Also solid.", approved=True)
    c = fai.Draft(key="c", name="C", team="1", error="boom", approved=True)
    d = fai.Draft(key="d", name="D", team="1", approved=True,
                  insufficient_evidence="Too few comments.")

    out = fai.approved_narratives({"a": a, "b": b, "c": c, "d": d})
    assert set(out) == {"b"}


def test_instructor_edit_overrides_the_model():
    d = fai.Draft(key="a", name="A", team="1",
                  strengths="Model wrote this.", approved=True)
    d.edited = "The instructor wrote this instead."
    assert fai.approved_narratives({"a": d})["a"] == "The instructor wrote this instead."


def test_flagged_draft_can_still_be_approved_deliberately():
    """The checks advise; the instructor decides. They must not be a hard block."""
    d = fai.Draft(key="a", name="A", team="1", strengths="Fine.", approved=True)
    d.flags = [fai.Flag(text="x", problem="y", severity="high")]
    assert fai.approved_narratives({"a": d})["a"] == "Fine."
    assert not d.clean          # but it was never eligible for bulk approval


def test_batch_stats_counts():
    drafts = {
        "a": fai.Draft(key="a", name="A", team="1", strengths="ok", approved=True),
        "b": fai.Draft(key="b", name="B", team="1", strengths="ok",
                       flags=[fai.Flag("t", "p", "high")]),
        "c": fai.Draft(key="c", name="C", team="1", error="nope"),
        "d": fai.Draft(key="d", name="D", team="1",
                       insufficient_evidence="too thin"),
    }
    st = fai.batch_stats(drafts)
    assert st["total"] == 4 and st["approved"] == 1
    assert st["flagged"] == 1 and st["high"] == 1
    assert st["errors"] == 1 and st["thin"] == 1


# --------------------------------------------------------------------------- #
# Prompt construction
# --------------------------------------------------------------------------- #

def test_prompt_contains_every_comment_and_nothing_else(student, settings):
    src = fai.build_source(student)
    prompt = fai.build_user_prompt(src, settings)
    for c in src.comments:
        assert c in prompt
    assert "Carried the whole project" not in prompt


def test_ratings_appear_only_when_enabled(student, settings):
    settings.include_ratings = False
    src = fai.build_source(student, include_ratings=False)
    assert "3.40" not in fai.build_user_prompt(src, settings)

    settings.include_ratings = True
    src = fai.build_source(student, include_ratings=True)
    assert "3.40" in fai.build_user_prompt(src, settings)


def test_extra_guidance_is_included(student, settings):
    settings.extra_guidance = "Write for a sophomore audience."
    prompt = fai.build_user_prompt(fai.build_source(student), settings)
    assert "sophomore audience" in prompt


# --------------------------------------------------------------------------- #
# Provider layer
# --------------------------------------------------------------------------- #

def test_every_provider_maps_to_a_real_sdk_path():
    for key, spec in PROVIDERS.items():
        assert spec.sdk in ("openai", "anthropic", "gemini"), key
        assert spec.requires_key is False or spec.env_var, key


def test_local_provider_needs_no_key():
    s = AISettings(enabled=True, provider="local", model="llama3.1:8b")
    ok, why = s.ready()
    assert ok, why


def test_missing_key_is_reported_not_crashed():
    s = AISettings(enabled=True, provider="anthropic",
                   model="claude-sonnet-4-5", api_key="")
    if not s.resolved_api_key():          # skip if the env happens to have one
        ok, why = s.ready()
        assert not ok and "API key" in why


def test_unpriced_model_estimates_zero_rather_than_guessing():
    assert estimate_cost("some-unlisted-model", Usage(1000, 1000, 1)) == 0.0
    assert estimate_cost("gpt-4.1", Usage(1_000_000, 0, 1)) == pytest.approx(2.0)


def test_free_router_is_an_exact_zero():
    assert estimate_cost("openrouter/free", Usage(9_000_000, 9_000_000, 9)) == 0.0


# --------------------------------------------------------------------------- #
# JSON handling
# --------------------------------------------------------------------------- #

def test_fenced_json_is_parsed():
    assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}


def test_json_with_surrounding_prose_is_parsed():
    assert parse_json_object('Sure! {"a": 1} Hope that helps.') == {"a": 1}


def test_open_brace_reads_as_truncation_not_garbage():
    with pytest.raises(TruncatedResponseError):
        parse_json_object('{"strengths": "half a sen')


def test_empty_reply_is_an_error():
    with pytest.raises(LLMError):
        parse_json_object("")


def test_salvage_keeps_finished_fields():
    out = salvage_object_fields('{"a": "one", "b": "two", "c": "thr')
    assert out == {"a": "one", "b": "two"}


def test_salvage_of_complete_object_is_the_whole_object():
    assert salvage_object_fields('{"a": 1, "b": 2}') == {"a": 1, "b": 2}


# --------------------------------------------------------------------------- #
# PDF integration
# --------------------------------------------------------------------------- #

def test_narrative_reaches_the_pdf(student):
    from peerparley import pdfgen
    plain = pdfgen.build_individual_pdf(student, "2", "MGT 301")
    withnarr = pdfgen.build_individual_pdf(
        student, "2", "MGT 301",
        narrative="Your teammates valued the regression model you built.\n\n"
                  "One noted your updates arrive late at night.")
    assert withnarr.startswith(b"%PDF")
    assert len(withnarr) > len(plain)


def test_narrative_section_can_be_switched_off(student):
    from peerparley import pdfgen
    off = pdfgen.build_individual_pdf(
        student, "2", "MGT 301", report={"narrative": False},
        narrative="Something long enough to change the byte count materially. " * 6)
    on = pdfgen.build_individual_pdf(
        student, "2", "MGT 301", report={"narrative": True},
        narrative="Something long enough to change the byte count materially. " * 6)
    assert len(on) > len(off)


def test_long_narrative_paginates_without_crashing(student):
    from peerparley import pdfgen
    pdf = pdfgen.build_individual_pdf(
        student, "2", "MGT 301",
        narrative="\n\n".join(["A long paragraph of peer feedback. " * 25] * 8))
    assert pdf.startswith(b"%PDF") and len(pdf) > 3000


# --------------------------------------------------------------------------- #
# Credential failures (added after a live run produced 40 identical 401s)
# --------------------------------------------------------------------------- #

from peerparley.llm import AuthLLMError, key_provider_hint  # noqa: E402


class AuthFailClient(FakeClient):
    """Rejects every request the way a provider with a bad key does."""

    def complete_json(self, system, user, max_tokens=None):
        self.prompts.append((system, user))
        raise AuthLLMError("OpenRouter rejected the API key. User not found.")


def test_auth_failure_escapes_generate_draft(student, settings):
    """It must not be buried in a per-student Draft like a normal error.

    A rate limit on one student says nothing about the next; a rejected key says
    everything about all of them, so it has to be distinguishable.
    """
    with pytest.raises(AuthLLMError):
        fai.generate_draft(AuthFailClient(), fai.build_source(student), settings)


def test_batch_aborts_on_first_auth_failure(student, settings):
    """40 students must cost one error, not forty."""
    from peerparley.grading import TeamResult

    members = []
    for i in range(6):
        m = make_student(name=f"Student {i}", key=f"s{i}")
        m.contributions = ["Built the model and explained it to everyone twice."]
        m.improvements = ["Sometimes sends updates very late in the evening."]
        members.append(m)
    teams = [TeamResult(team="1", members=members, team_score=100.0)]

    client = AuthFailClient()
    with pytest.raises(fai.BatchAborted) as caught:
        fai.generate_for_teams(teams, settings, client=client)

    # Stopped at the first student rather than trying all six.
    assert len(client.prompts) == 1
    assert "User not found" in str(caught.value)
    assert caught.value.drafts == {}


def test_batch_abort_keeps_the_drafts_already_finished(student, settings):
    """Partial work survives the stop; discarding it would be a second failure."""
    from peerparley.grading import TeamResult

    good = make_student(name="First Student", key="ok")
    good.contributions = ["Built the regression model and walked us through it."]
    good.improvements = ["Sends updates late at night so we read them next day."]
    bad = make_student(name="Second Student", key="fails")
    bad.contributions = ["Rebuilt the slide deck the night before the talk."]
    bad.improvements = ["Went quiet for a week without telling the team."]
    teams = [TeamResult(team="1", members=[good, bad], team_score=100.0)]

    payload = {
        "strengths": "Your teammates credited you with building the model.",
        "strength_points": [{
            "point": "You built the regression model.",
            "source": "Built the regression model and walked us through it.",
            "raised_by": 1}],
        "focus": "", "focus_points": [], "disagreement": "",
        "insufficient_evidence": "",
    }

    class OneThenAuthFail(FakeClient):
        def complete_json(self, system, user, max_tokens=None):
            self.prompts.append((system, user))
            self.usage.add(Usage(400, 150, 1))
            if len(self.prompts) == 1:
                return payload
            raise AuthLLMError("rejected: 401 invalid api key")

    with pytest.raises(fai.BatchAborted) as caught:
        fai.generate_for_teams(teams, settings, client=OneThenAuthFail())

    assert set(caught.value.drafts) == {"ok"}
    assert caught.value.drafts["ok"].ok


def test_401_is_not_retried_as_transient():
    """Retrying a rejected key four times with backoff only makes it slow."""
    from peerparley.llm import _is_auth, _is_transient

    for message in ("Error code: 401 - {'error': {'message': 'User not found.'}}",
                    "invalid api key provided",
                    "403 permission_denied",
                    "invalid x-api-key"):
        assert _is_auth(Exception(message)), message
    # Genuinely transient things still retry.
    assert _is_transient(Exception("429 rate limit exceeded"))
    assert not _is_auth(Exception("429 rate limit exceeded"))


def test_key_prefix_identifies_the_wrong_provider():
    assert key_provider_hint("sk-ant-api03-abc") == "anthropic"
    assert key_provider_hint("sk-or-v1-abc") == "openrouter"
    assert key_provider_hint("xai-abc") == "xai"
    assert key_provider_hint("AIzaSyAbc") == "gemini"
    # A bare sk- is ambiguous between vendors, so it must not guess.
    assert key_provider_hint("sk-abcdef") is None
    assert key_provider_hint("") is None
    assert key_provider_hint("   sk-ant-spaced  ") == "anthropic"


def test_wrong_provider_key_is_caught_before_any_request():
    """The check that would have saved 40 calls."""
    s = AISettings(enabled=True, provider="openrouter",
                   model="openrouter/free", api_key="sk-ant-api03-xxxx")
    assert s.key_mismatch() == "anthropic"
    ok, why = s.ready()
    assert not ok
    assert "Anthropic" in why and "OpenRouter" in why


def test_matching_key_is_not_flagged():
    s = AISettings(enabled=True, provider="openrouter",
                   model="openrouter/free", api_key="sk-or-v1-xxxx")
    assert s.key_mismatch() is None
    assert s.ready()[0]


def test_api_key_is_stripped():
    """A pasted key with a trailing newline is the classic silent 401."""
    s = AISettings(enabled=True, provider="openai", model="gpt-4.1",
                   api_key="  sk-abc123\n")
    assert s.resolved_api_key() == "sk-abc123"


def test_written_count_excludes_failures():
    """"Drafted 40" over forty failures is how a run looks successful when it wasn't."""
    drafts = {
        "a": fai.Draft(key="a", name="A", team="1", strengths="Real text."),
        "b": fai.Draft(key="b", name="B", team="1", error="401 rejected"),
        "c": fai.Draft(key="c", name="C", team="1", error="401 rejected"),
        "d": fai.Draft(key="d", name="D", team="1",
                       insufficient_evidence="Too few comments."),
    }
    st = fai.batch_stats(drafts)
    assert st["total"] == 4        # four Draft objects exist
    assert st["written"] == 1      # but only one produced text
    assert st["errors"] == 2
    assert st["thin"] == 1


def test_error_groups_collapse_a_shared_failure():
    same = "OpenRouter rejected the API key."
    drafts = {
        str(i): fai.Draft(key=str(i), name=f"S{i}", team="1", error=same)
        for i in range(5)
    }
    drafts["odd"] = fai.Draft(key="odd", name="Odd", team="2",
                              error="model not found: typo-4.1")
    drafts["ok"] = fai.Draft(key="ok", name="Fine", team="2", strengths="Text.")

    groups = fai.error_groups(drafts)
    assert groups[0][0] == same          # biggest shared cause first
    assert len(groups[0][1]) == 5
    assert groups[1][1] == ["Odd"]
    assert all("Fine" not in names for _, names in groups)


def test_error_groups_empty_when_nothing_failed():
    assert fai.error_groups({
        "a": fai.Draft(key="a", name="A", team="1", strengths="Text.")}) == []


# --------------------------------------------------------------------------- #
# Persistence and selective retry
# (added after a live run lost reviewed drafts on sign-out, and offered to
#  re-draft all 40 students when only 38 had failed)
# --------------------------------------------------------------------------- #

class MemoryVault:
    """Stand-in for peerparley.vault.Vault — same 3 methods the code touches."""

    def __init__(self, fail: bool = False):
        self.store = {}
        self.fail = fail
        self.writes = 0

    def put_bytes(self, name, data):
        if self.fail:
            raise RuntimeError("vault offline")
        self.writes += 1
        self.store[name] = bytes(data)
        return name

    def get_bytes(self, name):
        if name not in self.store:
            raise KeyError(name)
        return self.store[name]

    def delete(self, name):
        self.store.pop(name, None)


def _reviewed_batch():
    a = fai.Draft(key="a", name="Ann Lee", team="1",
                  strengths="Teammates valued the model you built.",
                  focus="One asked for earlier updates.")
    a.strength_points = [fai.Point(point="You built the model.",
                                   source="Ann built the model.", raised_by=2)]
    a.score, a.verified, a.quotes_checked = 0.95, True, True
    a.flags = [fai.Flag(text="x", problem="overstated count", severity="low")]
    a.approved = True
    a.edited = "The instructor's own wording, kept verbatim."
    a.usage = Usage(500, 220, 2)
    a.model, a.provider = "gpt-4.1", "openai"

    b = fai.Draft(key="b", name="Bob Kim", team="1", error="401 rejected")
    c = fai.Draft(key="c", name="Cara Ng", team="2",
                  insufficient_evidence="Only 1 comment.")
    return {"a": a, "b": b, "c": c}


def test_drafts_survive_a_save_load_cycle():
    """Sign out, sign back in: the review has to still be there."""
    vault, drafts = MemoryVault(), _reviewed_batch()
    saved, err = fai.save_drafts(vault, "mgt490c-1", drafts)
    assert saved, err

    back, err = fai.load_drafts(vault, "mgt490c-1")
    assert err == ""
    assert set(back) == {"a", "b", "c"}

    a = back["a"]
    assert a.approved is True
    assert a.edited == "The instructor's own wording, kept verbatim."
    assert a.text() == "The instructor's own wording, kept verbatim."
    assert a.strengths.startswith("Teammates valued")
    assert a.score == 0.95 and a.verified and a.quotes_checked
    assert [f.problem for f in a.flags] == ["overstated count"]
    assert a.strength_points[0].raised_by == 2
    assert a.usage.calls == 2 and a.usage.input_tokens == 500
    assert back["b"].error == "401 rejected" and not back["b"].ok
    assert back["c"].insufficient_evidence == "Only 1 comment."


def test_approved_narratives_survive_the_round_trip():
    """The gate to student-visible text must mean the same thing after reload."""
    vault = MemoryVault()
    fai.save_drafts(vault, "s", _reviewed_batch())
    back, _ = fai.load_drafts(vault, "s")
    out = fai.approved_narratives(back)
    assert set(out) == {"a"}
    assert out["a"] == "The instructor's own wording, kept verbatim."


def test_load_with_nothing_saved_is_not_an_error():
    back, err = fai.load_drafts(MemoryVault(), "never-saved")
    assert back == {} and err == ""


def test_unreadable_save_reports_rather_than_pretending_it_is_absent():
    """A changed Fernet key must not look like 'you never drafted anything'."""
    vault = MemoryVault()
    vault.store[fai.drafts_key("s")] = b"not json at all"
    back, err = fai.load_drafts(vault, "s")
    assert back == {}
    assert "could not be read" in err


def test_save_failure_is_reported_not_raised():
    """The panel is mid-review; a storage hiccup must not take it down."""
    saved, err = fai.save_drafts(MemoryVault(fail=True), "s", _reviewed_batch())
    assert not saved and "vault offline" in err


def test_save_without_a_slug_is_refused_clearly():
    saved, err = fai.save_drafts(MemoryVault(), "", _reviewed_batch())
    assert not saved and "nowhere to save" in err


def test_drafts_are_keyed_per_survey():
    """One cohort's narratives must never load under another's survey."""
    vault = MemoryVault()
    fai.save_drafts(vault, "mgt490c-1", {"a": fai.Draft(key="a", name="A", team="1",
                                                        strengths="Eval one.")})
    fai.save_drafts(vault, "mgt490c-2", {"z": fai.Draft(key="z", name="Z", team="9",
                                                        strengths="Eval two.")})
    one, _ = fai.load_drafts(vault, "mgt490c-1")
    two, _ = fai.load_drafts(vault, "mgt490c-2")
    assert set(one) == {"a"} and set(two) == {"z"}
    assert fai.drafts_key("mgt490c-1") != fai.drafts_key("mgt490c-2")


def test_fingerprint_changes_only_when_content_does():
    """Guards against re-uploading 40 drafts on every keystroke."""
    drafts = _reviewed_batch()
    fp = fai.fingerprint(drafts)
    assert fai.fingerprint(_reviewed_batch()) == fp      # same content, same fp

    drafts["a"].approved = False
    assert fai.fingerprint(drafts) != fp

    drafts["a"].approved = True
    assert fai.fingerprint(drafts) == fp                 # and back again

    drafts["a"].edited += " one more clause."
    assert fai.fingerprint(drafts) != fp


def test_retry_targets_only_what_is_outstanding():
    """The bug on screen: 38 failed, but the button offered to redo all 40."""
    drafts = _reviewed_batch()                    # a=ok, b=failed, c=thin
    member_keys = ["a", "b", "c", "d"]            # d was never drafted

    missing = [k for k in member_keys if k not in drafts]
    failed = [k for k, d in drafts.items() if not d.ok]
    todo = missing + failed
    done = [k for k, d in drafts.items() if d.ok and d.text().strip()]

    assert missing == ["d"]
    assert failed == ["b"]
    assert sorted(todo) == ["b", "d"]             # not all four
    assert done == ["a"]                          # the reviewed one is spared


def test_regenerating_only_outstanding_keys_leaves_finished_work_alone():
    from peerparley.grading import TeamResult

    keep = make_student(name="Kept Student", key="keep")
    keep.contributions = ["Built the regression model and explained it twice."]
    keep.improvements = ["Sends updates late at night."]
    redo = make_student(name="Redo Student", key="redo")
    redo.contributions = ["Rebuilt the slide deck the night before the talk."]
    redo.improvements = ["Went quiet for a week without telling the team."]
    teams = [TeamResult(team="1", members=[keep, redo], team_score=100.0)]

    payload = {
        "strengths": "Your teammates credited the slide deck you rebuilt.",
        "strength_points": [{
            "point": "You rebuilt the slide deck.",
            "source": "Rebuilt the slide deck the night before the talk.",
            "raised_by": 1}],
        "focus": "", "focus_points": [], "disagreement": "",
        "insufficient_evidence": "",
    }
    client = FakeClient(payload)
    settings = AISettings(enabled=True, provider="openai", model="gpt-4.1",
                          api_key="k", verify=False)

    fresh = fai.generate_for_teams(teams, settings, client=client,
                                    only_keys=["redo"])
    assert set(fresh) == {"redo"}          # "keep" was never sent
    assert len(client.prompts) == 1
    assert "slide deck" in client.prompts[0][1]
    assert "regression model" not in client.prompts[0][1]


def test_empty_reply_is_retryable_not_reported_as_truncation():
    """The OpenRouter free router's "0 characters" wall.

    An empty completion with finish_reason=length was being reported as
    "stopped at its output limit after 0 characters", which sends the instructor
    to shorten a draft that was never written. It is a transient failure: the
    free router picks a different upstream model per call, so a retry may land
    on one that answers.
    """
    from peerparley.llm import TransientLLMError

    class _Empty:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    msg = type("M", (), {"content": ""})()
                    ch = type("C", (), {"message": msg, "finish_reason": "length"})()
                    return type("R", (), {"choices": [ch], "usage": None})()

    from peerparley.llm import LLMClient
    from peerparley.aiconfig import get_provider
    c = LLMClient.__new__(LLMClient)
    c.spec = get_provider("openrouter"); c.api_key = "sk-or-x"
    c.model = "openrouter/free"; c.temperature = 0.2; c.max_tokens = 1600
    c.usage = Usage(); c.on_usage = None; c._client = _Empty()

    with pytest.raises(TransientLLMError) as caught:
        c._complete_openai("s", "u", 1600, True)
    assert "empty reply" in str(caught.value)
    assert "0 characters" not in str(caught.value)


def test_real_truncation_is_still_truncation():
    """The reclassification above must not swallow a genuinely partial reply."""
    class _Partial:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    msg = type("M", (), {"content": '{"strengths": "half a sen'})()
                    ch = type("C", (), {"message": msg, "finish_reason": "length"})()
                    return type("R", (), {"choices": [ch], "usage": None})()

    from peerparley.llm import LLMClient
    from peerparley.aiconfig import get_provider
    c = LLMClient.__new__(LLMClient)
    c.spec = get_provider("openrouter"); c.api_key = "sk-or-x"
    c.model = "openrouter/free"; c.temperature = 0.2; c.max_tokens = 1600
    c.usage = Usage(); c.on_usage = None; c._client = _Partial()

    with pytest.raises(TruncatedResponseError) as caught:
        c._complete_openai("s", "u", 1600, True)
    assert caught.value.raw.startswith('{"strengths"')


# --------------------------------------------------------------------------- #
# The OpenRouter catalog (ported from TransQ, which had this and v2 lost it)
# --------------------------------------------------------------------------- #

from peerparley import openrouter_catalog as orc  # noqa: E402


def _payload():
    """Shaped like OpenRouter's real /api/v1/models response."""
    return {"data": [
        {"id": "anthropic/claude-sonnet-4.5", "name": "Claude Sonnet 4.5",
         "context_length": 200000,
         "pricing": {"prompt": "0.000003", "completion": "0.000015"},
         "architecture": {"output_modalities": ["text"],
                          "modality": "text+image->text"}},
        {"id": "deepseek/deepseek-r1:free", "name": "DeepSeek R1 (free)",
         "context_length": 64000, "pricing": {"prompt": "0", "completion": "0"},
         "architecture": {"output_modalities": ["text"]}},
        {"id": "recraft/recraft-v3", "name": "Recraft V3",
         "context_length": 0,
         "pricing": {"prompt": "0.00004", "completion": "0"},
         "architecture": {"output_modalities": ["image"]}},
        {"id": "openai/gpt-4.1:batch", "name": "GPT-4.1 batch",
         "context_length": 1000000,
         "pricing": {"prompt": "0.000001", "completion": "0.000004"},
         "architecture": {"output_modalities": ["text"]}},
        {"id": "mystery/unpriced", "name": "No pricing",
         "context_length": 8000, "pricing": {},
         "architecture": {"output_modalities": ["text"]}},
        {"id": "legacy/old-style", "name": "Modality string only",
         "context_length": 4096,
         "pricing": {"prompt": "0.0000005", "completion": "0.0000015"},
         "architecture": {"modality": "text->text"}},
    ]}


def test_catalog_keeps_every_text_model():
    """The point of the port: not a curated handful, everything usable."""
    ids = [m.id for m in orc.parse_models(_payload())]
    assert "anthropic/claude-sonnet-4.5" in ids
    assert "deepseek/deepseek-r1:free" in ids
    assert "legacy/old-style" in ids       # older entries have no output_modalities
    assert "mystery/unpriced" in ids       # unknown price is not a reason to hide it


def test_catalog_drops_what_cannot_answer():
    ids = [m.id for m in orc.parse_models(_payload())]
    assert "recraft/recraft-v3" not in ids      # image generator
    assert "openai/gpt-4.1:batch" not in ids    # async batch endpoint


def test_prices_normalise_to_per_million():
    models = {m.id: m for m in orc.parse_models(_payload())}
    sonnet = models["anthropic/claude-sonnet-4.5"]
    assert sonnet.prompt_per_m == pytest.approx(3.0)
    assert sonnet.completion_per_m == pytest.approx(15.0)
    assert not sonnet.is_free
    assert "$3.00/$15.00 per M" in sonnet.option_label


def test_free_is_decided_by_price_not_by_the_slug():
    models = {m.id: m for m in orc.parse_models(_payload())}
    assert models["deepseek/deepseek-r1:free"].is_free
    assert models["deepseek/deepseek-r1:free"].option_label.startswith("🆓")


def test_unknown_price_is_not_reported_as_free():
    """Calling a missing price $0 would understate somebody's bill."""
    models = {m.id: m for m in orc.parse_models(_payload())}
    unpriced = models["mystery/unpriced"]
    assert not unpriced.is_free
    assert not unpriced.has_known_price
    assert unpriced.price_label == "price not known"
    assert "mystery/unpriced" not in orc.pricing_map(orc.parse_models(_payload()))


def test_catalog_is_sorted_and_grouped_by_vendor():
    models = orc.parse_models(_payload())
    assert [m.id for m in models] == sorted(m.id.lower() for m in models)
    assert orc.vendors(models) == ["anthropic", "deepseek", "legacy", "mystery"]


def test_free_router_is_always_offered():
    """It's a router, not a model, so it isn't reliably in /models — but it is
    what a new account starts on, so the picker must contain it."""
    models = orc.parse_models(_payload())
    assert not any(m.id == orc.FREE_ROUTER_ID for m in models)
    withrouter = orc._with_free_router(models)
    assert any(m.id == orc.FREE_ROUTER_ID for m in withrouter)
    # ...and adding it twice doesn't duplicate it.
    assert len(orc._with_free_router(withrouter)) == len(withrouter)


def test_bundled_snapshot_is_usable_offline():
    """The fallback must be a working list, not a placeholder."""
    models = list(orc.FALLBACK_MODELS)
    assert len(models) > 30
    assert any(m.id == orc.FREE_ROUTER_ID for m in models)
    assert len(orc.vendors(models)) > 10
    assert orc.pricing_map(models)          # some have real prices


def test_offline_fallback_warns_rather_than_failing_silently():
    def _boom(timeout=0):
        raise orc.CatalogError("Could not reach OpenRouter: offline")

    real = orc.fetch_models
    orc.fetch_models = _boom
    try:
        models, warning = orc.load_models()
    finally:
        orc.fetch_models = real

    assert models and warning
    assert "snapshot" in warning            # says which list is on screen
    assert "by hand" in warning             # and that a slug can still be typed


def test_malformed_response_is_an_error_not_an_empty_list():
    """An empty list must stay recognisable as a failure, not look like zero models."""
    with pytest.raises(orc.CatalogError):
        orc.parse_models({"nope": []})


def test_live_prices_reach_the_cost_meter():
    from peerparley.llm import PRICING, estimate_cost, register_pricing
    register_pricing(orc.pricing_map(orc.parse_models(_payload())))
    assert PRICING["anthropic/claude-sonnet-4.5"] == pytest.approx((3.0, 15.0))
    assert estimate_cost("anthropic/claude-sonnet-4.5",
                         Usage(1_000_000, 0, 1)) == pytest.approx(3.0)


# --------------------------------------------------------------------------- #
# Local model discovery and saved preferences (the rest of TransQ's flexibility)
# --------------------------------------------------------------------------- #

from peerparley import localmodels as lm  # noqa: E402
from peerparley.aiconfig import (  # noqa: E402
    load_settings,
    save_settings,
    settings_key,
)


def test_local_models_come_from_the_machine_not_a_hardcoded_list():
    """Both shapes seen in the wild: OpenAI-style and Ollama's native one."""
    assert lm._extract_models({"data": [{"id": "mistral"}, {"id": "phi4"}]}) == \
        ["mistral", "phi4"]
    assert lm._extract_models(
        {"models": [{"name": "qwen2.5:14b"}, {"name": "llama3.1:8b"}]}) == \
        ["llama3.1:8b", "qwen2.5:14b"]          # sorted, case-insensitive
    assert lm._extract_models({"data": ["bare-string"]}) == ["bare-string"]
    assert lm._extract_models({"nope": 1}) == []
    assert lm._extract_models("not a dict") == []


def test_duplicate_local_models_are_collapsed():
    assert lm._extract_models(
        {"data": [{"id": "a"}, {"id": "a"}, {"id": "b"}]}) == ["a", "b"]


def test_server_addresses_are_tidied_up():
    """A bare host or a missing /v1 should not be the user's problem."""
    assert lm.normalise("localhost:11434") == "http://localhost:11434/v1"
    assert lm.normalise("http://localhost:1234") == "http://localhost:1234/v1"
    assert lm.normalise("http://localhost:11434/v1/") == "http://localhost:11434/v1"


def test_probe_never_raises_on_a_closed_port():
    """A server that isn't running is the normal case, not an exception."""
    server = lm.probe("http://127.0.0.1:9/v1", timeout=1.0)   # discard port
    assert not server.is_usable
    assert server.detail                      # and it explains which cause
    assert "9" in server.detail or "reach" in server.detail.lower()


def test_hosted_detection_is_conservative(monkeypatch):
    """Being wrong loudly costs the credibility of every warning."""
    monkeypatch.delenv("STREAMLIT_SHARING_MODE", raising=False)
    monkeypatch.delenv("STREAMLIT_RUNTIME_ENV", raising=False)
    monkeypatch.setattr(lm.os.path, "isdir", lambda p: False)
    assert lm.is_hosted() is False

    monkeypatch.setenv("STREAMLIT_RUNTIME_ENV", "cloud")
    assert lm.is_hosted() is True


class PrefVault:
    def __init__(self):
        self.store = {}

    def put_bytes(self, name, data):
        self.store[name] = bytes(data)
        return name

    def get_bytes(self, name):
        return self.store[name]


def test_settings_round_trip_per_user():
    vault = PrefVault()
    a = AISettings(enabled=True, provider="openrouter",
                   model="anthropic/claude-sonnet-4.5", tone="direct",
                   target_words=240, verify=False, include_ratings=False,
                   extra_guidance="Write for sophomores.")
    b = AISettings(enabled=True, provider="anthropic", model="claude-haiku-4-5")
    assert save_settings(vault, "cms89", a)[0]
    assert save_settings(vault, "other", b)[0]

    back = load_settings(vault, "cms89")
    assert back.provider == "openrouter"
    assert back.model == "anthropic/claude-sonnet-4.5"
    assert back.tone == "direct" and back.target_words == 240
    assert back.verify is False and back.include_ratings is False
    assert back.extra_guidance == "Write for sophomores."
    assert load_settings(vault, "other").model == "claude-haiku-4-5"
    assert settings_key("cms89") != settings_key("other")


def test_the_api_key_is_never_persisted():
    """Its entire security story is that it dies with the session."""
    vault = PrefVault()
    s = AISettings(enabled=True, provider="openrouter", model="openrouter/free",
                   api_key="sk-or-v1-SECRETVALUE")
    save_settings(vault, "cms89", s)

    stored = vault.store[settings_key("cms89")].decode("utf-8")
    assert "SECRETVALUE" not in stored
    assert "api_key" not in stored
    assert load_settings(vault, "cms89").api_key == ""


def test_no_saved_settings_is_not_an_error():
    assert load_settings(PrefVault(), "nobody") is None


def test_corrupt_saved_settings_fall_back_to_defaults():
    vault = PrefVault()
    vault.store[settings_key("cms89")] = b"{not json"
    assert load_settings(vault, "cms89") is None


def test_saved_settings_tolerate_junk_field_types():
    """A hand-edited or version-skewed file must not crash the sidebar."""
    import json
    vault = PrefVault()
    vault.store[settings_key("u")] = json.dumps({
        "provider": "anthropic", "target_words": "not-a-number",
        "verify": "yes", "unknown_field": 1,
    }).encode("utf-8")
    s = load_settings(vault, "u")
    assert s.provider == "anthropic"
    assert s.target_words == AISettings().target_words   # junk ignored
    assert s.verify is True


# --------------------------------------------------------------------------- #
# Workspace autosave — the real answer to "my info was gone"
# (a live run lost an uploaded survey plus its AI output on sign-out)
# --------------------------------------------------------------------------- #

import pandas as pd  # noqa: E402

from peerparley import workspace as ws  # noqa: E402
from peerparley.ingest import Roster  # noqa: E402
from peerparley import survey as sv  # noqa: E402


def _working_set():
    df = pd.DataFrame([
        {"evaluator": "Ann Lee", "evaluator_key": "ann lee",
         "evaluator_team": "1", "evaluatee": "Bob Kim",
         "evaluatee_key": "bob kim", "points": 60.0,
         "public_comment": "Bob rebuilt the deck",
         "contribution_text": "Rebuilt the deck",
         "improve_text": "Went quiet in week 3",
         "confidential_comment": "instructor only", "rank": 1.0,
         "r0": 4.0, "r1": 4.0, "r2": 3.0, "r3": 4.0},
        {"evaluator": "Bob Kim", "evaluator_key": "bob kim",
         "evaluator_team": "1", "evaluatee": "Ann Lee",
         "evaluatee_key": "ann lee", "points": 40.0,
         "public_comment": "Ann led the analysis",
         "contribution_text": "Led the analysis",
         "improve_text": "Late updates", "confidential_comment": "",
         "rank": 2.0, "r0": 3.0, "r1": 3.0, "r2": 4.0, "r3": 3.0},
    ])
    self_evals = {
        ("1", "ann lee"): {"ratings": [3, 3, 3, 3], "rank": 1.0,
                           "self_contribution": "I led the analysis"},
        ("1", "bob kim"): {"ratings": [4, 4, 4, 4], "rank": 2.0,
                           "self_contribution": "I did the deck"},
    }
    roster = Roster()
    roster.by_key = {
        "ann lee": {"name": "Ann Lee", "first": "Ann", "last": "Lee",
                    "email": "ann@nau.edu", "team": "1"},
        "bob kim": {"name": "Bob Kim", "first": "Bob", "last": "Kim",
                    "email": "bob@nau.edu", "team": "1"},
    }
    return df, self_evals, roster


def test_the_whole_working_set_survives_a_logout():
    """Responses, self-evals, roster AND the course name — not just the frame."""
    vault = MemoryVault()
    df, self_evals, roster = _working_set()

    ok, err = ws.save(vault, "cms89", long_df=df, self_evals=self_evals,
                      roster=roster, course="Testing2", eval_no="1")
    assert ok, err

    state, err = ws.load(vault, "cms89")
    assert err == ""
    assert len(state["long_df"]) == 2
    assert list(state["long_df"].columns) == list(df.columns)
    assert state["course"] == "Testing2" and state["eval_no"] == "1"
    # The two things the old .ppx bundle silently dropped:
    assert state["self_evals"][("1", "ann lee")]["self_contribution"] == \
        "I led the analysis"
    assert state["roster"].match("Bob Kim")["email"] == "bob@nau.edu"


def test_restoring_the_course_name_reconnects_the_drafts():
    """The actual bug: drafts were in the vault under a key nobody asked for.

    They were saved under the slug for "Testing2", then looked for under the
    slug for "" because the course box resets on sign-in.
    """
    vault = MemoryVault()
    df, self_evals, roster = _working_set()
    ws.save(vault, "cms89", long_df=df, self_evals=self_evals, roster=roster,
            course="Testing2", eval_no="1")

    written_under = fai.drafts_key(sv.slugify("Testing2", "1"))
    looked_for_before = fai.drafts_key(sv.slugify("", "1"))
    assert written_under != looked_for_before        # the old failure

    state, _ = ws.load(vault, "cms89")
    looked_for_after = fai.drafts_key(
        sv.slugify(state["course"], state["eval_no"]))
    assert looked_for_after == written_under         # the fix


def test_self_eval_keys_round_trip_without_cross_contamination():
    """A separator that could appear in a team name would mis-file a self-rating."""
    tricky = {
        ("Team A|1", "o'brien, sean"): {"ratings": [1, 2, 3, 4], "rank": 1.0},
        ("1", "van der berg"): {"ratings": [4, 3, 2, 1], "rank": 2.0},
    }
    back = ws._decode_self_evals(ws._encode_self_evals(tricky))
    assert back == tricky


def test_autosave_is_skipped_when_there_is_nothing_loaded():
    ok, err = ws.save(MemoryVault(), "u", long_df=None)
    assert not ok and "nothing to autosave" in err
    ok, err = ws.save(MemoryVault(), "u", long_df=pd.DataFrame())
    assert not ok


def test_nothing_saved_is_silent_but_a_broken_save_speaks_up():
    assert ws.load(MemoryVault(), "nobody") == (None, "")

    vault = MemoryVault()
    vault.store[ws.workspace_key("u")] = b"{not json"
    state, err = ws.load(vault, "u")
    assert state is None and "could not be read" in err


def test_workspace_fingerprint_tracks_content_not_just_shape():
    df, _, _ = _working_set()
    fp = ws.fingerprint(df, "Testing2", "1")
    assert ws.fingerprint(df, "Testing2", "1") == fp
    assert ws.fingerprint(df, "OtherCourse", "1") != fp     # course matters
    assert ws.fingerprint(df, "Testing2", "2") != fp        # eval no matters

    edited = df.copy()
    edited.loc[0, "public_comment"] = "a different comment"
    assert ws.fingerprint(edited, "Testing2", "1") != fp    # content matters


def test_workspace_is_per_instructor():
    vault = MemoryVault()
    df, se, r = _working_set()
    ws.save(vault, "cms89", long_df=df, self_evals=se, roster=r, course="A", eval_no="1")
    ws.save(vault, "little", long_df=df.head(1), self_evals={}, roster=None,
            course="B", eval_no="2")
    assert ws.load(vault, "cms89")[0]["course"] == "A"
    assert ws.load(vault, "little")[0]["course"] == "B"
    assert ws.workspace_key("cms89") != ws.workspace_key("little")


def test_clear_removes_both_objects():
    vault = MemoryVault()
    df, se, r = _working_set()
    ws.save(vault, "u", long_df=df, self_evals=se, roster=r, course="A", eval_no="1")
    assert len(vault.store) == 2
    ws.clear(vault, "u")
    assert ws.load(vault, "u") == (None, "")


# ---- named .ppx bundles ---------------------------------------------------- #

def test_a_bundle_now_carries_everything_including_drafts():
    """The complaint: "the vault does not contain all the information"."""
    df, self_evals, roster = _working_set()
    drafts = {k: d.to_dict() for k, d in _reviewed_batch().items()}

    state, note = ws.read_bundle(
        ws.bundle_bytes(df, self_evals, roster, "Testing2", "1", drafts))

    assert note == ""
    assert len(state["long_df"]) == 2
    assert state["course"] == "Testing2" and state["eval_no"] == "1"
    assert state["self_evals"][("1", "bob kim")]["rank"] == 2.0
    assert state["roster"].match("Ann Lee")["email"] == "ann@nau.edu"
    assert set(state["drafts"]) == {"a", "b", "c"}
    assert state["drafts"]["a"]["approved"] is True
    assert not state["legacy"]


def test_bundle_drafts_rebuild_into_real_drafts_with_approvals_intact():
    df, se, r = _working_set()
    drafts = {k: d.to_dict() for k, d in _reviewed_batch().items()}
    state, _ = ws.read_bundle(ws.bundle_bytes(df, se, r, "T", "1", drafts))

    rebuilt = {k: fai.Draft.from_dict(v) for k, v in state["drafts"].items()}
    assert set(fai.approved_narratives(rebuilt)) == {"a"}
    assert rebuilt["a"].text() == "The instructor's own wording, kept verbatim."


def test_old_bundles_still_load_and_say_what_is_missing():
    """A file written last semester is exactly when compatibility matters."""
    import io as _io
    df, _, _ = _working_set()
    buf = _io.BytesIO()
    df.to_parquet(buf, index=False)          # what a legacy .ppx decrypts to

    state, note = ws.read_bundle(buf.getvalue())
    assert state["legacy"] is True
    assert len(state["long_df"]) == 2
    assert state["self_evals"] == {} and state["drafts"] == {}
    assert "responses only" in note          # and it says so rather than pretending


def test_empty_drafts_get_their_own_metric_not_unsupported_claims():
    """An empty reply is a model-quality problem, not a grounding one."""
    real_claim = fai.Draft(key="a", name="A", team="1", strengths="Real text.")
    real_claim.flags = [fai.Flag(text="x", problem="invented advice",
                                 severity="high")]
    came_back_empty = fai.Draft(key="b", name="B", team="1")
    came_back_empty.flags = [fai.Flag(text="(whole draft)",
                                      problem="returned no narrative text",
                                      severity="high")]
    recovered = fai.Draft(key="c", name="C", team="1", strengths="Partial text.",
                          truncated=True, raw_partial='{"strengths": "Partial')

    st = fai.batch_stats({"a": real_claim, "b": came_back_empty, "c": recovered})
    assert st["high"] == 1          # only the genuine ungrounded claim
    assert st["empty"] == 1
    assert st["truncated"] == 1
    assert st["written"] == 2       # the empty one produced nothing


# --------------------------------------------------------------------------- #
# Free-tier pacing and budgeting
# --------------------------------------------------------------------------- #

from peerparley.llm import (  # noqa: E402
    FREE_TIER_DAILY,
    FREE_TIER_RPM,
    is_free_model,
)


def test_free_models_are_recognised_by_slug():
    assert is_free_model("openrouter/free")
    assert is_free_model("nvidia/nemotron-3-ultra-550b:free")
    assert is_free_model("THINKINGMACHINES/INKLING:FREE")     # case-insensitive
    assert not is_free_model("openai/gpt-4.1-mini")
    assert not is_free_model("anthropic/claude-sonnet-4.5")
    assert not is_free_model("")


def test_calls_per_student_reflects_the_audit():
    assert fai.calls_per_student(AISettings(verify=True)) == 2
    assert fai.calls_per_student(AISettings(verify=False)) == 1


def test_a_forty_student_batch_with_the_audit_exceeds_the_free_daily_cap():
    """The arithmetic the panel now shows before spending a batch."""
    with_audit = 40 * fai.calls_per_student(AISettings(verify=True))
    without = 40 * fai.calls_per_student(AISettings(verify=False))
    assert with_audit == 80 and with_audit > FREE_TIER_DAILY
    assert without == 40 and without <= FREE_TIER_DAILY


def test_pacing_converts_requests_per_minute_into_an_interval():
    s = AISettings(enabled=True, provider="openrouter", model="openrouter/free",
                   api_key="sk-or-x", requests_per_minute=18)
    # make_client would need the openai package; check the arithmetic directly
    assert 60.0 / s.requests_per_minute == pytest.approx(60 / 18)
    assert AISettings().requests_per_minute == 0          # off unless asked


def test_throttle_waits_between_calls_and_is_off_by_default():
    import time
    from peerparley.llm import LLMClient
    from peerparley.aiconfig import get_provider

    c = LLMClient.__new__(LLMClient)
    c.spec = get_provider("openrouter")
    c.min_interval = 0.25
    c._last_call = 0.0

    c._throttle()                      # first call never waits
    started = time.monotonic()
    c._throttle()                      # second must honour the interval
    assert time.monotonic() - started >= 0.2

    c.min_interval = 0.0
    started = time.monotonic()
    c._throttle()
    c._throttle()
    assert time.monotonic() - started < 0.05      # disabled means no sleeping


# --------------------------------------------------------------------------- #
# Model suitability, recommendations, and the observed track record
# --------------------------------------------------------------------------- #

from peerparley import model_advisor as advisor  # noqa: E402
from peerparley.openrouter_catalog import (  # noqa: E402
    KeyStatus,
    fetch_key_status,
    parse_models,
)


def _catalog():
    return parse_models({"data": [
        {"id": "openai/gpt-4.1-mini", "name": "mini", "context_length": 1000000,
         "pricing": {"prompt": "0.0000004", "completion": "0.0000016"},
         "architecture": {"output_modalities": ["text"]},
         "supported_parameters": ["response_format", "structured_outputs"],
         "top_provider": {"max_completion_tokens": 32768}},
        {"id": "anthropic/claude-sonnet-4.5", "name": "sonnet",
         "context_length": 200000,
         "pricing": {"prompt": "0.000003", "completion": "0.000015"},
         "architecture": {"output_modalities": ["text"]},
         "supported_parameters": ["structured_outputs"],
         "top_provider": {"max_completion_tokens": 64000}},
        {"id": "good/free-model:free", "name": "good free",
         "context_length": 1000000,
         "pricing": {"prompt": "0", "completion": "0"},
         "architecture": {"output_modalities": ["text"]},
         "supported_parameters": ["structured_outputs"],
         "top_provider": {"max_completion_tokens": 16000}},
        {"id": "weak/rambler:free", "name": "weak free",
         "context_length": 262000,
         "pricing": {"prompt": "0", "completion": "0"},
         "architecture": {"output_modalities": ["text"]},
         "supported_parameters": ["max_tokens"],
         "top_provider": {"max_completion_tokens": 4096}},
        {"id": "tiny/toosmall:free", "name": "tiny",
         "context_length": 4000, "pricing": {"prompt": "0", "completion": "0"},
         "architecture": {"output_modalities": ["text"]},
         "supported_parameters": [],
         "top_provider": {"max_completion_tokens": 512}},
    ]})


def test_capability_fields_are_parsed():
    models = {m.id: m for m in _catalog()}
    assert models["openai/gpt-4.1-mini"].supports_json
    assert models["openai/gpt-4.1-mini"].max_completion_tokens == 32768
    assert not models["weak/rambler:free"].supports_json


def test_a_model_too_small_for_the_job_is_ruled_out_before_spending():
    models = {m.id: m for m in _catalog()}
    a = advisor.assess(models["tiny/toosmall:free"], reply_tokens=4000)
    assert a.verdict == "unusable" and not a.usable
    assert any("truncate" in r for r in a.reasons)


def test_no_structured_output_support_is_a_warning_not_a_ban():
    """Salvage exists, so it is risky rather than impossible."""
    models = {m.id: m for m in _catalog()}
    a = advisor.assess(models["weak/rambler:free"], reply_tokens=4000)
    assert a.verdict == "risky" and a.usable
    assert any("structured outputs" in w for w in a.warnings)


def test_a_reply_ceiling_under_the_configured_cap_warns():
    models = {m.id: m for m in _catalog()}
    a = advisor.assess(models["good/free-model:free"], reply_tokens=32000)
    assert a.verdict == "risky"
    assert any("ceiling" in w for w in a.warnings)


def test_recommendations_cover_both_free_and_paid():
    rec = advisor.recommend(_catalog(), reply_tokens=4000)
    assert rec["paid"] and rec["free"]
    assert rec["paid"].model == "openai/gpt-4.1-mini"       # cheapest of equals
    assert rec["free"].model == "good/free-model:free"
    assert rec["free"].usable and rec["paid"].usable


def test_the_free_router_is_never_recommended():
    """It picks a different model per call, so advising it advises a lottery."""
    models = _catalog() + list(parse_models({"data": [
        {"id": "openrouter/free", "name": "router", "context_length": 1000000,
         "pricing": {"prompt": "0", "completion": "0"},
         "architecture": {"output_modalities": ["text"]},
         "supported_parameters": ["structured_outputs"],
         "top_provider": {"max_completion_tokens": 32000}}]}))
    rec = advisor.recommend(models, reply_tokens=4000)
    assert rec["free"].model != "openrouter/free"
    # ...but it is never hidden from the picker either.
    assert any(m.id == "openrouter/free"
               for m in advisor.usable_only(models, reply_tokens=4000))


def test_observed_failure_downgrades_an_otherwise_fine_model():
    models = {m.id: m for m in _catalog()}
    records = {"good/free-model:free": advisor.ModelRecord(
        model="good/free-model:free", attempts=40, written=2, empty=30, failed=8)}

    clean = advisor.assess(models["good/free-model:free"], reply_tokens=4000)
    assert clean.verdict == "suitable"

    burned = advisor.assess(models["good/free-model:free"], reply_tokens=4000,
                            record=records["good/free-model:free"])
    assert burned.verdict == "risky"
    assert any("poor record" in w for w in burned.warnings)
    assert burned.score < clean.score
    # And the recommendation moves away from it.
    assert advisor.recommend(_catalog(), reply_tokens=4000,
                             records=records)["free"].model != "good/free-model:free"


def test_a_single_success_is_not_reported_as_a_100_percent_rate():
    """The most tempting dishonesty in the whole module."""
    one = advisor.ModelRecord(model="x", attempts=1, written=1)
    assert one.rate is None
    assert not one.enough_data
    assert "too few runs" in one.summary()
    assert "100%" not in one.summary()

    enough = advisor.ModelRecord(model="x", attempts=10, written=9)
    assert enough.rate == pytest.approx(0.9)
    assert "90%" in enough.summary() and "10 attempts" in enough.summary()


def test_track_record_accumulates_across_batches():
    vault = MemoryVault()
    good = fai.Draft(key="a", name="A", team="1", strengths="Real text.")
    cut = fai.Draft(key="b", name="B", team="1", strengths="Partial.",
                    truncated=True)
    blank = fai.Draft(key="c", name="C", team="1")
    broke = fai.Draft(key="d", name="D", team="1", error="401")
    thin = fai.Draft(key="e", name="E", team="1",
                     insufficient_evidence="Too few comments.")

    advisor.record_batch(vault, "some/model", [good, cut, blank, broke, thin])
    rec = advisor.load_records(vault)["some/model"]

    # The thin one is not the model's fault, so it is not counted either way.
    assert rec.attempts == 4
    assert rec.written == 2 and rec.truncated == 1
    assert rec.empty == 1 and rec.failed == 1

    advisor.record_batch(vault, "some/model", [good])
    assert advisor.load_records(vault)["some/model"].attempts == 5


def test_recording_never_raises_on_a_broken_vault():
    class Broken:
        def get_bytes(self, n): raise RuntimeError("down")
        def put_bytes(self, n, d): raise RuntimeError("down")
    advisor.record_batch(Broken(), "m", [fai.Draft(key="a", name="A", team="1")])
    assert advisor.load_records(Broken()) == {}


def test_key_status_reports_a_rejected_key_without_raising():
    st = fetch_key_status("")
    assert not st.valid and "No API key" in st.error


def test_key_status_free_allowance_is_readable():
    st = KeyStatus(valid=True, is_free_tier=True, free_used=45,
                   free_limit=50, free_remaining=5)
    assert st.free_known and st.free_remaining == 5
    assert not KeyStatus(valid=True, free_limit=0).free_known


def test_usable_filter_keeps_risky_models_and_drops_hopeless_ones():
    kept = {m.id for m in advisor.usable_only(_catalog(), reply_tokens=4000)}
    assert "weak/rambler:free" in kept          # risky, but salvage may work
    assert "tiny/toosmall:free" not in kept     # cannot ever fit


# --------------------------------------------------------------------------- #
# What students actually receive
# --------------------------------------------------------------------------- #

def test_the_four_written_feedback_modes_produce_different_reports():
    from peerparley import pdfgen
    m = make_student()
    m.contributions = ["His VP part in the simulation.",
                       "Good supply chain decisions."]
    m.improvements = ["Better communication."]
    narrative = ("Your teammates valued the VP role you played and your "
                 "supply-chain decisions. One asked for more communication.")

    sizes = {}
    for label, flags in (
        ("both", {"narrative": True, "valued": True, "focus": True}),
        ("summary", {"narrative": True, "valued": False, "focus": False}),
        ("comments", {"narrative": False, "valued": True, "focus": True}),
        ("neither", {"narrative": False, "valued": False, "focus": False}),
    ):
        pdf = pdfgen.build_individual_pdf(m, "1", "MGT 490C", report=flags,
                                          narrative=narrative)
        assert pdf.startswith(b"%PDF")
        sizes[label] = len(pdf)

    assert sizes["both"] > sizes["summary"] > sizes["neither"]
    assert sizes["both"] > sizes["comments"] > sizes["neither"]


def test_summary_only_still_honours_the_approval_gate():
    """Summary-only must not mean "send an unapproved draft"."""
    from peerparley import pdfgen
    m = make_student()
    m.contributions = ["Did the modelling."]
    m.improvements = ["Communicate earlier."]
    flags = {"narrative": True, "valued": False, "focus": False}

    unapproved = fai.Draft(key=m.key, name=m.name, team=m.team,
                           strengths="A draft nobody approved.")
    assert fai.approved_narratives({m.key: unapproved}) == {}

    # With no approved narrative and the bullets switched off, the student
    # simply gets no written feedback — never an unreviewed draft.
    pdf = pdfgen.build_individual_pdf(
        m, "1", "C", report=flags,
        narrative=fai.approved_narratives({m.key: unapproved}).get(m.key, ""))
    assert pdf.startswith(b"%PDF")


# --------------------------------------------------------------------------- #
# Language screening
# --------------------------------------------------------------------------- #

from peerparley import safety  # noqa: E402


def test_the_screen_leaves_legitimate_criticism_alone():
    """False positives train people to click through warnings."""
    for text in ("Kyle did everything he was required to do.",
                 "She could speak up more in meetings.",
                 "His drafts arrived close to the deadline.",
                 "Good supply chain decision making",
                 "assignment class passed analysis association"):
        assert safety.screen(text) == [], text


def test_blunt_but_substantive_criticism_is_only_noted():
    """"He did nothing" may be the honest and useful truth."""
    for text in ("He contributed nothing to the project.",
                 "She never showed up to a single meeting.",
                 "Honestly he was lazy all semester."):
        found = safety.screen(text)
        assert found, text
        assert safety.worst(found) == "mild"
        assert not safety.blocks_release(found)


def test_personal_insults_are_flagged_as_moderate():
    found = safety.screen("This guy is a useless idiot.")
    assert safety.worst(found) == "moderate"
    assert not safety.blocks_release(found)
    assert any("insult" in c.reason for c in found)


def test_not_belonging_in_the_programme_is_flagged():
    found = safety.screen("Total waste of space, shouldn't be in this major.")
    assert safety.worst(found) == "moderate"
    assert any("belong" in c.reason for c in found)


def test_threats_and_slurs_block_release():
    for text in ("kys", "He should die", "what a retarded thing to do"):
        found = safety.screen(text)
        assert safety.worst(found) == "severe", text
        assert safety.blocks_release(found), text


def test_advice_points_at_the_per_student_remedy():
    """A severe finding in a teammate's words has a specific fix."""
    from_comment = safety.screen("kys", where="comment")
    assert "Summary only" in safety.advice(from_comment)
    from_narrative = safety.screen("kys", where="narrative")
    assert "regenerate" in safety.advice(from_narrative).lower()


def test_a_bad_custom_pattern_cannot_break_the_screen():
    safety.EXTRA_PATTERNS.append(("([unclosed", "broken", "severe"))
    try:
        assert safety.screen("perfectly ordinary feedback") == []
    finally:
        safety.EXTRA_PATTERNS.pop()


def test_custom_patterns_are_honoured():
    safety.EXTRA_PATTERNS.append((r"\bcollege-specific-slur\b",
                                  "institution's own list", "severe"))
    try:
        found = safety.screen("that was a college-specific-slur moment")
        assert safety.blocks_release(found)
        assert any("institution" in c.reason for c in found)
    finally:
        safety.EXTRA_PATTERNS.pop()


def test_comments_are_screened_even_when_no_narrative_is_written(settings):
    """The case a narrative-only screen misses entirely.

    Too few comments to draft from, so no API call — but the comments still
    reach the student, and one of them is abusive.
    """
    settings.retry_truncated = False
    thin = make_student(name="Target", key="target")
    thin.contributions = ["kys"]
    thin.improvements = []
    thin.public_comments = []

    client = FakeClient()      # empty queue: a call would raise
    draft = fai.generate_draft(client, fai.build_source(thin), settings)

    assert client.usage.calls == 0          # still no API call
    assert draft.insufficient_evidence
    assert draft.blocked                    # but it is flagged
    assert draft.comment_concerns


def test_both_surfaces_are_screened(student, settings):
    settings.retry_truncated = False
    student.contributions = ["He is a useless idiot.",
                             "Built the regression model and explained it."]
    payload = dict(GOOD_PAYLOAD)
    client = FakeClient(payload)
    draft = fai.generate_draft(client, fai.build_source(student), settings)

    assert any(c.where == "comment" for c in draft.concerns)
    assert draft.worst_concern == "moderate"
    assert not draft.clean          # never bulk-approved with a flag


def test_the_audit_can_add_a_concern_the_word_list_misses(student, settings):
    """Contempt in clean language — no profanity to match on."""
    settings.verify = True
    settings.retry_truncated = False
    audit = {
        "grounded": True, "score": 1.0, "unsupported": [],
        "abusive": [{"text": "She simply is not capable of this work.",
                     "reason": "contempt directed at the person",
                     "severity": "moderate", "where": "comment"}],
    }
    client = FakeClient(GOOD_PAYLOAD, audit)
    draft = fai.generate_draft(client, fai.build_source(student), settings)

    assert draft.concerns
    assert any("contempt" in c.reason for c in draft.concerns)
    assert client.usage.calls == 2          # no extra call for the screen


def test_concerns_survive_the_vault_round_trip():
    vault = MemoryVault()
    d = fai.Draft(key="a", name="A", team="1", strengths="Text.")
    d.concerns = [safety.Concern(text="kys", reason="threat",
                                 severity="severe", where="comment")]
    d.delivery = "summary"
    fai.save_drafts(vault, "s", {"a": d})
    back, _ = fai.load_drafts(vault, "s")
    assert back["a"].blocked
    assert back["a"].comment_concerns[0].reason == "threat"
    assert back["a"].delivery == "summary"


# --------------------------------------------------------------------------- #
# Per-student delivery overrides
# --------------------------------------------------------------------------- #

def test_delivery_override_produces_per_student_report_flags():
    base = {"narrative": True, "valued": True, "focus": True, "pay_grade": True}
    drafts = {
        "default": fai.Draft(key="default", name="D", team="1"),
        "sum": fai.Draft(key="sum", name="S", team="1", delivery="summary"),
        "raw": fai.Draft(key="raw", name="R", team="1", delivery="comments"),
        "off": fai.Draft(key="off", name="O", team="1", delivery="none"),
        "both": fai.Draft(key="both", name="B", team="1", delivery="both"),
    }
    out = fai.report_overrides(drafts, base)

    assert "default" not in out          # follows the survey-wide setting
    assert out["sum"] == {**base, "narrative": True,
                          "valued": False, "focus": False}
    assert out["raw"] == {**base, "narrative": False,
                          "valued": True, "focus": True}
    assert out["off"] == {**base, "narrative": False,
                          "valued": False, "focus": False}
    assert out["both"] == {**base, "narrative": True,
                           "valued": True, "focus": True}
    # Unrelated flags are carried through untouched.
    assert all(v["pay_grade"] is True for v in out.values())


def test_an_override_changes_only_that_students_pdf():
    from peerparley import pdfgen
    base = {"narrative": True, "valued": True, "focus": True}
    m = make_student()
    m.contributions = ["Built the model and explained it twice."]
    m.improvements = ["Send updates earlier in the day."]
    narrative = "Your teammates valued the model you built."

    drafts = {m.key: fai.Draft(key=m.key, name=m.name, team=m.team,
                               strengths=narrative, approved=True,
                               delivery="summary")}
    overrides = fai.report_overrides(drafts, base)

    summary_only = pdfgen.build_individual_pdf(
        m, "1", "C", report=overrides[m.key], narrative=narrative)
    with_both = pdfgen.build_individual_pdf(
        m, "1", "C", report=base, narrative=narrative)
    assert len(with_both) > len(summary_only)


def test_delivery_summary_counts_against_the_survey_default():
    base = {"narrative": True, "valued": True, "focus": True}
    drafts = {
        "a": fai.Draft(key="a", name="A", team="1"),                  # default
        "b": fai.Draft(key="b", name="B", team="1"),                  # default
        "c": fai.Draft(key="c", name="C", team="1", delivery="summary"),
    }
    counts = fai.delivery_summary(drafts, base)
    assert counts["both"] == 2 and counts["summary"] == 1

    # With a summary-only survey default, the unset students count as summary.
    counts = fai.delivery_summary(
        drafts, {"narrative": True, "valued": False, "focus": False})
    assert counts["summary"] == 3


def test_comments_only_suppresses_an_approved_narrative():
    """Choosing comments-only must beat an approval, not be beaten by it."""
    base = {"narrative": True, "valued": True, "focus": True}
    d = fai.Draft(key="a", name="A", team="1", strengths="Approved text.",
                  approved=True, delivery="comments")
    flags = fai.report_overrides({"a": d}, base)["a"]
    assert flags["narrative"] is False
    # The narrative is still "approved" — the report simply does not show it.
    assert fai.approved_narratives({"a": d}) == {"a": "Approved text."}


def _checked_draft(key: str, name: str) -> fai.Draft:
    """A draft in the state a real one reaches: points cited, citations checked.

    Hand-built drafts used to count as `clean` with no points at all, which is
    the hole `Draft.clean` was tightened to close — so a test about language
    concerns has to build drafts that are otherwise genuinely clean, or it would
    be asserting the hole.
    """
    d = fai.Draft(key=key, name=name, team="1", strengths="Text.")
    d.strength_points = [fai.Point(point="Reliable.", source="On time", raised_by=1,
                                  quote_match=1.0, quote_ok=True)]
    d.quotes_checked = True
    return d


def test_batch_stats_counts_blocked_and_flagged_language():
    blocked = _checked_draft("a", "A")
    blocked.concerns = [safety.Concern(text="kys", reason="threat",
                                       severity="severe")]
    moderate = _checked_draft("b", "B")
    moderate.concerns = [safety.Concern(text="idiot", reason="insult",
                                        severity="moderate")]
    mild = _checked_draft("c", "C")
    mild.concerns = [safety.Concern(text="lazy", reason="harsh",
                                    severity="mild")]
    clean = _checked_draft("d", "D")

    st = fai.batch_stats({"a": blocked, "b": moderate, "c": mild, "d": clean})
    assert st["blocked"] == 1
    assert st["flagged_language"] == 2          # severe + moderate, not mild
    assert clean.clean and mild.clean           # mild does not block approval
    assert not blocked.clean and not moderate.clean


# --------------------------------------------------------------------------- #
# Running out of credit (a live 402 across a whole batch)
# --------------------------------------------------------------------------- #

from peerparley.llm import (  # noqa: E402
    BillingLLMError,
    _is_billing,
    affordable_tokens,
)

REAL_402 = ("Error code: 402 - {'error': {'message': 'This request requires "
            "more credits, or fewer max_tokens. You requested up to 4000 "
            "tokens, but can only afford 680.', 'code': 402}}")


def test_a_402_is_classified_as_billing_not_as_a_generic_failure():
    from peerparley.llm import _is_auth, _is_transient
    assert _is_billing(Exception(REAL_402))
    # And it must not be retried: the balance will not change on retry.
    assert not _is_transient(Exception(REAL_402))
    for other in ("insufficient_quota", "Payment Required",
                  "add credits to continue"):
        assert _is_billing(Exception(other)), other
    # Ordinary failures are not billing failures.
    assert not _is_billing(Exception("429 rate limit exceeded"))
    assert not _is_billing(Exception("500 internal error"))


def test_billing_failures_abort_the_batch_like_auth_failures():
    """Four students failing for one exhausted balance is one fact, not four."""
    from peerparley.grading import TeamResult

    members = []
    for i in range(6):
        m = make_student(name=f"Student {i}", key=f"s{i}")
        m.contributions = ["Built the model and explained it to everyone twice."]
        m.improvements = ["Sends updates late in the evening."]
        members.append(m)
    teams = [TeamResult(team="1", members=members, team_score=100.0)]

    class OutOfCredit(FakeClient):
        def complete_json(self, system, user, max_tokens=None):
            self.prompts.append((system, user))
            raise BillingLLMError("balance exhausted")

    client = OutOfCredit()
    settings = AISettings(enabled=True, provider="openrouter",
                          model="openai/gpt-4.1-mini", api_key="sk-or-x",
                          verify=False)
    with pytest.raises(fai.BatchAborted):
        fai.generate_for_teams(teams, settings, client=client)
    assert len(client.prompts) == 1      # stopped at the first, not the sixth


def test_the_402_message_names_both_remedies():
    from peerparley.llm import LLMClient
    from peerparley.aiconfig import get_provider
    c = LLMClient.__new__(LLMClient)
    c.spec = get_provider("openrouter")
    text = c._billing_help(Exception(REAL_402), 4000)

    assert "not have enough credit" in text
    assert "680" in text                 # what the balance actually covers
    assert "4,000" in text               # and what was asked for
    assert "credits" in text and "free model" in text
    # 680 is too little to rescue, so it must not suggest lowering the cap.
    assert "would let it through" not in text


def test_the_402_message_suggests_a_lower_cap_when_that_would_work():
    from peerparley.llm import LLMClient
    from peerparley.aiconfig import get_provider
    c = LLMClient.__new__(LLMClient)
    c.spec = get_provider("openrouter")
    plenty = ("Error code: 402 - requires more credits, or fewer max_tokens. "
              "You requested up to 8000 tokens, but can only afford 3500.")
    text = c._billing_help(Exception(plenty), 8000)
    assert "3,500" in text and "would let it through" in text


def test_affordable_tokens_matches_openrouters_own_arithmetic():
    """OpenRouter said 680; the app should land in the same place."""
    from peerparley.llm import PRICING
    PRICING["test/nano"] = (0.10, 0.40)        # $0.40 per M output
    assert affordable_tokens(0.000272, "test/nano") == 680
    assert affordable_tokens(0.50, "test/nano") == 1_250_000
    # Unknowable cases return None rather than a confident guess.
    assert affordable_tokens(None, "test/nano") is None
    assert affordable_tokens(1.0, "model/with-no-published-price") is None
    assert affordable_tokens(0.5, "openrouter/free") is None   # free: not the limit


def test_an_unaffordable_model_is_not_reported_as_suitable():
    """The screenshot: "Should handle this" beside a hard 402."""
    from peerparley.llm import PRICING
    PRICING["test/nano"] = (0.10, 0.40)
    model = parse_models({"data": [
        {"id": "test/nano", "name": "nano", "context_length": 1000000,
         "pricing": {"prompt": "0.0000001", "completion": "0.0000004"},
         "architecture": {"output_modalities": ["text"]},
         "supported_parameters": ["response_format", "structured_outputs"],
         "top_provider": {"max_completion_tokens": 32768}}]})[0]

    # Capable, and with credit it is genuinely suitable.
    assert advisor.assess(model, reply_tokens=4000,
                          credits_remaining=0.50).verdict == "suitable"

    # With the balance from the screenshot it cannot run at all.
    broke = advisor.assess(model, reply_tokens=4000, credits_remaining=0.000272)
    assert broke.verdict == "unusable"
    assert broke.unaffordable
    assert "Not enough credit" in broke.headline()
    assert any("balance" in r for r in broke.reasons)
    assert any("free model" in w for w in broke.warnings)

    # Enough to run, but under the cap: a warning, not a ban.
    tight = advisor.assess(model, reply_tokens=4000, credits_remaining=0.0015)
    assert tight.verdict == "risky"
    assert any("under the" in w for w in tight.warnings)

    # Balance unknown: judged on capability alone, as before.
    assert advisor.assess(model, reply_tokens=4000).verdict == "suitable"


def test_an_unaffordable_model_is_not_recommended():
    from peerparley.llm import PRICING
    PRICING["test/cheap"] = (0.10, 0.40)
    PRICING["test/dear"] = (10.0, 40.0)
    models = parse_models({"data": [
        {"id": "test/dear", "name": "dear", "context_length": 200000,
         "pricing": {"prompt": "0.00001", "completion": "0.00004"},
         "architecture": {"output_modalities": ["text"]},
         "supported_parameters": ["structured_outputs"],
         "top_provider": {"max_completion_tokens": 64000}},
        {"id": "test/cheap", "name": "cheap", "context_length": 200000,
         "pricing": {"prompt": "0.0000001", "completion": "0.0000004"},
         "architecture": {"output_modalities": ["text"]},
         "supported_parameters": ["structured_outputs"],
         "top_provider": {"max_completion_tokens": 32000}},
    ]})
    # A balance that covers the cheap model but not the dear one.
    rec = advisor.recommend(models, reply_tokens=4000, credits_remaining=0.01)
    assert rec["paid"].model == "test/cheap"

    # A balance that covers neither leaves no paid recommendation at all.
    assert advisor.recommend(models, reply_tokens=4000,
                             credits_remaining=0.0000001)["paid"] is None


def test_free_models_are_never_judged_on_the_balance():
    """They draw on the daily allowance, not on credit."""
    model = parse_models({"data": [
        {"id": "some/free:free", "name": "free", "context_length": 200000,
         "pricing": {"prompt": "0", "completion": "0"},
         "architecture": {"output_modalities": ["text"]},
         "supported_parameters": ["structured_outputs"],
         "top_provider": {"max_completion_tokens": 32000}}]})[0]
    a = advisor.assess(model, reply_tokens=4000, credits_remaining=0.0)
    assert a.verdict == "suitable" and not a.unaffordable


# --------------------------------------------------------------------------- #
# The local-model picker's view of a probe
# --------------------------------------------------------------------------- #
# These exist because of a shipped bug: the mapping below lived inside a
# Streamlit-cached wrapper, where no test could reach it, and it read a
# ``status`` attribute that LocalServer does not have. Choosing "On this
# computer" raised AttributeError and — because the sidebar renders before
# every tab — took the whole page down with it. The mapping is now a plain
# function, and these tests cover all three states a probe can return.

from peerparley import ai_ui  # noqa: E402
from peerparley import localmodels  # noqa: E402


def test_local_server_has_no_status_attribute():
    """The guard for the exact mistake that shipped.

    If a future refactor adds a ``status`` field, this test fails loudly rather
    than letting the two spellings drift apart again.
    """
    server = localmodels.LocalServer(base_url="http://localhost:11434/v1")
    assert not hasattr(server, "status")
    assert callable(server.status_line)


def test_an_unreachable_server_maps_to_an_unusable_state():
    server = localmodels.LocalServer(
        base_url="http://localhost:11434/v1", label="Ollama",
        detail="Nothing is listening on port 11434.")
    state = ai_ui._local_server_state(server)
    assert state["usable"] is False
    assert state["reachable"] is False
    assert state["models"] == []
    assert state["status"] == "Nothing is listening on port 11434."
    assert state["label"] == "Ollama"


def test_a_running_server_with_no_models_is_not_usable():
    """Reachable is not the same as ready, and the message has to say which."""
    server = localmodels.LocalServer(
        base_url="http://localhost:11434/v1", label="Ollama", reachable=True)
    state = ai_ui._local_server_state(server)
    assert state["usable"] is False
    assert state["reachable"] is True
    assert "no models are downloaded yet" in state["status"]


def test_a_ready_server_maps_to_its_model_list():
    server = localmodels.LocalServer(
        base_url="http://localhost:11434/v1", label="Ollama", reachable=True,
        models=["llama3.1:8b", "qwen2.5:14b"])
    state = ai_ui._local_server_state(server)
    assert state["usable"] is True
    assert state["models"] == ["llama3.1:8b", "qwen2.5:14b"]
    assert state["status"] == "Ollama · 2 models ready"


def test_the_state_is_a_copy_not_the_servers_own_list():
    """The picker mutates nothing, but a cached dict sharing a list would be a
    trap waiting for the first person who does."""
    server = localmodels.LocalServer(
        base_url="http://localhost:11434/v1", reachable=True, models=["a"])
    state = ai_ui._local_server_state(server)
    state["models"].append("b")
    assert server.models == ["a"]


def test_a_probe_that_raises_still_returns_the_shape_the_picker_reads():
    """A probe is a question about the outside world; it must never be able to
    take down the page that asked it."""
    def boom(_url):
        raise RuntimeError("socket exploded")

    ok = ai_ui._local_server_state(localmodels.LocalServer(
        base_url="x", reachable=True, models=["m"], label="Ollama"))
    fallback = ai_ui._probe_local.__wrapped__("http://localhost:9/v1")
    assert set(fallback) == set(ok), "fallback must not omit a key the picker reads"

    original = localmodels.probe
    localmodels.probe = boom
    try:
        state = ai_ui._probe_local.__wrapped__("http://localhost:11434/v1")
    finally:
        localmodels.probe = original
    assert state["usable"] is False
    assert "socket exploded" in state["status"]
    assert set(state) == set(ok)


# --------------------------------------------------------------------------- #
# v2.9 — the audit release
# --------------------------------------------------------------------------- #
# Each test below is tied to a specific defect found while reviewing the whole
# application, and each names what used to happen. They are the record of why
# these behaviours are the way they are.

from peerparley import grading as grd  # noqa: E402
from peerparley import survey as svy  # noqa: E402
from peerparley import workspace as wsp  # noqa: E402
from peerparley.llm import QuotaLLMError, _is_auth, _is_billing, _is_quota  # noqa: E402


def _rows(spec, team="T1", comment="{ee} carried the analysis and met every deadline."):
    """evaluator -> {evaluatee: dollars} as the tidy frame compute() consumes."""
    import pandas as pd
    out = []
    for ev, gives in spec.items():
        for ee, pts in gives.items():
            out.append(dict(evaluator=ev, evaluator_key=ev.lower(), evaluator_team=team,
                            evaluatee=ee, evaluatee_key=ee.lower(), points=pts,
                            public_comment=comment.format(ee=ee)))
    return pd.DataFrame(out)


# ---- the AI writer is off by default, and stays off ----------------------- #

def test_enabling_the_ai_writer_is_never_remembered():
    """It must be a deliberate act every session.

    Remembering "on" would mean an instructor who tried it once in March finds
    it armed in September, spending on an API account and sending student
    comments to a vendor because of a click they have forgotten.
    """
    from peerparley.aiconfig import SETTINGS_FIELDS
    assert "enabled" not in SETTINGS_FIELDS
    assert "api_key" not in SETTINGS_FIELDS
    assert AISettings().enabled is False


def test_saved_settings_restore_everything_except_the_switch():
    class _V:
        def __init__(self): self.store = {}
        def put_bytes(self, name, data): self.store[name] = data
        def get_bytes(self, name): return self.store[name]

    from peerparley.aiconfig import load_settings, save_settings
    v = _V()
    s = AISettings(enabled=True, provider="anthropic", model="claude-x",
                   tone="direct", api_key="sk-secret")
    assert save_settings(v, "chris", s)[0]
    back = load_settings(v, "chris")
    assert back.provider == "anthropic" and back.model == "claude-x"
    assert back.tone == "direct"
    assert back.enabled is False            # the switch does not come back
    assert back.api_key == ""               # nor does the key
    assert b"sk-secret" not in v.store["aisettings__chris.json"]


# ---- grading: a student nobody evaluated ---------------------------------- #

def test_a_student_nobody_rated_keeps_the_team_score():
    """They used to be scored as if every teammate gave them $0 — the maximum
    deduction — and their zero dragged the team average down for everyone."""
    teams = grd.compute(_rows({"Ann": {"Bob": 33, "Cid": 33, "Dee": 34}}),
                        grd.GradeSettings())
    by_name = {m.name: m for t in teams for m in t.members}
    ann = by_name["Ann"]
    assert ann.no_peer_input is True
    assert ann.multiplier == 1.0 and ann.signed_pct == 0.0
    assert ann.individual_score == 100.0
    assert ann.performance == grd.NO_DATA
    assert ann.pay_grade != ann.pay_grade          # nan: no share to report
    # And Ann is not in the average the others are measured against.
    assert all(0.9 <= by_name[n].pay_grade <= 1.1 for n in ("Bob", "Cid", "Dee"))


def test_the_instructor_is_told_who_was_never_evaluated():
    df = _rows({"Ann": {"Bob": 33, "Cid": 33, "Dee": 34}})
    teams = grd.compute(df, grd.GradeSettings())
    warnings = grd.data_warnings(df, teams)
    assert any("No teammate evaluated" in w and "Ann" in w for w in warnings)


def test_a_student_on_two_teams_is_reported_not_silently_merged():
    import pandas as pd
    df = pd.concat([_rows({"Ann": {"Bob": 50, "Cid": 50}}, team="T1"),
                    _rows({"Ann": {"Eve": 100}}, team="T2")], ignore_index=True)
    assert any("more than one team" in w for w in grd.data_warnings(df, None))


def test_names_missing_from_the_contact_list_are_reported():
    df = _rows({"Ann Apple": {"Bob Brown": 50, "Ann Aple": 50}})
    warnings = grd.data_warnings(df, None,
                                 roster_names=["Ann Apple", "Bob Brown"])
    assert any("Ann Aple" in w for w in warnings)


def test_wholly_different_name_formats_are_reported_as_that():
    """Not as a list of forty typos, which would bury the actual problem."""
    df = _rows({"Ann": {"Bob": 50, "Cid": 50}})
    warnings = grd.data_warnings(df, None, roster_names=["Ann Apple", "Bob Brown",
                                                         "Cid Cole"])
    assert any("writing names differently" in w for w in warnings)


# ---- grading: rounding decides the grade --------------------------------- #

def test_the_rounded_adjustment_is_the_one_applied():
    """A student used to be told "+5%" while the gradebook held 104.44."""
    df = _rows({"Ann": {"Bob": 34, "Cid": 33, "Dee": 33},
                "Bob": {"Ann": 40, "Cid": 30, "Dee": 30},
                "Cid": {"Ann": 38, "Bob": 34, "Dee": 28},
                "Dee": {"Ann": 36, "Bob": 34, "Cid": 30}})
    for step in (1, 5):
        teams = grd.compute(df, grd.GradeSettings(rounding_step=step))
        for t in teams:
            for m in t.members:
                assert abs(m.individual_score - (m.team_score + m.signed_pct)) < 1e-9
                assert abs(m.signed_pct % step) < 1e-9
                # multiplier times team score has to land on the same number
                assert abs(m.multiplier * m.team_score - m.individual_score) < 1e-6


def test_a_zero_adjustment_never_renders_as_minus_zero():
    """`-0.0` passed the `>= 0` test, so the label read "+-0%"."""
    teams = grd.compute(_rows({"Ann": {"Bob": 50, "Cid": 50},
                               "Bob": {"Ann": 50, "Cid": 50},
                               "Cid": {"Ann": 50, "Bob": 50}}),
                        grd.GradeSettings())
    frame = grd.results_to_frame(teams)
    assert not any(str(v).startswith("+-") for v in frame["Grade Δ"])


def test_the_adjustment_cap_still_holds_after_rounding():
    """Rounding happens before the cap is re-applied, so a +4.9% that rounds to
    +5% is fine, but nothing may exceed what the instructor set."""
    df = _rows({"Ann": {"Bob": 90, "Cid": 5, "Dee": 5},
                "Bob": {"Ann": 90, "Cid": 5, "Dee": 5},
                "Cid": {"Ann": 90, "Bob": 5, "Dee": 5},
                "Dee": {"Ann": 90, "Bob": 5, "Cid": 5}},
               comment="{ee} did their part of the project to a reasonable standard.")
    teams = grd.compute(df, grd.GradeSettings(rounding_step=5, sensitivity_B=1.5))
    for t in teams:
        for m in t.members:
            assert -50.0 <= m.signed_pct <= 5.0


# ---- grading: the leniency correction stays on the same scale ------------- #

def test_the_leniency_correction_is_a_no_op_when_raters_agree():
    df = _rows({"Ann": {"Bob": 40, "Cid": 30, "Dee": 30},
                "Bob": {"Ann": 40, "Cid": 30, "Dee": 30},
                "Cid": {"Ann": 40, "Bob": 30, "Dee": 30},
                "Dee": {"Ann": 40, "Bob": 30, "Cid": 30}})
    off = grd.compute(df, grd.GradeSettings(normalize_raters=False))
    on = grd.compute(df, grd.GradeSettings(normalize_raters=True))
    for ta, tb in zip(off, on):
        for ma, mb in zip(ta.members, tb.members):
            assert abs(ma.individual_score - mb.individual_score) < 1e-6


def test_the_leniency_correction_keeps_adjustments_inside_the_caps():
    """It used to feed a standard score where a ratio was expected, which put
    the adjustment on a different scale: one extreme rater could swing a
    teammate from +0% to -35% and reorder the team."""
    df = _rows({"Ann": {"Bob": 34, "Cid": 33, "Dee": 33},
                "Bob": {"Ann": 34, "Cid": 33, "Dee": 33},
                "Cid": {"Ann": 60, "Bob": 20, "Dee": 20},
                "Dee": {"Ann": 34, "Bob": 33, "Cid": 33}})
    teams = grd.compute(df, grd.GradeSettings(normalize_raters=True))
    for t in teams:
        for m in t.members:
            assert -50.0 <= m.signed_pct <= 5.0


# ---- grading: bad input that used to crash or mislead --------------------- #

def test_an_allocation_column_that_did_not_parse_does_not_crash():
    """A renamed Qualtrics question left every cell unparseable; the column
    stayed object dtype and float(None) took the results tab down."""
    df = _rows({"Ann": {"Bob": 33, "Cid": 33}})
    df["points"] = [None, None]
    teams = grd.compute(df, grd.GradeSettings())
    assert teams and all(m.no_peer_input for t in teams for m in t.members)


def test_the_grade_bands_are_evenly_spaced():
    """0.78 for B- made that band twice as wide as every other and B a sliver."""
    assert grd.letter_grade(0.714) == "B"
    assert grd.letter_grade(0.70) == "B-"
    assert grd.letter_grade(0.79) == "B+"
    thresholds = [thr for thr, _ in grd.GRADE_SCALE if 0.36 <= thr <= 0.78]
    gaps = {round(b - a, 2) for a, b in zip(thresholds, thresholds[1:])}
    assert gaps == {0.07}


def test_a_confidential_note_is_filed_under_its_author():
    """One free-text box per submission, parked on the first teammate's row by
    both intake paths — so it used to read as evidence *about* that teammate."""
    df = _rows({"Ann": {"Bob": 50, "Cid": 50}})
    df.loc[0, "confidential_comment"] = "I did most of the work myself."
    df.loc[1, "confidential_comment"] = ""
    teams = grd.compute(df, grd.GradeSettings())
    by_name = {m.name: m for t in teams for m in t.members}
    assert by_name["Ann"].confidential_comments == ["I did most of the work myself."]
    assert by_name["Bob"].confidential_comments == []


# ---- the survey form: the $100 goes to teammates ------------------------- #

def test_the_hundred_dollars_is_required_across_teammates_only():
    """The form collected a self-allocation, required it in the $100, and then
    threw it away — so withholding dollars lowered every teammate for free."""
    answers = {"0": {"alloc": 0}, "1": {"alloc": 50}, "2": {"alloc": 50}}
    cfg = {"ask_ratings": False, "ask_ranking": False, "ask_allocation": True,
           "points_total": 100}
    assert svy.submission_problems(answers, cfg, 3, self_pos=0) == []
    # Keeping $40 back no longer balances: the teammates must still total 100.
    answers = {"0": {"alloc": 40}, "1": {"alloc": 30}, "2": {"alloc": 30}}
    problems = svy.submission_problems(answers, cfg, 3, self_pos=0)
    assert problems and "100" in problems[0]


# ---- persistence ---------------------------------------------------------- #

def test_a_delivery_override_changes_the_fingerprint():
    """The one control that withholds an abusive comment from a student was
    outside the autosave guard, so it was never written and was gone by the
    next sign-in."""
    d = fai.Draft(key="a", name="A", team="1", strengths="Text.")
    before = fai.fingerprint({"a": d})
    d.delivery = "summary"
    assert fai.fingerprint({"a": d}) != before


def test_clearing_a_draft_means_no_narrative_not_the_original():
    """An empty `edited` already meant "as generated", so clearing the box and
    approving shipped the model's text — the opposite of what clearing says."""
    d = fai.Draft(key="a", name="A", team="1", strengths="Generated text.")
    d.approved = True
    assert fai.approved_narratives({"a": d}) == {"a": "Generated text."}
    d.cleared = True
    assert d.text() == ""
    assert fai.approved_narratives({"a": d}) == {}
    # And it survives the vault round trip.
    assert fai.Draft.from_dict(d.to_dict()).cleared is True


def test_an_unreadable_saved_session_is_reported_not_called_absent():
    """Decryption happens inside get_bytes, so a changed key looked like "never
    saved" — and the next autosave then overwrote the only copy."""
    class _V:
        def list(self): return [wsp.workspace_key("chris")]
        def get_bytes(self, name): raise RuntimeError("Could not decrypt")
        def put_bytes(self, name, data): raise AssertionError("must not write")
    state, err = wsp.load(_V(), "chris")
    assert state is None
    assert "could not be opened" in err and "encryption key" in err


def test_nothing_stored_still_reports_nothing_at_all():
    class _V:
        def list(self): return []
        def get_bytes(self, name): raise RuntimeError("not found")
    assert wsp.load(_V(), "chris") == (None, "")


def test_grading_settings_survive_a_save_and_load():
    """A resumed session used to regrade at the defaults, silently."""
    import dataclasses
    import pandas as pd

    class _V:
        def __init__(self): self.store = {}
        def put_bytes(self, name, data): self.store[name] = data
        def get_bytes(self, name): return self.store[name]
        def list(self): return list(self.store)

    v = _V()
    df = pd.DataFrame([{"evaluator_key": "a", "evaluatee_key": "b", "points": 50}])
    s = grd.GradeSettings(sensitivity_B=0.8, rounding_step=1, normalize_raters=True)
    ok, err = wsp.save(v, "chris", long_df=df, settings=dataclasses.asdict(s))
    assert ok, err
    state, err = wsp.load(v, "chris")
    assert not err
    assert grd.GradeSettings(**state["settings"]) == s


# ---- provider errors ----------------------------------------------------- #

def test_a_token_count_containing_402_is_not_a_billing_failure():
    """"you requested 14025 tokens" contains "402", and a bare substring match
    aborted the batch telling the instructor to buy credits they already had."""
    exc = RuntimeError("maximum context length is 8192 tokens, however you "
                       "requested 14025 tokens")
    assert not _is_billing(exc)
    exc401 = RuntimeError("requested 14012 tokens")
    assert not _is_auth(exc401)


def test_a_real_402_is_still_a_billing_failure():
    assert _is_billing(RuntimeError("Error code: 402 - requires more credits"))
    assert _is_billing(RuntimeError("insufficient credits"))


def test_a_structured_status_code_beats_the_prose():
    class _Err(Exception):
        status_code = 400
    assert not _is_billing(_Err("you requested 14025 tokens"))

    class _Billing(Exception):
        status_code = 402
    assert _is_billing(_Billing("no detail at all"))


def test_a_spent_daily_allowance_is_its_own_global_failure():
    """It arrives as a 429, indistinguishable from the per-minute rate limit
    that *is* worth retrying — so it was retried four times per student: 160
    doomed requests for a 40-student batch."""
    exc = RuntimeError("Rate limit exceeded: free-models-per-day")
    assert _is_quota(exc)
    assert not _is_quota(RuntimeError("Rate limit exceeded: 20 requests per minute"))
    assert issubclass(QuotaLLMError, LLMError)


def test_the_batch_stops_on_a_spent_allowance_and_keeps_what_finished(
        student, settings):
    from peerparley.grading import TeamResult
    import copy
    second = copy.deepcopy(student)
    second.name, second.key = "Sam Vega", "sam-vega"
    teams = [TeamResult(team="B", members=[student, second], team_score=100.0)]
    calls = {"n": 0}

    class _Client(FakeClient):
        def complete_json(self, system, user, max_tokens=None):
            calls["n"] += 1
            if calls["n"] > 1:
                raise QuotaLLMError("no free requests left today")
            return super().complete_json(system, user, max_tokens)

    client = _Client(GOOD_PAYLOAD)
    with pytest.raises(fai.BatchAborted) as caught:
        fai.generate_for_teams(teams, settings, client=client)
    assert len(caught.value.drafts) == 1          # the first student is kept
    assert calls["n"] == 2                        # it stopped at the failure


# ---- the citation check -------------------------------------------------- #

def test_a_real_quote_with_invented_advice_bolted_on_is_flagged_high():
    """Containment in the wrong direction scored it a perfect 1.00: a real
    comment wearing an invented recommendation passed every check."""
    pool = ["Always on time and well prepared for every meeting."]
    draft = fai.Draft(key="a", name="A", team="1", strengths="Prompt.")
    draft.strength_points = [fai.Point(
        point="You were prompt.",
        source=("Always on time and well prepared for every meeting, and she told "
                "us we should set up a shared Gantt chart for the next project."),
        raised_by=1)]
    src = fai.FeedbackSource(key="a", name="A", team="1", valued=pool)
    fai.check_quotes(draft, src)
    assert draft.high_severity
    assert any("adds words to it" in f.problem for f in draft.flags)


def test_a_terse_whole_comment_counts_as_a_real_citation():
    """"On time" is somebody's entire comment in plenty of sections; calling a
    verbatim copy of it fabricated teaches the instructor to ignore the flags."""
    pool = ["On time", "Quiet in meetings"]
    draft = fai.Draft(key="a", name="A", team="1", strengths="Prompt.")
    draft.strength_points = [fai.Point(point="You were prompt.",
                                       source="On time", raised_by=1)]
    src = fai.FeedbackSource(key="a", name="A", team="1", valued=pool)
    fai.check_quotes(draft, src)
    assert draft.strength_points[0].quote_ok
    assert not draft.flags


def test_a_draft_with_no_sourced_points_is_not_clean():
    """Paragraphs with no points gave the citation check nothing to check, so
    it raised no flags and scored 1.0 — and read as "Grounded"."""
    d = fai.Draft(key="a", name="A", team="1",
                  strengths="You were a reliable teammate all semester.")
    d.quotes_checked = True
    d.score = 1.0
    assert not d.clean


def test_a_failed_draft_is_still_screened_for_abusive_language(student, settings):
    """The student's own report falls back to the raw comments, so those reach
    them either way — and a comment needing an instructor's eyes needs them
    most when there is no AI output to look at."""
    student.public_comments = ["He is an idiot and should not be in this programme."]
    student.contributions = list(student.public_comments)
    src = fai.build_source(student)
    draft = fai._failed(src, settings, "the request timed out")
    assert draft.concerns
    assert fai.batch_stats({"a": draft})["flagged_language"] == 1
