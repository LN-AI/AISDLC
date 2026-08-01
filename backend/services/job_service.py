"""Background-job wrapper for long-running, multi-step agent work.

Most agent endpoints are one LLM call and stay synchronous — a client just waits
for the response. `full_review` is different: it chains up to 7 sequential LLM
calls (each potentially 60-150s+) behind a single HTTP request, which has failed
or timed out in every test run to date because the client has no way to know the
request is still progressing. This module lets a route hand `full_review` off to
a background thread, return a `job_id` immediately, and let the client poll
`GET /api/v1/agents/jobs/{job_id}` for live step progress instead of holding the
HTTP connection open for the whole chain.

Not used for the other ~24 single-LLM-call agents — a single call has no
meaningful sub-steps, and they already get real headroom from the generous
max_tokens/timeout values in config.py/base_agent.py.
"""
import asyncio
import logging
from typing import Any, Callable, Dict, Optional

from backend import database

logger = logging.getLogger(__name__)


def start_job(
    agent_name: str,
    total_steps: int,
    run_fn: Callable[[Callable[[int, int, str], None]], Dict[str, Any]],
) -> str:
    """Creates an `agent_jobs` row and schedules `run_fn` on a background thread
    (via `asyncio.to_thread`, so the event loop stays free for other requests
    while `run_fn`'s blocking LLM calls are in flight). `run_fn` is called with
    one argument, `on_progress(step_index, total_steps, step_label)`, which it
    should invoke before each of its own sequential steps. Returns the job_id
    immediately — the actual work has not finished (or necessarily started) by
    the time this function returns."""
    job_id = database.create_job(agent_name, total_steps=total_steps)

    def on_progress(step_index: int, total: int, step_label: str) -> None:
        database.update_job(job_id, status="running", current_step=step_index, total_steps=total, step_label=step_label)

    async def _runner() -> None:
        database.update_job(job_id, status="running", step_label="Starting")
        try:
            result = await asyncio.to_thread(run_fn, on_progress)
            database.update_job(job_id, status="completed", result=result)
        except Exception as e:
            # Imported lazily to avoid a circular import (error_utils imports from
            # llm_gateway, which this module doesn't otherwise need).
            from backend.services.error_utils import format_error_response
            logger.exception("Job %s (agent=%s) failed", job_id, agent_name)
            status_code, body = format_error_response(e, agent_name=agent_name)
            database.update_job(job_id, status="failed", error={"status_code": status_code, **body})

    asyncio.create_task(_runner())
    return job_id


def get_job_status(job_id: str) -> Optional[Dict[str, Any]]:
    return database.get_job(job_id)
