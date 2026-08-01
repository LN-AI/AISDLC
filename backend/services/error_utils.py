"""Single place that decides what an error response looks like.

Before this module existed, `main.py`'s global exception handlers (registered via
`@app.exception_handler`) already mapped a few known exception types to informative
status codes/bodies — but ~28 legacy `/api/*` routes and `routes/agents.py`'s own
handler each had their own local `except Exception as e: raise HTTPException(500,
detail=str(e))`, which caught everything *before* it ever reached those global
handlers and collapsed it to a generic, undifferentiated 500. `format_error_response`
is now the one function both the global handlers and every remaining local except
block call, so a given exception always produces the same status/body no matter
which route it surfaced on.
"""
from typing import Any, Dict, Optional, Tuple

from backend.logging_config import request_id_ctx
from backend.services.agent_guard import BudgetExceededError, ScopeDeniedError
from backend.services.guardrail_service import GuardrailViolation
from backend.services.llm_gateway import LLMProviderError, LLMTruncatedResponseError

try:
    import anthropic
    import openai
    _RETRYABLE_PROVIDER_ERRORS: Tuple[type, ...] = (
        anthropic.APIConnectionError, anthropic.APITimeoutError,
        anthropic.RateLimitError, anthropic.InternalServerError,
        openai.APIConnectionError, openai.APITimeoutError,
        openai.RateLimitError, openai.InternalServerError,
    )
except ImportError:
    _RETRYABLE_PROVIDER_ERRORS = ()


def format_error_response(exc: Exception, agent_name: Optional[str] = None) -> Tuple[int, Dict[str, Any]]:
    """Map an exception to (status_code, body_dict). `body_dict` always includes
    `detail` and `error_type`, plus whatever extra context is available for that
    exception type (agent, elapsed_ms, token counts, provider, retry attempts).
    Always includes `request_id` so a user-reported error can be matched to server
    logs (see `backend.logging_config.request_id_ctx`, set per-request in main.py's
    `log_requests` middleware)."""
    request_id = request_id_ctx.get()
    body: Dict[str, Any] = {"request_id": request_id}
    if agent_name:
        body["agent"] = agent_name

    if isinstance(exc, GuardrailViolation):
        body.update(detail=str(exc), error_type="guardrail_violation")
        return 413, body

    if isinstance(exc, ScopeDeniedError):
        body.update(detail=str(exc), error_type="scope_denied")
        return 403, body

    if isinstance(exc, BudgetExceededError):
        body.update(detail=str(exc), error_type="budget_exceeded", **exc.budget)
        return 402, body

    if isinstance(exc, LLMTruncatedResponseError):
        body.update(
            detail=str(exc), error_type="llm_truncated", tool_name=exc.tool_name,
            max_tokens=exc.max_tokens, elapsed_ms=exc.elapsed_ms,
            input_tokens=exc.input_tokens, output_tokens=exc.output_tokens,
            model=exc.model, provider=exc.provider,
        )
        return 502, body

    if isinstance(exc, LLMProviderError):
        body.update(
            detail=f"Upstream LLM provider unavailable: {exc}",
            error_type="llm_provider_unavailable", provider=exc.provider,
            tool_name=exc.tool_name, attempts=exc.attempts, elapsed_ms=exc.elapsed_ms,
        )
        return 503, body

    if isinstance(exc, _RETRYABLE_PROVIDER_ERRORS):
        # Legacy path: a retryable SDK exception that reached here un-wrapped (e.g. a
        # code path outside llm_gateway.call_tool). LLMProviderError above is the
        # normal path now that llm_gateway wraps these at the source.
        body.update(
            detail=f"Upstream LLM provider unavailable: {exc}",
            error_type="llm_provider_unavailable",
        )
        return 503, body

    if isinstance(exc, ValueError):
        body.update(detail=str(exc), error_type="invalid_request")
        return 400, body

    body.update(detail=str(exc) or type(exc).__name__, error_type="internal_error", exception_type=type(exc).__name__)
    return 500, body
