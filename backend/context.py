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

__all__ = ["bind_project", "current_project_id", "current_project_name",
           "current_product_id", "collect_saved_reviews", "note_review_saved",
           "saved_review_ids"]

_project_id: ContextVar[Optional[str]] = ContextVar("current_project_id", default=None)
_project_name: ContextVar[str] = ContextVar("current_project_name", default="")

# Which *product* is running, for the same reason and by the same route as the project.
#
# `review_type` is too coarse to identify a screen's own history: "code" is written by
# `orchestrator.review_code`, by `workflows/review_pr.py` and by `workflows/autofix_pr.py`,
# which are three different products on three different screens — so a per-agent run list
# filtered on review_type showed all three everywhere. The product id is known at
# `agent_guard.guarded_call`, which already opens `bind_project`, so it costs one more
# contextvar and no new call sites.
_product_id: ContextVar[Optional[str]] = ContextVar("current_product_id", default=None)


@contextmanager
def bind_project(project_id: Optional[str], project_name: str = "",
                 product_id: Optional[str] = None):
    """Mark everything done inside this block as belonging to `project_id`.

    Binding None is meaningful and must still be done: it says "this work has no project",
    which stops a previous request's value leaking into one that isn't bound to anything.
    The same applies to `product_id`.
    """
    id_token = _project_id.set(project_id or None)
    name_token = _project_name.set(project_name or "")
    product_token = _product_id.set(product_id or None)
    try:
        yield
    finally:
        _project_id.reset(id_token)
        _project_name.reset(name_token)
        _product_id.reset(product_token)


def current_project_id() -> Optional[str]:
    return _project_id.get()


def current_project_name() -> str:
    return _project_name.get()


def current_product_id() -> Optional[str]:
    return _product_id.get()


# ── Which reviews this request produced, reported back up ────────────────────
#
# Everything above travels *down*: the boundary knows the project and publishes it so
# `save_review` can read it. This travels the other way. The cost of a run is only known
# once the model calls have finished, which is after `save_review` has already written and
# serialized the row — so the review could never carry its own cost, and reopening a run
# from history showed findings with no indication of what they cost. The ledger row in
# `usage_events` has a `review_id` column for exactly this link, and on the synchronous
# routes it was always NULL because the middleware doing the metering never learns which
# review the handler wrote.
#
# **A mutable list, not a value.** A `set()` from inside a copied context does not
# propagate back to the parent, and a synchronous FastAPI endpoint runs in a worker thread
# with a *copy* of the caller's context — so a plain `ContextVar.set(review_id)` in
# `save_review` would be invisible to the middleware that needs it. Appending to a list the
# parent still holds a reference to is visible, because it is the same object.
# `llm_gateway.track_usage()` accumulates token counts across the same boundary the same
# way, and for the same reason.
_saved_reviews: ContextVar[Optional[list]] = ContextVar("saved_reviews", default=None)


@contextmanager
def collect_saved_reviews():
    """Collect the ids of every review saved inside this block, newest last.

    Deliberately separate from `bind_project`: that one marks work as belonging to a
    project, this one gathers a result. A caller that wants neither should open neither.
    """
    token = _saved_reviews.set([])
    try:
        yield
    finally:
        _saved_reviews.reset(token)


def note_review_saved(review_id: str) -> None:
    """Called by `save_review`. A no-op when nobody is collecting, which is most callers —
    the smoke suites, `scripts/`, and any code path that saves a review outside a request."""
    sink = _saved_reviews.get()
    if sink is not None and review_id:
        sink.append(review_id)


def saved_review_ids() -> list:
    """The reviews saved so far in the enclosing `collect_saved_reviews()` block."""
    return list(_saved_reviews.get() or [])
