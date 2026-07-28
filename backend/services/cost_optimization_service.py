"""LM AI-SDLC — Cost Optimization (Headroom integration).

Wraps https://github.com/headroomlabs-ai/headroom to compress a raw artifact
(logs, stack traces, codebase files, JSON) before it's wrapped into an agent's
instruction prompt, cutting token usage — and therefore API cost.

Design principles:
  - Fail OPEN, always. A compression-library bug, missing dependency, or transient
    error must never break an actual governance review — on any failure we fall
    back to the original, uncompressed content and record zeroed stats.
  - Called at the ORCHESTRATOR level (see `SDLCOrchestrator._compress()`), on the
    raw field, BEFORE it's interpolated into an agent's prompt template — not
    inside llm_gateway.call_tool(). Headroom's content router only applies real
    structural compression (SmartCrusher/CodeCompressor/log compressor) to a
    message that is purely JSON/code/log content; once combined with instruction
    text into one prompt string, the whole blob reads as mixed prose and the
    router no-ops. Real-world behavior observed against the actual package:
      - JSON-shaped content: ~35-55% token reduction (SmartCrusher)
      - Log-shaped content: ~15% reduction (dedicated log compressor)
      - Code content: no reduction — Headroom protects the "most recent" turn's
        code from compression by design (avoids harming an agent's ability to
        reason about code it just read), and every call in this app is a single,
        fresh turn.
      - Plain prose (requirement text, compliance descriptions, etc.): no
        reduction without the optional `headroom-ai[ml]` extra (Kompress-v2-base).
    In short: expect real savings mainly on JSON/log-shaped inputs, not on code
    or prose, unless the `[ml]` extra is installed.
  - Regulated-evidence agents (21 CFR Part 11, GxP Document Check, GxP Document
    Generation/Plan) are exempt by default — their orchestrator methods simply
    never call `_compress()`, so the exact original text always reaches the model.
  - Migration execution (`execute_migration`) is also not compressed — it
    generates replacement code 1:1 from the source, where exact fidelity matters
    more than for a review-only agent.
  - The global enable/disable toggle is DB-persisted (survives restarts) with an
    in-memory cache; falls back to config.COST_OPTIMIZATION_ENABLED_DEFAULT the
    first time the app boots against a fresh DB.
"""
import logging
from typing import Any, Dict, Optional, Tuple

from backend import config, database

logger = logging.getLogger(__name__)

SETTING_KEY = "cost_optimization_enabled"

# Approximate list price per 1M tokens (input / output), USD — used only for the
# dashboard's estimated-savings tile. Headroom's measured stat is always tokens,
# not dollars; this is a best-effort estimate and should be read as directional.
MODEL_PRICING_PER_MILLION_TOKENS = {
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
    "claude-opus-4-1": {"input": 15.00, "output": 75.00},
    "claude-haiku-4-5": {"input": 0.80, "output": 4.00},
    "gpt-4o": {"input": 2.50, "output": 10.00},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
}
DEFAULT_PRICING = {"input": 3.00, "output": 15.00}  # Sonnet-class fallback for unrecognized models

_enabled_cache: Optional[bool] = None


def is_enabled() -> bool:
    global _enabled_cache
    if _enabled_cache is not None:
        return _enabled_cache
    stored = database.get_platform_setting(SETTING_KEY)
    _enabled_cache = (stored == "true") if stored is not None else config.COST_OPTIMIZATION_ENABLED_DEFAULT
    return _enabled_cache


def set_enabled(enabled: bool, actor: str = "system") -> None:
    global _enabled_cache
    database.set_platform_setting(SETTING_KEY, "true" if enabled else "false", actor)
    _enabled_cache = enabled


def estimate_cost_saved_usd(tokens_saved: int, model: str = "") -> float:
    pricing = MODEL_PRICING_PER_MILLION_TOKENS.get(model, DEFAULT_PRICING)
    return round((tokens_saved / 1_000_000) * pricing["input"], 4)


def compress_content(content: str, model: str = "") -> Tuple[str, Dict[str, Any]]:
    """Compress `content` via Headroom. Always returns (content, stats) — falls
    back to the original content and zeroed stats on any error or if disabled."""
    zero_stats = {
        "compressed": False, "tokens_before": 0, "tokens_after": 0,
        "tokens_saved": 0, "compression_ratio": 1.0, "cost_saved_usd": 0.0,
    }
    if not content or not content.strip():
        return content, zero_stats
    if not is_enabled():
        return content, zero_stats

    try:
        from headroom import compress
    except ImportError:
        logger.warning("Cost optimization is enabled but the 'headroom-ai' package is not installed — skipping compression.")
        return content, zero_stats

    try:
        # Headroom's content router treats `role: "user"` as a protected, human-authored
        # instruction and largely skips it; `role: "tool"` is treated as retrieved/tool
        # output and gets the real SmartCrusher/CodeCompressor treatment. Every agent call
        # in this app sends a large constructed artifact (code, logs, JSON, KB context) for
        # the model to analyze — closer to tool output than a human chat message — so we
        # present it as a tool-result message to actually get compression applied.
        result = compress([{"role": "tool", "tool_call_id": "artifact", "content": content}], model=model or config.MODEL)
        compressed_text = result.messages[-1]["content"] if result.messages else content
        tokens_saved = int(getattr(result, "tokens_saved", 0) or 0)
        stats = {
            "compressed": bool(getattr(result, "compressed", tokens_saved > 0)),
            "tokens_before": int(getattr(result, "tokens_before", 0) or 0),
            "tokens_after": int(getattr(result, "tokens_after", 0) or 0),
            "tokens_saved": tokens_saved,
            "compression_ratio": float(getattr(result, "compression_ratio", 1.0) or 1.0),
            "cost_saved_usd": estimate_cost_saved_usd(tokens_saved, model),
        }
        return compressed_text, stats
    except Exception as e:
        logger.warning(f"Headroom compression failed, falling back to uncompressed content: {e}")
        return content, zero_stats
