"""Which project the work happening right now belongs to.

`database.save_review()` is called from 19 places across the orchestrator, the swarm and
the autofix workflow, none of which know anything about governance — they are handed
artifacts and asked to review them. The governing project is only known at the API
boundary, where the credential was resolved.

Threading a `project_id` parameter down through all of them (and through every orchestrator
method that calls them) would touch far more code than the change is worth, and would leave
19 chances to forget one — which is exactly how reviews came to be unattributed in the
first place. So the boundary publishes it here and `save_review` reads it, the same shape
as `llm_gateway.track_usage()`, which accumulates token counts across nested calls for the
same reason.

Lives in its own module with no imports from the rest of the package: `database` reads it
and `subject_service` sets it, and anything shared by those two cannot import either.

Context propagation is already handled everywhere this needs to reach:
`asyncio.to_thread` (background jobs) and `contextvars.copy_context()` (the swarm's
ThreadPoolExecutor) both carry the calling context into their worker threads.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Optional

__all__ = ["bind_project", "current_project_id", "current_project_name"]

_project_id: ContextVar[Optional[str]] = ContextVar("current_project_id", default=None)
_project_name: ContextVar[str] = ContextVar("current_project_name", default="")


@contextmanager
def bind_project(project_id: Optional[str], project_name: str = ""):
    """Mark everything done inside this block as belonging to `project_id`.

    Binding None is meaningful and must still be done: it says "this work has no project",
    which stops a previous request's value leaking into one that isn't bound to anything.
    """
    id_token = _project_id.set(project_id or None)
    name_token = _project_name.set(project_name or "")
    try:
        yield
    finally:
        _project_id.reset(id_token)
        _project_name.reset(name_token)


def current_project_id() -> Optional[str]:
    return _project_id.get()


def current_project_name() -> str:
    return _project_name.get()
