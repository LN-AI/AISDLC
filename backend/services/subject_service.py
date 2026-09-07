"""What a call is governed against — the "billing subject".

Governance used to hang off the API key row: a plan, a budget and a set of quotas were
columns on `api_keys`, and every limit was looked up by `key_id`. A browser login has no
such row, so the guards short-circuited and the whole UI was ungoverned.

The subject fixes that by moving the allowance onto the **project** and making both
credentials resolve to one:

    API key  ──┐
               ├──► PROJECT ──► plan / budget / quotas / usage
    UI login ──┘

An API key carries its project already (`api_keys.project_id`). A browser session sends
the project the user picked in the header dropdown as `X-Project-Id`. Either way the same
project row is read, so a $500 cap covers what the pipeline spends *and* what an engineer
spends, and the usage ledger adds up to one number per customer.

Two subjects, not one
---------------------
A key caller resolves to BOTH a project and its own key row, and both are enforced. The
key's own plan and budget become a ceiling *inside* the project envelope, so an admin can
say "Acme has $500, and the CI bot may use at most $100 of it". This is also what makes
the change safe: limits intersect and never widen, so no caller that is restricted today
becomes less restricted (see entitlement_service.resolve_entitlements).

Binding is authorised
---------------------
A user may only bind to a project they are a **member** of, with super admins able to bind
anywhere. Otherwise picking someone else's unrestricted project would be a one-header
bypass of your own plan.

Membership, not administration: `database.list_member_project_ids` covers both people
added to the project and people who administer it. Working in a project and configuring
one are different rights, and requiring the second to get the first is what previously
forced every developer to be made a project admin.
"""
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from backend import config, database
from backend.services.auth_service import is_super_admin

logger = logging.getLogger(__name__)

PROJECT_HEADER = "x-project-id"

__all__ = [
    "Subject", "ProjectBindingError", "resolve_subject", "require_project",
    "project_warning", "subject_from_caller",
]


class ProjectBindingError(Exception):
    """The caller asked to act in a project they may not use, or none at all when the
    deployment requires one. Surfaced as 403 — the credential is valid, the scope isn't."""

    def __init__(self, message: str):
        super().__init__(message)


@dataclass
class Subject:
    """Everything a limit can be read from, for one request."""

    project_id: Optional[str] = None
    project_name: str = ""
    key_record: Optional[Dict[str, Any]] = None  # None for UI and anonymous callers
    user: Optional[Dict[str, Any]] = field(default=None, repr=False)  # decoded JWT

    @property
    def key_id(self) -> Optional[str]:
        return (self.key_record or {}).get("id")

    @property
    def governed(self) -> bool:
        """False only for a caller with neither a project nor a key — nothing to read a
        limit from, so it resolves to unrestricted exactly as the UI does today."""
        return bool(self.project_id or self.key_record)

    def describe(self) -> str:
        parts = []
        if self.project_id:
            parts.append(f"project={self.project_name or self.project_id}")
        if self.key_record:
            parts.append(f"key={self.key_record.get('id')}")
        return " ".join(parts) or "ungoverned"


EMPTY = Subject()


def _resolve_project_for_user(user: Dict[str, Any], project_id: str) -> Dict[str, Any]:
    project = database.get_project(project_id)
    if not project:
        raise ProjectBindingError(f"Project '{project_id}' does not exist.")
    if not project.get("is_active"):
        raise ProjectBindingError(f"Project '{project.get('name')}' is no longer active.")
    if not is_super_admin(user):
        allowed = database.list_member_project_ids(user.get("sub") or "")
        if project_id not in allowed:
            logger.warning(
                "Rejected project binding: user=%s project=%s not a member",
                user.get("username"), project_id,
            )
            raise ProjectBindingError(
                f"You do not have access to project '{project.get('name')}'."
            )
    return project


def resolve_subject(caller: Optional[dict], headers: Optional[Any] = None) -> Subject:
    """Work out what this request is governed against.

    `headers` is any mapping with a case-insensitive `.get()` — a Starlette `Headers` or a
    plain dict of lowercased names. It is only consulted for UI callers; an API key's
    project comes from its own row and can't be overridden by a header, which would
    otherwise let a key holder charge another customer's budget.
    """
    caller = caller or {}
    record = caller.get("_record")

    if record:
        project_id = record.get("project_id") or None
        project = database.get_project(project_id) if project_id else None
        return Subject(
            project_id=project_id,
            project_name=(project or {}).get("name", "") or caller.get("team", ""),
            key_record=record,
        )

    if caller.get("type") != "jwt":
        return EMPTY  # anonymous — only reachable with LEGACY_ROUTES_REQUIRE_AUTH=false

    user = caller.get("_user") or {}
    requested = ""
    if headers is not None:
        try:
            requested = (headers.get(PROJECT_HEADER) or "").strip()
        except Exception:  # a mapping that doesn't behave like Headers
            requested = ""

    if not requested:
        return Subject(user=user)

    project = _resolve_project_for_user(user, requested)
    return Subject(project_id=project["id"], project_name=project.get("name", ""), user=user)


def project_warning(subject: Subject) -> Optional[str]:
    """The sentence to put on a result that ran without a project, or None.

    Only speaks in `UI_PROJECT_MODE=warn` -- the middle state that makes enforcement
    shippable (C-80). `off` says nothing by design, and `enforce` never gets here because
    `require_project` has already refused the call.

    Deliberately states the consequence rather than suggesting a project. A grace period
    only ends if somebody acts during it, and "select a project" reads as a preference
    while "this run was not counted against any budget" reads as a problem.
    """
    if config.UI_PROJECT_MODE != "warn":
        return None
    if subject.project_id or subject.key_record:
        return None
    return (
        "This run was not attributed to a project, so no plan, quota or budget cap applied "
        "to it and its cost is recorded against nothing. Select a project in the sidebar. "
        "A future release will refuse unattributed runs."
    )


def require_project(subject: Subject) -> None:
    """Refuse an unbound session when `UI_PROJECT_MODE=enforce`, for calls that spend money.

    Deliberately separate from `resolve_subject`: resolution is pure and runs on read-only
    endpoints too (the product catalogue, key entitlements), where refusing an unbound UI
    session would break the very screens that let a user pick a project.

    In `warn` mode this returns normally and `project_warning` supplies the sentence that
    goes on the result instead. Enforcement stays here, and the warning stays out of it, so
    a caller cannot accidentally satisfy the check by rendering the warning.
    """
    if not config.UI_REQUIRE_PROJECT:
        return
    if subject.project_id or subject.key_record:
        return
    raise ProjectBindingError(
        "Select a project before running an agent — this deployment requires every call "
        "to be attributed to one."
    )


def subject_from_caller(caller: Optional[dict]) -> Subject:
    """Subject for a caller whose request headers aren't available — background jobs and
    the swarm runner, which are handed a caller dict that was already resolved upstream.

    A UI caller resolves without a project here, so nothing is charged to the wrong one.
    Where the project matters for a background job, stash the resolved subject on the
    caller as `_subject` at request time and this returns it unchanged.
    """
    if isinstance((caller or {}).get("_subject"), Subject):
        return caller["_subject"]
    return resolve_subject(caller, None)
