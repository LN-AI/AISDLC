"""The observation/action contract for application-based testing.

D-62 established that *the results contract is the interface, not the producer* -- one JUnit
endpoint, and the customer's CI, a local script and a future sandbox worker are three
interchangeable things that fill it. This module applies the same move one level further
out, to the exploring rather than the reporting:

    **The observation/action contract is the interface, not the browser.**

A caller sends what a page currently offers and receives one action to take. That is the
whole protocol. Where the browser runs is then a deployment choice rather than an
architecture: today it is `scripts/explore_app.py` on somebody's machine, tomorrow it could
be a worker we operate for publicly reachable applications, and neither requires this file
to change. It is also why nothing here touches D-05 -- no browser is launched on the API
host, because no browser is launched by this process at all.

Three consequences of that shape are worth stating, because they are the reasons to prefer
it over hosting a browser fleet first:

* **There is no SSRF surface.** This platform never fetches the customer's URL and never
  learns it. A guard like `notify._refuse_private` is what a hosted runner would need; a
  contract that never makes the request needs nothing.
* **Credentials never arrive here.** The driver holds them because the driver opens the
  connection. Nothing in this module accepts a password.
* **An application behind a VPN works on day one**, because the browser is already inside
  the network. A hosted fleet could never reach it.

Metering is per step, not per session, which is the opposite of the rule for every other
product on the `/api/v1` surface and is the same exception modernization makes
(`modernization/runner.py:17-24`): a session is not one request. It is dozens, spread over
minutes, that an operator may abandon halfway -- and a reservation held across all of them
would bill for steps nobody took.
"""
import logging
import re
import time
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend import database, scope
from backend.routes.v1_agents import _effective_llm_override, _orch, get_caller
from backend.services import (
    action_policy,
    app_basis,
    agent_guard,
    app_session,
    review_estimate,
)
from backend.services.agent_guard import BudgetExceededError, ScopeDeniedError
from backend.services.app_session import SessionError
from backend.services.subject_service import (
    ProjectBindingError,
    require_project,
    subject_from_caller,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/app-testing", tags=["Application testing"])

# How much of the brief the screen may show back. The whole thing can be 40 000 chars
# (`app_basis.MAX_BRIEF_CHARS`) and this response is drawn on every add and remove.
BRIEF_PREVIEW_CHARS = 2_000

# A screen watching a live session reads this route every 2.5 seconds, and the read path is
# where the page-text sweep runs because there is no scheduler. Unthrottled that is 24 table
# scans a minute per watcher. A retention promise that holds to the minute is the same
# promise, and this keeps the poll from becoming a load generator.
SWEEP_MIN_INTERVAL_S = 60.0
_last_sweep = 0.0


def _sweep_if_due(conn) -> None:
    global _last_sweep
    now = time.monotonic()
    if now - _last_sweep < SWEEP_MIN_INTERVAL_S:
        return
    _last_sweep = now
    app_session.sweep_expired_page_text(conn)

PRODUCT = "app-testing"

# Roughly what one step's prompt costs, in characters, for the free quote. A page's
# accessibility tree is the bulk of it. Measured against nothing yet -- it is a quote, it is
# labelled as one, and `eval_app_testing.py` is what will replace the guess with a number.
ASSUMED_STEP_CHARS = 6_000


# ── Request models ────────────────────────────────────────────────────────

class Candidate(BaseModel):
    """One thing the page offers, as the driver read it from the accessibility tree."""
    role: str = Field("", description="The ARIA role Playwright reported, e.g. 'button'.")
    name: str = Field("", description="The accessible name, e.g. 'Save changes'.")
    value: str = Field("", description="Current value, for inputs. Never send a password.")
    enabled: bool = Field(True, description="False for a disabled control.")
    submits: str = Field(
        "",
        description="For a control that submits a form: the method and same-origin path it "
                    "sends, e.g. 'POST /tasks/1/edit'. A path, never an absolute URL — this "
                    "service is not told where the application lives. Anything else is "
                    "dropped rather than stored.",
        examples=["POST /tasks/1/edit"],
    )
    locator: str = Field(
        "",
        description="How Playwright addresses this element, worked out by the driver from "
                    "what the element actually is — `getByRole(...)` where the element has "
                    "that role in the accessibility tree, `getByText(...)` where it only "
                    "behaves like a control. Carried so the generated suite targets "
                    "something observed instead of assumed.",
        examples=["getByRole('button', { name: 'Save changes' })"],
    )


class SessionRequest(BaseModel):
    target_label: str = Field(
        "",
        description="A hostname, for telling sessions apart. Never send the full URL: this "
                    "service does not need it and does not store it.",
        examples=["staging.example.com"],
    )
    goal: str = Field(
        "", description="What this session should try to cover, in a sentence.",
        examples=["Cover signing in, searching, and adding an item to the cart."],
    )
    requirement: str = Field(
        "",
        description="Optional. The behaviour the application is supposed to have. Supplying "
                    "it is the difference between tests that check a requirement and tests "
                    "that agree with whatever the application does today.",
    )
    max_steps: int = Field(
        app_session.DEFAULT_MAX_STEPS,
        description=f"Step budget. Clamped to {app_session.MAX_STEPS_CEILING}. Every step is "
                    f"a model call, so this is what makes a session's cost knowable.",
    )
    source_review_ids: List[str] = Field(
        default_factory=list,
        description="Optional. Finished Test Scenario or Test Case runs whose output this "
                    "session should try to cover. Use it when those agents have already run "
                    "for this system; most sessions will not have them, and that is fine — "
                    "a typed requirement or project documents do the same job. What it "
                    "changes is honesty: a test tracing to a generated case is labelled "
                    "'derived', not 'stated', because the platform wrote that case.",
    )
    collect_page_text: bool = Field(
        True,
        description="Whether the explorer may send a digest of what each page displays. "
                    "Leave it on unless the target holds data you would rather this service "
                    "never saw: with it off, a test can check that a control is present and "
                    "cannot check that a value is correct, because the value was never "
                    "observed. The finished suite says which of the two it is.",
    )
    page_text_retention_days: int = Field(
        app_session.DEFAULT_PAGE_TEXT_RETENTION_DAYS,
        description="How long that digest is kept, encrypted, after the suite is written. "
                    f"Clamped to {app_session.MAX_PAGE_TEXT_RETENTION_DAYS}. **0 wipes it "
                    "the moment the suite exists**, which is the tightest setting that still "
                    "produces tests about values.",
    )
    attested_test_env: bool = Field(
        False,
        description="Required. Confirms the target is a test environment, not production. "
                    "This explorer takes real actions in a real system.",
    )
    project_id: Optional[str] = Field(None, description="Defaults to the caller's project.")
    llm_connection_id: Optional[str] = None


class StepRequest(BaseModel):
    url_path: str = Field("/", description="Path only, never the full URL.")
    page_title: str = Field("", description="The document title, for orientation.")
    candidates: List[Candidate] = Field(
        default_factory=list,
        description="Everything interactive on the page. Send them all -- the dangerous ones "
                    "are removed here, before the model sees them, and counted for the "
                    "session record.",
    )
    page_texts: List[str] = Field(
        default_factory=list,
        description="What the page says, as short landmark lines — headings, live regions, "
                    "identified short text. Without it an explorer can only assert which "
                    "controls exist, and most defects show as wrong text rather than a "
                    "missing control. Capped on arrival; send no more than a page's "
                    "landmarks and never its whole body.",
        examples=[["Your basket", "Total: GBP 8.00"]],
    )
    last_outcome: str = Field("", description="What happened when the previous action ran.")
    credentials_note: str = Field(
        "",
        description="How to sign in, e.g. a username and which field it goes in. Passed to "
                    "the model for this step only and never stored.",
    )
    llm_connection_id: Optional[str] = None


class FinishRequest(BaseModel):
    stop_reason: str = Field("", description="Why the session ended.")
    last_error: str = Field("", description="If it ended badly, what went wrong.")


class GenerateRequest(BaseModel):
    context: str = Field("", description="Anything else the suite writer should know.")
    project_name: str = Field("", description="What to file the resulting run under.")
    llm_connection_id: Optional[str] = None


class PreflightRequest(BaseModel):
    max_steps: int = Field(app_session.DEFAULT_MAX_STEPS)
    requirement: str = Field("")
    goal: str = Field("")
    llm_connection_id: Optional[str] = None


class AllowanceRequest(BaseModel):
    """One control name a person is permitting for a project."""
    control_name: str = Field(..., min_length=1, max_length=120)
    project_id: str = ""


class BasisRequest(BaseModel):
    """What a session would have to check the application against, before it opens."""
    requirement: str = Field("")
    project_id: Optional[str] = None
    source_review_ids: List[str] = Field(default_factory=list)


# ── Helpers ───────────────────────────────────────────────────────────────

def _project_for(caller: dict, requested: Optional[str]) -> Optional[str]:
    """Which project a session may be filed under.

    Same rule as `github_credentials._project_for`: a session drives a browser against
    somebody's system, so it cannot be filed against a project the caller cannot already
    see. 404 rather than 403, because naming a project that exists is itself a disclosure.
    """
    visible = scope.visible_from_caller(caller)
    if requested:
        if visible is not None and requested not in visible:
            raise HTTPException(404, f"No project with id '{requested}'.")
        return requested
    try:
        return subject_from_caller(caller).project_id
    except Exception:
        return None


def _visible_session(session_id: str, caller: dict) -> dict:
    """Fetch a session the caller may act on, or 404.

    404 for "not yours" as well as "not found", deliberately: distinguishing them tells an
    unauthorised caller that a given session id is real.
    """
    conn = database.get_connection()
    try:
        session = app_session.get(conn, session_id)
    finally:
        conn.close()
    if not session:
        raise HTTPException(404, f"No exploration session with id '{session_id}'.")
    visible = scope.visible_from_caller(caller)
    if visible is not None and session.get("project_id") not in visible:
        raise HTTPException(404, f"No exploration session with id '{session_id}'.")
    return session


def _history(steps: List[dict], limit: int = 12) -> str:
    """The last few steps, as one line each.

    Capped rather than complete: the history is model input that grows with every step, so
    an uncapped one makes step forty cost several times what step two cost, for context the
    model mostly does not use. The journey names carry the structure that matters.
    """
    lines = []
    for step in steps[-limit:]:
        action = step.get("action") or {}
        kind = action.get("action_kind", "?")
        detail = action.get("target_name") or action.get("value") or ""
        lines.append(
            f"{step.get('step_index')}. [{step.get('journey', '')}] {kind} {detail} "
            f"on {step.get('url_path', '')} -> {step.get('outcome', '') or 'ok'}"
        )
    return "\n".join(lines)


# A submit target is `METHOD /path`, and nothing else is stored. The driver is trusted to
# observe the page; it is not trusted to decide what this service may hold. An absolute URL
# would carry the origin, which D-407 says this side never learns -- so one is dropped
# rather than trimmed, because a value that arrived in the wrong shape is a bug somewhere
# and silently repairing it hides the bug.
_SUBMIT_TARGET = re.compile(r"^(GET|POST|PUT|PATCH|DELETE) (/[^\s?#]{0,180}(?:\?[^\s#]{0,60})?)$")


def _submit_target(raw: str) -> str:
    match = _SUBMIT_TARGET.match(str(raw or "").strip())
    return match.group(0) if match else ""


# A locator is advisory text for the suite writer -- nothing here or in the driver ever
# executes it, because an action is carried out against the stamped candidate and not against
# a string. It is still page-controlled input on its way into a prompt, so it is accepted only
# in the two shapes the driver produces and dropped otherwise.
_LOCATOR = re.compile(r"^getBy(Role|Text|Label)\(.{1,240}\)$", re.S)


def _locator(raw: str) -> str:
    text = " ".join(str(raw or "").split())
    return text if _LOCATOR.match(text) else ""


def _journeys_markdown(steps: List[dict]) -> str:
    """The session, rendered for the suite writer.

    Grouped by journey rather than listed chronologically, because a journey is what becomes
    a test file. Each step carries the role and accessible name it acted on, which is what
    lets the suite be written in durable locators instead of invented ones.
    """
    grouped: dict = {}
    order: List[str] = []
    for step in steps:
        name = step.get("journey") or "Unlabelled"
        if name not in order:
            order.append(name)
            grouped[name] = []
        grouped[name].append(step)

    out = []
    for name in order:
        out.append(f"### Journey: {name}")
        for step in grouped[name]:
            action = step.get("action") or {}
            bits = [f"- `{action.get('action_kind', '?')}`"]
            if action.get("target_role") or action.get("target_name"):
                bits.append(
                    f"{action.get('target_role', '')} named "
                    f"\"{action.get('target_name', '')}\""
                )
            if action.get("value"):
                bits.append(f"with value `{action['value']}`")
            bits.append(f"on `{step.get('url_path', '')}`")
            if action.get("locator"):
                # Worked out from the element itself rather than from its role alone. A nav
                # item that is an `<li>` with a click handler has no role in the
                # accessibility tree, so `getByRole('button', ...)` would match nothing --
                # this says what does match.
                bits.append(f"— address it with `page.{action['locator']}`")
            if action.get("submits"):
                # The request this control actually sends. Without it a test for "the server
                # refuses this" has to invent a path, which is the guessing D-408 designs
                # out; with it, the request can be written from something that was observed.
                bits.append(f"— sends `{action['submits']}`")
            if action.get("rationale"):
                bits.append(f"— expected: {action['rationale']}")
            if step.get("outcome"):
                bits.append(f"— observed: {step['outcome']}")
            out.append(" ".join(bits))
            if step.get("page_texts"):
                # What the page said when this step was taken. This is where an assertion
                # about a *value* comes from: without it a suite can only check which
                # controls exist, and "the total is wrong" looks identical to "the total is
                # right" when all you can see is buttons.
                shown = "; ".join(str(t) for t in step["page_texts"][:8])
                out.append(f"  page showed: {shown}")
        out.append("")
    return "\n".join(out).strip()


def _quote(max_steps: int, requirement: str, goal: str, model: str = "") -> dict:
    """Price a whole session before it starts.

    Two different calls with two different shapes, so the two quotes are summed with
    `review_estimate.combine` -- the helper that exists because Apply Fixes bills a fix call
    and a re-review call against different agents, and pricing both off one would quote a
    prompt that is never sent. Here it is N small step calls plus one large generation.

    The step cap is the only reason this is quotable at all. An unbounded exploration has no
    ceiling, and a quote without a ceiling is not a quote.
    """
    steps = app_session.normalise_max_steps(max_steps)
    step_quote = review_estimate.for_text(
        "x" * ASSUMED_STEP_CHARS, agent="app-testing-step", model=model,
        extra_calls=max(0, steps - 1),
    )
    suite_quote = review_estimate.for_text(
        (requirement or "") + (goal or "") + "x" * 4_000, agent=PRODUCT, model=model,
    )
    combined = review_estimate.combine(step_quote, suite_quote)
    combined["max_steps"] = steps
    return combined


# ── Routes ────────────────────────────────────────────────────────────────

@router.post("/sessions", summary="Open an exploration session (free, no model call)")
def open_session(req: SessionRequest, caller: dict = Depends(get_caller)):
    """Start a session. Free: nothing is generated until the first step.

    The attestation is checked in `app_session.create` before anything else, because it is
    the one condition here that is about somebody else's data rather than about bookkeeping.
    """
    subject = subject_from_caller(caller)
    try:
        agent_guard.check_scope(subject, PRODUCT)
    except ScopeDeniedError as exc:
        raise HTTPException(403, str(exc))

    # Opening a session spends nothing, so by the ordinary rule `require_project` would not
    # apply to it -- it is reserved for calls that cost money. It applies here anyway,
    # because a session is not a read: it is the door to a flow in which *every* subsequent
    # call is a paid one. Without this the deployment that requires project binding lets a
    # session open, prints a cost estimate, and then refuses all forty steps -- which is how
    # a live run against a real application actually failed, after the browser had started.
    # Failing at the door costs an error message; failing after it costs the run.
    try:
        require_project(subject)
    except ProjectBindingError as exc:
        raise HTTPException(403, str(exc))

    project_id = _project_for(caller, req.project_id)

    # What this session can check the application against, resolved before it opens. Three
    # places it can come from and the session needs at least one: without any, the suite can
    # only describe what it saw, which is the "green suite that proves nothing" this product
    # most has to avoid. Resolved here rather than at generation time so the refusal costs an
    # error message instead of a whole paid run.
    basis = app_basis.collect(
        requirement=req.requirement,
        project_id=project_id,
        source_review_ids=req.source_review_ids,
        visible_project_ids=scope.visible_from_caller(caller),
    )
    basis["refusal"] = app_basis.NOTHING_TO_CHECK_AGAINST

    conn = database.get_connection()
    try:
        session = app_session.create(
            conn,
            project_id=project_id,
            target_label=req.target_label,
            goal=req.goal,
            requirement=req.requirement,
            max_steps=req.max_steps,
            attested_test_env=req.attested_test_env,
            created_by=(caller.get("username") or caller.get("key_name") or "api"),
            collect_page_text=req.collect_page_text,
            page_text_retention_days=req.page_text_retention_days,
            basis=basis,
        )
    except SessionError as exc:
        raise HTTPException(400, str(exc))
    finally:
        conn.close()

    # Frozen onto the session, and handed back, so the driver screens to the identical
    # list rather than looking one up for itself.
    conn = database.get_connection()
    try:
        permitted = sorted(app_session.allowance_names(conn, project_id))
        app_session.record_allowances(conn, session["id"], permitted)
    finally:
        conn.close()

    policy = dict(action_policy.describe_policy())
    policy["permitted"] = permitted
    if permitted:
        policy["permitted_note"] = (
            "Someone permitted these control names for this project, so the usual refusal "
            "does not apply to them here. Screen with exactly this list: the platform "
            "chooses an index into it, and a different list means a different element.")

    return {
        "session": app_session_public(session["id"]),
        "policy": policy,
        "estimate": _quote(session["max_steps"], req.requirement, req.goal),
        "note": (
            "This service never receives the application's URL or its credentials. Send "
            "page observations and it will answer with one action at a time."
        ),
    }


def app_session_public(session_id: str) -> Optional[dict]:
    conn = database.get_connection()
    try:
        return app_session.public(conn, session_id)
    finally:
        conn.close()


@router.post("/sessions/{session_id}/step", summary="Send an observation, receive one action")
def take_step(session_id: str, req: StepRequest, caller: dict = Depends(get_caller)):
    """One decision, metered on its own.

    The order here is the safety design and should not be rearranged: candidates are
    screened *before* the model is called, the model chooses only from what survived, and
    its choice is screened *again* on the way out. The driver then screens a third time
    before touching the browser, because that is the copy running where the consequences
    are.
    """
    _visible_session(session_id, caller)
    override = _effective_llm_override(req, caller, PRODUCT)

    conn = database.get_connection()
    try:
        # A stop asked for from a screen is collected here, before `require_open` and before
        # anything is metered. Both orderings matter. `require_open` raises a 400 and the
        # driver turns any status >= 400 into SystemExit, losing the session; `done` is the
        # clean exit it already handles, after which it still calls /finish and /generate,
        # so the steps already paid for still become a suite. And being ahead of
        # `guarded_call` is what makes the stopping step free.
        stopping = app_session.get(conn, session_id)
        if stopping is not None and app_session.stop_requested(stopping)                 and str(stopping["status"] or "") == "exploring":
            app_session.finish(conn, session_id, status="finished",
                               stop_reason="stopped from the platform")
            return {
                "done": True,
                "reason": "stopped from the platform",
                "session": app_session.public(conn, session_id),
            }

        try:
            session = app_session.require_open(conn, session_id)
        except SessionError as exc:
            raise HTTPException(400, str(exc))

        raw = [c.model_dump() for c in req.candidates]
        # Dropped here rather than trusted from the driver. A customer who turned this off
        # is making a statement about what this service may hold, and a setting a client can
        # defeat by sending the field anyway is not a setting.
        texts = (app_session.clip_texts(req.page_texts)
                 if session.get("collect_page_text", 1) else [])
        # Read off the *session*, never off the project.
        #
        # The driver screens the same candidates again to resolve the index it is handed,
        # so the two lists have to be identical or the index points at something nobody
        # chose. Taking it from the project would mean somebody adding an allowance halfway
        # through a session silently shifts every subsequent index under a driver still
        # using the list it was given at the start. Frozen at open, both sides agree for
        # the whole session, and `permits` cross-checks the name as a backstop.
        allow = set(app_session.allowances_applied(session))
        allowed, refused = action_policy.screen(raw, allow)
        signature = action_policy.page_signature(req.url_path, raw, texts)

        # The cheap stopping rule, evaluated before spending anything: if the last several
        # steps and this page are all the same signature, the session has stopped finding
        # anything and another model call would buy nothing.
        if app_session.stalled(conn, session_id, signature):
            app_session.finish(
                conn, session_id, status="finished",
                stop_reason=f"no new page in {app_session.NO_PROGRESS_LIMIT} steps")
            return {
                "done": True,
                "reason": (
                    f"The last {app_session.NO_PROGRESS_LIMIT} steps reached no page this "
                    f"session had not already seen, so exploring further would add nothing."
                ),
                "session": app_session.public(conn, session_id),
            }

        history = _history(app_session.steps(conn, session_id))
        step_index = session["steps_used"] + 1
        refused_note = "; ".join(
            r.get("refused_because", "") for r in refused[:10] if r.get("refused_because")
        )
        observation_chars = min(
            app_session.MAX_OBSERVATION_CHARS,
            sum(len(str(c)) for c in raw) + sum(len(t) for t in texts),
        )
    finally:
        conn.close()

    try:
        decision = agent_guard.guarded_call(
            caller, PRODUCT, override,
            lambda: _orch.app_explorer_agent.next_action(
                goal=session.get("goal", ""),
                url_path=req.url_path,
                page_title=req.page_title,
                candidates=allowed,
                page_texts=texts,
                history=history,
                requirement=session.get("requirement", ""),
                refused_note=refused_note,
                credentials_note=req.credentials_note,
                llm_override=override,
            ),
        )
    except BudgetExceededError as exc:
        raise HTTPException(402, str(exc))
    except ScopeDeniedError as exc:
        raise HTTPException(403, str(exc))

    budget = decision.get("_budget_stats")

    if decision.get("done"):
        conn = database.get_connection()
        try:
            app_session.finish(conn, session_id, status="finished",
                               stop_reason=(decision.get("done_reason") or "")[:200])
            public = app_session.public(conn, session_id)
        finally:
            conn.close()
        return {"done": True, "reason": decision.get("done_reason", ""),
                "session": public, "_budget_stats": budget}

    action = {
        "action_kind": decision.get("action_kind"),
        "candidate_index": decision.get("candidate_index"),
        "value": decision.get("value", ""),
        "journey": decision.get("journey", ""),
        "rationale": decision.get("rationale", ""),
    }
    # With the same list `screen` used. A re-check under different rules refuses exactly
    # what it has just offered, which is not a safety property -- it is the policy
    # disagreeing with itself one line later.
    ok, reason = action_policy.permits(action, allowed, allow)
    if not ok:
        # Refused rather than corrected. Picking a different element on the model's behalf
        # is how an explorer ends up doing something nobody can account for afterwards; the
        # driver is told to observe again and the step is not charged to the step budget.
        logger.warning("app-testing: refused a chosen action (%s)", reason)
        return {
            "done": False, "refused": True, "reason": reason,
            "retry": True, "_budget_stats": budget,
        }

    chosen = allowed[action["candidate_index"]] if isinstance(
        action.get("candidate_index"), int) and action["action_kind"] not in ("back", "goto") \
        else {}
    action["target_role"] = chosen.get("role", "")
    action["target_name"] = chosen.get("name", "")
    action["submits"] = _submit_target(chosen.get("submits", ""))
    action["locator"] = _locator(chosen.get("locator", ""))

    conn = database.get_connection()
    try:
        app_session.record_step(
            conn, session_id,
            step_index=step_index,
            journey=action["journey"],
            url_path=req.url_path,
            page_signature=signature,
            action=action,
            observation_chars=observation_chars,
            candidates_offered=len(allowed),
            candidates_refused=len(refused),
            page_texts=texts,
            outcome=req.last_outcome,
        )
        # What was withheld, by name and group, on the session rather than per step. The
        # counts were always kept and the names always thrown away, which left the record
        # able to say "14 controls declined" and unable to say which -- and which is the
        # half that tells an operator whether the denylist is too tight for their
        # application. Bounded by distinct name, so a loop cannot grow it.
        app_session.record_refusals(conn, session_id, refused)
        public = app_session.public(conn, session_id)
    finally:
        conn.close()

    return {
        "done": False,
        "action": action,
        "withheld": len(refused),
        "session": public,
        "_budget_stats": budget,
    }


@router.post("/sessions/{session_id}/finish", summary="Close a session (free)")
def finish_session(session_id: str, req: FinishRequest, caller: dict = Depends(get_caller)):
    _visible_session(session_id, caller)
    conn = database.get_connection()
    try:
        status = "failed" if req.last_error else "finished"
        app_session.finish(conn, session_id, status=status,
                           stop_reason=req.stop_reason, last_error=req.last_error)
        return {"session": app_session.public(conn, session_id)}
    finally:
        conn.close()


@router.post("/sessions/{session_id}/generate", summary="Write a Playwright suite from a session")
def generate_suite(session_id: str, req: GenerateRequest, caller: dict = Depends(get_caller)):
    """The one large call, at the end. Produces an ordinary `reviews` row.

    That row is what puts this product on a screen, through `deliver-tests` and into the
    results contract with no special case anywhere -- the suite it holds has exactly the
    shape the code-based generator produces, plus the assertion-basis labels.
    """
    session = _visible_session(session_id, caller)
    override = _effective_llm_override(req, caller, PRODUCT)

    conn = database.get_connection()
    try:
        recorded = app_session.steps(conn, session_id)
    finally:
        conn.close()

    if not recorded:
        raise HTTPException(
            400,
            "This session took no steps, so there is nothing to write a suite from. A suite "
            "generated from an empty session would be invented rather than observed.",
        )

    # A second generation writes a second `reviews` row and repoints `review_id` at it,
    # orphaning the first -- a suite somebody may already have delivered to a repository,
    # now unreachable from the session that produced it. Harmless while nothing in the
    # browser could call this route; the screen now offers it for abandoned sessions, so it
    # is refused here rather than left to the UI being careful.
    if session.get("review_id"):
        raise HTTPException(
            400,
            "This session has already written a suite. Open it rather than generating a "
            "second one - a second generation would leave the first unreachable from this "
            "session, including one that has already been delivered to a repository.",
        )

    try:
        result = agent_guard.guarded_call(
            caller, PRODUCT, override,
            lambda: _orch.generate_app_suite(
                journeys_markdown=_journeys_markdown(recorded),
                goal=session.get("goal", ""),
                requirement=session.get("requirement", ""),
                target_label=session.get("target_label", ""),
                context=req.context or "",
                project_name=(req.project_name or session.get("target_label")
                              or "Unnamed Project"),
                llm_override=override,
                session={"session_id": session_id,
                         "steps": len(recorded),
                         "target_label": session.get("target_label", ""),
                         "collect_page_text": bool(session.get("collect_page_text", 1)),
                         # Re-resolved from what the session recorded, rather than carried in
                         # memory: a session is a conversation spread over minutes and the
                         # process that generates may not be the one that opened it.
                         "basis": app_basis.collect(
                             requirement=session.get("requirement", ""),
                             project_id=session.get("project_id"),
                             source_review_ids=app_session.source_review_ids(session),
                             visible_project_ids=scope.visible_from_caller(caller),
                         )},
            ),
        )
    except BudgetExceededError as exc:
        raise HTTPException(402, str(exc))
    except ScopeDeniedError as exc:
        raise HTTPException(403, str(exc))
    except RuntimeError as exc:
        # `_refuse_degenerate` raises this. A 400 rather than a 500: the run happened, the
        # response was empty or malformed, and the caller can act on the sentence it carries.
        raise HTTPException(400, str(exc))

    conn = database.get_connection()
    try:
        app_session.attach_review(conn, session_id, result.get("review_id"))
        # The digest has now done its work, so the clock starts here rather than at `finish`
        # -- and at a retention of 0 this is where it is wiped, which is the whole point of
        # that setting being 0 rather than "off".
        app_session.start_page_text_retention(conn, session_id)
        public = app_session.public(conn, session_id)
    finally:
        conn.close()

    result["session_detail"] = public
    return result


@router.get("/sessions/{session_id}", summary="A session's progress, journeys and refusals")
def read_session(session_id: str, since_step: int = 0, caller: dict = Depends(get_caller)):
    """One session, with its steps.

    `since_step` returns only steps after that index. Steps are append-only and immutable,
    so a screen watching a live session accumulates them and each poll then carries the
    session row plus at most one new step -- rather than re-sending every page digest every
    two and a half seconds, which is what makes watching affordable at all.
    """
    _visible_session(session_id, caller)
    conn = database.get_connection()
    try:
        # Swept on the read path as well as at startup, for the reason the header of
        # `notify.py` gives about this deployment having no scheduler: a process that has
        # been up for three weeks would otherwise still be holding digests it promised to
        # delete after seven days. This is the read that depends on them, so this is where
        # it belongs.
        _sweep_if_due(conn)
        rows = app_session.steps(conn, session_id)
        if since_step > 0:
            rows = [r for r in rows if int(r.get("step_index") or 0) > since_step]
        return {
            "session": app_session.public(conn, session_id),
            "steps": rows,
            # Every age on screen is measured against this rather than the browser's clock,
            # so a machine three minutes fast does not report "last step -180s ago".
            "now": datetime.now(timezone.utc).isoformat(),
            "policy": action_policy.describe_policy(),
        }
    finally:
        conn.close()


@router.get("/sessions", summary="Sessions for this project, newest first (free)")
def list_sessions(limit: int = 20, offset: int = 0, status: str = "",
                  caller: dict = Depends(get_caller)):
    """The list the browser had no way to ask for.

    Until now the only way to find a past session was the `reviews` row that `/generate`
    writes -- so a session that never produced a suite was invisible by construction, even
    though its steps were paid for. That is the case a person most needs to see.

    Free, and scoped the same way `GET /api/reviews` is: `visible_from_caller` returns
    `None` for unrestricted, `[]` for nothing, and `[]` must never widen to everything.
    """
    conn = database.get_connection()
    try:
        # No scheduler in this deployment, so time-based work runs at startup *and* on the
        # read path that depends on it -- the rule `lapse_expired_waivers` and
        # `review_source_store.sweep_expired` already follow. This is the read that depends
        # on it: a session whose driver stopped talking to us must show as closed here.
        app_session.reap_abandoned(conn)
        visible = scope.visible_from_caller(caller)
        rows = app_session.list_sessions(conn, project_ids=visible, limit=min(limit, 100),
                                         offset=offset, status=status)
        return {
            "sessions": rows,
            "total": app_session.count_sessions(conn, project_ids=visible, status=status),
            "limit": limit,
            "offset": offset,
            "live_count": app_session.count_sessions(conn, project_ids=visible,
                                                     status="exploring"),
        }
    finally:
        conn.close()


@router.post("/sessions/{session_id}/request-stop",
             summary="Ask a running session to stop at its next step (free)")
def request_stop(session_id: str, caller: dict = Depends(get_caller)):
    """Stop a session from a screen, without being able to reach the machine running it.

    The browser is on the customer's side and this platform cannot touch it (D-407). What it
    can do is answer the *next* step with `done`, which every driver already honours -- so
    this needs no change to `explore_app.py` and every copy in the field obeys it. The flag
    is collected in `take_step` before `require_open` and before metering, so the stopping
    step makes no model call and costs nothing.

    Enforcement is server-side rather than cooperative: the session row is closed, so a
    driver that ignored the reply would be refused on the step after.

    What it cannot do, and what the screen must say: it lands at a step boundary, not
    immediately, and the browser window stays open on the machine driving it.

    Free, so `_visible_session` is the whole authorisation -- the same policy as `/finish`,
    which also spends nothing. Scope is checked before anything is written.
    """
    _visible_session(session_id, caller)
    conn = database.get_connection()
    try:
        outcome = app_session.request_stop(
            conn, session_id, who=str((caller or {}).get("username") or ""))
        if outcome is None:
            raise HTTPException(404, "No such session.")
        if not outcome.get("stopped"):
            # 200, not 409: the caller wanted this session not to be running, and it is not.
            # A race where the driver finished a second earlier is not an error to report.
            return {
                "stopped": False,
                "already": outcome.get("already"),
                "note": ("It had already stopped on its own, so nothing was cancelled."),
                "session": app_session.public(conn, session_id),
            }
        return {
            "stopped": True,
            "requested_at": outcome.get("requested_at"),
            "note": (
                "The session will stop at its next step - usually a few seconds, up to about "
                "twenty if the browser is part way through loading a page. A step already in "
                "flight finishes and is recorded. The browser window stays open on the "
                "machine driving it; this platform cannot close it, and any screenshots, "
                "video or trace stay there."
            ),
            "session": app_session.public(conn, session_id),
        }
    finally:
        conn.close()


@router.get("/allowances", summary="Controls this project permits (free)")
def list_allowances(project_id: str = "", caller: dict = Depends(get_caller)):
    """What somebody has decided is safe to press in this project's test environment.

    The denylist reads button labels with no context, so it cannot tell "Remove [from
    basket]" from "Remove [user]". Measured against real applications it withheld every
    money movement on a banking demo in three steps, and on a shop it withheld `Remove` and
    `Checkout` -- leaving six of fifteen supplied requirements untestable and forcing the
    explorer to guess a URL for the rest. This is how a customer says which of those are
    safe *here*, and it is theirs to say because only they know which environment this is.
    """
    subject = subject_from_caller(caller)
    try:
        agent_guard.check_scope(subject, PRODUCT)
    except ScopeDeniedError as exc:
        raise HTTPException(403, str(exc))

    resolved = _project_for(caller, project_id)
    if not resolved:
        # Deliberately not "everything visible to you": an allowance is a statement about
        # one application, and pooling them across projects would widen a safety rule by
        # accident.
        return {"project_id": "", "allowances": [], "total": 0,
                "note": "Choose a project. An allowance belongs to one application."}

    conn = database.get_connection()
    try:
        rows = app_session.list_allowances(conn, resolved)
    finally:
        conn.close()
    return {
        "project_id": resolved,
        "allowances": rows,
        "total": len(rows),
        "max": app_session.MAX_ALLOWANCES,
        "note": (
            "These are offered to the model in this project's sessions instead of being "
            "withheld. They apply to whatever is at that address, so they are only as true "
            "as your statement that it is a test environment."),
    }


@router.post("/allowances", summary="Permit one control by name (free)")
def add_allowance(req: AllowanceRequest, caller: dict = Depends(get_caller)):
    """Permit exactly one control name. Matching is exact and case-insensitive.

    Never a substring: a substring allow-list would let "delete" permit "Delete account",
    which is the failure the denylist exists to prevent, arrived at from the other side.

    It takes effect on the *next* session. A session freezes the list at the moment it
    opens, because the platform screens to choose an index and the driver screens again to
    resolve it -- changing the list under a running session would shift every later index
    under a driver still using the one it was handed.
    """
    subject = subject_from_caller(caller)
    try:
        agent_guard.check_scope(subject, PRODUCT)
    except ScopeDeniedError as exc:
        raise HTTPException(403, str(exc))

    resolved = _project_for(caller, req.project_id)
    if not resolved:
        raise HTTPException(400, "An allowance belongs to a project. Choose one first.")

    verdict, reason, group = action_policy.classify_detail(
        {"name": req.control_name, "role": "button", "enabled": True})
    if verdict == "allow" and not group:
        raise HTTPException(
            400, f"{req.control_name!r} is not being withheld, so permitting it changes "
                 "nothing. This list is for controls the safety policy refused.")

    conn = database.get_connection()
    try:
        stored = app_session.add_allowance(
            conn, resolved, req.control_name, group,
            who=str(caller.get("email") or caller.get("name") or ""))
        rows = app_session.list_allowances(conn, resolved)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    finally:
        conn.close()

    return {
        "permitted": stored,
        "was_refused_because": reason,
        "group": group,
        "allowances": rows,
        "applies_from": "the next session -- a running one keeps the list it opened with",
    }


@router.delete("/allowances", summary="Withdraw one (free)")
def remove_allowance(control_name: str, project_id: str = "",
                     caller: dict = Depends(get_caller)):
    """Put a control back behind the denylist. Takes effect on the next session."""
    subject = subject_from_caller(caller)
    try:
        agent_guard.check_scope(subject, PRODUCT)
    except ScopeDeniedError as exc:
        raise HTTPException(403, str(exc))

    resolved = _project_for(caller, project_id)
    if not resolved:
        raise HTTPException(400, "An allowance belongs to a project. Choose one first.")

    conn = database.get_connection()
    try:
        removed = app_session.remove_allowance(conn, resolved, control_name)
        rows = app_session.list_allowances(conn, resolved)
    finally:
        conn.close()
    return {"removed": removed, "allowances": rows}


@router.post("/preflight", summary="What a session would cost (free, calls no model)")
def preflight(req: PreflightRequest, caller: dict = Depends(get_caller)):
    """Free, and entitlement-checked but not budget-enforced.

    Same policy as every other preflight on this surface: refusing to *quote* a caller who
    is near their cap tells them less than the quote itself would, and the quote is what
    they need in order to decide.
    """
    subject = subject_from_caller(caller)
    try:
        agent_guard.check_scope(subject, PRODUCT)
    except ScopeDeniedError as exc:
        raise HTTPException(403, str(exc))

    quote = _quote(req.max_steps, req.requirement, req.goal)
    headroom = review_estimate.budget_headroom(subject)
    return {
        "agent": PRODUCT,
        "estimate": quote,
        "budget": headroom,
        "explanation": review_estimate.describe(quote),
        "within_budget": (headroom is None
                          or quote.get("expected_usd", 0) <= headroom),
        "ceiling_within_budget": (headroom is None
                                  or quote.get("ceiling_usd", 0) <= headroom),
        "policy": action_policy.describe_policy(),
        "max_steps": quote.get("max_steps"),
        "note": (
            "A session costs one small model call per step plus one large call to write the "
            "suite. The step cap is what bounds it. Replaying the suite afterwards calls no "
            "model at all."
        ),
    }


@router.post("/basis", summary="What this session could check against (free, calls no model)")
def basis(req: BasisRequest, caller: dict = Depends(get_caller)):
    """The same question `open_session` asks, asked early enough to be useful.

    A session refuses to open without a basis (D-419), and that refusal used to arrive
    *after* somebody had launched a browser on their own machine -- the screen guessed at the
    answer in JavaScript, which it cannot do: it knows nothing about the project's uploaded
    documents, so it warned about a project holding a full specification and stayed quiet
    when every ticked reference was stale. This calls `app_basis.collect` directly, so the
    screen and the session reach the same verdict in the same words.

    Free and calls no model, so it is entitlement-checked and not budget-enforced -- the same
    policy as `/preflight`, for the same reason.

    What it deliberately does not return is the *documents*. Their presence and size are the
    useful facts; the text itself is the customer's own content, already on their Knowledge
    Base screen, and echoing it here would put it in a response nothing needs it in. The
    brief is different: it is what the model will be sent, the person is choosing whether to
    send it, and showing it is the only way that choice is informed.
    """
    subject = subject_from_caller(caller)
    try:
        agent_guard.check_scope(subject, PRODUCT)
    except ScopeDeniedError as exc:
        raise HTTPException(403, str(exc))

    resolved = app_basis.collect(
        requirement=req.requirement,
        project_id=_project_for(caller, req.project_id),
        source_review_ids=req.source_review_ids,
        visible_project_ids=scope.visible_from_caller(caller),
    )
    brief = resolved.get("brief_markdown") or ""
    return {
        "has_stated": resolved["has_stated"],
        "has_derived": resolved["has_derived"],
        "has_any": resolved["has_any"],
        "strongest_basis": resolved["strongest_basis"],
        "project_name": resolved["project_name"],
        "has_requirement": bool(resolved["requirement"]),
        "has_documents": resolved["has_documents"],
        "document_chars": len(resolved.get("documents") or ""),
        "source_reviews": resolved["source_reviews"],
        # Verbatim. Each one is already written for a customer to read, and the whole point
        # of the list is that a reference which silently did nothing is how somebody believes
        # their cases are covered when they are not.
        "skipped_sources": resolved["skipped_sources"],
        "case_ids": resolved["case_ids"],
        "brief_markdown": brief[:BRIEF_PREVIEW_CHARS],
        "brief_chars": len(brief),
        "brief_truncated": len(brief) > BRIEF_PREVIEW_CHARS,
        "max_sources": app_basis.MAX_SOURCE_REVIEWS,
        "refusal": None if resolved["has_any"] else app_basis.NOTHING_TO_CHECK_AGAINST,
    }
