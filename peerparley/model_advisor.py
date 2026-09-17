"""Deciding whether a model can do this job — before paying to find out.

Selecting a model and discovering forty requests later that it cannot hold a
JSON shape, or that its output ceiling is below what a narrative needs, is a
waste of the instructor's time and of tokens. Enough is knowable in advance to
stop most of it.

Two kinds of knowledge, kept deliberately separate, because conflating them
would manufacture confidence that does not exist:

**Capability** — hard facts from OpenRouter's own catalog. Does the model
support structured outputs? What is its maximum completion length? Is it free,
and therefore rate-limited? These produce a *verdict*: won't work, risky, or
suitable. A verdict is a statement about the model's declared abilities, and it
is honest because every input is published fact.

**Track record** — what this app has actually observed. Attempts, usable drafts,
empty replies, truncations, failures, per model, accumulated across runs. This
produces a *rate*, always reported with its sample size, and only once the
sample is large enough to mean anything. One success out of one attempt is not
a 100% success rate, and presenting it as one would be the single most
misleading thing this module could do.

What this module deliberately does **not** do is emit a single blended
"probability of success". That number would look authoritative while resting on
a made-up weighting between two incommensurable things. An instructor is better
served by "supports JSON, 32k output ceiling, and 9 of 10 drafts landed here
last time" than by "87%".

The caveat OpenRouter documents itself, and which this module honours: a model's
``supported_parameters`` is the union across every provider serving it, and only
some providers may honour structured outputs. So JSON support raises confidence
rather than guaranteeing anything — which is exactly why the generator still
asks for JSON in the prompt and salvages the reply either way.
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .openrouter_catalog import ORModel

# Below this many recorded attempts, a success rate says more about luck than
# about the model, so it is reported as a count instead of a percentage.
MIN_SAMPLE = 5

# A narrative plus its sourced points needs real headroom. Models whose
# published completion ceiling is under this will truncate on this task, which
# is exactly the failure that cost 7233 characters in a live run.
MIN_USEFUL_OUTPUT_TOKENS = 2000

# The prompt carries a student's comments and the instructions — small. Anything
# modern clears this; it is here to exclude the genuinely tiny.
MIN_USEFUL_CONTEXT = 8000

# How observed performance moves a model's ranking. The baseline is the rate a
# competent model is expected to hit on this task; above it a model gains,
# below it a model loses. The weight is large enough that a demonstrated
# failure outranks nothing — evidence should beat metadata.
OBSERVED_BASELINE = 0.75
OBSERVED_WEIGHT = 6.0

STATS_KEY = "aimodelstats.json"


# --------------------------------------------------------------------------- #
# Track record
# --------------------------------------------------------------------------- #

@dataclass
class ModelRecord:
    """What happened the last times this model was asked to do this job."""

    model: str
    attempts: int = 0
    written: int = 0        # produced a usable narrative
    empty: int = 0          # succeeded as a request, returned nothing
    truncated: int = 0      # had to be recovered from a cut-off reply
    failed: int = 0         # errored
    last_used: str = ""

    @property
    def rate(self) -> Optional[float]:
        """Fraction of attempts that produced a usable draft, or None.

        None when the sample is too small to report — the caller shows the raw
        counts instead of a percentage it would be wrong to trust.
        """
        if self.attempts < MIN_SAMPLE:
            return None
        return self.written / self.attempts

    @property
    def enough_data(self) -> bool:
        return self.attempts >= MIN_SAMPLE

    def summary(self) -> str:
        if not self.attempts:
            return "not used here yet"
        if not self.enough_data:
            # Honest about the sample: counts, not a rate.
            return (f"{self.written} of {self.attempts} draft(s) landed here "
                    f"so far — too few runs to draw a rate from")
        bits = [f"{self.rate:.0%} of {self.attempts} attempts produced a draft"]
        if self.truncated:
            bits.append(f"{self.truncated} cut off")
        if self.empty:
            bits.append(f"{self.empty} came back empty")
        if self.failed:
            bits.append(f"{self.failed} errored")
        return " · ".join(bits)

    def to_dict(self) -> Dict[str, Any]:
        return {"model": self.model, "attempts": self.attempts,
                "written": self.written, "empty": self.empty,
                "truncated": self.truncated, "failed": self.failed,
                "last_used": self.last_used}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ModelRecord":
        def _i(k):
            try:
                return int(d.get(k) or 0)
            except (TypeError, ValueError):
                return 0
        return cls(model=str(d.get("model") or ""), attempts=_i("attempts"),
                   written=_i("written"), empty=_i("empty"),
                   truncated=_i("truncated"), failed=_i("failed"),
                   last_used=str(d.get("last_used") or ""))


def load_records(vault: Any) -> Dict[str, ModelRecord]:
    """Every model's history. Empty on first run, which is not an error."""
    try:
        raw = vault.get_bytes(STATS_KEY)
        payload = json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    out: Dict[str, ModelRecord] = {}
    for item in (payload or {}).get("models") or []:
        if isinstance(item, dict) and item.get("model"):
            rec = ModelRecord.from_dict(item)
            out[rec.model] = rec
    return out


def save_records(vault: Any, records: Dict[str, ModelRecord]) -> Tuple[bool, str]:
    try:
        vault.put_bytes(STATS_KEY, json.dumps(
            {"version": 1,
             "models": [r.to_dict() for r in records.values()]},
            default=str).encode("utf-8"))
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def record_batch(vault: Any, model: str, drafts: Sequence[Any]) -> None:
    """Fold one batch's outcomes into the track record. Best-effort.

    Called after a run so the next selection is informed by this one. A failure
    to record must never surface: the instructor's drafts are the point, and
    statistics are a convenience built on top of them.
    """
    if not model or not drafts:
        return
    try:
        records = load_records(vault)
        rec = records.get(model) or ModelRecord(model=model)
        for d in drafts:
            rec.attempts += 1
            if not getattr(d, "ok", False):
                rec.failed += 1
                continue
            if getattr(d, "empty", False):
                rec.empty += 1
                continue
            if getattr(d, "insufficient_evidence", ""):
                # Not the model's doing — too few comments to write from. It
                # neither counts for nor against it, so back the attempt out.
                rec.attempts -= 1
                continue
            rec.written += 1
            if getattr(d, "truncated", False):
                rec.truncated += 1
        rec.last_used = dt.datetime.now().isoformat(timespec="seconds")
        records[model] = rec
        save_records(vault, records)
    except Exception:  # noqa: BLE001
        return


# --------------------------------------------------------------------------- #
# Capability verdicts
# --------------------------------------------------------------------------- #

@dataclass
class Assessment:
    """Whether a model can do this job, and why."""

    model: str
    verdict: str                      # "suitable" | "risky" | "unusable"
    reasons: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    score: float = 0.0
    record: Optional[ModelRecord] = None

    @property
    def icon(self) -> str:
        return {"suitable": "✅", "risky": "⚠️", "unusable": "🚫"}.get(
            self.verdict, "•")

    @property
    def usable(self) -> bool:
        return self.verdict != "unusable"

    def headline(self) -> str:
        return {"suitable": "Should handle this",
                "risky": "Might struggle",
                "unusable": "Cannot do this job"}.get(self.verdict, self.verdict)


def assess(model: ORModel, *, reply_tokens: int = 4000,
           record: Optional[ModelRecord] = None) -> Assessment:
    """Judge one model against what this task actually needs.

    The task: read a handful of short comments and return a small JSON object
    holding a few paragraphs and their sourced points. Undemanding in context,
    genuinely demanding in format discipline.
    """
    reasons: List[str] = []
    warnings: List[str] = []
    score = 0.0
    verdict = "suitable"

    # ---- output ceiling: the failure that actually bit ------------------
    cap = model.max_completion_tokens
    if cap and cap < MIN_USEFUL_OUTPUT_TOKENS:
        verdict = "unusable"
        reasons.append(
            f"its replies are capped at {cap:,} tokens, below the "
            f"{MIN_USEFUL_OUTPUT_TOKENS:,} a narrative needs — it will truncate"
        )
    elif cap and cap < reply_tokens:
        verdict = "risky"
        warnings.append(
            f"its {cap:,}-token reply ceiling is under your {reply_tokens:,} "
            "setting, so drafts may be cut off"
        )
        score += 1.0
    elif cap:
        reasons.append(f"{cap:,}-token reply ceiling")
        score += 2.0
    else:
        # Not published. Common, and not a mark against the model.
        score += 1.0

    # ---- JSON discipline ------------------------------------------------
    if model.supports_json:
        reasons.append("supports structured outputs")
        score += 3.0
    else:
        if verdict == "suitable":
            verdict = "risky"
        warnings.append(
            "does not advertise structured outputs, so its JSON has to be "
            "salvaged from prose — the commonest cause of an empty draft"
        )

    # ---- context --------------------------------------------------------
    if model.context_length and model.context_length < MIN_USEFUL_CONTEXT:
        verdict = "unusable"
        reasons.append(f"only {model.context_length:,} tokens of context")
    elif model.context_length >= 32000:
        score += 1.0

    # ---- price and rate limits ------------------------------------------
    if model.is_free:
        warnings.append(
            "free, so it is rate-limited and counts against the daily "
            "allowance"
        )
    elif model.has_known_price:
        reasons.append(model.price_label)

    # ---- observed here: the only real evidence --------------------------
    # Scored against a baseline rather than added to, so that evidence can
    # count *against* a model. Adding rate * weight made a model with a 5%
    # success rate outrank an untried one, which is precisely backwards: a
    # demonstrated failure is worse news than no news.
    if record and record.attempts:
        rate = record.rate
        if rate is not None:
            score += (rate - OBSERVED_BASELINE) * OBSERVED_WEIGHT
            if rate < 0.5:
                verdict = "risky" if verdict == "suitable" else verdict
                warnings.append(
                    f"a poor record on this task here: {record.summary()}")
            else:
                reasons.append(f"proven here: {record.summary()}")
        else:
            reasons.append(record.summary())

    return Assessment(model=model.id, verdict=verdict, reasons=reasons,
                      warnings=warnings, score=score, record=record)


def assess_all(models: Sequence[ORModel], *, reply_tokens: int = 4000,
               records: Optional[Dict[str, ModelRecord]] = None
               ) -> Dict[str, Assessment]:
    records = records or {}
    return {m.id: assess(m, reply_tokens=reply_tokens,
                         record=records.get(m.id)) for m in models}


# --------------------------------------------------------------------------- #
# Recommendations
# --------------------------------------------------------------------------- #

def recommend(models: Sequence[ORModel], *, reply_tokens: int = 4000,
              records: Optional[Dict[str, ModelRecord]] = None
              ) -> Dict[str, Optional[Assessment]]:
    """Best free and best paid candidate for this task.

    Two answers rather than one, because the choice between them is the
    instructor's budget decision and not a technical one. Within each bucket the
    ranking is the score above: declared capability, plus observed performance
    once there is enough of it to count.

    Ties break toward the cheaper model, so a recommendation never costs more
    than it needs to.
    """
    graded = assess_all(models, reply_tokens=reply_tokens, records=records)
    by_id = {m.id: m for m in models}

    def _best(free: bool) -> Optional[Assessment]:
        pool = [
            a for mid, a in graded.items()
            if a.usable and by_id[mid].is_free is free
            # The free router picks a different model per call, so it cannot be
            # assessed as a model — and recommending it would recommend a
            # lottery. It stays selectable; it is just never the advice.
            and mid != "openrouter/free"
        ]
        if not pool:
            return None
        return sorted(
            pool,
            key=lambda a: (-a.score,
                           by_id[a.model].completion_per_m,
                           a.model.lower()),
        )[0]

    return {"free": _best(True), "paid": _best(False)}


def usable_only(models: Sequence[ORModel], *, reply_tokens: int = 4000,
                records: Optional[Dict[str, ModelRecord]] = None
                ) -> List[ORModel]:
    """The models worth offering, with the hopeless ones removed.

    Kept as a filter the instructor switches on rather than a permanent
    restriction: "unusable" rests on published metadata, which is occasionally
    wrong or missing, and silently hiding a model the instructor asked for would
    be its own kind of failure.
    """
    graded = assess_all(models, reply_tokens=reply_tokens, records=records)
    return [m for m in models
            if m.id == "openrouter/free" or graded[m.id].usable]
