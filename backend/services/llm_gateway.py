"""Provider-agnostic LLM gateway — Bring Your Own LLM (BYOL).

Every review agent submits its work as a single "tool call" turn (a system
prompt + user content + a JSON-schema tool the model must call). This module
is the one place that turn is actually sent to a model, so it's the one place
that needs to know about more than one provider.

By default every call goes to the platform's own Anthropic account
(config.ANTHROPIC_API_KEY / config.MODEL) — unchanged behaviour. A caller can
override this per-request or per-API-key with their own credentials for:
  - Anthropic direct, AWS Bedrock Claude, GCP Vertex Claude, or any Anthropic
    Messages-API-compatible gateway (base_url override)
  - OpenAI, Azure OpenAI, or any OpenAI-compatible chat-completions gateway

Agent code never picks a provider itself — it calls `call_tool(...)` and the
active override (if any) is read from a contextvar set by the route handler
for the duration of that request. This keeps ~10 agent classes and the
orchestrator free of any BYOL-specific plumbing.
"""
import json
import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Dict, List, Optional

import anthropic
import openai

from backend import config
from backend.services.guardrail_service import enforce_input_limit, scan_for_injection_markers

logger = logging.getLogger(__name__)

_default_anthropic_client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

_current_override: ContextVar[Optional[Dict[str, Any]]] = ContextVar("llm_override", default=None)

# Per-request token-usage accumulator, used for cost-control budget enforcement
# (see backend/services/agent_guard.py). Only active inside a `track_usage()`
# block — outside of one, usage is simply not recorded (no-op, zero overhead).
_usage_accumulator: ContextVar[Optional[List[Dict[str, Any]]]] = ContextVar("llm_usage_acc", default=None)

_RETRYABLE_ANTHROPIC = (anthropic.APIConnectionError, anthropic.APITimeoutError,
                        anthropic.RateLimitError, anthropic.InternalServerError)
_RETRYABLE_OPENAI = (openai.APIConnectionError, openai.APITimeoutError,
                     openai.RateLimitError, openai.InternalServerError)


@contextmanager
def track_usage():
    """Every call_tool() invocation inside this block records its token usage;
    read the accumulated total with get_tracked_usage()."""
    token = _usage_accumulator.set([])
    try:
        yield
    finally:
        _usage_accumulator.reset(token)


def get_tracked_usage() -> Dict[str, int]:
    calls = _usage_accumulator.get() or []
    return {
        "calls": len(calls),
        "input_tokens": sum(c["input_tokens"] for c in calls),
        "output_tokens": sum(c["output_tokens"] for c in calls),
    }


def _record_usage(provider: str, model: str, input_tokens: int, output_tokens: int) -> None:
    acc = _usage_accumulator.get()
    if acc is not None:
        acc.append({
            "provider": provider, "model": model,
            "input_tokens": input_tokens, "output_tokens": output_tokens,
        })


@contextmanager
def with_override(override: Optional[Dict[str, Any]]):
    """Make `override` the active BYOL config for every call_tool() inside this block."""
    token = _current_override.set(override)
    try:
        yield
    finally:
        _current_override.reset(token)


def get_override() -> Optional[Dict[str, Any]]:
    return _current_override.get()


def _call_anthropic(
    api_key: Optional[str], base_url: Optional[str], model: Optional[str],
    system_prompt: str, user_content: str, tool_schema: Dict[str, Any],
    max_tokens: Optional[int] = None,
) -> Dict[str, Any]:
    client = _default_anthropic_client if not api_key else anthropic.Anthropic(
        api_key=api_key, base_url=base_url or None
    )
    resolved_model = model or config.MODEL
    last_err = None
    response = None
    start = time.perf_counter()
    for attempt in range(config.LLM_MAX_RETRIES + 1):
        try:
            response = client.messages.create(
                model=resolved_model,
                max_tokens=max_tokens or config.MAX_TOKENS,
                system=system_prompt,
                tools=[tool_schema],
                tool_choice={"type": "tool", "name": tool_schema["name"]},
                messages=[{"role": "user", "content": user_content}],
                timeout=config.LLM_TIMEOUT_SECONDS,
            )
            break
        except _RETRYABLE_ANTHROPIC as e:
            last_err = e
            if attempt < config.LLM_MAX_RETRIES:
                logger.warning(
                    "Anthropic call for tool '%s' failed (attempt %d/%d): %s — retrying",
                    tool_schema["name"], attempt + 1, config.LLM_MAX_RETRIES + 1, e,
                )
                time.sleep(2 ** attempt)
                continue
            logger.error(
                "Anthropic call for tool '%s' failed after %d attempts: %s",
                tool_schema["name"], config.LLM_MAX_RETRIES + 1, e,
            )
            raise
    duration_ms = (time.perf_counter() - start) * 1000
    usage = getattr(response, "usage", None)
    input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
    _record_usage("anthropic", resolved_model, input_tokens, output_tokens)
    logger.info(
        "Anthropic call tool='%s' model=%s tokens_in=%d tokens_out=%d duration=%.1fms stop_reason=%s",
        tool_schema["name"], resolved_model, input_tokens, output_tokens, duration_ms, response.stop_reason,
    )
    if response.stop_reason == "max_tokens":
        logger.warning(
            "Anthropic call for tool '%s' hit max_tokens (%d) — response is likely truncated/incomplete",
            tool_schema["name"], max_tokens or config.MAX_TOKENS,
        )
    for block in response.content:
        if block.type == "tool_use" and block.name == tool_schema["name"]:
            return block.input
    logger.error("Anthropic call for tool '%s' returned no matching tool_use block", tool_schema["name"])
    raise ValueError(f"Anthropic call did not return structured output for tool '{tool_schema['name']}'")


def _call_openai(
    api_key: str, base_url: Optional[str], model: Optional[str],
    system_prompt: str, user_content: str, tool_schema: Dict[str, Any],
    max_tokens: Optional[int] = None,
) -> Dict[str, Any]:
    client = openai.OpenAI(api_key=api_key, base_url=base_url or None)
    resolved_model = model or "gpt-4o"
    response = None
    start = time.perf_counter()
    for attempt in range(config.LLM_MAX_RETRIES + 1):
        try:
            response = client.chat.completions.create(
                model=resolved_model,
                max_tokens=max_tokens or 4096,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
                tools=[{
                    "type": "function",
                    "function": {
                        "name": tool_schema["name"],
                        "description": tool_schema.get("description", ""),
                        "parameters": tool_schema["input_schema"],
                    },
                }],
                tool_choice={"type": "function", "function": {"name": tool_schema["name"]}},
                timeout=config.LLM_TIMEOUT_SECONDS,
            )
            break
        except _RETRYABLE_OPENAI as e:
            if attempt < config.LLM_MAX_RETRIES:
                logger.warning(
                    "OpenAI call for tool '%s' failed (attempt %d/%d): %s — retrying",
                    tool_schema["name"], attempt + 1, config.LLM_MAX_RETRIES + 1, e,
                )
                time.sleep(2 ** attempt)
                continue
            logger.error(
                "OpenAI call for tool '%s' failed after %d attempts: %s",
                tool_schema["name"], config.LLM_MAX_RETRIES + 1, e,
            )
            raise
    duration_ms = (time.perf_counter() - start) * 1000
    usage = getattr(response, "usage", None)
    input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
    _record_usage("openai", resolved_model, input_tokens, output_tokens)
    finish_reason = response.choices[0].finish_reason if response.choices else None
    logger.info(
        "OpenAI call tool='%s' model=%s tokens_in=%d tokens_out=%d duration=%.1fms finish_reason=%s",
        tool_schema["name"], resolved_model, input_tokens, output_tokens, duration_ms, finish_reason,
    )
    if finish_reason == "length":
        logger.warning(
            "OpenAI call for tool '%s' hit the token limit (%d) — response is likely truncated/incomplete",
            tool_schema["name"], max_tokens or 4096,
        )
    message = response.choices[0].message
    for call in (message.tool_calls or []):
        if call.function.name == tool_schema["name"]:
            return json.loads(call.function.arguments)
    logger.error("OpenAI call for tool '%s' returned no matching tool_call", tool_schema["name"])
    raise ValueError(f"OpenAI call did not return structured output for tool '{tool_schema['name']}'")


def call_tool(
    system_prompt: str,
    user_content: str,
    tool_schema: Dict[str, Any],
    llm_override: Optional[Dict[str, Any]] = None,
    max_tokens: Optional[int] = None,
) -> Dict[str, Any]:
    """Run one tool-call turn against the platform default LLM or a BYOL override.

    llm_override shape: {"provider": "anthropic"|"openai", "api_key": str,
                          "base_url": Optional[str], "model": Optional[str]}
    Falls back to `get_override()` (contextvar) when llm_override is not passed
    explicitly, then to the platform default when neither is set.
    `max_tokens` lets callers with larger output needs (e.g. code generation)
    override the platform default response-length budget for this call only.

    Cost-optimization (Headroom) compression is NOT done here — it happens at the
    orchestrator level, on the raw artifact field, before it's wrapped into this
    prompt string. Headroom's content router only applies structural compression
    to messages that are purely JSON/code/log content; once instructions and an
    artifact are combined into one prompt string (as every agent does), the whole
    blob reads as mixed prose and the router no-ops. See orchestrator.py's
    `_compress()` helper.

    Guardrails applied here, uniformly, for every agent (see guardrail_service.py):
      - `enforce_input_limit` — hard stop if user_content exceeds config.MAX_AGENT_INPUT_CHARS.
      - `scan_for_injection_markers` — non-blocking; any hits are attached to the
        returned dict as `guardrail_flags` for audit visibility.
    """
    enforce_input_limit(user_content)

    override = llm_override if llm_override is not None else get_override()
    provider = (override or {}).get("provider") or "anthropic"
    api_key = (override or {}).get("api_key")
    base_url = (override or {}).get("base_url")
    model = (override or {}).get("model")

    if provider == "openai":
        if not api_key:
            raise ValueError("An OpenAI-compatible BYOL config requires an api_key.")
        result = _call_openai(api_key, base_url, model, system_prompt, user_content, tool_schema, max_tokens)
    else:
        result = _call_anthropic(api_key, base_url, model, system_prompt, user_content, tool_schema, max_tokens)

    flags = scan_for_injection_markers(user_content)
    if flags:
        logger.warning("Injection markers flagged for tool '%s': %s", tool_schema["name"], flags)
        if isinstance(result, dict):
            result["guardrail_flags"] = flags
    return result


def describe(llm_override: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """Small metadata blob describing which LLM actually served a call — for audit trails."""
    if not llm_override:
        return {"provider": "anthropic", "model": config.MODEL, "source": "platform"}
    provider = llm_override.get("provider") or "anthropic"
    default_model = config.MODEL if provider == "anthropic" else "gpt-4o"
    return {
        "provider": provider,
        "model": llm_override.get("model") or default_model,
        "source": llm_override.get("source", "byol"),
    }
