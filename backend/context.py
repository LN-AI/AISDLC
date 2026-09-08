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
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Optional

__all__ = ["bind_project", "current_project_id", "current_project_name",
           "current_product_id", "current_actor", "bind_source", "current_source_ref",
           "collect_saved_reviews", "note_review_saved", "saved_review_ids",
           "current_release_id"]

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

# *Who* is doing this, for the audit trail, by the same route and for the same reason.
#
# `save_review` writes a `review_created` audit row and had no actor to name, so it took
# the column default and every review in the trail was attributed to "system". On a
# governance product that is the one question the trail exists to answer, and it could not
# be answered from the trail at all -- only by joining `usage_events.caller_ref`, which no
# endpoint does. The boundary already knows the caller (`agent_guard._caller_identity`
# computes exactly this string), so publishing it here costs one contextvar and changes no
# call site of `save_review`.
_actor: ContextVar[str] = ContextVar("current_actor", default="")

# **Which release decision this work belongs to**, so a reviewer's queue is a list of
# releases rather than a list of findings-events.
#
# An approval used to be raised per *review*, and one Core SDLC Review saves several: the
# code review, the VAPT and the aggregate each hit `save_review`, each satisfied
# `requires_human_signoff`, and each queued its own sign-off request. A reviewer opened
# three items that were one decision -- "ship this or not" -- and the screen could not say
# which release any of them was about (C-37, C-39).
#
# **One API call is the release.** Both guards already open `bind_project` exactly once per
# call (`agent_guard.guarded_call` and `legacy_guard`), so binding the id here costs no new
# call sites and no threading through the 19 places that call `save_review` -- the same
# reasoning that put `project_id` here in the first place.
#
# Inherited rather than replaced when a block nests: an inner `bind_project` inside an
# outer one is still the same release, and minting a fresh id there would split one run
# back into the several rows this exists to merge.
_release_id: ContextVar[Optional[str]] = ContextVar("current_release_id", default=None)


@contextmanager
def bind_project(project_id: Optional[str], project_name: str = "",
                 product_id: Optional[str] = None, actor: str = ""):
    """Mark everything done inside this block as belonging to `project_id`.

    Binding None is meaningful and must still be done: it says "this work has no project",
    which stops a previous request's value leaking into one that isn't bound to anything.
    The same applies to `product_id` and to `actor` -- an empty actor means "we do not
    know who", which must not inherit the previous request's answer.
    """
    id_token = _project_id.set(project_id or None)
    name_token = _project_name.set(project_name or "")
    product_token = _product_id.set(product_id or None)
    actor_token = _actor.set(actor or "")
    # Note the `or`: an already-bound release wins. See the comment on `_release_id` --
    # nesting must not start a second release, or one run's reviews split across two
    # queue items again.
    release_token = _release_id.set(_release_id.get() or str(uuid.uuid4()))
    try:
        yield
    finally:
        _project_id.reset(id_token)
        _project_name.reset(name_token)
        _product_id.reset(product_token)
        _actor.reset(actor_token)
        _release_id.reset(release_token)


def current_project_id() -> Optional[str]:
    return _project_id.get()


def current_release_id() -> Optional[str]:
    """The release this work belongs to, or None outside any bound request.

    None is not an error: a review saved by a smoke script or a `scripts/` driver has no
    release, and `save_review` falls back to one-approval-per-review there, which is the
    old behaviour and the honest one -- nothing grouped them.
    """
    return _release_id.get()


def current_project_name() -> str:
    return _project_name.get()


def current_product_id() -> Optional[str]:
    return _product_id.get()


def current_actor() -> str:
    """Who the enclosing request belongs to, or "" when nothing bound one."""
    return _actor.get()


# ── Which repository this work is about ──────────────────────────────────────
#
# `reviews` had no repository, branch, commit or pull-request column, so no question about
# a *place* could be answered by a query: is this repo improving, what has it cost us, which
# of our repositories is worst. The facts were all present per run — `Bundle.meta` carries
# them and `review_sources.ref` stores one of them — and never promoted to a dimension.
#
# Denormalized onto `reviews` rather than joined from `review_sources`, deliberately:
# `review_sources` rows exist only for the two products that record a re-readable source
# (code and vapt), while `github_repo_url` is a declared source for eight products. Putting
# it on the row every product writes means a repository view covers whatever reviews a repo
# later, without a second migration.
#
# Same route as the project and the actor above: resolved at the boundary where the bundle
# exists, read inside `save_review`, so none of the 19 call sites change.
_source_ref: ContextVar[Optional[dict]] = ContextVar("current_source_ref", default=None)


@contextmanager
def bind_source(source_ref: Optional[dict]):
    """Mark everything inside this block as being about one repository/commit.

    Takes `Bundle.source_ref()` verbatim. Binding None is meaningful and must still be
    done — it says "this work is not about a repository", which stops the previous
    request's repository leaking onto a pasted snippet.
    """
    token = _source_ref.set(dict(source_ref) if source_ref else None)
    try:
        yield
    finally:
        _source_ref.reset(token)


def current_source_ref() -> dict:
    """The bound repository facts, or an empty dict when nothing bound any."""
    return dict(_source_ref.get() or {})


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
