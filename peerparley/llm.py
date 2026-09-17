"""Provider-agnostic LLM access.

Ported from the TransQ lecture-quiz app. One interface, three code paths, six
vendors: everything above this file asks for "JSON matching this shape" and does
not care who produced it, so switching providers is a dropdown rather than a
refactor.

* Gemini    — native ``google-genai`` SDK
* Claude    — native ``anthropic`` SDK
* OpenAI / xAI / OpenRouter / local — the ``openai`` SDK with a different
  ``base_url``

Nothing here knows about peer evaluation. The feedback-specific prompting and
the grounding check live in ``ai_prompts.py`` and ``feedback_ai.py``.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .aiconfig import Provider, get_provider

try:
    from tenacity import (
        retry,
        retry_if_exception_type,
        stop_after_attempt,
        wait_exponential,
    )
    _HAS_TENACITY = True
except Exception:  # pragma: no cover - tenacity is a listed dependency
    _HAS_TENACITY = False


class LLMError(RuntimeError):
    pass


class TransientLLMError(LLMError):
    """Rate limits, overloads, timeouts — worth retrying."""


class AuthLLMError(LLMError):
    """The provider rejected the credential.

    Its own type because it is the one failure that is *global* rather than
    per-request. A rate limit on student fourteen says nothing about student
    fifteen; a rejected API key says everything about all of them. Without this
    distinction a batch of forty students produces forty identical 401s, which
    bills nothing but wastes the instructor's time and buries the one fact that
    matters under thirty-nine copies of itself.

    Callers abort the batch on this rather than continuing.
    """


class TruncatedResponseError(LLMError):
    """The model stopped mid-reply because it hit its output limit.

    Its own type because it is the one failure with a specific, actionable fix
    (ask for less at a time), and because retrying it unchanged just bills again
    for the same truncated answer.

    Carries ``raw``: the text received before the cut. A reply truncated partway
    through a narrative still contains complete sentences that were paid for,
    and discarding them turns a partial success into a total loss.
    """

    def __init__(self, message: str, raw: str = ""):
        super().__init__(message)
        self.raw = raw or ""


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.calls += other.calls


# USD per million tokens (input, output). Used only for the on-screen estimate,
# never for billing. OpenRouter entries are approximate — it routes to whichever
# upstream host is cheapest or fastest at the moment, so the real rate moves.
# Any model not listed here shows tokens but no dollar figure, which is the
# honest answer rather than a fabricated one.
PRICING: Dict[str, Tuple[float, float]] = {
    # The Free Models Router only ever routes to free models, so this is an
    # exact zero rather than an estimate — and stating it is what makes the
    # meter read "$0.00" instead of "no published price".
    "openrouter/free": (0.0, 0.0),
    "anthropic/claude-sonnet-4.5": (3.0, 15.0),
    "openai/gpt-4.1": (2.0, 8.0),
    "google/gemini-3.8-flash": (0.75, 3.75),
    "deepseek/deepseek-v4-flash": (0.07, 0.17),
    "deepseek/deepseek-v3.2": (0.21, 0.31),
    "x-ai/grok-4.6": (2.0, 6.0),
    # Anthropic
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-opus-4-1": (15.0, 75.0),
    "claude-haiku-4-5": (1.0, 5.0),
    # OpenAI
    "gpt-4.1": (2.0, 8.0),
    "gpt-4.1-mini": (0.4, 1.6),
    "gpt-4o": (2.5, 10.0),
    # Google
    "gemini-3.8-flash": (0.75, 3.75),
    "gemini-3.5-flash": (1.50, 9.00),
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-2.5-pro": (1.25, 10.00),
    "gemini-2.5-flash": (0.30, 2.50),
    # xAI
    "grok-4.6": (2.0, 6.0),
    "grok-4": (3.0, 15.0),
}

# A local model on a laptop can spend minutes on one request where a hosted
# model takes seconds. Ten minutes is generous enough that a slow machine
# finishes, short enough that a hung server does not hold the run open all
# afternoon.
LOCAL_TIMEOUT_SECONDS = 600.0


# Distinctive key prefixes, used only to catch a key pasted into the wrong
# provider — a slip that produces a bare 401 and no hint as to why.
_KEY_PREFIXES = (
    ("sk-or-", "openrouter"),
    ("sk-ant-", "anthropic"),
    ("xai-", "xai"),
    ("AIza", "gemini"),
    ("sk-proj-", "openai"),
    ("sk-svcacct-", "openai"),
)


def key_provider_hint(api_key: str) -> Optional[str]:
    """Which provider a key's prefix suggests, or None when it is ambiguous.

    Deliberately conservative. A bare ``sk-`` could be OpenAI or an older
    OpenRouter key, so it returns None rather than guessing and sending someone
    to change a setting that was already right.
    """
    key = (api_key or "").strip()
    if not key:
        return None
    for prefix, provider in _KEY_PREFIXES:
        if key.startswith(prefix):
            return provider
    return None


def register_pricing(rates: Dict[str, Tuple[float, float]]) -> None:
    """Merge live prices (OpenRouter's catalog) into the table above.

    Live figures beat the static ones, which are only a starting point for
    providers with no price API. Called by the OpenRouter picker on every run,
    so the cost meter quotes what OpenRouter is charging today rather than what
    it charged when this file was written.
    """
    PRICING.update(rates)


def estimate_cost(model: str, usage: Usage) -> float:
    """Best-effort USD estimate. Returns 0.0 for models with no listed price."""
    inp, out = PRICING.get(model, (0.0, 0.0))
    return (usage.input_tokens / 1e6) * inp + (usage.output_tokens / 1e6) * out


def has_pricing(model: str) -> bool:
    return model in PRICING


@dataclass
class LLMClient:
    """Thin wrapper over whichever vendor SDK the selected provider needs."""

    provider: str
    model: str
    api_key: str = ""
    temperature: float = 0.2
    max_tokens: int = 1600
    usage: Usage = field(default_factory=Usage)
    app_name: str = "PeerParley"
    app_url: str = "https://github.com/"
    # Fired after every request with (input_tokens, output_tokens, rate-or-None)
    # so a live meter can count up during a batch instead of only at the end.
    on_usage: Optional[Callable[[int, int, Optional[Tuple[float, float]]], None]] = None
    # Overrides the provider's built-in address. Only local servers need this.
    base_url_override: str = ""

    def __post_init__(self) -> None:
        self.spec: Provider = get_provider(self.provider)
        if self.spec.requires_key and not self.api_key:
            raise LLMError(
                f"No API key for {self.spec.label}. Add it in the sidebar, or set "
                f"{self.spec.env_var} in the app's secrets. "
                f"Get one at {self.spec.console_url}"
            )
        self._client = self._build_client()

    # ------------------------------------------------------------------ #
    # Client construction
    # ------------------------------------------------------------------ #

    def _build_client(self) -> Any:
        if self.spec.sdk == "gemini":
            try:
                from google import genai
            except ImportError as exc:  # pragma: no cover
                raise LLMError(
                    "Google Gemini needs the google-genai package: "
                    "pip install google-genai"
                ) from exc
            return genai.Client(api_key=self.api_key)

        if self.spec.sdk == "anthropic":
            try:
                from anthropic import Anthropic
            except ImportError as exc:  # pragma: no cover
                raise LLMError(
                    "Anthropic Claude needs the anthropic package: "
                    "pip install anthropic"
                ) from exc
            return Anthropic(api_key=self.api_key)

        if self.spec.sdk == "openai":
            try:
                from openai import OpenAI
            except ImportError as exc:  # pragma: no cover
                raise LLMError(
                    f"{self.spec.label} needs the openai package: pip install openai"
                ) from exc
            kwargs: Dict[str, Any] = {"api_key": self.api_key or "not-needed"}
            if self.spec.base_url:
                kwargs["base_url"] = self.spec.base_url
            if self.base_url_override:
                kwargs["base_url"] = self.base_url_override
            if self.spec.is_local:
                # The SDK's default timeout would abandon a local request that
                # was going to succeed. Retries drop to one because a local
                # server that failed is not a busy server that will recover —
                # it is a machine out of memory, and asking again just makes
                # the instructor wait through the same failure.
                kwargs["timeout"] = LOCAL_TIMEOUT_SECONDS
                kwargs["max_retries"] = 0
            if self.spec.key == "openrouter":
                kwargs["default_headers"] = {
                    "HTTP-Referer": self.app_url,
                    "X-Title": self.app_name,
                }
            return OpenAI(**kwargs)

        raise LLMError(f"Unknown provider: {self.provider}")

    # ------------------------------------------------------------------ #
    # Completion
    # ------------------------------------------------------------------ #

    def _account(self, input_tokens: int, output_tokens: int) -> None:
        """Record one call's tokens and notify any live meter watching."""
        self.usage.add(Usage(input_tokens, output_tokens, 1))
        if self.on_usage is not None:
            # A model on your own machine bills nothing, whatever it is called.
            rate = (0.0, 0.0) if self.spec.is_local else PRICING.get(self.model)
            self.on_usage(input_tokens, output_tokens, rate)

    def _complete_once(
        self, system: str, user: str, max_tokens: Optional[int], json_mode: bool
    ) -> str:
        tokens = max_tokens or self.max_tokens
        try:
            if self.spec.sdk == "gemini":
                return self._complete_gemini(system, user, tokens, json_mode)
            if self.spec.sdk == "anthropic":
                return self._complete_anthropic(system, user, tokens)
            return self._complete_openai(system, user, tokens, json_mode)
        except (LLMError, TransientLLMError):
            # Includes TruncatedResponseError: retrying it unchanged produces
            # the same truncated reply and bills for it again.
            raise
        except Exception as exc:
            # Auth is checked first: some providers return 401 with wording that
            # also matches a transient marker ("connection"), and retrying a
            # rejected key four times with backoff just makes the failure slow.
            if _is_auth(exc):
                raise AuthLLMError(self._auth_help(exc)) from exc
            if _is_transient(exc):
                raise TransientLLMError(str(exc)) from exc
            raise LLMError(f"{self.spec.label} request failed: {exc}") from exc

    def _auth_help(self, exc: Exception) -> str:
        """Turn a provider's auth rejection into something actionable."""
        spec = self.spec
        detail = str(exc).strip()
        lines = [f"{spec.label} rejected the API key."]

        if "user not found" in detail.lower():
            lines.append(
                "OpenRouter answers \"User not found\" when it does not "
                "recognise the key at all — the key was deleted, the account "
                "was removed, or what was pasted is not an OpenRouter key."
            )

        mismatch = key_provider_hint(self.api_key)
        if mismatch and mismatch != spec.key:
            other = get_provider(mismatch)
            lines.append(
                f"This key's prefix belongs to {other.label}, not to "
                f"{spec.label}. Either switch the provider to {other.label}, "
                f"or paste a key from {spec.label}."
            )
        elif self.api_key and self.api_key != self.api_key.strip():
            lines.append(
                "The key has leading or trailing whitespace, which some "
                "providers reject — check for a stray space or newline."
            )

        if spec.key == "openrouter":
            lines.append(
                "Note that the free model router still needs a valid "
                "OpenRouter key: the models cost nothing, but the account is "
                "still what authenticates the request."
            )
        if spec.console_url:
            lines.append(f"Check or create a key at {spec.console_url}")
        return " ".join(lines)

    def complete(
        self,
        system: str,
        user: str,
        max_tokens: Optional[int] = None,
        json_mode: bool = False,
    ) -> str:
        """Send one prompt, return raw text. Retries transient failures."""
        if not _HAS_TENACITY:
            return self._complete_once(system, user, max_tokens, json_mode)

        @retry(
            retry=retry_if_exception_type(TransientLLMError),
            wait=wait_exponential(multiplier=2, min=2, max=30),
            stop=stop_after_attempt(4),
            reraise=True,
        )
        def _run() -> str:
            return self._complete_once(system, user, max_tokens, json_mode)

        return _run()

    def _complete_gemini(
        self, system: str, user: str, tokens: int, json_mode: bool
    ) -> str:
        from google.genai import types

        config: Dict[str, Any] = {
            "system_instruction": system,
            "temperature": self.temperature,
            # Gemini 3.x counts reasoning tokens against this budget, so give
            # it real headroom or a long batch gets truncated mid-JSON.
            "max_output_tokens": max(tokens, 4096),
        }
        if json_mode:
            config["response_mime_type"] = "application/json"

        resp = self._client.models.generate_content(
            model=self.model,
            contents=user,
            config=types.GenerateContentConfig(**config),
        )

        meta = getattr(resp, "usage_metadata", None)
        self._account(
            int(getattr(meta, "prompt_token_count", 0) or 0) if meta else 0,
            int(getattr(meta, "candidates_token_count", 0) or 0) if meta else 0,
        )

        reason = ""
        for candidate in getattr(resp, "candidates", None) or []:
            reason = str(getattr(candidate, "finish_reason", "") or "")
            break

        text = getattr(resp, "text", None)
        if "MAX_TOKENS" in reason.upper():
            raise TruncatedResponseError(
                "Gemini stopped at its output limit — the draft is incomplete. "
                "Shorten the target length, or generate fewer students per run. "
                "Gemini 3.x counts its reasoning against the output budget.",
                raw=text or "",
            )
        if not text:
            raise LLMError(
                "Gemini returned no text"
                + (f" (finish reason: {reason})." if reason else ".")
                + " If this repeats, try a different model."
            )
        return text

    def _complete_anthropic(self, system: str, user: str, tokens: int) -> str:
        resp = self._client.messages.create(
            model=self.model,
            max_tokens=tokens,
            temperature=self.temperature,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        self._account(resp.usage.input_tokens, resp.usage.output_tokens)
        text = "".join(
            block.text for block in resp.content if getattr(block, "type", "") == "text"
        )
        if str(getattr(resp, "stop_reason", "") or "").lower() == "max_tokens":
            raise TruncatedResponseError(
                "Claude stopped at its output limit — the draft is incomplete. "
                "Shorten the target length, or raise the token cap.",
                raw=text,
            )
        return text

    def _complete_openai(
        self, system: str, user: str, tokens: int, json_mode: bool
    ) -> str:
        kwargs: Dict[str, Any] = {
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if json_mode and self.spec.supports_json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        resp = self._client.chat.completions.create(**kwargs)

        usage = getattr(resp, "usage", None)
        self._account(
            int(getattr(usage, "prompt_tokens", 0) or 0) if usage else 0,
            int(getattr(usage, "completion_tokens", 0) or 0) if usage else 0,
        )

        choices = getattr(resp, "choices", None) or []
        if not choices:
            raise LLMError(f"{self.spec.label} returned no choices.")

        finish = str(getattr(choices[0], "finish_reason", "") or "").lower()
        content = choices[0].message.content or ""

        # An empty reply is not a truncated one, whatever finish_reason says.
        # OpenRouter's free router assigns a different upstream model per call
        # and some of them answer with zero characters under load; reporting
        # that as "stopped at its output limit after 0 characters" sends the
        # instructor to shorten a draft that was never written. Retrying is the
        # right move, because the next call may land on a model that answers.
        if not content.strip():
            raise TransientLLMError(
                f"{self.spec.label} returned an empty reply"
                + (f" (finish reason: {finish})" if finish else "")
                + ". On the OpenRouter free router this usually means the model "
                  "it picked was rate-limited or overloaded; retrying often "
                  "lands on a working one. Pinning a specific model is more "
                  "reliable than the free router for a whole section."
            )
        if finish == "length":
            raise TruncatedResponseError(
                f"{self.spec.label} stopped at its output limit after "
                f"{len(content)} characters — the draft is incomplete. Shorten "
                "the target length, or raise the token cap.",
                raw=content,
            )
        return content

    def complete_json(
        self, system: str, user: str, max_tokens: Optional[int] = None
    ) -> Dict[str, Any]:
        """Send one prompt and parse the reply as a JSON object.

        Models occasionally wrap JSON in prose or a code fence even when told
        not to — and on OpenRouter, whether native JSON mode is honored depends
        on the underlying model — so the response is salvaged rather than
        thrown away.
        """
        system = (
            system.rstrip()
            + "\n\nRespond with a single valid JSON object and nothing else."
        )
        raw = self.complete(system, user, max_tokens=max_tokens, json_mode=True)
        return parse_json_object(raw)


# --------------------------------------------------------------------------- #
# JSON recovery
# --------------------------------------------------------------------------- #

def _looks_truncated(text: str) -> bool:
    """Unbalanced braces mean the reply stopped early, not that it was nonsense."""
    in_string, escaped, depth = False, False, 0
    for char in text:
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
    return in_string or depth > 0


def salvage_object_fields(raw: str) -> Dict[str, Any]:
    """Recover the top-level fields of a JSON object that was cut off.

    The model writes three good fields and stops midway through the fourth;
    throwing away all four is the wrong trade. Every finished field is followed
    by a comma sitting at depth one, so close the object at one of those commas
    and try to parse, walking backwards until one succeeds. The field in
    progress at the cut is lost, which is right — half a sentence of feedback is
    worse than none.
    """
    text = (raw or "").strip()
    if not text:
        return {}

    fence = re.search(r"```(?:json)?\s*(.*)", text, re.DOTALL)
    if fence:
        text = fence.group(1)

    opening = text.find("{")
    if opening < 0:
        return {}
    text = text[opening:]

    try:
        # raw_decode rather than loads: a reply that closed its object and then
        # added a closing fence, or a sentence of commentary, is complete.
        whole, _ = json.JSONDecoder().raw_decode(text)
    except json.JSONDecodeError:
        pass
    else:
        return whole if isinstance(whole, dict) else {}

    breaks: List[int] = []
    depth, in_string, escaped = 0, False, False
    for i, char in enumerate(text):
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char in "{[":
            depth += 1
        elif char in "}]":
            depth -= 1
        elif char == "," and depth == 1:
            breaks.append(i)

    for cut in reversed(breaks):
        try:
            data = json.loads(text[:cut] + "}")
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return {}


# Every provider phrases a bad credential differently. OpenRouter in particular
# answers "User not found." for a key it does not recognise, which reads like a
# problem with the *student* rather than the key — so it is worth translating.
_AUTH_MARKERS = (
    "401", "403", "user not found", "invalid api key", "invalid_api_key",
    "incorrect api key", "no auth credentials", "unauthorized",
    "authentication", "api key not valid", "invalid authentication",
    "permission_denied", "invalid x-api-key",
)


def _is_auth(exc: Exception) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    # A 403 can also mean "region blocked" or "model not allowed", but the fix
    # in every case starts with the credential, so it lands here.
    return any(m in text for m in _AUTH_MARKERS)


def salvage_partial_strings(raw: str, keys: Sequence[str]) -> Dict[str, str]:
    """Recover named string fields from a reply cut off mid-value.

    ``salvage_object_fields`` needs a comma at depth one to close the object, so
    a reply truncated *inside the first field* yields nothing — and when that
    field is a paragraph of narrative, "nothing" can mean discarding seven
    thousand characters of usable prose that was already paid for. A model that
    rambles past its token budget is exactly the case where the most text is at
    stake.

    So this reads the raw text directly: find ``"<key>": "``, then walk forward
    to the closing quote, or to the end of what arrived. Whatever came back is
    returned as plain text with JSON escapes undone.

    Deliberately not a JSON parser. It is the last thing tried, after real
    parsing and structural salvage have both failed, and its only job is to
    rescue prose a human can read and edit.
    """
    text = (raw or "").strip()
    if not text:
        return {}

    fence = re.search(r"```(?:json)?\s*(.*)", text, re.DOTALL)
    if fence:
        text = fence.group(1)

    found: Dict[str, str] = {}
    for key in keys:
        marker = re.search(rf'"{re.escape(key)}"\s*:\s*"', text)
        if not marker:
            continue
        out: List[str] = []
        escaped = False
        for char in text[marker.end():]:
            if escaped:
                out.append({"n": "\n", "t": "\t", "r": "\r"}.get(char, char))
                escaped = False
                continue
            if char == "\\":
                escaped = True
                continue
            if char == '"':          # the value closed normally
                break
            out.append(char)
        value = "".join(out).strip()
        if value:
            found[key] = value
    return found


def readable_fragment(raw: str, min_chars: int = 40) -> str:
    """Whatever prose can be pulled from a reply that defied every parser.

    The floor of the recovery ladder. A model that answered with an essay
    instead of JSON still produced the instructor's feedback — it simply put it
    in the wrong shape, and throwing it away for that would be perverse when a
    human can read it in five seconds.
    """
    text = (raw or "").strip()
    if not text:
        return ""
    fence = re.search(r"```(?:json)?\s*(.*)", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    # Strip the JSON scaffolding a half-written object leaves behind, so what
    # is shown reads as sentences rather than as debris.
    text = re.sub(r'^\s*[\{\[]', " ", text)
    text = re.sub(r'"\s*[a-z_]+"\s*:\s*', " ", text)
    text = text.replace('\\n', "\n").replace('\\"', '"')
    text = re.sub(r'[\{\[\]"]+', " ", text)
    text = re.sub(r"[ \t]{2,}", " ", text).strip(" ,:\n\t")
    return text if len(text) >= min_chars else ""


def _is_transient(exc: Exception) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    markers = (
        "rate limit", "ratelimit", "resource_exhausted", "resource exhausted",
        "overloaded", "unavailable", "429", "500", "502", "503", "529",
        "timeout", "timed out", "connection", "temporarily",
    )
    return any(m in text for m in markers)


def parse_json_object(raw: str) -> Dict[str, Any]:
    """Pull the first JSON object out of a model response."""
    text = (raw or "").strip()
    if not text:
        raise LLMError("The model returned an empty response.")

    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            # An opening brace with no closing one is the signature of a reply
            # that ran out of room, which has a different fix from a model that
            # simply ignored the format instruction.
            if start != -1:
                raise TruncatedResponseError(
                    "The model's reply was cut off before the JSON closed "
                    f"({len(text)} characters received). Shorten the target "
                    "length, or raise the token cap.",
                    raw=text,
                )
            raise LLMError(
                f"Could not find JSON in the model response: {text[:300]}"
            )
        try:
            parsed = json.loads(text[start:end + 1])
        except json.JSONDecodeError as exc:
            if _looks_truncated(text):
                raise TruncatedResponseError(
                    "The model's reply was cut off mid-JSON. Shorten the target "
                    "length, or raise the token cap.",
                    raw=text,
                ) from exc
            raise LLMError(f"Model returned malformed JSON: {exc}") from exc

    if not isinstance(parsed, dict):
        raise LLMError("Expected a JSON object at the top level.")
    return parsed
