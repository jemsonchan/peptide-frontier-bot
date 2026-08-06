"""
Provider-agnostic text generation.

Two providers, chosen by env var, because the cheapest good model changes every
few months and you should not have to refactor to switch:

  LLM_PROVIDER=anthropic  -> Anthropic Messages API (default; Haiku is ~$0.001
                             per reply at these token counts, i.e. an order of
                             magnitude cheaper than the $0.015 it costs to
                             publish the reply)
  LLM_PROVIDER=openai     -> any OpenAI-compatible /chat/completions endpoint.
                             Set LLM_BASE_URL to point at Groq, xAI, DeepSeek,
                             OpenRouter, or a local server.

Note on xAI specifically: buying X API credits earns free xAI credits, but the
reward rate is 0% below $200 cumulative spend, so at a $6 budget that is not a
reason to pick Grok.

Generation cost is small next to publication cost. That asymmetry is the whole
design: generate freely, publish rarely. Draft, critique, and discard is cheap;
a bad published reply is expensive twice over -- in credits and in the
mute/block signals the ranker weights negatively.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass

log = logging.getLogger(__name__)

SKIP = "SKIP"


class LLMError(RuntimeError):
    pass


@dataclass
class LLMConfig:
    provider: str = "anthropic"
    model: str = "claude-haiku-4-5-20251001"
    base_url: str = ""
    api_key: str = ""
    max_tokens: int = 300
    temperature: float = 0.4
    timeout: int = 60
    # Gemini 2.5-flash reasons before answering, and reasoning tokens come
    # out of max_tokens. Left unset with a small budget, the reply is empty.
    reasoning_effort: str = ""

    @classmethod
    def from_env(cls) -> "LLMConfig":
        provider = os.getenv("LLM_PROVIDER", "anthropic").lower()
        default_model = (
            "claude-haiku-4-5-20251001" if provider == "anthropic" else "llama-3.3-70b-versatile"
        )
        return cls(
            provider=provider,
            model=os.getenv("LLM_MODEL", default_model),
            base_url=os.getenv("LLM_BASE_URL", ""),
            api_key=os.getenv("LLM_API_KEY", "") or os.getenv("ANTHROPIC_API_KEY", ""),
            max_tokens=int(os.getenv("LLM_MAX_TOKENS", "300")),
            reasoning_effort=os.getenv("LLM_REASONING_EFFORT", ""),
            temperature=float(os.getenv("LLM_TEMPERATURE", "0.4")),
        )


def _http_json(url: str, headers: dict[str, str], body: dict, timeout: int) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise LLMError(f"{e.code}: {e.read().decode()[:400]}") from e
    except urllib.error.URLError as e:
        raise LLMError(str(e)) from e


def generate(system: str, user: str, cfg: LLMConfig | None = None) -> str:
    cfg = cfg or LLMConfig.from_env()
    if not cfg.api_key:
        raise LLMError("no API key: set LLM_API_KEY (or ANTHROPIC_API_KEY)")

    if cfg.provider == "anthropic":
        data = _http_json(
            "https://api.anthropic.com/v1/messages",
            {
                "content-type": "application/json",
                "x-api-key": cfg.api_key,
                "anthropic-version": "2023-06-01",
            },
            {
                "model": cfg.model,
                "max_tokens": cfg.max_tokens,
                "temperature": cfg.temperature,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            },
            cfg.timeout,
        )
        parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
        return "".join(parts).strip()

    base = (cfg.base_url or "https://api.openai.com/v1").rstrip("/")
    body = {
        "model": cfg.model,
        "max_tokens": cfg.max_tokens,
        "temperature": cfg.temperature,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    if cfg.reasoning_effort:
        body["reasoning_effort"] = cfg.reasoning_effort
    data = _http_json(
        f"{base}/chat/completions",
        {"content-type": "application/json", "authorization": f"Bearer {cfg.api_key}"},
        body,
        cfg.timeout,
    )
    try:
        text = (data["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError) as e:
        raise LLMError(f"unexpected response shape: {str(data)[:200]}") from e
    if not text:
        # Almost always a thinking model that spent the whole budget reasoning.
        raise LLMError(
            f"empty completion from {cfg.model} — raise LLM_MAX_TOKENS or set "
            f"LLM_REASONING_EFFORT=none"
        )
    return text


def is_skip(text: str) -> bool:
    t = (text or "").strip().strip('."\'').upper()
    return t == SKIP or t.startswith("SKIP")


def skip_reason(text: str) -> str:
    """
    Pull the model's own explanation out of a `SKIP: ...` reply.

    Worth capturing: a run where everything skips is indistinguishable from a
    run where everything failed a gate, unless the skip says why. That
    ambiguity cost a full diagnosis cycle on 2026-08-06.
    """
    t = (text or "").strip().strip('."\'')
    if ":" in t:
        return t.split(":", 1)[1].strip()[:120]
    return t[len(SKIP):].strip(" -–—.")[:120] or "no reason given"


def critique(draft: str, context: str, cfg: LLMConfig | None = None) -> tuple[bool, str]:
    """
    Second opinion on our own draft. Returns (passed, reason).

    Worth the extra call: this catches invented citations, which is the single
    failure mode that would do lasting damage to an account whose entire pitch
    is 'cited'. Runs at temperature 0 -- we want a judge, not a writer.
    """
    from .prompts import CRITIC

    cfg = cfg or LLMConfig.from_env()
    judge = LLMConfig(**{**cfg.__dict__, "temperature": 0.0, "max_tokens": 60})
    try:
        verdict = generate(CRITIC, f"{context}\n\nDRAFT:\n{draft}", judge)
    except LLMError as e:
        # Fail closed. If the judge is unavailable we do not publish.
        log.warning("critic unavailable (%s); failing closed", e)
        return False, "critic unavailable"
    v = verdict.strip()
    if v.upper().startswith("PASS"):
        return True, ""
    return False, v[:120]
