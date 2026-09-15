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
    payload = dict(GOOD_PAYLOAD)
    payload["strength_points"] = [
        {"point": "You were helpful.", "source": "good", "raised_by": 1}]
    payload["focus_points"] = []
    client = FakeClient(payload)
    draft = fai.generate_draft(client, fai.build_source(student), settings)
    assert draft.high_severity


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


def test_unsalvageable_truncation_fails_cleanly(student, settings):
    client = FakeClient(TruncatedResponseError("cut off", raw="{"))
    draft = fai.generate_draft(client, fai.build_source(student), settings)
    assert not draft.ok and draft.truncated


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
