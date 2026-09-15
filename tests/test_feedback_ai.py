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
