"""LLM provider registry and AI settings for the feedback narrator.

Ported from the TransQ lecture-quiz app, which solved the same problem: one
instructor-facing Streamlit app that must talk to whichever AI vendor the
department happens to have a key for. The registry is the whole abstraction —
``peerparley/llm.py`` reads ``Provider.sdk`` to pick a code path, so adding a
vendor is an entry here rather than a branch in the client.

Key resolution order, same as the rest of PeerParley's config: Streamlit
secrets, then environment, then whatever the instructor typed into the sidebar
for this session only. A key typed into the sidebar is never written anywhere —
it lives in ``st.session_state`` and dies with the session. That is deliberate:
PeerParley's security model is "the public host holds no plaintext secrets", and
an API key is a secret like any other.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

try:  # Streamlit is present at runtime but not in unit tests.
    import streamlit as st
except Exception:  # pragma: no cover
    st = None  # type: ignore[assignment]


@dataclass(frozen=True)
class Provider:
    """Everything needed to talk to one LLM vendor.

    ``sdk`` picks the code path in ``peerparley.llm``:

    * ``"gemini"``    — the native ``google-genai`` SDK
    * ``"anthropic"`` — the native ``anthropic`` SDK
    * ``"openai"``    — the ``openai`` SDK, pointed at ``base_url``

    OpenAI, xAI, OpenRouter and local runners all share the ``openai`` path
    because they all expose the same ``/chat/completions`` contract. Gemini has
    an OpenAI-compatible endpoint too, but its ``response_format`` handling is
    still inconsistent and this app depends on reliable JSON, so Gemini uses its
    native SDK where ``response_mime_type`` is a first-class guarantee.
    """

    key: str
    label: str
    sdk: str
    env_var: str
    models: Tuple[str, ...]
    base_url: Optional[str] = None
    supports_json_mode: bool = True
    allow_custom_model: bool = False
    console_url: str = ""
    note: str = ""
    # A model on the instructor's own machine needs no credential, and demanding
    # one would be a fake gate in front of a service that does not check it.
    requires_key: bool = True
    # True when the endpoint lives on the same machine as the app. The UI keys
    # its "Streamlit Cloud cannot reach your laptop" warning off this flag
    # rather than off the provider's name.
    is_local: bool = False


# OpenRouter's Free Models Router reads each request, filters to free models
# that can serve it, and picks one at random. It costs nothing and never goes
# stale as free models come and go. The trade-offs are real and worth stating
# because they are invisible in the price: lower rate limits, higher latency at
# peak, and a *different model per call* — so two runs over the same team's
# comments can differ in quality in a way a pinned model's do not. Fine for a
# first draft; pin a model when the wording matters.
FREE_ROUTER = "openrouter/free"

# Where a local runner listens. The port depends on which one is installed
# (11434 Ollama, 1234 LM Studio) and people do move it.
DEFAULT_LOCAL_BASE_URL = "http://localhost:11434/v1"


PROVIDERS: Dict[str, Provider] = {
    "openrouter": Provider(
        key="openrouter",
        label="OpenRouter",
        sdk="openai",
        env_var="OPENROUTER_API_KEY",
        models=(
            FREE_ROUTER,
            "anthropic/claude-sonnet-4.5",
            "openai/gpt-4.1",
            "google/gemini-3.8-flash",
            "deepseek/deepseek-v4-flash",
            "deepseek/deepseek-v3.2",
            "x-ai/grok-4.6",
        ),
        base_url="https://openrouter.ai/api/v1",
        # OpenRouter proxies hundreds of models and not all honor
        # response_format, so JSON is asked for in the prompt and salvaged from
        # the reply rather than enforced by the API.
        supports_json_mode=False,
        allow_custom_model=True,
        console_url="https://openrouter.ai/keys",
        note="One key, many models — including a free router for trying this out.",
    ),
    "anthropic": Provider(
        key="anthropic",
        label="Anthropic Claude",
        sdk="anthropic",
        env_var="ANTHROPIC_API_KEY",
        models=("claude-sonnet-4-5", "claude-opus-4-1", "claude-haiku-4-5"),
        console_url="https://console.anthropic.com/settings/keys",
        note="Strongest at holding to the 'invent nothing' rule in the prompt.",
    ),
    "openai": Provider(
        key="openai",
        label="OpenAI",
        sdk="openai",
        env_var="OPENAI_API_KEY",
        models=("gpt-4.1", "gpt-4.1-mini", "gpt-4o"),
        base_url=None,  # SDK default
        console_url="https://platform.openai.com/api-keys",
    ),
    "gemini": Provider(
        key="gemini",
        label="Google Gemini",
        sdk="gemini",
        env_var="GEMINI_API_KEY",
        models=(
            "gemini-3.8-flash",
            "gemini-3.5-flash",
            "gemini-3.5-flash-lite",
            "gemini-2.5-pro",
            "gemini-2.5-flash",
        ),
        console_url="https://aistudio.google.com/apikey",
        note="Fast and inexpensive, with a free tier that covers a course or two.",
    ),
    "xai": Provider(
        key="xai",
        label="xAI Grok",
        sdk="openai",
        env_var="XAI_API_KEY",
        models=("grok-4.6", "grok-4"),
        base_url="https://api.x.ai/v1",
        console_url="https://console.x.ai",
        note="OpenAI-compatible endpoint at api.x.ai.",
    ),
    "local": Provider(
        key="local",
        label="On this computer",
        sdk="openai",
        env_var="",  # nothing to configure; the server does not check one
        # A placeholder only. What is installed differs on every machine, so the
        # UI offers a free-text box; a curated list here would advertise models
        # the instructor does not have and miss the ones they do.
        models=(),
        base_url=DEFAULT_LOCAL_BASE_URL,
        # Small models honor response_format unevenly, and a refusal costs a
        # whole request. The prompt asks for JSON and llm.py salvages it.
        supports_json_mode=False,
        allow_custom_model=True,
        requires_key=False,
        is_local=True,
        console_url="https://ollama.com/download",
        note=(
            "Ollama or LM Studio on your own machine — free, and no student "
            "comment ever leaves the room. Only reachable when PeerParley runs "
            "on that same machine, not from Streamlit Cloud. See docs/AI_FEEDBACK.md."
        ),
    ),
}

DEFAULT_PROVIDER = "openrouter"


def get_provider(key: str) -> Provider:
    return PROVIDERS.get(key, PROVIDERS[DEFAULT_PROVIDER])


def get_secret(name: str, default: str = "") -> str:
    """Read one secret from Streamlit secrets, then the environment.

    Also looks inside an ``[ai]`` secrets section, so a deployment can group its
    keys (``ai.ANTHROPIC_API_KEY``) instead of scattering them at the top level.
    """
    if not name:
        return default
    # Every return path is stripped. A key pasted into the Streamlit Cloud
    # secrets box or exported in a shell profile very often carries a trailing
    # newline or space, and several providers reject that with a bare 401 —
    # which sends the instructor hunting for a broken key that is actually fine.
    if st is not None:
        try:
            if name in st.secrets:
                return str(st.secrets[name]).strip()
        except Exception:
            pass
        try:
            node = st.secrets.get("ai", {}) or {}
            if name in node:
                return str(node[name]).strip()
        except Exception:
            pass
    return (os.environ.get(name) or default or "").strip()


def configured_providers() -> List[str]:
    """Provider keys usable without the instructor typing anything.

    A local server needs no credential, so requiring one before listing it
    would hide the only provider that is always free.
    """
    return [
        k for k, p in PROVIDERS.items()
        if not p.requires_key or get_secret(p.env_var)
    ]


# --------------------------------------------------------------------------- #
# Instructor-facing AI settings
# --------------------------------------------------------------------------- #

# Tone presets. The wording an instructor wants differs by course and by how
# bruising the round was, and this is cheaper to offer than asking them to edit
# a prompt file.
TONES: Dict[str, str] = {
    "supportive": (
        "Warm and encouraging, while still stating the improvement points plainly. "
        "Suitable for a first evaluation round."
    ),
    "neutral": (
        "Even and professional. States what teammates said without softening or "
        "sharpening it. The default."
    ),
    "direct": (
        "Brisk and specific. Leads with the substance and spends no words on "
        "cushioning. Suitable for a final round with a mature cohort."
    ),
}

DEFAULT_TONE = "neutral"


@dataclass
class AISettings:
    """What the instructor chose in the sidebar for this session."""

    enabled: bool = False
    provider: str = DEFAULT_PROVIDER
    model: str = PROVIDERS[DEFAULT_PROVIDER].models[0]
    api_key: str = ""
    # Low by default. This task is rewording someone else's words, not creative
    # writing — a higher temperature buys nothing and costs faithfulness.
    temperature: float = 0.2
    max_tokens: int = 1600
    tone: str = DEFAULT_TONE
    # Roughly how long the narrative should run. Students stop reading long
    # before an instructor stops writing.
    target_words: int = 180
    # Run the second-pass grounding check. On by default: the whole promise of
    # this feature is that it does not invent advice, and a promise nobody
    # verifies is a hope.
    verify: bool = True
    # A draft whose grounding score falls below this is held back from the
    # "approve all" action and flagged for a read.
    verify_threshold: float = 0.75
    local_base_url: str = DEFAULT_LOCAL_BASE_URL
    # Include the four dimension ratings and the performance label as context,
    # so the narrative can match the magnitude of the numbers rather than
    # treating every round as equally rosy.
    include_ratings: bool = True
    extra_guidance: str = ""
    usage: Dict[str, float] = field(default_factory=dict)

    def resolved_api_key(self) -> str:
        if self.api_key:
            return self.api_key.strip()
        return get_secret(get_provider(self.provider).env_var)

    def spec(self) -> Provider:
        return get_provider(self.provider)

    def ready(self) -> Tuple[bool, str]:
        """(usable, why not). The UI shows the reason rather than a dead button."""
        spec = self.spec()
        if not self.model.strip():
            return False, "Choose a model."
        if spec.requires_key and not self.resolved_api_key():
            return False, (
                f"No API key for {spec.label}. Paste one in the sidebar, or set "
                f"{spec.env_var} in the app's secrets. Keys: {spec.console_url}"
            )
        # Catch a key pasted under the wrong provider before a batch of forty
        # requests discovers it one 401 at a time.
        mismatch = self.key_mismatch()
        if mismatch:
            other = get_provider(mismatch)
            return False, (
                f"That looks like a {other.label} key, but the provider is set "
                f"to {spec.label}. Switch the provider to {other.label}, or "
                f"paste a {spec.label} key ({spec.console_url})."
            )
        return True, ""

    def key_mismatch(self) -> Optional[str]:
        """The provider this key's prefix belongs to, when it isn't the chosen one."""
        from .llm import key_provider_hint  # local import: llm imports this module
        spec = self.spec()
        if not spec.requires_key:
            return None
        hint = key_provider_hint(self.resolved_api_key())
        return hint if hint and hint != spec.key else None
