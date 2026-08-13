"""Which projects a caller may *read*.

The spend side of governance already has one answer to "whose work is this?" —
`subject_service.resolve_subject`, which binds the request before anything is charged. The
read side needs a different answer: not "which project pays for this call" but "which
projects' rows may this person see", which for a super admin is everything and for a user
with no selection is every project they belong to.

That logic lived in `main.py` and was therefore reachable only by routes defined in
`main.py` — `/api/reviews`, `/api/reports`, `/api/dashboard`. Any router that wanted the
same scoping had to reinvent it, and `routes/approvals.py` shows what happens when nobody
does: it lists every approval in the deployment to anyone holding any token.

Lives beside `context.py` with no imports from `backend.routes`, because the routers import
it and `main` imports the routers.
"""
from typing import Any, List, Optional

from backend import database
from backend.services.auth_service import is_super_admin

__all__ = ["visible_project_ids", "visible_from_caller"]

PROJECT_HEADER = "x-project-id"


def visible_project_ids(reader: dict, request: Any) -> Optional[List[str]]:
    """Which projects' rows this caller may read. `None` means every project.

    Selecting a project narrows these screens to it, for everyone. "Working in: Acme"
    has to mean the same thing on every screen, or the picker means one thing for spend
    and another for history.

    With nothing selected:
      * super admin — everything, including the unattributed rows from before reviews
        carried a project;
      * anyone else — every project they are a member of;
      * an API key — its own project, and only that, selected or not.

    A user who belongs to no project gets an empty list, which means no rows. That is the
    intended answer, not a bug: their work isn't attributed to anything, so there is
    nothing for them to be shown.
    """
    if reader.get("type") == "api_key":
        project_id = (reader.get("_record") or {}).get("project_id")
        return [project_id] if project_id else []

    user = reader.get("_user") or {}
    selected = ""
    if request is not None:
        try:
            selected = (request.headers.get(PROJECT_HEADER) or "").strip()
        except Exception:  # a caller handed us something that isn't a request
            selected = ""

    if is_super_admin(user):
        return [selected] if selected else None

    allowed = database.list_member_project_ids(user.get("sub") or "")
    if selected:
        # Not a member of it: show nothing rather than falling back to everything they
        # can see, which would quietly ignore a selection they can see on screen.
        return [selected] if selected in allowed else []
    return allowed


def visible_from_caller(caller: dict) -> Optional[List[str]]:
    """Same question, for a `v1_agents.get_caller` dict whose request is out of reach.

    `get_caller` has already resolved the `X-Project-Id` header into `_subject`, so the
    selection is available without the request — but an unbound *user* session still has to
    fall back to their memberships rather than to "everything", which is the difference
    between this and reading `_subject.project_id` directly.
    """
    subject = caller.get("_subject")
    project_id = getattr(subject, "project_id", None)
    if caller.get("type") == "api_key":
        return [project_id] if project_id else []

    user = caller.get("_user") or {}
    if is_super_admin(user):
        return [project_id] if project_id else None
    if project_id:
        return [project_id]          # already membership-checked by resolve_subject
    return database.list_member_project_ids(user.get("sub") or "")
