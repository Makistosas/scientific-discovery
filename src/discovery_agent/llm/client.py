"""Thin wrapper around the Anthropic SDK shared by the synthesizer and the investigator.

* Default model: ``claude-opus-5-5`` (override with ``DISCOVERY_MODEL``).
* Adaptive thinking + configurable effort.
* Server-side refusal fallbacks (``fallbacks="default"``) are enabled: if a
  safety classifier declines a (benign) biomedical request, the API retries it
  on Anthropic's recommended fallback model inside the same call.
"""

from __future__ import annotations

from typing import Any

from ..config import Settings

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMError(RuntimeError):
    pass


class LLMRefusal(LLMError):
    pass


def require_anthropic():
    try:
        import anthropic  # noqa: F401
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise LLMError("The AI layer needs the Anthropic SDK: pip install 'scientific-discovery-agent[ai]'") from exc
    return anthropic


def make_client(settings: Settings | None = None) -> Any:
    anthropic = require_anthropic()
    return anthropic.Anthropic(max_retries=3)


def request_kwargs(settings: Settings, *, use_fallbacks: bool = True) -> dict:
    kwargs: dict[str, Any] = {
        "model": settings.model,
        "thinking": {"type": "adaptive"},
    }
    if use_fallbacks:
        kwargs["betas"] = [FALLBACK_BETA]
        kwargs["fallbacks"] = "default"
    return kwargs


def check_stop(message: Any) -> None:
    """Raise on stop reasons that mean the content is unusable."""
    reason = getattr(message, "stop_reason", None)
    if reason == "refusal":
        details = getattr(message, "stop_details", None)
        category = getattr(details, "category", None) if details else None
        explanation = getattr(details, "explanation", None) if details else None
        raise LLMRefusal(f"Model declined the request (category={category}): {explanation or ''}".strip())
    if reason == "max_tokens":
        raise LLMError("Model output hit max_tokens before finishing; raise max_tokens or lower effort.")


def text_of(message: Any) -> str:
    return "".join(getattr(b, "text", "") for b in getattr(message, "content", []) or []
                   if getattr(b, "type", None) == "text")


def sanitize_for_echo(content: list[Any]) -> list[Any]:
    """Drop model-internal blocks that precede the last fallback boundary.

    After a mid-output fallback, thinking / tool_use blocks emitted *before* the
    final ``fallback`` block must not be echoed back on the next turn.
    """
    last = -1
    for i, block in enumerate(content):
        if getattr(block, "type", None) == "fallback":
            last = i
    if last < 0:
        return list(content)
    drop = {"thinking", "redacted_thinking", "tool_use", "server_tool_use"}
    return [b for i, b in enumerate(content) if i >= last or getattr(b, "type", None) not in drop]


def friendly_api_error(exc: Exception) -> LLMError:
    anthropic = require_anthropic()
    if isinstance(exc, anthropic.AuthenticationError):
        return LLMError("Anthropic authentication failed: set ANTHROPIC_API_KEY (or run `ant auth login`).")
    if isinstance(exc, anthropic.PermissionDeniedError):
        return LLMError(f"Anthropic API permission denied: {exc}")
    if isinstance(exc, anthropic.NotFoundError):
        return LLMError(f"Model or endpoint not found (check DISCOVERY_MODEL): {exc}")
    if isinstance(exc, anthropic.RateLimitError):
        return LLMError("Anthropic rate limit reached; try again later or lower concurrency.")
    if isinstance(exc, anthropic.BadRequestError):
        return LLMError(f"Anthropic API rejected the request: {exc}")
    if isinstance(exc, anthropic.APIStatusError):
        return LLMError(f"Anthropic API error {exc.status_code}: {exc}")
    if isinstance(exc, anthropic.APIConnectionError):
        return LLMError("Could not reach the Anthropic API (network).")
    return LLMError(str(exc))
