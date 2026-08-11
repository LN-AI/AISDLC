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


class LLMTruncatedResponseError(RuntimeError):
    """Raised when the provider's stop_reason/finish_reason indicates the tool-call
    response was cut off by the max_tokens budget before it finished — the returned
    tool input (if any) is structurally incomplete and must not be trusted as a
    genuine result."""

    def __init__(
        self, tool_name: str, max_tokens: int, *,
        elapsed_ms: Optional[float] = None, input_tokens: Optional[int] = None,
        output_tokens: Optional[int] = None, model: Optional[str] = None,
        provider: Optional[str] = None,
    ):
        self.tool_name = tool_name
        self.max_tokens = max_tokens
        self.elapsed_ms = elapsed_ms
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.model = model
        self.provider = provider
        detail = f"LLM response for '{tool_name}' was truncated at the {max_tokens}-token limit before it finished generating"
        if elapsed_ms is not None:
            detail += f" (after {elapsed_ms:.0f}ms"
            if output_tokens is not None:
                detail += f", {output_tokens} output tokens generated"
            detail += ")"
        detail += " — retry, or increase max_tokens for this call."
        super().__init__(detail)


class LLMProviderError(RuntimeError):
    """Raised when the LLM provider's SDK reported a retryable failure (connection,
    timeout, rate limit, or 5xx) and every retry attempt was exhausted. Wraps the raw
    SDK exception so callers get consistent, informative attributes instead of a
    provider-specific exception type."""

    def __init__(
        self, provider: str, tool_name: str, attempts: int, original: Exception, *,
        elapsed_ms: Optional[float] = None,
    ):
        self.provider = provider
        self.tool_name = tool_name
        self.attempts = attempts
        self.original = original
        self.elapsed_ms = elapsed_ms
        detail = (
            f"{provider} provider call for '{tool_name}' failed after {attempts} attempt(s)"
        )
        if elapsed_ms is not None:
            detail += f" ({elapsed_ms:.0f}ms elapsed)"
        detail += f": {type(original).__name__}: {original}"
        super().__init__(detail)


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
        # Cache tokens are reported separately by the provider and are NOT included in
        # input_tokens. They are surfaced here for visibility; billing (agent_guard)
        # deliberately does not price them yet — see the note in _call_anthropic.
        "cache_write_tokens": sum(c.get("cache_write_tokens", 0) for c in calls),
        "cache_read_tokens": sum(c.get("cache_read_tokens", 0) for c in calls),
    }


def _record_usage(
    provider: str, model: str, input_tokens: int, output_tokens: int,
    cache_write_tokens: int = 0, cache_read_tokens: int = 0,
) -> None:
    acc = _usage_accumulator.get()
    if acc is not None:
        acc.append({
            "provider": provider, "model": model,
            "input_tokens": input_tokens, "output_tokens": output_tokens,
            "cache_write_tokens": cache_write_tokens, "cache_read_tokens": cache_read_tokens,
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


def platform_default() -> Optional[Dict[str, Any]]:
    """The admin-selected platform LLM connection, or None to mean config.ANTHROPIC_API_KEY.

    This is the rung that makes the browser UI work against a non-Anthropic provider.
    It is resolved *here*, at call time, rather than pushed through `with_override()`:
    `agent_guard.guarded_call` sets the contextvar to None for every UI call, so an
    outer contextvar value would be shadowed and never seen.

    Never raises. A settings lookup failing must degrade to the platform default, not
    take down every agent in the product.
    """
    try:
        from backend.services import llm_settings_service  # local: it imports this module
        return llm_settings_service.active_override()
    except Exception as e:  # noqa: BLE001
        logger.warning("Could not resolve the platform LLM connection (using default): %s", e)
        return None


def resolve_override(llm_override: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """The single definition of which LLM config wins, shared with agent_guard so the
    ledger records the model that actually served a call rather than assuming Anthropic.

    Explicit argument > request contextvar > active platform connection > None (.env).
    """
    if llm_override is not None:
        return llm_override
    return get_override() or platform_default()


def _cacheable_system(system_prompt: str):
    """Return the `system` parameter, marked cacheable when prompt caching is on.

    Anthropic assembles the prompt as tools -> system -> messages, so a single
    cache_control breakpoint on the last system block covers the tool schema *and* the
    system prompt — the part of every agent call that is byte-identical across calls.
    The per-request artifact stays in `messages`, after the breakpoint, so it never
    invalidates the cached prefix.

    Nothing about the model's output changes; only billing does. Prefixes shorter than
    the model's minimum cacheable length (1024 tokens on Sonnet) are silently not
    cached rather than rejected, so short system prompts are safe to send this way.
    """
    if not config.PROMPT_CACHE_ENABLED:
        return system_prompt
    return [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}]


def _call_anthropic(
    api_key: Optional[str], base_url: Optional[str], model: Optional[str],
    system_prompt: str, user_content: str, tool_schema: Dict[str, Any],
    max_tokens: Optional[int] = None, timeout: Optional[float] = None,
) -> Dict[str, Any]:
    client = _default_anthropic_client if not api_key else anthropic.Anthropic(
        api_key=api_key, base_url=base_url or None
    )
    timeout = timeout or config.LLM_TIMEOUT_SECONDS
    resolved_model = model or config.MODEL
    last_err = None
    response = None
    start = time.perf_counter()
    system_param = _cacheable_system(system_prompt)
    for attempt in range(config.LLM_MAX_RETRIES + 1):
        try:
            response = client.messages.create(
                model=resolved_model,
                max_tokens=max_tokens or config.MAX_TOKENS,
                system=system_param,
                tools=[tool_schema],
                tool_choice={"type": "tool", "name": tool_schema["name"]},
                messages=[{"role": "user", "content": user_content}],
                timeout=timeout,
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
            elapsed_ms = (time.perf_counter() - start) * 1000
            logger.error(
                "Anthropic call for tool '%s' failed after %d attempts (%.0fms): %s",
                tool_schema["name"], config.LLM_MAX_RETRIES + 1, elapsed_ms, e,
            )
            raise LLMProviderError(
                "anthropic", tool_schema["name"], config.LLM_MAX_RETRIES + 1, e, elapsed_ms=elapsed_ms,
            ) from e
    duration_ms = (time.perf_counter() - start) * 1000
    usage = getattr(response, "usage", None)
    input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
    # Reported separately from input_tokens by the API. A write costs 1.25x an input
    # token and a read 0.1x; agent_guard prices only input/output, so a cached call is
    # currently costed slightly *under* its true price rather than over. Logged here so
    # the effect is visible: if cache_read stays 0 across a multi-call run, something in
    # the prefix is changing between calls and the cache is never being hit.
    cache_write = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
    cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
    _record_usage("anthropic", resolved_model, input_tokens, output_tokens, cache_write, cache_read)
    logger.info(
        "Anthropic call tool='%s' model=%s tokens_in=%d tokens_out=%d cache_write=%d cache_read=%d "
        "duration=%.1fms stop_reason=%s",
        tool_schema["name"], resolved_model, input_tokens, output_tokens, cache_write, cache_read,
        duration_ms, response.stop_reason,
    )
    if response.stop_reason == "max_tokens":
        logger.warning(
            "Anthropic call for tool '%s' hit max_tokens (%d) — response is likely truncated/incomplete",
            tool_schema["name"], max_tokens or config.MAX_TOKENS,
        )
        raise LLMTruncatedResponseError(
            tool_schema["name"], max_tokens or config.MAX_TOKENS,
            elapsed_ms=duration_ms, input_tokens=input_tokens, output_tokens=output_tokens,
            model=resolved_model, provider="anthropic",
        )
    for block in response.content:
        if block.type == "tool_use" and block.name == tool_schema["name"]:
            return block.input
    logger.error("Anthropic call for tool '%s' returned no matching tool_use block", tool_schema["name"])
    raise ValueError(f"Anthropic call did not return structured output for tool '{tool_schema['name']}'")


def _call_openai(
    api_key: str, base_url: Optional[str], model: Optional[str],
    system_prompt: str, user_content: str, tool_schema: Dict[str, Any],
    max_tokens: Optional[int] = None, timeout: Optional[float] = None,
) -> Dict[str, Any]:
    client = openai.OpenAI(api_key=api_key, base_url=base_url or None)
    resolved_model = model or "gpt-4o"
    # Was hardcoded to 4096 while the Anthropic path used config.MAX_TOKENS (8000). Any
    # agent that doesn't name its own budget got half as much room here, and a response
    # cut off at the limit raises LLMTruncatedResponseError rather than degrading — so
    # the same agent would work on Anthropic and fail on an OpenAI-compatible endpoint.
    resolved_max_tokens = max_tokens or config.MAX_TOKENS
    timeout = timeout or config.LLM_TIMEOUT_SECONDS
    response = None
    start = time.perf_counter()
    for attempt in range(config.LLM_MAX_RETRIES + 1):
        try:
            response = client.chat.completions.create(
                model=resolved_model,
                max_tokens=resolved_max_tokens,
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
                timeout=timeout,
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
            elapsed_ms = (time.perf_counter() - start) * 1000
            logger.error(
                "OpenAI call for tool '%s' failed after %d attempts (%.0fms): %s",
                tool_schema["name"], config.LLM_MAX_RETRIES + 1, elapsed_ms, e,
            )
            raise LLMProviderError(
                "openai", tool_schema["name"], config.LLM_MAX_RETRIES + 1, e, elapsed_ms=elapsed_ms,
            ) from e
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
            tool_schema["name"], resolved_max_tokens,
        )
        raise LLMTruncatedResponseError(
            tool_schema["name"], resolved_max_tokens,
            elapsed_ms=duration_ms, input_tokens=input_tokens, output_tokens=output_tokens,
            model=resolved_model, provider="openai",
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
                          "base_url": Optional[str], "model": Optional[str],
                          "max_tokens": Optional[int], "timeout_seconds": Optional[float]}
    Falls back to `get_override()` (contextvar) when llm_override is not passed
    explicitly, then to the admin-selected platform connection (`platform_default()`),
    then to config.ANTHROPIC_API_KEY when none is set.
    `max_tokens` lets callers with larger output needs (e.g. code generation)
    override the platform default response-length budget for this call only. A connection's
    configured `max_tokens` is a hard ceiling and clamps that request down — see the
    comment at the resolution site for why an endpoint's real limit has to win.

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

    override = resolve_override(llm_override)
    provider = (override or {}).get("provider") or "anthropic"
    api_key = (override or {}).get("api_key")
    base_url = (override or {}).get("base_url")
    model = (override or {}).get("model")
    # Resolved once, here, rather than separately inside each provider function — the two
    # used to disagree (Anthropic config.MAX_TOKENS, OpenAI a hardcoded 4096), so the same
    # agent got half the room on an OpenAI-compatible endpoint and raised
    # LLMTruncatedResponseError instead of finishing.
    #
    # The connection's `max_tokens` is a **ceiling**, not just a default: agents now ask for
    # up to 32000, which is above what several OpenAI-compatible models will accept as an
    # output cap at all (gpt-4o tops out at 16384, gpt-4-turbo at 4096). Letting the agent's
    # request through unchanged makes those endpoints reject the whole call with a
    # non-retryable 400 — strictly worse than clamping, which at most truncates and raises
    # LLMTruncatedResponseError with a message saying so. An admin who knows their endpoint's
    # limit can now express it, and the clamp is logged so a short answer is never mysterious.
    requested = max_tokens or config.MAX_TOKENS
    ceiling = (override or {}).get("max_tokens")
    if ceiling and requested > ceiling:
        logger.warning(
            "Clamping max_tokens for tool '%s' from %d to the connection's ceiling of %d — "
            "a response longer than that will be reported as truncated",
            tool_schema["name"], requested, ceiling,
        )
        requested = ceiling
    max_tokens = requested
    timeout = (override or {}).get("timeout_seconds")

    if provider == "openai":
        if not api_key:
            # Local routers (Ollama, OmniRoute, LM Studio) don't check the key, but the
            # OpenAI SDK refuses to construct a client without one. Send a placeholder
            # rather than making the admin invent one — a real endpoint that does check
            # will reject it and the connection test will say so.
            api_key = "not-required"
        result = _call_openai(api_key, base_url, model, system_prompt, user_content, tool_schema, max_tokens, timeout)
    else:
        result = _call_anthropic(api_key, base_url, model, system_prompt, user_content, tool_schema, max_tokens, timeout)

    flags = scan_for_injection_markers(user_content)
    if flags:
        logger.warning("Injection markers flagged for tool '%s': %s", tool_schema["name"], flags)
        if isinstance(result, dict):
            result["guardrail_flags"] = flags
    return result


def count_tokens(
    system_prompt: str,
    user_content: str,
    tool_schema: Optional[Dict[str, Any]] = None,
    llm_override: Optional[Dict[str, Any]] = None,
) -> int:
    """How many input tokens this exact turn would cost, before sending it.

    The platform has only ever sized inputs by character count (MAX_AGENT_INPUT_CHARS),
    which cannot answer "will this fit" or "how should this be split" — characters per
    token vary by more than 2x between prose and minified code. Callers that need to
    budget or chunk should use this instead of len().

    Uses Anthropic's own count_tokens endpoint, so it is exact for the Anthropic path.
    For OpenAI-compatible BYOL there is no equivalent free endpoint, so it falls back to
    a documented ~3.5 chars/token estimate. Deliberately NOT tiktoken: it is OpenAI's
    tokenizer and materially undercounts Claude tokens on code.

    Never raises — a token count is an optimisation input, not a correctness one, so any
    failure degrades to the character estimate rather than breaking the caller.
    """
    override = resolve_override(llm_override)
    provider = (override or {}).get("provider") or "anthropic"
    combined = f"{system_prompt}\n{user_content}"
    estimate = int(len(combined) / 3.5) + 1

    if provider != "anthropic":
        return estimate
    try:
        api_key = (override or {}).get("api_key")
        base_url = (override or {}).get("base_url")
        client = _default_anthropic_client if not api_key else anthropic.Anthropic(
            api_key=api_key, base_url=base_url or None
        )
        kwargs: Dict[str, Any] = {
            "model": (override or {}).get("model") or config.MODEL,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_content}],
        }
        if tool_schema:
            kwargs["tools"] = [tool_schema]
        return int(client.messages.count_tokens(**kwargs).input_tokens)
    except Exception as e:
        logger.warning("count_tokens failed, falling back to character estimate: %s", e)
        return estimate


def describe(llm_override: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """Small metadata blob describing which LLM actually served a call — for audit trails.

    Resolves the same chain call_tool does, so a UI call running on an admin-selected
    connection is recorded as that connection rather than as Anthropic.
    """
    resolved = llm_override or platform_default()
    if not resolved:
        return {"provider": "anthropic", "model": config.MODEL, "source": "platform"}
    provider = resolved.get("provider") or "anthropic"
    default_model = config.MODEL if provider == "anthropic" else "gpt-4o"
    described = {
        "provider": provider,
        "model": resolved.get("model") or default_model,
        "source": resolved.get("source", "byol"),
    }
    if resolved.get("connection_name"):
        described["connection"] = resolved["connection_name"]
    return described
