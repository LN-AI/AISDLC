import asyncio
import os
import io
import json
import logging
import time
import uuid
import zipfile
from contextlib import asynccontextmanager
from typing import Any, List, Optional

from fastapi import (Depends, FastAPI, File, Form, HTTPException, Query, Request,
                     Response, Security, UploadFile)
from pydantic import BaseModel, Field
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import APIKeyHeader, HTTPBearer, HTTPAuthorizationCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from backend.logging_config import configure_logging, request_id_ctx

configure_logging()
logger = logging.getLogger(__name__)

from backend import database, config, scope
# Aliased: `_run_bundle_review` and several review handlers take a parameter named
# `context` (the free-text hint a user types), so the module cannot be `context` here.
from backend import context as request_context
from backend.workflows import full_review as full_review_workflow
from backend.workflows.base import count_steps
from backend.routes.auth import router as auth_router
from backend.routes.approvals import router as approvals_router
from backend.routes.audit_trail import router as audit_router
from backend.routes.agents import router as agents_router
from backend.routes.inventory import router as inventory_router
from backend.routes.findings import router as findings_router
from backend.routes.notifications import router as notifications_router
from backend.routes.github_credentials import router as github_credentials_router
from backend.routes.github_webhook import router as github_webhook_router
from backend.routes.modernization import router as modernization_router
from backend.routes.v1_agents import router as v1_agents_router
from backend.routes.frameworks import router as frameworks_router
from backend.routes.api_keys import router as api_keys_router
from backend.routes.plans import router as plans_router
from backend.routes.products import router as products_router
from backend.routes.usage import router as usage_router
from backend.routes.swarm import router as swarm_router
from backend.routes.llm_config import router as llm_config_router
from backend.routes.llm_providers import router as llm_providers_router
from backend.routes.projects import router as projects_router
from backend.routes.verticals import router as verticals_router
from backend.routes.issue_triage_dashboard import router as issue_triage_dashboard_router
from backend.routes.cost_optimization import router as cost_optimization_router
from backend.routes.client_log import router as client_log_router
from backend.models.schemas import (
    ReviewRequest,
    CodeReviewRequest,
    PromptReviewRequest,
    FullReviewRequest,
    GithubPRRequest,
    GithubAutoFixRequest,
    ReleaseGateRequest,
    VAPTReviewRequest,
    SecurityReviewRequest,
    ArchitectureReviewRequest,
    CodeReviewSourceRequest,
    VAPTSourceRequest,
    ArchitectureSourceRequest,
    SecuritySourceRequest,
    SensitiveDataSourceRequest,
    MigrationAssessmentSourceRequest,
    RegulatedSourceRequest,
    DeliverGeneratedCodeRequest,
    CodeBriefRequest,
)
from backend.models.api_models import (
    AIProvenanceRequest,
    CFR21VerificationRequest,
    CodeGenAdvisoryRequest,
    CodeGenScaffoldRequest,
    GxPDocumentCheckRequest,
    GxPDocumentGenerateRequest,
    GxPValidationPlanRequest,
    IssueTriageBatchGitHubRequest,
    IssueTriageRequest,
    MigrationAssessmentRequest,
    MigrationExecutionRequest,
    TestCaseRequest,
    TestScenarioRequest,
)
from backend.services.orchestrator import SDLCOrchestrator
from backend.services import job_service
from backend.services import notify
from backend.services.knowledge_base import knowledge_base
from backend.services import agent_guard, legacy_guard, llm_settings_service, subject_service
from backend.services import code_brief
from backend.services import code_delivery
from backend.services import (fix_apply, guardrail_service, review_estimate,
                              review_source_store, source_bundle)
from backend.services.subject_service import resolve_subject
from backend.services.agent_guard import BudgetExceededError, QuotaExceededError, ScopeDeniedError
from backend.services.api_key_service import (
    RateLimitExceeded,
    check_project_rate_limit,
    validate_api_key,
)
from backend.services.auth_service import decode_token, is_super_admin
from backend.services.guardrail_service import GuardrailViolation
from backend.services.subject_service import ProjectBindingError
from backend.services.llm_gateway import LLMTruncatedResponseError, LLMProviderError
from backend.services.error_utils import format_error_response, _RETRYABLE_PROVIDER_ERRORS


def _init_default_admin():
    if not database.get_user_by_username("admin"):
        from backend.services.auth_service import hash_password
        pw_hash = hash_password(config.DEFAULT_ADMIN_PASSWORD)
        database.create_user("admin", "admin@sdlc.local", pw_hash, "admin", "System Administrator")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting up - env=%s model=%s", config.APP_ENV, config.MODEL)
    # Before anything else, and deliberately fatal: a production deployment must not run on
    # the shipped default SECRET_KEY or admin password. Raising here rather than warning
    # because a warning in a startup log is a warning nobody reads — and on the default
    # SECRET_KEY every stored credential in the database is decryptable by anyone with a
    # copy of this repository. No-ops unless APP_ENV=production.
    config.assert_production_secrets()
    database.init_db()
    # Background jobs live in this process only, so anything still marked running
    # belongs to a process that no longer exists. Without this they stay 'running'
    # forever and the client polls a job that will never finish.
    database.recover_orphaned_jobs()
    # Same idea, different layer: that reaps the progress row, this reaps the *work*. A
    # modernization run killed mid-conversion has durable state, so its in-flight units go
    # back on the queue and the run is paused rather than failed — resuming continues from
    # the last converted file.
    from backend.services.modernization import runner as modernization_runner
    modernization_runner.recover_all()
    _init_default_admin()
    # Two housekeeping sweeps that stand in for the scheduler this deployment does not have.
    # Both are also called on the read paths that depend on them (a baseline lookup, an
    # inventory read), so a long-running process does not drift — this is the cheap pass that
    # makes a restarted process correct immediately rather than on first use.
    try:
        lapsed = database.lapse_expired_waivers()
        swept = review_source_store.sweep_expired()
        if lapsed or swept:
            logger.info("Housekeeping: %d risk acceptance(s) lapsed, %d source(s) expired",
                        lapsed, swept)
    except Exception:
        # Never allowed to stop the app coming up. A missed sweep is a stale row; a failed
        # startup is an outage.
        logger.exception("Startup housekeeping failed — continuing")
    # A third sweep, for the same missing scheduler. A notification is written as a row
    # inside the review's own transaction and then attempted on a thread, so anything the
    # last process was holding when it stopped is still `pending`. This is where it gets
    # picked up — on a thread, so a slow chat service cannot delay the app coming up.
    # Also retried whenever somebody reads the delivery log.
    try:
        # Put anything left mid-flight back in the queue first. A `sending` row belongs to
        # a process that no longer exists, exactly like the job rows above.
        stranded = notify.recover_sending()
        if stranded:
            logger.info("Notifications: %d delivery(ies) were mid-flight at shutdown and "
                        "are queued again", stranded)
        notify.kick()
    except Exception:
        logger.exception("Could not start the notification dispatcher — pending "
                         "deliveries wait for the next read of the delivery log")
    knowledge_base.load()  # preload all docs/ markdown files into memory
    logger.info("Startup complete")
    yield
    logger.info("Shutting down")


app = FastAPI(
    title="LM AI-SDLC ReviewOps Agent",
    description="AI-powered governance and security review control tower for the software delivery lifecycle.",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    # Not "*" — see config.CORS_ALLOW_ORIGINS for why a wildcard here was a real hole
    # rather than a convenience. Empty by default: the bundled SPA is same-origin.
    allow_origins=config.CORS_ALLOW_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    # Response headers a browser is allowed to *read*. Without this list, only the six
    # CORS-safelisted headers are visible to JavaScript on a cross-origin request — so a
    # client served from anywhere but this app could not see what a run cost, and could not
    # see how much rate-limit budget it had left in order to back off. Both sets exist to be
    # read; the bundled SPA is same-origin and never needed the permission, which is why it
    # was missing.
    expose_headers=[
        "X-Spend-Cost-USD", "X-Spend-Calls", "X-Spend-Tokens-In", "X-Spend-Tokens-Out",
        "X-Spend-Model", "X-Spend-Provider", "X-Spend-Billable", "X-Spend-Cost-Estimated",
        "X-Project-Warning",
        "X-RateLimit-Limit-Minute", "X-RateLimit-Remaining-Minute",
        "X-RateLimit-Limit-Day", "X-RateLimit-Remaining-Day",
    ],
)


@app.exception_handler(GuardrailViolation)
async def _guardrail_violation_handler(request, exc: GuardrailViolation):
    logger.warning("Guardrail violation on %s %s: %s", request.method, request.url.path, exc)
    status, body = format_error_response(exc)
    return JSONResponse(status_code=status, content=body)


@app.exception_handler(ScopeDeniedError)
async def _scope_denied_handler(request, exc: ScopeDeniedError):
    logger.warning("Scope denied on %s %s: %s", request.method, request.url.path, exc)
    status, body = format_error_response(exc)
    return JSONResponse(status_code=status, content=body)


@app.exception_handler(BudgetExceededError)
async def _budget_exceeded_handler(request, exc: BudgetExceededError):
    logger.warning("Budget exceeded on %s %s: %s", request.method, request.url.path, exc)
    status, body = format_error_response(exc)
    return JSONResponse(status_code=status, content=body)


@app.exception_handler(QuotaExceededError)
async def _quota_exceeded_handler(request, exc: QuotaExceededError):
    logger.warning("Quota exceeded on %s %s: %s", request.method, request.url.path, exc)
    status, body = format_error_response(exc)
    return JSONResponse(status_code=status, content=body)


@app.exception_handler(ProjectBindingError)
async def _project_binding_handler(request, exc: ProjectBindingError):
    logger.warning("Project binding denied on %s %s: %s", request.method, request.url.path, exc)
    status, body = format_error_response(exc)
    return JSONResponse(status_code=status, content=body)


@app.exception_handler(RateLimitExceeded)
async def _rate_limit_handler(request, exc: RateLimitExceeded):
    logger.warning("Rate limit exceeded on %s %s: %s", request.method, request.url.path, exc)
    status, body = format_error_response(exc)
    return JSONResponse(status_code=status, content=body,
                        headers={"Retry-After": str(exc.retry_after_seconds)})


@app.exception_handler(LLMTruncatedResponseError)
async def _llm_truncated_handler(request, exc: LLMTruncatedResponseError):
    logger.warning("LLM response truncated on %s %s: %s", request.method, request.url.path, exc)
    status, body = format_error_response(exc)
    return JSONResponse(status_code=status, content=body)


@app.exception_handler(LLMProviderError)
async def _llm_provider_error_handler(request, exc: LLMProviderError):
    logger.warning("LLM provider error on %s %s: %s", request.method, request.url.path, exc)
    status, body = format_error_response(exc)
    return JSONResponse(status_code=status, content=body)


async def _retryable_sdk_error_handler(request, exc):
    # Backstop for a retryable SDK exception reaching here un-wrapped (i.e. not via
    # llm_gateway.call_tool, which now wraps these as LLMProviderError at the source).
    logger.warning("LLM provider error on %s %s: %s", request.method, request.url.path, exc)
    status, body = format_error_response(exc)
    return JSONResponse(status_code=status, content=body)


for _exc_type in _RETRYABLE_PROVIDER_ERRORS:
    app.add_exception_handler(_exc_type, _retryable_sdk_error_handler)


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request, exc: Exception):
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    status, body = format_error_response(exc)
    return JSONResponse(status_code=status, content=body)


@app.middleware("http")
async def legacy_product_guard(request: Request, call_next):
    """Entitlement + metering for the legacy /api/review/* surface — see
    backend/services/legacy_guard.py for why this is a middleware."""
    return await legacy_guard.legacy_product_guard(request, call_next)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
    token = request_id_ctx.set(request_id)
    start = time.perf_counter()
    try:
        response = await call_next(request)
        duration_ms = (time.perf_counter() - start) * 1000
        logger.info(
            "%s %s -> %s (%.1fms) client=%s",
            request.method, request.url.path, response.status_code, duration_ms,
            request.client.host if request.client else "-",
        )
        response.headers["X-Request-ID"] = request_id
        return response
    finally:
        request_id_ctx.reset(token)


app.include_router(auth_router)
app.include_router(approvals_router)
app.include_router(audit_router)
app.include_router(agents_router)
app.include_router(inventory_router)
app.include_router(v1_agents_router)
app.include_router(findings_router)
app.include_router(notifications_router)
app.include_router(github_credentials_router)
app.include_router(github_webhook_router)
app.include_router(modernization_router)
app.include_router(api_keys_router)
app.include_router(plans_router)
app.include_router(products_router)
app.include_router(frameworks_router)
app.include_router(usage_router)
app.include_router(swarm_router)
app.include_router(llm_config_router)
app.include_router(llm_providers_router)
app.include_router(projects_router)
app.include_router(verticals_router)
app.include_router(issue_triage_dashboard_router)
app.include_router(cost_optimization_router)
app.include_router(client_log_router)

orchestrator = SDLCOrchestrator()


def _require_llm() -> None:
    """Refuse an agent call only when *nothing* can serve it.

    This used to be a bare `if not config.ANTHROPIC_API_KEY` repeated on every route,
    which 503'd even with a perfectly good local connection selected — the app claimed
    to need a key it wasn't going to call. An admin-selected LLM connection (see
    /api/v1/llm-providers) now satisfies it too, so ANTHROPIC_API_KEY can be left empty.
    """
    if not llm_settings_service.is_llm_configured():
        raise HTTPException(
            status_code=503,
            detail="No LLM configured. Set ANTHROPIC_API_KEY, or add and activate a "
                   "connection under Settings → LLM Providers.",
        )


# Security schemes for the read endpoints below. `auto_error=False` on both so a missing
# credential falls through to require_reader's own 401 rather than FastAPI's terser one.
_read_api_key_hdr = APIKeyHeader(name="X-API-Key", auto_error=False)
_read_bearer = HTTPBearer(auto_error=False)


# ── Review sources: preflight, zip upload, repository ─────────────────────
#
# The three screens the plan is about (Code Review, VAPT, GitHub PR) posted to routes that
# accepted a paste and nothing else, while the v1 API behind them accepted PR and file URLs
# — so users concluded the capability did not exist because the screen said so (C-74).
#
# Two things every route below shares:
#
# * **`def`, not `async def`.** Every other legacy handler is `async def` and then calls the
#   orchestrator synchronously, which holds the event loop for the whole review — measured:
#   during a ten-minute VAPT, `GET /` never returned (C-31). Plain `def` makes FastAPI run
#   the handler on its threadpool instead. These are the routes where it matters most,
#   because a repository scan makes every call that long. The sweep across the other ~30
#   legacy routes is deliberately not done here: it changes concurrency for all of them at
#   once and wants measuring rather than a blind edit.
# * **A quote before the spend.** `/preflight` prices the exact call, free, so the screen can
#   show it and ask. The modernization plan gate is the precedent; `review_estimate` is the
#   shared arithmetic.


def _ui_subject(request: Request) -> Any:
    """The project a browser call is governed by, for a quote's budget line.

    Resolved the same way `legacy_guard` resolves it for metering, so the headroom shown
    beside a price is the headroom that will actually be checked. Degrades to an unbound
    subject rather than failing: a quote is more useful without a budget line than not at
    all.
    """
    try:
        caller, _ = legacy_guard.resolve_caller(request.headers)
        return resolve_subject(caller, request.headers)
    except Exception:
        logger.info("Could not resolve a subject for the quote — omitting the budget line")
        return None


def _project_id_for(request: Request) -> Optional[str]:
    """The project a browser call names, for picking its GitHub credential."""
    subject = _ui_subject(request)
    return getattr(subject, "project_id", None) if subject is not None else None



def _free_findings_summary(bundle) -> dict:
    """What the free scanners already know, for the cost quote.

    On the quote because it sometimes answers the question before any money is spent: "5
    vulnerable dependencies and 3 hardcoded credentials, found for nothing" may be the whole
    of what someone needed. `dependency` also says what could *not* be checked, so a quote
    never implies a clean bill of health it has not earned.
    """
    from backend.services import source_bundle as _sb
    deterministic = _sb.deterministic_findings(bundle)
    secrets = sum(1 for f in deterministic["findings"] if f.get("detector") == "pattern")
    deps = sum(1 for f in deterministic["findings"]
               if f.get("detector") == "dependency-database")
    return {
        "secrets_already_found": secrets,
        "vulnerable_dependencies_found": deps,
        "dependency_scan": deterministic["dependency"],
    }

def _preflight(req, request: Request, agent: str) -> dict:
    try:
        bundle = source_bundle.resolve(req, orchestrator.github,
                                       project_id=_project_id_for(request))
    except source_bundle.SourceError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("Preflight source resolution failed for %s", agent)
        raise HTTPException(status_code=502, detail=f"Could not read that source: {exc}")

    quote = review_estimate.for_bundle(bundle, agent=agent)
    subject = _ui_subject(request)
    headroom = agent_guard.remaining_budget(subject) if subject is not None else None
    return {
        "agent": agent,
        "source": bundle.summary(),
        "estimate": quote,
        "budget": headroom,
        "explanation": review_estimate.describe(
            quote, cap_usd=(headroom or {}).get("remaining_usd")
        ),
        "within_budget": (
            None if headroom is None else quote["expected_usd"] <= headroom["remaining_usd"]
        ),
        # Deterministic findings the ingest scanner already has, before any model runs. Shown
        # on the quote because "we already found 3 hardcoded credentials, for free" changes
        # whether someone wants to spend the money at all.
        **_free_findings_summary(bundle),
        # Which files, by name. The quote used to give a count and no way to check it, so
        # "6 filtered out" could not be reconciled with what the customer believed they had
        # uploaded. Inline here because a preflight has no review id yet to hang an inventory
        # off; a finished review answers the same question through `/sources`, which is paged
        # and does not travel inside `result_json`.
        "files_preview": _files_preview(bundle),
        "files_preview_truncated": max(
            0, len((bundle.meta or {}).get("files_changed") or bundle.files) - 400),
        "skipped_all": bundle.skipped[:400],
        "retention": _retention_note(req, bundle),
    }


def _files_preview(bundle) -> list:
    """The files a quote is for, by name.

    A diff-shaped source is *one* pseudo-file holding the whole diff, so listing
    `bundle.files` for a pull request produced a single nameless row — "1 file(s)" with
    nothing to check it against, which is what made a real user doubt the review had seen
    their code. A diff knows which files it touches, so those are the list; sizes are absent
    because a diff carries changed lines rather than whole files, and inventing a size would
    be worse than omitting one.
    """
    changed = (bundle.meta or {}).get("files_changed") or []
    if bundle.is_diff and changed:
        return [{"path": p, "in_diff": True} for p in changed[:400]]
    return [
        {"path": f.get("path") or "(pasted content)",
         "chars": len(f.get("content") or ""),
         "lines": (f.get("content") or "").count("\n") + 1}
        for f in bundle.files[:400]
    ]


def _retention_note(req, bundle) -> dict:
    """What will happen to this source after the run, in the quote that precedes it.

    On the quote rather than only in the form, because it is a data-handling commitment and
    the moment a person is deciding to spend money is the moment they are actually reading.
    """
    days = review_source_store.normalize_retention(
        getattr(req, "retention_days", None)
        if getattr(req, "retention_days", None) is not None
        else review_source_store.DEFAULT_RETENTION_DAYS
    )
    refetchable = bundle.source_type in review_source_store.REFETCHABLE
    return {
        "days": 0 if refetchable else days,
        "refetchable": refetchable,
        "choices": list(review_source_store.RETENTION_CHOICES),
        "note": (
            "Nothing is copied: this source is read back from the repository at the exact "
            "commit this review used, so there is nothing to keep or expire."
            if refetchable else review_source_store.describe_retention(days)
        ),
    }


def _run_sensitive_bundle(bundle, project_name: str, context: str) -> dict:
    """Shared body for a sensitive-data scan over a bundle.

    Separate from `_run_bundle_review` rather than folded into it: that one attaches
    `suppression` and the deterministic dependency findings and routes through
    `review_code`/`review_vapt`'s argument shape. This agent takes the secret findings
    (which are exactly its subject) but has no use for a dependency scan.
    """
    _require_llm()
    parts = [f"Source: {bundle.source_type} — {bundle.label}"]
    if bundle.untrusted_label:
        parts.append(guardrail_service.untrusted_notice(bundle.untrusted_label))
    if context:
        parts.append(context)
    if len(bundle.files) > 1:
        parts.append(
            f"This scan covers {len(bundle.files)} file(s) from one codebase. Each part "
            f"lists the files it contains; do not assert that data is handled safely "
            f"elsewhere when the file that would show it was not shown to you.")
    single = len(bundle.files) == 1 and not bundle.files[0].get("path")
    deterministic = source_bundle.deterministic_findings(bundle)
    return orchestrator.scan_sensitive_data(
        content=(bundle.files[0].get("content") or "") if single else "",
        files=None if single else bundle.files,
        context="\n".join(parts),
        project_name=project_name or bundle.label[:60] or "Unnamed Project",
        coverage_incomplete=bundle.truncated,
        coverage_reason=bundle.truncation_reason,
        # The credentials the ingest scanner already matched, for free, before any model
        # ran. This agent's own subject matter, so they are merged in rather than reported
        # beside the result: they get verification, de-duplication, a fingerprint and a
        # vote in the gate like everything else.
        extra_findings=None if single else deterministic["findings"],
        suppression=None if single else bundle.suppression,
        source=bundle.summary(),
    )


def _preflight_design(req, request: Request, agent: str) -> dict:
    """Quote a design review of a codebase. Free — neither the brief nor the quote calls a
    model.

    Priced from the **brief**, not the bundle, because that is what the agent is actually
    sent: `for_bundle` would chunk three megabytes of source and quote twenty calls for a
    run that makes one. Quoting the wrong artifact is worse than not quoting — a quote is
    believed.

    The coverage statement travels with the price, which is the point of doing this for
    free: "only 12 of your 400 files can be parsed for dependencies" is worth knowing
    *before* deciding to spend, not after reading a thin review.
    """
    try:
        bundle = source_bundle.resolve(req, orchestrator.github,
                                       project_id=_project_id_for(request))
    except source_bundle.SourceError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("Preflight source resolution failed for %s", agent)
        raise HTTPException(status_code=502, detail=f"Could not read that source: {exc}")

    brief = code_brief.build(bundle)
    quote = review_estimate.for_text(brief["markdown"], agent=agent)
    subject = _ui_subject(request)
    headroom = agent_guard.remaining_budget(subject) if subject is not None else None
    return {
        "agent": agent,
        "source": bundle.summary(),
        "estimate": quote,
        "budget": headroom,
        "explanation": review_estimate.describe(
            quote, cap_usd=(headroom or {}).get("remaining_usd")),
        "within_budget": (
            None if headroom is None else quote["expected_usd"] <= headroom["remaining_usd"]),
        "code_brief": {k: brief[k] for k in ("coverage", "coverage_note", "stats", "files")},
        "files_preview": _files_preview(bundle),
        "skipped_all": bundle.skipped[:400],
        # No retention block: a design review stores no source. It is not one of the two
        # products with an inventory (`AGENT_SCREENS[...].inventory`), and offering a
        # retention choice that nothing honours would be a promise about data handling that
        # no code keeps.
    }


@app.post("/api/review/architecture/preflight")
def preflight_architecture_ui(req: ArchitectureSourceRequest, request: Request):
    return _preflight_design(req, request, "architecture")


@app.post("/api/review/security/preflight")
def preflight_security_ui(req: SecuritySourceRequest, request: Request):
    return _preflight_design(req, request, "security")


@app.post("/api/review/code/preflight")
def preflight_code_ui(req: CodeReviewSourceRequest, request: Request):
    return _preflight(req, request, "code")


@app.post("/api/review/vapt/preflight")
def preflight_vapt_ui(req: VAPTSourceRequest, request: Request):
    return _preflight(req, request, "vapt")


def _run_bundle_review(bundle, agent: str, project_name: str, context: str = "",
                       language: str = "", scope: str = "", on_progress=None,
                       retention_days=None) -> dict:
    """Shared body for a bundle-backed review on the UI surface."""
    _require_llm()
    label = bundle.untrusted_label
    ctx_parts = [f"Source: {bundle.source_type} — {bundle.label}"]
    if label:
        ctx_parts.append(guardrail_service.untrusted_notice(label))
    if context:
        ctx_parts.append(context)
    if len(bundle.files) > 1:
        ctx_parts.append(
            f"This review covers {len(bundle.files)} file(s) from one codebase. Each chunk "
            f"lists the files it contains and the files it does not; do not assert that a "
            f"control is missing when the file that would contain it was not shown to you."
        )

    args: dict = {
        "context": "\n".join(ctx_parts),
        "project_name": project_name or bundle.label[:60],
        "coverage_incomplete": bundle.truncated,
        "coverage_reason": bundle.truncation_reason,
        "source": bundle.summary(),
        "on_progress": on_progress,
    }
    deterministic = source_bundle.deterministic_findings(bundle)
    args["extra_findings"] = deterministic["findings"]
    args["source"] = {**args["source"], "dependency": deterministic["dependency"]}
    # What the repository itself says not to gate on. Passed even when empty, so the agent
    # can report "checked, no rules" rather than leaving a reader unable to tell that from
    # "never looked".
    args["suppression"] = bundle.suppression
    single = len(bundle.files) == 1 and not bundle.files[0].get("path")
    if single:
        text = bundle.files[0].get("content") or ""
        if label:
            text = guardrail_service.wrap_untrusted_content(text, label)
    else:
        args["files"] = bundle.files

    # Which repository this review is about, published for `save_review` to store. Opened
    # around the agent call rather than at the route, because this is the one function both
    # Code Review and VAPT go through and the bundle is already in hand here.
    with request_context.bind_source(bundle.source_ref()):
        if agent == "vapt":
            if single:
                args["artifact"] = text
            result = orchestrator.review_vapt(scope=scope, **args)
        else:
            if single:
                args["code"] = text
            result = orchestrator.review_code(language=language, **args)

    # Recorded here rather than inside the orchestrator for two reasons: `review_id` does not
    # exist until the result is saved, and the retention choice belongs to the surface the
    # person chose it on — the orchestrator has 19 `save_review` call sites and none of them
    # should have to know about it.
    _record_sources(result, bundle, retention_days)
    return result


def _record_sources(result: dict, bundle, retention_days) -> None:
    """Store what this review looked at. Never allowed to fail the review.

    A review that has already been paid for must not be lost to a bookkeeping error, so
    everything here is best-effort and logged. The response says whether it worked, so a
    screen can offer a file viewer only when there is something to view.
    """
    review_id = (result or {}).get("review_id")
    if not review_id:
        return
    try:
        days = review_source_store.normalize_retention(
            retention_days if retention_days is not None
            else review_source_store.DEFAULT_RETENTION_DAYS)
        chunk_of_path = {}
        for row in ((result.get("coverage") or {}).get("chunk_files") or []):
            for path in (row.get("files") or []):
                chunk_of_path.setdefault(path, row.get("index"))
        review_source_store.record(
            review_id, bundle, days,
            findings=result.get("findings") or [],
            chunk_of_path=chunk_of_path,
        )
        result.setdefault("source", {})["inventory"] = review_source_store.summarize(review_id)
    except Exception:
        logger.exception("Could not record the source inventory for review %s", review_id)


@app.post("/api/review/code/source")
def review_code_source(req: CodeReviewSourceRequest, request: Request):
    """Code review over any source: paste, file list, zip-by-reference, repo, PR, branch diff.

    Separate from `/api/review/code` rather than replacing it, because that route is the one
    every existing screen and integration posts to and its contract is "one artifact in,
    one review out". This one takes the full source set.
    """
    try:
        bundle = source_bundle.resolve(req, orchestrator.github,
                                       project_id=_project_id_for(request))
    except source_bundle.SourceError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    try:
        return _run_bundle_review(
            bundle, "code", req.project_name or "", req.context or "", req.language or "",
            retention_days=req.retention_days)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unhandled error: %s", e)
        status, body = format_error_response(e)
        raise HTTPException(status_code=status, detail=body)


@app.post("/api/review/vapt/source")
def review_vapt_source(req: VAPTSourceRequest, request: Request):
    """VAPT over any source. See `/api/review/code/source`."""
    try:
        bundle = source_bundle.resolve(req, orchestrator.github,
                                       project_id=_project_id_for(request))
    except source_bundle.SourceError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    try:
        return _run_bundle_review(
            bundle, "vapt", req.project_name or "", req.context or "", scope=req.scope or "",
            retention_days=req.retention_days)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unhandled error: %s", e)
        status, body = format_error_response(e)
        raise HTTPException(status_code=status, detail=body)


def _upload_bundle(raw: bytes, filename: str):
    """One upload, whether it is an archive or a single source file.

    `from_upload` decides from the bytes rather than the extension — see its docstring for
    why the screen's two upload modes have to land in one place.
    """
    try:
        return source_bundle.from_upload(raw, filename=filename or "")
    except source_bundle.SourceError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/review/code/upload-zip")
def review_code_zip(
    file: UploadFile = File(..., description="A zip of the source, or a single source file."),
    project_name: str = Form(""),
    language: str = Form(""),
    context: str = Form(""),
    retention_days: int = Form(
        review_source_store.DEFAULT_RETENTION_DAYS,
        description="Days to keep the uploaded source so files can be opened and fixed "
                    "later: 0, 1, 3, 7, 14 or 30. An upload has no origin to re-read, so "
                    "this is the only thing that decides whether a file can be shown again."),
):
    """Review a whole codebase from a zip.

    Path safety, symlink rejection, credential-file skipping, secret redaction and zip-bomb
    limits all come from `repo_ingest`, the ingester modernization already uses — not from a
    second implementation written here.
    """
    bundle = _upload_bundle(file.file.read(), file.filename or "")
    try:
        return _run_bundle_review(bundle, "code", project_name, context, language,
                                  retention_days=retention_days)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unhandled error: %s", e)
        status, body = format_error_response(e)
        raise HTTPException(status_code=status, detail=body)


@app.post("/api/review/vapt/upload-zip")
def review_vapt_zip(
    file: UploadFile = File(..., description="A zip of the source, or a single source file."),
    project_name: str = Form(""),
    scope: str = Form(""),
    context: str = Form(""),
    retention_days: int = Form(review_source_store.DEFAULT_RETENTION_DAYS),
):
    """Assess a whole codebase from a zip. See `/api/review/code/upload-zip`."""
    bundle = _upload_bundle(file.file.read(), file.filename or "")
    try:
        return _run_bundle_review(bundle, "vapt", project_name, context, scope=scope,
                                  retention_days=retention_days)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unhandled error: %s", e)
        status, body = format_error_response(e)
        raise HTTPException(status_code=status, detail=body)


@app.post("/api/review/zip/preflight")
def preflight_zip(
    file: UploadFile = File(...),
    agent: str = Form("code"),
    request: Request = None,
):
    """Price a zip before reviewing it — the same quote, for the upload path.

    Reads and filters the archive (free) and returns the count of files that will actually be
    reviewed, which is the number people are most surprised by: a 900-file repository is
    often 300 reviewable files, and seeing that before the spend is the point.
    """
    if agent not in ("code", "vapt"):
        raise HTTPException(status_code=400, detail="agent must be 'code' or 'vapt'.")
    bundle = _upload_bundle(file.file.read(), file.filename or "")
    quote = review_estimate.for_bundle(bundle, agent=agent)
    subject = _ui_subject(request) if request is not None else None
    headroom = agent_guard.remaining_budget(subject) if subject is not None else None
    return {
        "agent": agent,
        "source": bundle.summary(),
        "estimate": quote,
        "budget": headroom,
        "explanation": review_estimate.describe(
            quote, cap_usd=(headroom or {}).get("remaining_usd")),
        **_free_findings_summary(bundle),
    }

class _NoCacheStaticFiles(StaticFiles):
    """Forces browsers to revalidate (If-None-Match/ETag) on every load instead of
    silently serving a stale cached copy of index.html/app.js/styles.css — the
    ETag/Last-Modified headers still make an unchanged file a fast 304."""
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


frontend_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend")
if os.path.exists(frontend_path):
    app.mount("/static", _NoCacheStaticFiles(directory=frontend_path), name="static")


@app.get("/", include_in_schema=False)
async def serve_dashboard():
    index_path = os.path.join(frontend_path, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path, headers={"Cache-Control": "no-cache"})
    return {"message": "LM AI-SDLC ReviewOps Agent API", "docs": "/docs"}


@app.get("/health")
async def health():
    return {"status": "ok", "model": config.MODEL, "env": config.APP_ENV}


# ── Reading stored work ────────────────────────────────────────────────────
#
# Plans, budgets and quotas govern what a caller may *spend*. They say nothing about what
# a caller may *read*, and these endpoints return the actual submitted artifacts and the
# findings produced from them — source code, architecture documents, security results.
# They answered to no credential at all, and LEGACY_ROUTES_REQUIRE_AUTH does not cover
# them because that flag (and the middleware behind it) only matches POST.
#
# Any valid credential is accepted, key or login, because an integration polling a job or
# fetching its own scaffold is a legitimate reader. What is refused is *no* credential.
# This is deliberately not behind a config flag: spend limits are a business policy and
# can sensibly be switched off, but "strangers may read customer source code" is not a
# mode worth offering.

def require_reader(
    api_key: Optional[str] = Security(_read_api_key_hdr),
    creds: Optional[HTTPAuthorizationCredentials] = Security(_read_bearer),
) -> dict:
    if api_key:
        record = validate_api_key(api_key)
        if record:
            return {"type": "api_key", "team": record["team_name"], "_record": record}
    if creds:
        user = decode_token(creds.credentials)
        if user:
            return {"type": "jwt", "username": user.get("username"), "_user": user}
    raise HTTPException(
        status_code=401,
        detail="Authentication required. Supply 'X-API-Key: lmsdlc_...' or 'Authorization: Bearer <jwt>'.",
        headers={"WWW-Authenticate": 'Bearer realm="LM AI-SDLC"'},
    )


def _visible_project_ids(reader: dict, request: Request) -> Optional[List[str]]:
    """Which projects' reviews this caller may read. None means every project.

    The rule itself now lives in `backend/scope.py` so routers other than this module can
    apply it — `routes/approvals.py` and `routes/findings.py` list governance rows and were
    reinventing (or, in the approvals case, simply omitting) the same scoping.
    """
    return scope.visible_project_ids(reader, request)



# ── Long scans as background jobs ─────────────────────────────────────────
#
# A paste is one model call and a synchronous request is the right shape for it. A repository
# is fifty to a hundred calls, and holding an HTTP connection open for that is how a review
# gets lost to a proxy timeout with the spend already incurred. The chunk fan-out already
# reports its progress — `chunked_review._fan_out` calls `on_progress(done, total)` after
# every chunk — and until now nothing consumed it.
#
# Modelled on `/api/review/full`, which has done this since it shipped, and guarded the same
# way and for the same reason: these routes are deliberately absent from the legacy
# middleware's product map, because they return 202 and do their LLM work after the response
# is sent, so there is nothing request-scoped left for a middleware to meter. The preflight
# therefore runs *here*, before the 202, so a denied caller gets 402/403/429 rather than an
# accepted job that fails a minute later.


def _start_review_job(req, request: Request, agent: str) -> dict:
    """Accept a review, return a job id, and run it on a background thread."""
    _require_llm()
    try:
        bundle = source_bundle.resolve(req, orchestrator.github,
                                       project_id=_project_id_for(request))
    except source_bundle.SourceError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("Async review source resolution failed for %s", agent)
        raise HTTPException(status_code=502, detail=f"Could not read that source: {exc}")

    # The **real** chunk count, not `estimate_chunks*`. Those divide and document that they
    # err low — the right trade inside a middleware that has only a request body, and the
    # wrong one here for two reasons: the progress bar would read "6 of 4 pieces", and the
    # reservation would be short on exactly the large scans it exists to bound. The split is
    # free (pure string work, no model), and `review_estimate.for_bundle` has already run it.
    quote = review_estimate.for_bundle(bundle, agent=agent)
    expected = max(quote.get("chunks") or 1, 1)

    caller, rejection = legacy_guard.resolve_caller(request.headers)
    if rejection is not None:
        return rejection
    # Stashed now because `_run` executes on a background thread with no request headers —
    # without it the job would meter against no project at all.
    caller["_subject"] = subject_service.resolve_subject(caller, request.headers)
    check_project_rate_limit(caller["_subject"], request.url.path)
    with agent_guard.expect_calls(expected):
        agent_guard.preflight(caller["_subject"], agent)

    project_name = (getattr(req, "project_name", "") or bundle.label[:60])
    language = getattr(req, "language", "") or ""
    scope_text = getattr(req, "scope", "") or ""
    context_text = getattr(req, "context", "") or ""

    def _run(on_progress):
        # `on_progress` from job_service takes (step_index, total, label); the agent reports
        # (done, total). Adapting here rather than changing either signature keeps the agent
        # unaware of jobs, which is what lets the same agent serve the synchronous route.
        def report(done, total):
            on_progress(done, total, f"Reviewed {done} of {total} piece(s)")

        with agent_guard.expect_calls(expected):
            return agent_guard.guarded_call(
                caller, agent, None, skip_preflight=True, project_name=project_name,
                fn=lambda: _run_bundle_review(
                    bundle, agent, project_name, context_text, language, scope_text,
                    on_progress=report,
                    retention_days=getattr(req, "retention_days", None),
                ),
            )

    job_id = job_service.start_job(agent, total_steps=expected, run_fn=_run)
    logger.info(
        "Started %s review job %s over %d file(s), ~%d call(s)",
        agent, job_id, len(bundle.files), expected,
    )
    return {
        "job_id": job_id,
        "status": "accepted",
        "agent": agent,
        # The source summary and the estimate come back with the 202 so the screen can show
        # what it is waiting for, and how much it agreed to, while it waits.
        "source": bundle.summary(),
        "estimate": quote,
        "poll_url": f"/api/review/jobs/{job_id}",
    }


@app.post("/api/review/code/source/async", status_code=202)
async def review_code_source_async(req: CodeReviewSourceRequest, request: Request):
    """Start a code review and return a job id. For a repository or a large zip.

    `async def` is load-bearing here, unlike the synchronous twins: `job_service.start_job`
    schedules the work with `asyncio.create_task`, which needs a running loop on the calling
    thread. The handler itself does no blocking work, so it does not hold the loop.
    """
    return _start_review_job(req, request, "code")


@app.post("/api/review/vapt/source/async", status_code=202)
async def review_vapt_source_async(req: VAPTSourceRequest, request: Request):
    """Start a VAPT assessment and return a job id. See the code twin."""
    return _start_review_job(req, request, "vapt")


@app.get("/api/review/jobs/{job_id}")
async def review_job_status(job_id: str, reader: dict = Depends(require_reader)):
    """Progress, then the finished review.

    Behind `require_reader` because a completed job carries the whole result — the findings
    and the quoted source lines — which is a read of customer work product, not a status
    ping.
    """
    job = job_service.get_job_status(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"No job found with id '{job_id}'")
    return job

# ── Dashboard ──────────────────────────────────────────────────────────────

@app.get("/api/repositories", summary="Every repository this caller has reviewed, worst first")
async def list_repositories(request: Request, limit: int = Query(100, ge=1, le=500),
                            reader: dict = Depends(require_reader)):
    """What no screen could ask before: how is each of our repositories doing.

    `reviews` gained a repository dimension, so this is a query rather than a subsystem —
    runs, last reviewed, the latest gate, open Critical and High, and what the repository
    has cost. Reviews with no repository (a paste, a zip upload) are absent by design
    rather than grouped under a blank name.
    """
    return {"repositories": database.list_repositories(
        project_ids=_visible_project_ids(reader, request), limit=limit)}


@app.get("/api/dashboard")
async def get_dashboard(request: Request, reader: dict = Depends(require_reader)):
    return database.get_dashboard_stats(project_ids=_visible_project_ids(reader, request))


@app.get("/api/dashboard/trend", summary="New vs resolved findings, and gate mix, per week")
async def get_dashboard_trend(request: Request, weeks: int = Query(8, ge=1, le=52),
                              reader: dict = Depends(require_reader)):
    """Whether this is getting better, which six lifetime totals cannot say.

    "44 open" reads the same in a month where thirty were fixed and one where thirty were
    introduced. `findings` and `gates` are separate series on purpose: one Red release can
    carry forty findings and forty Green ones carry none, so the two answer different
    questions and averaging them would answer neither.
    """
    visible = _visible_project_ids(reader, request)
    return {
        "findings": database.get_findings_trend(project_ids=visible, weeks=weeks),
        "gates": database.get_gate_trend(project_ids=visible, weeks=weeks),
        "weeks": weeks,
    }


@app.get("/api/reviews")
async def list_reviews(request: Request, limit: int = Query(20, ge=1, le=200),
                       offset: int = Query(0, ge=0),
                       review_type: Optional[str] = Query(
                           None, description="Only this kind of review — 'code', 'vapt', "
                                             "'architecture' and so on. Omit for all. "
                                             "Coarser than product_id: 'code' covers Code "
                                             "Review, the GitHub PR review and Auto-Fix."),
                       product_id: Optional[str] = Query(
                           None, description="Only this product's reviews — 'code-review', "
                                             "'github-pr-review', 'auto-fix', 'vapt'. This "
                                             "is what a single agent's screen should filter "
                                             "on. Rows written before this was recorded have "
                                             "no product_id, so pass review_type as well to "
                                             "include them."),
                       reader: dict = Depends(require_reader)):
    """One page of reviews, newest first.

    `count` is how many rows this page holds; `total` is how many the caller may see in
    all, which is what a pager needs to know whether there is a next page.

    Summary columns only — a list does not carry `result_json`. Fetch one review by id for
    that.
    """
    visible = _visible_project_ids(reader, request)
    reviews = database.list_reviews(limit=limit, offset=offset, project_ids=visible,
                                    review_type=review_type, product_id=product_id)
    # One ledger query for the whole page, not one per row — a run list is the only place
    # in this app that reads the same thing once per card, and N+1 there would be felt.
    spend = database.get_review_spend([r["id"] for r in reviews])
    for row in reviews:
        row["spend"] = spend.get(row["id"])
    return {
        "reviews": reviews,
        "count": len(reviews),
        "total": database.count_reviews(project_ids=visible, review_type=review_type,
                                        product_id=product_id),
        "limit": limit,
        "offset": offset,
        "review_type": review_type,
        "product_id": product_id,
    }


# ── Applying fixes ────────────────────────────────────────────────────────
#
# Auto-Fix existed and could only be pointed at a pull request, which meant it re-ran the
# review, chose the findings itself, and had one possible output. These two routes are the
# same machinery started from a finished review and the findings a person ticked.
#
# `/fix/plan` is free and calls no model, like every other quote on this platform. `/fix`
# spends, and refuses to spend on a selection the plan said it could not serve.


class FixRequest(BaseModel):
    finding_ids: Optional[List[str]] = Field(
        None,
        description="Which findings to fix, by their `id`. Omit to take every fixable "
                    "finding in the review — which is a larger bill, so the plan endpoint "
                    "will tell you what that costs first.")
    output: str = Field(
        "diff",
        description="Where the fix goes. `diff` shows it and writes nothing. `zip` returns "
                    "the patched files plus a unified diff. `branch` pushes a branch off the "
                    "commit the review read. `pr` does that and opens a pull request against "
                    "the branch it came from. The last two need a repository source and a "
                    "token with write access.")
    context: Optional[str] = Field(
        None, description="Anything the fixer should know that is not in the code.")


@app.post("/api/reviews/{review_id}/fix/plan",
          summary="What fixing this selection would involve, and cost. Free.")
async def fix_plan(review_id: str, req: FixRequest, request: Request,
                   reader: dict = Depends(require_reader)):
    """Price a fix before running one, and say up front what cannot be fixed and why.

    Calls no model. The one number people misread here is the call count: a fix call carries
    a whole file, so five findings in one file is **one** call, not five — the response says
    so in `note` rather than leaving it to be inferred from a total.
    """
    review = _review_in_scope(review_id, request, reader)
    try:
        return fix_apply.plan(review_id, req.finding_ids,
                              project_id=review.get("project_id"))
    except fix_apply.FixError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/reviews/{review_id}/fix",
          summary="Fix the selected findings and return the result in the requested shape")
async def fix_run(review_id: str, req: FixRequest, request: Request,
                  reader: dict = Depends(require_reader)):
    """Generate patches, guard every one of them, then deliver.

    Two guardrails, both mandatory and neither skippable: a patch that introduces a hardcoded
    secret is rejected, and every patch is **re-reviewed** by the same reviewer that found
    the original problem — a patched file that still holds a Critical does not ship. Both run
    before anything is written to a repository, because a push cannot be undone by returning
    an error.

    A partial result is a success: three files patched and two rejected returns both lists
    rather than an error, because the three are worth having.
    """
    review = _review_in_scope(review_id, request, reader)
    _require_llm()
    # Resolved the same way the review routes resolve it, so a fix is entitled, budgeted,
    # quota'd and metered against the same project the review it came from was.
    caller, rejection = legacy_guard.resolve_caller(request.headers)
    if rejection is not None:
        return rejection
    caller["_subject"] = subject_service.resolve_subject(caller, request.headers)
    check_project_rate_limit(caller["_subject"], request.url.path)

    try:
        sized = fix_apply.plan(review_id, req.finding_ids,
                               project_id=review.get("project_id"))
    except fix_apply.FixError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    expected = max(1, sized.get("calls") or 1)
    try:
        with agent_guard.expect_calls(expected):
            result = agent_guard.guarded_call(
                caller, "auto-fix", None, project_name=review.get("project_name") or "",
                fn=lambda: fix_apply.apply(
                    orchestrator, review_id, req.finding_ids,
                    project_id=review.get("project_id"),
                    output=req.output, context=req.context or "",
                ),
            )
    except fix_apply.FixError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Fix run failed for review %s", review_id)
        status, body = format_error_response(exc, agent_name="auto-fix")
        raise HTTPException(status_code=status, detail=body)

    # A zip cannot travel in JSON. It is held for one download rather than written to disk:
    # the patches are already in the response, so the archive is a convenience and losing it
    # to a restart costs nothing but a re-run of the delivery step.
    if result.get("zip_bytes") is not None:
        blob = result.pop("zip_bytes")
        token = uuid.uuid4().hex
        _FIX_ZIPS[token] = (blob, review_id)
        result["download_url"] = f"/api/reviews/{review_id}/fix/download/{token}"
        result["download_bytes"] = len(blob)

    # A preview holds the patched file contents server-side and hands back a token.
    #
    # They must not travel to the browser and back: a client that could post file contents
    # to the delivery route would be able to commit anything it liked to the customer's
    # repository under a fix's name. The token is the only thing that crosses, and the
    # contents it names never leave this process.
    if result.pop("preview", False):
        token = uuid.uuid4().hex
        _hold_pending_fix(token, review_id, result)
        result["deliver_token"] = token
        # The diff and the per-file explanations stay in the response — that is what there
        # is to read. `patches` and `before` are the raw contents and are stripped.
        result.pop("patches", None)
        result.pop("before", None)
    return result


# One generated-but-undelivered patch per token. Same shape and same reasoning as
# `_FIX_ZIPS` below: in memory rather than on disk because it is a copy of the customer's
# patched source, single use because a token that keeps working is a token that ends up in
# a chat log, and capped because a long-lived process must not accumulate patches nobody
# came back for. Losing one to a restart costs a re-run of the generation step, which is
# the same trade already accepted for the archive.
_FIX_PENDING: dict = {}
_MAX_PENDING_FIXES = 8


def _hold_pending_fix(token: str, review_id: str, result: dict) -> None:
    while len(_FIX_PENDING) >= _MAX_PENDING_FIXES:
        _FIX_PENDING.pop(next(iter(_FIX_PENDING)), None)
    _FIX_PENDING[token] = {
        "review_id": review_id,
        "patches": result.get("patches") or [],
        "before": result.get("before") or {},
        "diff": result.get("diff") or "",
        "blocked_patches": result.get("blocked_patches") or [],
        "files_patched": result.get("files_patched") or [],
        "unfixed_finding_titles": result.get("unfixed_finding_titles") or [],
        "fixed_finding_titles": result.get("fixed_finding_titles") or [],
        "commit_message": result.get("commit_message") or "",
        "pr_title": result.get("pr_title") or "",
        "pr_description": result.get("pr_description") or "",
        "reviewed_by": result.get("reviewed_by") or "",
        "verification_note": result.get("verification_note") or "",
        "carried_over_criticals": result.get("carried_over_criticals") or [],
        "unreadable_files": result.get("unreadable_files") or [],
    }


class FixDeliverRequest(BaseModel):
    deliver_token: str = Field(..., description="The token the preview returned.")
    output: str = Field(..., description="Where the patch should go: "
                                        "pr | same_pr | branch | zip | diff")


@app.post("/api/reviews/{review_id}/fix/deliver",
          summary="Deliver a patch that was generated and previewed")
async def fix_deliver(review_id: str, req: FixDeliverRequest, request: Request,
                      reader: dict = Depends(require_reader)):
    """Put a previewed patch where the person who read it chose.

    **Calls no model.** That is the whole point of splitting this from `POST .../fix`: the
    diff somebody approved is the diff that ships. Re-generating to change the destination
    would pay for the same work twice and could produce a *different* patch the second
    time, so the approval would refer to something that no longer exists.

    Still entitlement-checked against `auto-fix` (D-307), because this is the step that
    writes to a real repository — no model call does not mean no governance. `preflight`
    rather than `guarded_call`: there is nothing to meter, and a zero-cost ledger row for
    every delivery would make the usage export harder to read, not easier.
    """
    review = _review_in_scope(review_id, request, reader)
    caller, rejection = legacy_guard.resolve_caller(request.headers)
    if rejection is not None:
        return rejection
    caller["_subject"] = subject_service.resolve_subject(caller, request.headers)
    try:
        agent_guard.preflight(caller, "auto-fix")
    except agent_guard.ScopeDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))

    held = _FIX_PENDING.pop(req.deliver_token, None)
    if not held or held.get("review_id") != review_id:
        raise HTTPException(
            status_code=404,
            detail="That patch has already been delivered, belongs to another review, or "
                   "was generated before the server restarted. Generate it again — the "
                   "quote and the diff both come back before anything is written.")
    try:
        return fix_apply.deliver(orchestrator, review_id, held, req.output,
                                 project_id=review.get("project_id"))
    except fix_apply.FixError as exc:
        # Put it back: a refused delivery has spent nothing and the patch is still good,
        # so the person can choose a different destination rather than pay to regenerate.
        _FIX_PENDING[req.deliver_token] = held
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("Fix delivery failed for review %s", review_id)
        raise HTTPException(status_code=502, detail=f"Could not deliver the patch: {exc}")


# One pending archive per download, keyed by a single-use token. Capped so a long-lived
# process cannot accumulate archives it will never be asked for.
_FIX_ZIPS: dict = {}
_MAX_PENDING_ZIPS = 8


@app.get("/api/reviews/{review_id}/fix/download/{token}",
         summary="Download the patched files from a fix run")
async def fix_download(review_id: str, token: str, request: Request,
                       reader: dict = Depends(require_reader)):
    """Hand over the archive a fix run produced. Single use.

    Single use because it is a copy of the customer's patched source: a URL that keeps
    working is a URL that ends up in a chat log.
    """
    _review_in_scope(review_id, request, reader)
    entry = _FIX_ZIPS.pop(token, None)
    if not entry or entry[1] != review_id:
        raise HTTPException(
            status_code=404,
            detail="That download has already been taken, or belongs to another review. "
                   "Run the fix again to produce a new archive.")
    return Response(
        content=entry[0], media_type="application/zip",
        headers={"Content-Disposition":
                 f'attachment; filename="fixes-{review_id[:8]}.zip"'},
    )


# ── What a review looked at ────────────────────────────────────────────────
#
# A result could say "4 file(s) read, 6 filtered out" and not say which. These two routes
# are the answer, and they are deliberately separate from `GET /api/reviews/{id}`: the
# inventory of a 1,500-file repository has no business travelling inside every read of the
# review row, and it needs paging that a nested field cannot have.
#
# Both are scoped the same way as every other governance read, and answer 404 rather than
# 403 for a review in someone else's project — whether it exists is itself not this
# caller's business.


def _review_in_scope(review_id: str, request: Request, reader: dict) -> dict:
    review = database.get_review(review_id)
    if not review:
        raise HTTPException(status_code=404, detail="Review not found")
    visible = _visible_project_ids(reader, request)
    if visible is not None and review.get("project_id") not in visible:
        raise HTTPException(status_code=404, detail="Review not found")
    return review


@app.get("/api/reviews/{review_id}/sources",
         summary="Which files a review read, and which it filtered out")
async def review_sources(review_id: str, request: Request,
                         included: Optional[bool] = Query(
                             None, description="True for only the files that were reviewed, "
                                               "False for only the exclusions."),
                         limit: int = Query(500, ge=1, le=2000),
                         offset: int = Query(0, ge=0),
                         reader: dict = Depends(require_reader)):
    """The file-by-file record, with each exclusion's reason and each finding's count.

    Content is never returned here — a list endpoint that carried file bodies would be the
    largest response in the app and would defeat the retention window it is describing.
    """
    _review_in_scope(review_id, request, reader)
    return review_source_store.get_inventory(
        review_id, included=included, limit=limit, offset=offset)


@app.get("/api/reviews/{review_id}/sources/file",
         summary="Read one file exactly as the review saw it")
async def review_source_file(review_id: str, request: Request,
                             path: str = Query(..., description="Path as listed by /sources"),
                             reader: dict = Depends(require_reader)):
    """One file, from the stored copy or re-read from its repository at the recorded commit.

    Answers 200 with an `unavailable` reason rather than an error status when the content is
    genuinely gone, because "we never kept this", "the retention window passed" and "the
    token that read this repository no longer works" are three different problems with three
    different fixes, and a 404 for all of them sends someone to the wrong one.
    """
    review = _review_in_scope(review_id, request, reader)
    return review_source_store.get_file(
        review_id, path, project_id=review.get("project_id"))


@app.delete("/api/reviews/{review_id}/sources",
            summary="Delete a review's stored source now, before its window expires")
async def delete_review_sources(review_id: str, request: Request,
                                reader: dict = Depends(require_reader)):
    """Forget the source early.

    Retention is a ceiling, not a commitment to hold something for that long. Somebody who
    uploaded the wrong archive, or who has been asked to remove a copy, should not have to
    wait out a window they chose before they knew that.
    """
    _review_in_scope(review_id, request, reader)
    review_source_store.delete_for_review(review_id)
    return {"deleted": True, "review_id": review_id}


@app.get("/api/reviews/{review_id}")
async def get_review(review_id: str, request: Request, reader: dict = Depends(require_reader)):
    review = database.get_review(review_id)
    if not review:
        raise HTTPException(status_code=404, detail="Review not found")
    visible = _visible_project_ids(reader, request)
    # 404 rather than 403: whether a review exists in someone else's project is itself
    # something this caller has no business learning.
    if visible is not None and review.get("project_id") not in visible:
        raise HTTPException(status_code=404, detail="Review not found")
    # What it cost, read from the billing ledger rather than stored on the row — so the
    # figure on a reopened run is the same number that moved the budget. Absent, not zero,
    # for a run that predates the link: "$0.00" would be a claim about a run whose cost
    # nobody recorded. See `database.get_review_spend`.
    review["spend"] = database.get_review_spend([review_id]).get(review_id)
    # Enough about each parent to render it as a link to the screen that owns it rather
    # than as an eight-character id nobody can act on. A parent that has since been deleted
    # is simply absent here; the id stays in `derived_from`, because what this run was built
    # from does not stop being true when the source is removed.
    review["derived_from_detail"] = database.get_review_parents(review.get("derived_from") or [])
    return review


@app.get("/api/reports")
async def list_reports(request: Request, limit: int = 25, offset: int = 0,
                       reader: dict = Depends(require_reader)):
    """A project's governance reports, newest first.

    Reports were previously reachable only by UUID — there was no way to list them, which
    is why nothing in the UI could show one.
    """
    reports = database.list_reports(limit=limit, offset=offset,
                                    project_ids=_visible_project_ids(reader, request))
    return {"reports": reports, "count": len(reports)}


@app.get("/api/reports/{report_id}")
async def get_report(report_id: str, request: Request, reader: dict = Depends(require_reader)):
    report = database.get_report(report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    # A report is attributed to a project directly (`reports.project_id`, stamped from the
    # request context by `save_report`). It used to be scoped only through the review it
    # came from — but both workflows that write reports pass `review_id=None`
    # (`full_review.py`, `standalone_report.py`), so that path made every report 404 for
    # everyone except a super admin. Falling back to the review keeps rows written before
    # the column existed reachable.
    visible = _visible_project_ids(reader, request)
    if visible is not None:
        project_id = report.get("project_id")
        if project_id is None and report.get("review_id"):
            source = database.get_review(report["review_id"])
            project_id = source.get("project_id") if source else None
        if project_id not in visible:
            raise HTTPException(status_code=404, detail="Report not found")
    return report


# ── Requirement Review ─────────────────────────────────────────────────────

@app.post("/api/review/requirement")
def review_requirement(req: ReviewRequest, project_name: str = ""):
    if not req.content.strip():
        raise HTTPException(status_code=400, detail="Content is required")
    _require_llm()
    try:
        result = orchestrator.review_requirement(
            content=req.content,
            context=req.context or "",
            project_name=project_name,
        )
        return result
    except Exception as e:
        logger.exception("Unhandled error: %s", e)
        status, body = format_error_response(e)
        raise HTTPException(status_code=status, detail=body)


@app.post("/api/review/requirement/upload")
async def review_requirement_file(
    file: UploadFile = File(...),
    project_name: str = Form(""),
    context: str = Form(""),
):
    content = await file.read()
    text = await asyncio.to_thread(_extract_text, content, file.filename or "")
    if not text:
        raise HTTPException(status_code=400, detail="Could not extract text from file")
    try:
        result = await asyncio.to_thread(
            orchestrator.review_requirement,
            content=text,
            context=context,
            project_name=project_name or file.filename,
        )
        return result
    except Exception as e:
        logger.exception("Unhandled error: %s", e)
        status, body = format_error_response(e)
        raise HTTPException(status_code=status, detail=body)


# ── Code Review ────────────────────────────────────────────────────────────

@app.post("/api/review/code")
def review_code(req: CodeReviewRequest, project_name: str = ""):
    if not req.code.strip():
        raise HTTPException(status_code=400, detail="Code is required")
    _require_llm()
    try:
        result = orchestrator.review_code(
            code=req.code,
            language=req.language or "",
            context=req.context or "",
            project_name=project_name,
            pr_url=req.pr_url or "",
        )
        return result
    except Exception as e:
        logger.exception("Unhandled error: %s", e)
        status, body = format_error_response(e)
        raise HTTPException(status_code=status, detail=body)


# ── Prompt Safety Review ───────────────────────────────────────────────────

@app.post("/api/review/prompt")
def review_prompt(req: PromptReviewRequest, project_name: str = ""):
    if not any([req.system_prompt, req.user_prompt, req.tool_definitions, req.rag_config]):
        raise HTTPException(status_code=400, detail="At least one prompt field is required")
    _require_llm()
    try:
        result = orchestrator.review_prompt(
            system_prompt=req.system_prompt or "",
            user_prompt=req.user_prompt or "",
            tool_definitions=req.tool_definitions or "",
            rag_config=req.rag_config or "",
            context=req.context or "",
            project_name=project_name,
        )
        return result
    except Exception as e:
        logger.exception("Unhandled error: %s", e)
        status, body = format_error_response(e)
        raise HTTPException(status_code=status, detail=body)


# ── Full SDLC Review ───────────────────────────────────────────────────────
# Chains up to 7 sequential LLM calls (60-150s+ each) — runs as a background job
# rather than blocking this HTTP request, since it has failed/timed out under a
# synchronous request in every test run to date. Returns a job_id immediately;
# poll GET /api/v1/agents/jobs/{job_id} for step progress and the final result.

@app.post("/api/review/full", status_code=202)
async def full_review(req: FullReviewRequest, request: Request):
    if not any([req.requirement, req.compliance_artifact, req.architecture, req.code, req.prompt]):
        raise HTTPException(status_code=400, detail="At least one artifact is required")
    _require_llm()

    prompt_config = None
    if req.prompt:
        prompt_config = {"system_prompt": req.prompt}

    # Step plan comes from the workflow itself, so this can't drift from what it runs.
    total_steps = count_steps(full_review_workflow.plan_steps(
        requirement=req.requirement, compliance_artifact=req.compliance_artifact,
        architecture=req.architecture, code=req.code, prompt_config=prompt_config,
        include_security=req.include_security, include_vapt=req.include_vapt,
        include_sensitive_data=req.include_sensitive_data,
    ))

    # Guarded here rather than by the legacy middleware: this returns 202 and does its
    # LLM work in a background job, so there is nothing request-scoped left to meter.
    # The preflight runs before the 202 so a denied caller gets 403/402/429, not an
    # accepted job that later fails.
    caller, rejection = legacy_guard.resolve_caller(request.headers)
    if rejection is not None:
        return rejection
    # Stash the subject now: `_run` executes on a background thread with no request
    # headers, so without this the job would meter against no project.
    caller["_subject"] = subject_service.resolve_subject(caller, request.headers)
    # The rate limit has to be applied by hand here for the same reason the metering is:
    # this route is deliberately absent from the legacy middleware's map. Without it the
    # single most expensive product on the platform — up to seven chained LLM calls per
    # click — was the one thing a per-minute limit did not cover.
    check_project_rate_limit(caller["_subject"], request.url.path)
    # Reservation sized from the step plan above rather than the fixed
    # `_EXPECTED_CALLS_BY_PRODUCT["full"]`, since the opt-in stages make the real call
    # count vary from 5 to 10 per request (D-25's contextvar, same as the chunked reviewer).
    with agent_guard.expect_calls(total_steps):
        agent_guard.preflight(caller["_subject"], "full")

    def _run(on_progress):
        # Bound here rather than at the boundary, unlike every other browser route. This
        # one is a 202 job and so is deliberately absent from `legacy_guard`'s product map
        # (the work happens after the response is sent), which means the middleware that
        # normally reads `derived_from` off the request body never sees this call. Bound
        # *inside* `_run` because the body runs on a background thread that does not
        # inherit the request's context -- the same reason `caller["_subject"]` is stashed
        # above rather than resolved here.
        with request_context.bind_derived_from(req.derived_from), \
                agent_guard.expect_calls(total_steps):
            return agent_guard.guarded_call(
                caller, "full", None, skip_preflight=True,
                project_name=req.project_name or "Unnamed Project",
                fn=lambda: orchestrator.full_review(
                    requirement=req.requirement,
                    compliance_artifact=req.compliance_artifact,
                    architecture=req.architecture,
                    code=req.code,
                    prompt_config=prompt_config,
                    test_evidence=req.test_evidence,
                    project_name=req.project_name or "Unnamed Project",
                    context=req.context or "",
                    on_progress=on_progress,
                    include_security=req.include_security,
                    include_vapt=req.include_vapt,
                    include_sensitive_data=req.include_sensitive_data,
                ),
            )

    job_id = job_service.start_job("full", total_steps, _run)
    return {"job_id": job_id, "status": "queued", "poll_url": f"/api/v1/agents/jobs/{job_id}"}


@app.get("/api/review/full/jobs/{job_id}")
async def full_review_job_status(job_id: str, reader: dict = Depends(require_reader)):
    # A completed job carries the full review result, so this is a read of the work
    # product and needs the same credential the rest of them do.
    job = job_service.get_job_status(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"No job found with id '{job_id}'")
    return job


# ── GitHub PR Review ───────────────────────────────────────────────────────

@app.post("/api/github/pr")
def review_github_pr(req: GithubPRRequest):
    if not req.pr_url:
        raise HTTPException(status_code=400, detail="PR URL is required")
    _require_llm()
    try:
        result = orchestrator.review_github_pr(
            pr_url=req.pr_url,
            jira_ticket=req.jira_ticket or "",
            include_prompt_review=req.include_prompt_review,
            post_mode=req.post_mode,
            set_status=req.set_status,
            request_changes=req.request_changes,
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("Unhandled error: %s", e)
        status, body = format_error_response(e)
        raise HTTPException(status_code=status, detail=body)


@app.post("/api/github/pr/async", status_code=202)
async def review_github_pr_async(req: GithubPRRequest, request: Request):
    """Start a PR review, return a job id, and report which stage it is on.

    The synchronous route above stays: it is the documented API surface and
    `verify_pr_review.py` drives it. This one exists because the browser needs to say *what*
    is happening. A PR review fetches a diff, reviews it in n pieces, writes a comment and
    reports a merge check, and behind one spinner and one static sentence all of that is
    indistinguishable from a hang — most of all the reviewing step, which is the slow one.

    Guarded before the 202 for the same reason the review jobs are: a refusal has to reach
    the caller as a 402/403/429 now, not as an accepted job that dies a minute later with
    nobody watching.
    """
    if not req.pr_url:
        raise HTTPException(status_code=400, detail="PR URL is required")
    _require_llm()

    caller, rejection = legacy_guard.resolve_caller(request.headers)
    if rejection is not None:
        return rejection
    # Stashed now: `_run` executes on a background thread with no request headers, and
    # without this the job would meter against no project at all.
    caller["_subject"] = subject_service.resolve_subject(caller, request.headers)
    check_project_rate_limit(caller["_subject"], request.url.path)

    # The real chunk count is not knowable without fetching the diff, which costs a request
    # and is the first thing the job does anyway. `expect_calls` therefore reserves for the
    # documented per-product allowance; the fan-out's own reservation inside the workflow
    # (C-17) is what bounds a large diff.
    try:
        with agent_guard.expect_calls(2):
            agent_guard.preflight(caller["_subject"], "github-pr-review")
    except Exception as exc:
        status, detail = format_error_response(exc, agent_name="github-pr-review")
        raise HTTPException(status, detail)

    def _run(on_progress):
        return agent_guard.guarded_call(
            caller, "github-pr-review", None, skip_preflight=True,
            project_name=req.pr_url,
            fn=lambda: orchestrator.review_github_pr(
                pr_url=req.pr_url,
                jira_ticket=req.jira_ticket or "",
                include_prompt_review=req.include_prompt_review,
                post_mode=req.post_mode,
                set_status=req.set_status,
                request_changes=req.request_changes,
                on_progress=on_progress,
            ),
        )

    from backend.workflows.review_pr import STAGES as _PR_STAGES

    job_id = job_service.start_job("github-pr-review", total_steps=len(_PR_STAGES),
                                   run_fn=_run)
    logger.info("Started PR review job %s for %s (post_mode=%s)",
                job_id, req.pr_url, req.post_mode)
    return {
        "job_id": job_id,
        "status": "accepted",
        "agent": "github-pr-review",
        "stages": list(_PR_STAGES),
        "poll_url": f"/api/review/jobs/{job_id}",
    }


@app.post("/api/github/pr/auto-fix")
def auto_fix_github_pr(req: GithubAutoFixRequest):
    if not req.pr_url:
        raise HTTPException(status_code=400, detail="PR URL is required")
    if not config.GITHUB_TOKEN:
        raise HTTPException(status_code=503, detail="GITHUB_TOKEN not configured")
    _require_llm()
    try:
        result = orchestrator.auto_fix_github_pr(
            pr_url=req.pr_url,
            jira_ticket=req.jira_ticket or "",
            max_files=req.max_files,
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("Unhandled error: %s", e)
        status, body = format_error_response(e)
        raise HTTPException(status_code=status, detail=body)


# ── Release Gate ───────────────────────────────────────────────────────────

@app.post("/api/release-gate")
def release_gate(req: ReleaseGateRequest):
    _require_llm()

    # Routed through the workflow rather than calling `gate_agent` inline. The inline
    # version evaluated the gate and returned the verdict without saving anything, so a
    # release decision made on the Release Gate page — the platform's most consequential
    # output — was the one result that never reached History, could never be picked up by
    # the Governance Report's review picker, and never entered the approval queue. The
    # v1 endpoint for the same product has always persisted; this was two behaviours for
    # one product depending on which door you came through.
    try:
        return orchestrator.run_release_gate(
            project_name=req.project_name or "Unnamed",
            review_ids=req.review_ids,
            release_version=req.release_version or "",
            findings=[f.model_dump() for f in req.findings] if req.findings else None,
        )
    except Exception as e:
        logger.exception("Unhandled error: %s", e)
        status, body = format_error_response(e)
        raise HTTPException(status_code=status, detail=body)


# ── Architecture Review ────────────────────────────────────────────────────

@app.post("/api/review/architecture")
def review_architecture(req: ArchitectureReviewRequest):
    if not req.artifact.strip():
        raise HTTPException(status_code=400, detail="Architecture description is required")
    _require_llm()
    if req.review_mode not in ("review", "recommend", "compare"):
        raise HTTPException(status_code=400, detail="review_mode must be: review | recommend | compare")
    try:
        result = orchestrator.review_architecture(
            artifact=req.artifact,
            requirements=req.requirements or "",
            review_mode=req.review_mode or "review",
            context=req.context or "",
            project_name=req.project_name or "Unnamed Project",
        )
        return result
    except Exception as e:
        logger.exception("Unhandled error: %s", e)
        status, body = format_error_response(e)
        raise HTTPException(status_code=status, detail=body)


def _brief_for(bundle, agent: str):
    """A codebase turned into something a single-call design review can actually read.

    Free — `code_brief.build` calls no model. Refused rather than silently reviewed when the
    input holds no source it can parse at all: an architecture review of an empty inventory
    would return a confident verdict about nothing, which is the degenerate result `attest`
    exists to stop reaching a screen.
    """
    brief = code_brief.build(bundle)
    if not brief["markdown"]:
        raise HTTPException(
            status_code=400,
            detail="No readable source files were found in that input, so there is nothing "
                   "to describe. Check the repository, branch or archive.")
    return brief


def _brief_context(bundle, brief, context: str) -> str:
    """What the agent is told about where this description came from and how solid it is."""
    parts = [f"Source: {bundle.source_type} — {bundle.label}",
             "The description below was computed from the source, not written by a person.",
             brief["coverage_note"]]
    if bundle.untrusted_label:
        parts.append(guardrail_service.untrusted_notice(bundle.untrusted_label))
    if bundle.truncated:
        parts.append(f"The source itself was truncated before analysis: "
                     f"{bundle.truncation_reason}")
    if context:
        parts.append(context)
    return "\n".join(parts)


def _design_review_from_source(bundle, agent: str, req) -> dict:
    """Shared body for the three single-call agents that read a whole codebase.

    Architecture Review, Security Review and Migration Assessment are the same shape of
    question: each returns **one** verdict about the system as a whole, so none of them can
    be fanned out over chunks and merged. `MIGRATION_ASSESSMENT_TOOL_SCHEMA` has one
    `complexity` and one `estimated_effort` the way `ARCHITECTURE_TOOL_SCHEMA` has one
    `quality_scores` — twenty partial answers have no merge rule. All three therefore get
    the computed structural description and make a single call.
    """
    _require_llm()
    brief = _brief_for(bundle, agent)
    ctx = _brief_context(bundle, brief, req.context or "")
    project = req.project_name or bundle.label[:60] or "Unnamed Project"
    if agent == "migration-assessment":
        target = (getattr(req, "migration_target", "") or "").strip()
        if not target:
            raise HTTPException(
                status_code=400,
                detail="A migration target is required — 'assess this codebase' has no "
                       "answer without saying what it is being moved to.")
        result = orchestrator.assess_migration(
            source=brief["markdown"], migration_target=target,
            constraints=getattr(req, "constraints", "") or "",
            context=ctx, project_name=project)
    elif agent == "architecture":
        mode = req.review_mode or "review"
        if mode not in ("review", "recommend", "compare"):
            raise HTTPException(status_code=400,
                                detail="review_mode must be: review | recommend | compare")
        result = orchestrator.review_architecture(
            artifact=brief["markdown"], requirements=req.requirements or "",
            review_mode=mode, context=ctx, project_name=project)
    else:
        result = orchestrator.review_security(
            artifact=brief["markdown"], system_type=req.system_type or "",
            context=ctx, project_name=project)
    # The same coverage facts the prompt was given, returned so the screen prints the
    # identical sentence. Two accounts of how much was read would be one too many.
    result["source"] = bundle.summary()
    result["code_brief"] = {k: brief[k] for k in ("coverage", "coverage_note", "stats", "files")}
    return result


@app.post("/api/review/architecture/source")
def review_architecture_source(req: ArchitectureSourceRequest, request: Request):
    """Architecture review of a codebase — zip, repository at a ref, PR, or branch compare.

    Not a chunked review. See `services/code_brief` for why a holistic question cannot be
    fanned out over chunks, and what is sent instead.
    """
    try:
        bundle = source_bundle.resolve(req, orchestrator.github,
                                       project_id=_project_id_for(request))
    except source_bundle.SourceError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    try:
        return _design_review_from_source(bundle, "architecture", req)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unhandled error: %s", e)
        status, body = format_error_response(e)
        raise HTTPException(status_code=status, detail=body)


@app.post("/api/review/security/source")
def review_security_source(req: SecuritySourceRequest, request: Request):
    """Security review (STRIDE) of a codebase. See `/api/review/architecture/source`."""
    try:
        bundle = source_bundle.resolve(req, orchestrator.github,
                                       project_id=_project_id_for(request))
    except source_bundle.SourceError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    try:
        return _design_review_from_source(bundle, "security", req)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unhandled error: %s", e)
        status, body = format_error_response(e)
        raise HTTPException(status_code=status, detail=body)


class _FormReq:
    """The attribute bag `_design_review_from_source` reads, for the multipart doors.

    A plain object rather than a Pydantic model because the fields arrive as `Form(...)`
    parameters, already validated by FastAPI — building a model here would only re-validate
    strings that are already strings.
    """

    def __init__(self, **kw):
        self.project_name = kw.get("project_name", "")
        self.context = kw.get("context", "")
        self.requirements = kw.get("requirements", "")
        self.review_mode = kw.get("review_mode", "review")
        self.system_type = kw.get("system_type", "")
        self.migration_target = kw.get("migration_target", "")
        self.constraints = kw.get("constraints", "")


# Two upload endpoints rather than one taking an `agent` form field, even though the bodies
# are identical. `legacy_guard.product_for_request` maps a *path* to a product, so one path
# serving two products would meter and entitle both as whichever the map named — a customer
# entitled to Architecture Review could reach the security agent through it.
@app.post("/api/review/architecture/upload-zip")
async def review_architecture_zip(
    file: UploadFile = File(..., description="A zip of the codebase, or a single source file."),
    project_name: str = Form(""),
    requirements: str = Form(""),
    review_mode: str = Form("review"),
    context: str = Form(""),
):
    """Architecture review of an uploaded codebase. A zip travels as multipart, and the
    JSON route cannot take one (D-234), so this is the archive door for the same review."""
    bundle = _upload_bundle(await file.read(), file.filename or "")
    req = _FormReq(project_name=project_name, context=context,
                   requirements=requirements, review_mode=review_mode)
    try:
        return await asyncio.to_thread(_design_review_from_source, bundle, "architecture", req)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unhandled error: %s", e)
        status, body = format_error_response(e)
        raise HTTPException(status_code=status, detail=body)


@app.post("/api/review/security/upload-zip")
async def review_security_zip(
    file: UploadFile = File(..., description="A zip of the codebase, or a single source file."),
    project_name: str = Form(""),
    system_type: str = Form(""),
    context: str = Form(""),
):
    """Security review (STRIDE) of an uploaded codebase. See the architecture twin above."""
    bundle = _upload_bundle(await file.read(), file.filename or "")
    req = _FormReq(project_name=project_name, context=context, system_type=system_type)
    try:
        return await asyncio.to_thread(_design_review_from_source, bundle, "security", req)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unhandled error: %s", e)
        status, body = format_error_response(e)
        raise HTTPException(status_code=status, detail=body)


@app.post("/api/review/architecture/upload")
async def review_architecture_file(
    file: UploadFile = File(...),
    project_name: str = Form(""),
    requirements: str = Form(""),
    review_mode: str = Form("review"),
    context: str = Form(""),
):
    content = await file.read()
    text = await asyncio.to_thread(_extract_text, content, file.filename or "")
    if not text:
        raise HTTPException(status_code=400, detail="Could not extract text from file")
    _require_llm()
    try:
        result = await asyncio.to_thread(
            orchestrator.review_architecture,
            artifact=text,
            requirements=requirements,
            review_mode=review_mode or "review",
            context=context,
            project_name=project_name or file.filename,
        )
        return result
    except Exception as e:
        logger.exception("Unhandled error: %s", e)
        status, body = format_error_response(e)
        raise HTTPException(status_code=status, detail=body)


# ── VAPT ──────────────────────────────────────────────────────────────────

@app.post("/api/review/vapt")
def review_vapt(req: VAPTReviewRequest):
    if not req.artifact.strip():
        raise HTTPException(status_code=400, detail="Artifact content is required")
    _require_llm()
    try:
        result = orchestrator.review_vapt(
            artifact=req.artifact,
            scope=req.scope or "",
            context=req.context or "",
            project_name=req.project_name or "Unnamed Project",
        )
        return result
    except Exception as e:
        logger.exception("Unhandled error: %s", e)
        status, body = format_error_response(e)
        raise HTTPException(status_code=status, detail=body)


@app.post("/api/review/vapt/upload")
async def review_vapt_file(
    file: UploadFile = File(...),
    project_name: str = Form(""),
    scope: str = Form(""),
    context: str = Form(""),
):
    content = await file.read()
    text = await asyncio.to_thread(_extract_text, content, file.filename or "")
    if not text:
        raise HTTPException(status_code=400, detail="Could not extract text from file")
    _require_llm()
    try:
        result = await asyncio.to_thread(
            orchestrator.review_vapt,
            artifact=text,
            scope=scope,
            context=context,
            project_name=project_name or file.filename,
        )
        return result
    except Exception as e:
        logger.exception("Unhandled error: %s", e)
        status, body = format_error_response(e)
        raise HTTPException(status_code=status, detail=body)


# ── Security Review ────────────────────────────────────────────────────────

@app.post("/api/review/security")
def review_security(req: SecurityReviewRequest):
    if not req.artifact.strip():
        raise HTTPException(status_code=400, detail="Artifact content is required")
    _require_llm()
    try:
        result = orchestrator.review_security(
            artifact=req.artifact,
            system_type=req.system_type or "",
            context=req.context or "",
            project_name=req.project_name or "Unnamed Project",
        )
        return result
    except Exception as e:
        logger.exception("Unhandled error: %s", e)
        status, body = format_error_response(e)
        raise HTTPException(status_code=status, detail=body)


@app.post("/api/review/security/upload")
async def review_security_file(
    file: UploadFile = File(...),
    project_name: str = Form(""),
    system_type: str = Form(""),
    context: str = Form(""),
):
    content = await file.read()
    text = await asyncio.to_thread(_extract_text, content, file.filename or "")
    if not text:
        raise HTTPException(status_code=400, detail="Could not extract text from file")
    _require_llm()
    try:
        result = await asyncio.to_thread(
            orchestrator.review_security,
            artifact=text,
            system_type=system_type,
            context=context,
            project_name=project_name or file.filename,
        )
        return result
    except Exception as e:
        logger.exception("Unhandled error: %s", e)
        status, body = format_error_response(e)
        raise HTTPException(status_code=status, detail=body)


# ── Code Generation ─────────────────────────────────────────────────────────

@app.post("/api/code-brief")
def build_code_brief(req: CodeBriefRequest, request: Request,
                     reader: dict = Depends(require_reader)):
    """A structural description of a codebase. **Free — calls no model.**

    The same computation the design agents are given, handed back on its own so a screen
    whose agent is *not* reading the code can still tell it what exists. Code Generation is
    the case: "build something that fits this repository" is a different request from
    "build this from scratch", and the difference is entirely in what the prompt knows.

    Unmetered and mapped to no product, for the reason the `/preflight` routes are:
    it spends nothing, and refusing to describe a repository to somebody near their cap
    tells them less than the description would.
    """
    try:
        bundle = source_bundle.resolve(req, orchestrator.github,
                                       project_id=_project_id_for(request))
    except source_bundle.SourceError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("Code brief source resolution failed")
        raise HTTPException(status_code=502, detail=f"Could not read that source: {exc}")
    brief = _brief_for(bundle, "code-brief")
    return {
        "source": bundle.summary(),
        "markdown": brief["markdown"],
        "coverage": brief["coverage"],
        "coverage_note": brief["coverage_note"],
        "stats": brief["stats"],
    }


@app.post("/api/codegen/advisory")
def codegen_advisory(req: CodeGenAdvisoryRequest):
    if not req.requirement.strip():
        raise HTTPException(status_code=400, detail="Requirement is required")
    _require_llm()
    try:
        pins = {
            "language": req.pin_language,
            "backend_framework": req.pin_backend_framework,
            "frontend_framework": req.pin_frontend_framework,
            "architecture_pattern": req.pin_architecture_pattern,
            "database": req.pin_database,
        }
        result = orchestrator.generate_codegen_advisory(
            requirement=req.requirement,
            constraints=req.constraints or "",
            pins=pins,
            context=req.context or "",
            project_name=req.project_name or req.requirement[:60],
        )
        return result
    except Exception as e:
        logger.exception("Unhandled error: %s", e)
        status, body = format_error_response(e)
        raise HTTPException(status_code=status, detail=body)


@app.post("/api/codegen/scaffold")
def codegen_scaffold(req: CodeGenScaffoldRequest):
    _require_llm()
    try:
        result = orchestrator.generate_codegen_scaffold(
            project_name=req.project_name,
            language=req.language,
            backend_framework=req.backend_framework,
            frontend_framework=req.frontend_framework or "",
            database_choice=req.database or "",
            architecture_pattern=req.architecture_pattern or "Modular Monolith",
            requirement=req.requirement or "",
            include_auth=req.include_auth,
            include_docker=req.include_docker,
            include_tests=req.include_tests,
            auto_review=req.auto_review,
            context=req.context or "",
        )
        return result
    except Exception as e:
        logger.exception("Unhandled error: %s", e)
        status, body = format_error_response(e)
        raise HTTPException(status_code=status, detail=body)


@app.get("/api/codegen/scaffold/{review_id}/download")
async def codegen_scaffold_download(review_id: str, request: Request,
                                    reader: dict = Depends(require_reader)):
    # The UI used to link here with a plain <a href>, which cannot carry a token — it now
    # fetches the zip through apiFetch and saves the blob instead (see downloadScaffold in
    # app.js). The v1 twin of this route has always required a credential.
    review = database.get_review(review_id)
    if not review or review.get("review_type") != "codegen_scaffold":
        raise HTTPException(status_code=404, detail="Scaffold not found")
    # Generated source code, so it is scoped like any other review — a credential alone
    # must not be enough to pull another project's scaffold by id.
    visible = _visible_project_ids(reader, request)
    if visible is not None and review.get("project_id") not in visible:
        raise HTTPException(status_code=404, detail="Scaffold not found")
    files = review["result"].get("files", [])
    if not files:
        raise HTTPException(status_code=404, detail="No files stored for this scaffold")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            zf.writestr(f.get("path", "file.txt"), f.get("content", ""))
    buf.seek(0)
    project = str(review["result"].get("project_name", "scaffold")).replace(" ", "_")
    return StreamingResponse(
        buf, media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{project}.zip"'},
    )


_DELIVERABLE_RUNS = {
    "codegen_scaffold": {"prefix": "lm-scaffold", "what": "generated starter project"},
    "migration_execute": {"prefix": "lm-migration", "what": "migrated code"},
}


@app.post("/api/reviews/{review_id}/deliver-code")
async def deliver_generated_code(review_id: str, req: DeliverGeneratedCodeRequest,
                                 request: Request, reader: dict = Depends(require_reader)):
    """Commit a finished run's generated files to a new branch, optionally opening a PR.

    Until this existed, "generate the code for this requirement" ended at a zip somebody had
    to unpack and copy in by hand, which is most of the value of generating it.

    Scoped exactly like the zip download beside it: generated source is a review, and a
    credential alone must not reach another project's. Makes **no model call** — the files
    were produced and stored when the run happened — so nothing is metered here. It is
    audited, before the write, by `code_delivery.deliver`.
    """
    review = database.get_review(review_id)
    if not review or review.get("review_type") not in _DELIVERABLE_RUNS:
        raise HTTPException(status_code=404, detail="No deliverable run with that id")
    visible = _visible_project_ids(reader, request)
    if visible is not None and review.get("project_id") not in visible:
        raise HTTPException(status_code=404, detail="No deliverable run with that id")

    spec = _DELIVERABLE_RUNS[review["review_type"]]
    result = review.get("result") or {}
    files = result.get("files") or []

    # What checked this code, said plainly, because it goes into the pull request body and
    # the person merging it is entitled to know before they read a line. A run without
    # `auto_review` has had nothing look at it at all.
    gov = result.get("governance_review") or {}
    if gov:
        reviewed = (f"Reviewed by the platform's Code Review and Sensitive Data agents — "
                    f"gate {gov.get('gate_status', 'Unknown')}, "
                    f"{len(gov.get('findings') or [])} finding(s).")
    else:
        reviewed = ("NOT reviewed. This run was produced without the pre-release check, so "
                    "no agent has read this code.")

    branch = (req.branch or "").strip() or code_delivery.branch_name(spec["prefix"], review_id)
    project = str(result.get("project_name") or review.get("project_name") or "").strip()
    message = (req.commit_message or "").strip() or (
        f"Add {spec['what']}" + (f" for {project}" if project else ""))
    try:
        return await asyncio.to_thread(
            code_delivery.deliver,
            files, req.github_repo_url,
            project_id=review.get("project_id"),
            branch=branch,
            base_branch=req.base_branch or "",
            commit_message=message,
            open_pr=bool(req.open_pr),
            pr_title=(req.pr_title or "").strip() or message,
            pr_body=code_delivery.pr_body_for(
                review["review_type"], review_id, reviewed,
                result.get("summary") or result.get("migration_summary") or ""),
            token=(req.github_token or "").strip(),
            review_id=review_id,
            review_type=review["review_type"],
            actor=str(reader.get("username") or reader.get("sub") or "system"),
        )
    except code_delivery.DeliveryError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as e:
        logger.exception("Could not deliver generated code for %s", review_id)
        status, body = format_error_response(e)
        raise HTTPException(status_code=status, detail=body)


# ── AI Code Provenance & Risk ────────────────────────────────────────────────

@app.post("/api/review/ai-provenance")
def review_ai_provenance(req: AIProvenanceRequest):
    if not (req.code or "").strip():
        raise HTTPException(status_code=400, detail="Code is required")
    _require_llm()
    disclosure = {
        "ai_used": req.ai_used,
        "ai_tool": req.ai_tool,
        "purpose": req.purpose,
        "percent_ai_assisted": req.percent_ai_assisted,
        "sensitive_data_shared": req.sensitive_data_shared,
        "prompt_stored": req.prompt_stored,
        "human_review_completed": req.human_review_completed,
    }
    try:
        result = orchestrator.analyze_ai_provenance(
            artifact=req.code,
            disclosure=disclosure,
            context=req.context or "",
            project_name=req.project_name or "Unnamed Project",
        )
        return result
    except Exception as e:
        logger.exception("Unhandled error: %s", e)
        status, body = format_error_response(e)
        raise HTTPException(status_code=status, detail=body)


# ── Code Migration ─────────────────────────────────────────────────────────

@app.post("/api/review/migration-assessment")
def review_migration_assessment(req: MigrationAssessmentRequest):
    if not (req.code or "").strip():
        raise HTTPException(status_code=400, detail="Code is required")
    if not req.migration_target.strip():
        raise HTTPException(status_code=400, detail="migration_target is required")
    _require_llm()
    try:
        result = orchestrator.assess_migration(
            source=req.code,
            migration_target=req.migration_target,
            constraints=req.constraints or "",
            context=req.context or "",
            project_name=req.project_name or "Unnamed Project",
        )
        return result
    except Exception as e:
        logger.exception("Unhandled error: %s", e)
        status, body = format_error_response(e)
        raise HTTPException(status_code=status, detail=body)


@app.post("/api/review/migration-assessment/source")
def review_migration_assessment_source(req: MigrationAssessmentSourceRequest, request: Request):
    """Assess a migration from the codebase rather than from one pasted file.

    Reuses the architecture path wholesale: one holistic question, so a computed structural
    description and a single call. The **language caveat matters more here than anywhere
    else** — migration assessment is most wanted for the oldest stacks, and `code_brief`
    only builds real dependency edges for Python, PHP, Java/Kotlin/Scala, C#/VB.NET and
    Delphi/Pascal. A COBOL or RPG codebase degrades to an annotated file listing, and the
    brief's own coverage note says so in the prompt and on the screen.
    """
    try:
        bundle = source_bundle.resolve(req, orchestrator.github,
                                       project_id=_project_id_for(request))
    except source_bundle.SourceError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    try:
        return _design_review_from_source(bundle, "migration-assessment", req)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unhandled error: %s", e)
        status, body = format_error_response(e)
        raise HTTPException(status_code=status, detail=body)


@app.post("/api/review/migration-assessment/upload-zip")
async def review_migration_assessment_zip(
    file: UploadFile = File(..., description="A zip of the codebase, or a single source file."),
    project_name: str = Form(""),
    migration_target: str = Form(""),
    constraints: str = Form(""),
    context: str = Form(""),
):
    """The archive door for the assessment above — a zip cannot travel on a JSON route."""
    bundle = _upload_bundle(await file.read(), file.filename or "")
    req = _FormReq(project_name=project_name, context=context,
                   migration_target=migration_target, constraints=constraints)
    try:
        return await asyncio.to_thread(
            _design_review_from_source, bundle, "migration-assessment", req)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unhandled error: %s", e)
        status, body = format_error_response(e)
        raise HTTPException(status_code=status, detail=body)


@app.post("/api/review/migration-assessment/preflight")
def preflight_migration_assessment(req: MigrationAssessmentSourceRequest, request: Request):
    """What assessing this codebase will cost. Free — neither the brief nor the quote calls
    a model. Deliberately does not require `migration_target`: someone sizing the job before
    committing to a target should not have to invent one to get a price."""
    return _preflight_design(req, request, "migration-assessment")


@app.post("/api/review/migration-execute")
def review_migration_execute(req: MigrationExecutionRequest):
    if not (req.code or "").strip():
        raise HTTPException(status_code=400, detail="Code is required")
    if not req.migration_target.strip():
        raise HTTPException(status_code=400, detail="migration_target is required")
    _require_llm()
    try:
        result = orchestrator.execute_migration(
            source=req.code,
            migration_target=req.migration_target,
            file_path=req.file_path or "",
            requirement=req.requirement or "",
            auto_review=req.auto_review,
            context=req.context or "",
            project_name=req.project_name or "Unnamed Project",
        )
        return result
    except Exception as e:
        logger.exception("Unhandled error: %s", e)
        status, body = format_error_response(e)
        raise HTTPException(status_code=status, detail=body)


# ── 21 CFR Part 11 Verification ──────────────────────────────────────────────

_CFR21_EXCLUDED_DIRS = {
    "node_modules", ".git", "__pycache__", "venv", ".venv", "dist", "build",
    ".next", "target", "vendor", ".idea", ".vscode",
}
_CFR21_ALLOWED_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".go", ".rb", ".php", ".cs",
    ".sql", ".yaml", ".yml", ".json", ".env", ".md", ".html", ".cshtml",
}
_CFR21_MAX_FILES = 60
_CFR21_MAX_TOTAL_CHARS = 120_000


def _regulated_files(bundle) -> dict:
    """The regulated products' file intake — Part 11 and GxP — with its caps made visible.

    **On the same ingester as everything else.** This used to be a second, weaker
    implementation: a bare `zipfile` walk with an excluded directory list and an extension
    filter, and **no secret redaction, no path-traversal guard, no symlink guard and no
    zip-bomb cap**. So a customer uploading their codebase to a 21 CFR Part 11 verification
    had their hardcoded credentials sent to the model verbatim, while the identical upload
    to Code Review had them replaced with `__REDACTED_ASSIGNED_CREDENTIAL__` first. One of
    those is the product's stated behaviour and the other was an accident of having two
    doors. Callers now build the `Bundle` through `source_bundle` — from a zip, or from a
    repository at a ref — and this only applies the regulated bounds to it.

    The 60-file / 120,000-character caps stay: they bound what one regulated assessment is
    priced and split against, and `review_estimate.for_regulated_files` is calibrated on
    them. A repository makes them much easier to hit than a hand-assembled archive did,
    which is why the cap is now **reported rather than applied silently**. Before this, a
    400-file system had 60 files assessed and the report said nothing about the other 340 —
    and `attest.coverage_claim` could not catch it, because it reconciles the model's claim
    against *the input it was given*, which was already the truncated 60. So the gap was
    invisible at exactly the point an FDA-facing report is least allowed to overstate its
    coverage. The note returned here goes into the prompt and onto the screen, the same
    sentence in both places.
    """
    files, total_chars, considered, oversize = [], 0, 0, []
    for entry in bundle.files:
        text = (entry.get("content") or "")
        if not text.strip():
            continue
        considered += 1
        if len(files) >= _CFR21_MAX_FILES:
            continue
        # **A file goes in whole or not at all.** This used to be
        # `text[:remaining]`, which cut the last file that fit in half and handed the model
        # a partial file with nothing saying so — and "no audit-trail write in this file" is
        # then a *wrong* finding in an FDA-facing report whenever the write was in the half
        # that was dropped. Skipping it instead keeps every file the assessment sees
        # complete, and the file lands in the coverage note as unread, which is true.
        if total_chars + len(text) > _CFR21_MAX_TOTAL_CHARS:
            if len(text) > _CFR21_MAX_TOTAL_CHARS:
                oversize.append(entry.get("path") or "")
            continue
        files.append({"path": entry.get("path") or "", "content": text})
        total_chars += len(text)

    capped = considered > len(files)
    note = ""
    if capped:
        note = (
            f"Coverage limit: this source holds {considered:,} readable files and this "
            f"assessment covers {len(files):,} of them "
            f"({total_chars:,} characters). The remaining {considered - len(files):,} were "
            f"not read, so no conclusion here — including a compliant one — applies to "
            f"them. Split the system and assess it in parts to cover all of it.")
        if oversize:
            # Named, because this one is not "we ran out of room" — the file is larger than
            # the whole budget, so it can never be covered by splitting the *system*, only
            # by splitting the file. Somebody trying to get to full coverage needs to know
            # which case they are in.
            note += (
                f" {len(oversize)} file(s) are individually larger than one assessment's "
                f"whole budget and can never be included by re-running: "
                f"{', '.join(oversize[:5])}"
                f"{'…' if len(oversize) > 5 else ''}.")
    return {"files": files, "considered": considered, "assessed": len(files),
            "chars": total_chars, "capped": capped, "cap_note": note,
            "oversize": oversize}


def _regulated_context(bundle, batch: dict, context: str) -> str:
    """What a regulated assessment is told about where its files came from.

    The cap note is first because it is the one thing that changes how the *report* may be
    read: an assessment of 60 files out of 400 is not an assessment of the system.
    """
    parts = [f"Source: {bundle.source_type} — {bundle.label}"]
    if batch["cap_note"]:
        parts.append(batch["cap_note"])
    if bundle.untrusted_label:
        parts.append(guardrail_service.untrusted_notice(bundle.untrusted_label))
    if bundle.truncated:
        parts.append(f"The source was truncated before analysis: {bundle.truncation_reason}")
    if context:
        parts.append(context)
    return "\n".join(parts)


def _run_regulated_bundle(bundle, agent: str, system_description: str,
                          project_name: str, context: str) -> dict:
    """Shared body for a Part 11 verification or a GxP validation plan over a codebase.

    These get the **real files**, not the structural brief the design agents get: "is there
    an audit-trail write when a record changes" is a question about the code, and a summary
    of a codebase cannot answer it. That makes them the same shape as Code Review — which is
    why `cfr21_agent.verify` already fans out over `chunked_review` when the prompt does not
    fit (D-354) and needed no change to accept a repository.
    """
    _require_llm()
    batch = _regulated_files(bundle)
    if not batch["files"]:
        raise HTTPException(
            status_code=400,
            detail="No readable source files were found in that input. Check the "
                   "repository, branch or archive — a regulated assessment reads code, "
                   "not documentation.")
    ctx = _regulated_context(bundle, batch, context)
    project = project_name or bundle.label[:60] or "Unnamed Project"
    runner = (orchestrator.verify_cfr21 if agent == "cfr21-verification"
              else orchestrator.plan_gxp_validation_package)
    result = runner(files=batch["files"], system_description=system_description,
                    context=ctx, project_name=project)
    result["source"] = bundle.summary()
    # The same three numbers the prompt was given and the quote was based on. One account
    # of how much was read, not three.
    result["coverage_limit"] = {k: batch[k] for k in
                                ("considered", "assessed", "chars", "capped", "cap_note")}
    return result


def _preflight_regulated(bundle, agent: str, request: Request) -> dict:
    """What a regulated assessment of this source will cost. Free — no model call."""
    batch = _regulated_files(bundle)
    if not batch["files"]:
        raise HTTPException(
            status_code=400,
            detail="No readable source files were found in that input.")
    quote = review_estimate.for_regulated_files(batch["files"], agent=agent)
    subject = _ui_subject(request)
    headroom = agent_guard.remaining_budget(subject) if subject is not None else None
    return {
        "agent": agent,
        "source": bundle.summary(),
        "estimate": quote,
        "budget": headroom,
        "explanation": review_estimate.describe(
            quote, cap_usd=(headroom or {}).get("remaining_usd")),
        "within_budget": (
            None if headroom is None else quote["expected_usd"] <= headroom["remaining_usd"]),
        "coverage_limit": {k: batch[k] for k in
                           ("considered", "assessed", "chars", "capped", "cap_note")},
        "files_preview": [f["path"] for f in batch["files"]][:200],
        "skipped_all": bundle.skipped[:400],
    }


@app.post("/api/review/cfr21-verification")
def review_cfr21_verification(req: CFR21VerificationRequest):
    files = [{"path": f.path, "content": f.content} for f in (req.files or [])]
    if not files and not (req.code or "").strip():
        raise HTTPException(status_code=400, detail="Provide `files` or `code`")
    _require_llm()
    try:
        result = orchestrator.verify_cfr21(
            files=files,
            code=req.code or "" if not files else "",
            system_description=req.system_description or "",
            context=req.context or "",
            project_name=req.project_name or "Unnamed Project",
        )
        return result
    except Exception as e:
        logger.exception("Unhandled error: %s", e)
        status, body = format_error_response(e)
        raise HTTPException(status_code=status, detail=body)


@app.post("/api/review/cfr21-verification/preflight-zip")
async def preflight_cfr21_upload_zip(request: Request, file: UploadFile = File(...)):
    """What a Part 11 verification of this zip will cost. **Free — no model call.**

    The browser's Part 11 screen uploads a zip, so a quote that existed only on the JSON
    API would be a quote no actual user ever saw. Reads and splits the archive exactly as
    the run will, and stops.

    Now built by `_preflight_regulated`, the same function the repository door uses. It was
    a second implementation, and it had drifted twice over: no budget line, and no statement
    of the 60-file cap — so a 400-file archive was quoted as though all of it would be read.
    """
    if not (file.filename or "").endswith(".zip"):
        raise HTTPException(status_code=400, detail="Only .zip files are accepted")
    content = await file.read()
    try:
        bundle = await asyncio.to_thread(
            source_bundle.from_zip_bytes, content, "upload", _CFR21_ALLOWED_EXTENSIONS)
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="Not a valid zip file")
    except source_bundle.SourceError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _preflight_regulated(bundle, "cfr21-verification", request)


@app.post("/api/review/cfr21-verification/upload-zip")
async def review_cfr21_verification_upload_zip(
    file: UploadFile = File(...),
    system_description: str = Form(""),
    project_name: str = Form(""),
    context: str = Form(""),
):
    if not (file.filename or "").endswith(".zip"):
        raise HTTPException(status_code=400, detail="Only .zip files are accepted")
    content = await file.read()
    try:
        bundle = await asyncio.to_thread(
            source_bundle.from_zip_bytes, content, file.filename or "upload",
            _CFR21_ALLOWED_EXTENSIONS)
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="Not a valid zip file")
    except source_bundle.SourceError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    try:
        result = await asyncio.to_thread(
            _run_regulated_bundle, bundle, "cfr21-verification",
            system_description, project_name or (file.filename or "Unnamed Project"),
            context)
        # Kept as a count for the screen that has always read it as one.
        result["files_extracted"] = result["coverage_limit"]["assessed"]
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unhandled error: %s", e)
        status, body = format_error_response(e)
        raise HTTPException(status_code=status, detail=body)


@app.post("/api/review/cfr21-verification/source")
def review_cfr21_source(req: RegulatedSourceRequest, request: Request):
    """A Part 11 verification of a repository, branch, pull request or file list.

    The archive door has always existed; this is the same assessment reached from a
    repository at a ref, which is how a validated system is usually kept. Both go through
    `_regulated_files`, so a repo and a zip of that repo are priced, split and capped
    identically.
    """
    try:
        bundle = source_bundle.resolve(req, orchestrator.github,
                                       extensions=_CFR21_ALLOWED_EXTENSIONS,
                                       project_id=_project_id_for(request))
    except source_bundle.SourceError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    try:
        return _run_regulated_bundle(bundle, "cfr21-verification",
                                     req.system_description or "",
                                     req.project_name or "", req.context or "")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unhandled error: %s", e)
        status, body = format_error_response(e)
        raise HTTPException(status_code=status, detail=body)


@app.post("/api/review/cfr21-verification/preflight")
def preflight_cfr21_source(req: RegulatedSourceRequest, request: Request):
    """What a Part 11 verification of this repository will cost. Free — no model call.

    Separate from `/preflight-zip` because a zip cannot travel on a JSON route (D-234), not
    because they answer different questions: both call `_preflight_regulated`.
    """
    try:
        bundle = source_bundle.resolve(req, orchestrator.github,
                                       extensions=_CFR21_ALLOWED_EXTENSIONS,
                                       project_id=_project_id_for(request))
    except source_bundle.SourceError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("Part 11 preflight source resolution failed")
        raise HTTPException(status_code=502, detail=f"Could not read that source: {exc}")
    return _preflight_regulated(bundle, "cfr21-verification", request)


# ── GxP Document Check ────────────────────────────────────────────────────────

@app.post("/api/review/gxp-document-check")
def review_gxp_document_check(req: GxPDocumentCheckRequest):
    documents = [{"name": d.name, "content": d.content} for d in (req.documents or [])]
    if not documents and not (req.content or "").strip():
        raise HTTPException(status_code=400, detail="Provide `documents` or `content`")
    _require_llm()
    try:
        result = orchestrator.check_gxp_documents(
            documents=documents,
            content=req.content or "" if not documents else "",
            document_type_hint=req.document_type_hint or "",
            context=req.context or "",
            project_name=req.project_name or "Unnamed Project",
        )
        return result
    except Exception as e:
        logger.exception("Unhandled error: %s", e)
        status, body = format_error_response(e)
        raise HTTPException(status_code=status, detail=body)


@app.post("/api/review/gxp-document-check/upload")
async def review_gxp_document_check_upload(
    files: List[UploadFile] = File(...),
    document_type_hint: str = Form(""),
    project_name: str = Form(""),
    context: str = Form(""),
):
    _require_llm()
    documents = []
    for f in files:
        content_bytes = await f.read()
        text = await asyncio.to_thread(_extract_text, content_bytes, f.filename or "")
        if text.strip():
            documents.append({"name": f.filename or "document", "content": text})
    if not documents:
        raise HTTPException(status_code=400, detail="Could not extract text from any uploaded file")
    try:
        result = await asyncio.to_thread(
            orchestrator.check_gxp_documents,
            documents=documents,
            document_type_hint=document_type_hint,
            context=context,
            project_name=project_name or (files[0].filename if files else "Unnamed Project"),
        )
        result["files_uploaded"] = len(documents)
        return result
    except Exception as e:
        logger.exception("Unhandled error: %s", e)
        status, body = format_error_response(e)
        raise HTTPException(status_code=status, detail=body)


# ── GxP Validation Document Generation ───────────────────────────────────────

@app.post("/api/review/gxp-generate-plan")
def review_gxp_generate_plan(req: GxPValidationPlanRequest):
    files = [{"path": f.path, "content": f.content} for f in (req.files or [])]
    if not files and not (req.code or "").strip():
        raise HTTPException(status_code=400, detail="Provide `files` or `code`")
    _require_llm()
    try:
        result = orchestrator.plan_gxp_validation_package(
            files=files,
            code=req.code or "" if not files else "",
            system_description=req.system_description or "",
            context=req.context or "",
            project_name=req.project_name or "Unnamed Project",
        )
        return result
    except Exception as e:
        logger.exception("Unhandled error: %s", e)
        status, body = format_error_response(e)
        raise HTTPException(status_code=status, detail=body)


@app.post("/api/review/gxp-generate-plan/upload-zip")
async def review_gxp_generate_plan_upload_zip(
    file: UploadFile = File(...),
    system_description: str = Form(""),
    project_name: str = Form(""),
    context: str = Form(""),
):
    if not (file.filename or "").endswith(".zip"):
        raise HTTPException(status_code=400, detail="Only .zip files are accepted")
    content = await file.read()
    try:
        bundle = await asyncio.to_thread(
            source_bundle.from_zip_bytes, content, file.filename or "upload",
            _CFR21_ALLOWED_EXTENSIONS)
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="Not a valid zip file")
    except source_bundle.SourceError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    try:
        result = await asyncio.to_thread(
            _run_regulated_bundle, bundle, "gxp-generate-plan",
            system_description, project_name or (file.filename or "Unnamed Project"),
            context)
        # Return the extracted files too, so the frontend can reuse them for
        # gxp-generate-document calls without re-uploading the zip for every document.
        result["files_extracted"] = await asyncio.to_thread(
            lambda: _regulated_files(bundle)["files"])
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unhandled error: %s", e)
        status, body = format_error_response(e)
        raise HTTPException(status_code=status, detail=body)


@app.post("/api/review/gxp-generate-plan/source")
def review_gxp_plan_source(req: RegulatedSourceRequest, request: Request):
    """A GxP validation plan built from a repository rather than an uploaded archive.

    Same intake and same caps as the Part 11 twin above — see `_run_regulated_bundle`. This
    one makes a single model call (`gxp_generator_agent.plan` does not chunk), so it takes
    no `_CHUNKABLE_ROUTES` entry.
    """
    try:
        bundle = source_bundle.resolve(req, orchestrator.github,
                                       extensions=_CFR21_ALLOWED_EXTENSIONS,
                                       project_id=_project_id_for(request))
    except source_bundle.SourceError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    try:
        result = _run_regulated_bundle(bundle, "gxp-generate-plan",
                                       req.system_description or "",
                                       req.project_name or "", req.context or "")
        # The files travel back for the same reason the zip door returns them: generating
        # each document is a separate call that needs the same source, and re-reading the
        # repository per document would be several more GitHub round trips for bytes the
        # browser already has.
        result["files_extracted"] = _regulated_files(bundle)["files"]
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unhandled error: %s", e)
        status, body = format_error_response(e)
        raise HTTPException(status_code=status, detail=body)


@app.post("/api/review/gxp-generate-plan/preflight")
def preflight_gxp_plan_source(req: RegulatedSourceRequest, request: Request):
    """What a GxP validation plan for this repository will cost. Free — no model call."""
    try:
        bundle = source_bundle.resolve(req, orchestrator.github,
                                       extensions=_CFR21_ALLOWED_EXTENSIONS,
                                       project_id=_project_id_for(request))
    except source_bundle.SourceError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("GxP plan preflight source resolution failed")
        raise HTTPException(status_code=502, detail=f"Could not read that source: {exc}")
    return _preflight_regulated(bundle, "gxp-generate-plan", request)


@app.post("/api/review/gxp-generate-plan/preflight-zip")
async def preflight_gxp_plan_zip(request: Request, file: UploadFile = File(...)):
    """The archive twin of the quote above. This screen had no cost preview at all, which
    made it the one regulated door where the first number a customer saw was the invoice."""
    if not (file.filename or "").endswith(".zip"):
        raise HTTPException(status_code=400, detail="Only .zip files are accepted")
    content = await file.read()
    try:
        bundle = await asyncio.to_thread(
            source_bundle.from_zip_bytes, content, "upload", _CFR21_ALLOWED_EXTENSIONS)
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="Not a valid zip file")
    except source_bundle.SourceError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _preflight_regulated(bundle, "gxp-generate-plan", request)


@app.post("/api/review/gxp-generate-document")
def review_gxp_generate_document(req: GxPDocumentGenerateRequest):
    files = [{"path": f.path, "content": f.content} for f in (req.files or [])]
    if not files and not (req.code or "").strip():
        raise HTTPException(status_code=400, detail="Provide `files` or `code`")
    if not req.document_type.strip():
        raise HTTPException(status_code=400, detail="document_type is required")
    _require_llm()
    try:
        result = orchestrator.generate_gxp_document(
            document_type=req.document_type,
            files=files,
            code=req.code or "" if not files else "",
            system_description=req.system_description or "",
            outline=req.outline,
            plan_context=req.plan_context or "",
            context=req.context or "",
            project_name=req.project_name or "Unnamed Project",
        )
        return result
    except Exception as e:
        logger.exception("Unhandled error: %s", e)
        status, body = format_error_response(e)
        raise HTTPException(status_code=status, detail=body)


# ── Issue Triaging Agent ─────────────────────────────────────────────────────

@app.post("/api/review/issue-triage")
def review_issue_triage(req: IssueTriageRequest):
    title, description = req.title or "", req.description or ""
    if req.github_issue_url and not (title.strip() or description.strip()):
        from backend.services.github_service import GitHubService
        try:
            gh = GitHubService().get_issue(req.github_issue_url)
        except Exception as e:
            logger.exception("GitHub issue fetch failed for %s", req.github_issue_url)
            raise HTTPException(status_code=502, detail=f"GitHub fetch failed: {e}")
        title, description = gh.get("title", ""), gh.get("body", "")
    if not (title.strip() or description.strip()):
        raise HTTPException(status_code=400, detail="Supply `title`/`description`, or `github_issue_url`")
    _require_llm()
    try:
        result = orchestrator.triage_issue(
            title=title, description=description, logs=req.logs or "", stack_trace=req.stack_trace or "",
            error_code=req.error_code or "", environment=req.environment or "", application=req.application or "",
            module=req.module or "", reporter=req.reporter or "", build_version=req.build_version or "",
            test_case_ref=req.test_case_ref or "", customer_impact_notes=req.customer_impact_notes or "",
            team_roster=req.team_roster or "", context=req.context or "",
            project_name=req.project_name or "Unnamed Project",
        )
        return result
    except Exception as e:
        logger.exception("Unhandled error: %s", e)
        status, body = format_error_response(e)
        raise HTTPException(status_code=status, detail=body)


@app.post("/api/review/issue-triage/batch-github")
def review_issue_triage_batch_github(req: IssueTriageBatchGitHubRequest):
    if not req.github_repo_url.strip():
        raise HTTPException(status_code=400, detail="github_repo_url is required")
    _require_llm()
    try:
        result = orchestrator.triage_github_repo_issues(
            github_repo_url=req.github_repo_url, limit=req.limit, team_roster=req.team_roster or "",
            context=req.context or "", project_name=req.project_name or req.github_repo_url,
        )
        return result
    except Exception as e:
        logger.exception("Unhandled error: %s", e)
        status, body = format_error_response(e)
        raise HTTPException(status_code=status, detail=body)


@app.post("/api/review/issue-triage/batch-github/async", status_code=202)
async def review_issue_triage_batch_github_async(req: IssueTriageBatchGitHubRequest,
                                                 request: Request):
    """Start a repository-wide triage and return a job id.

    The batch is **serial by design** — each issue re-reads the project's triage history so
    later issues can be matched as duplicates of earlier ones from the same run — which makes
    it one model call per issue, in sequence. Forty issues is forty sequential calls, and the
    synchronous twin holds the connection for all of them while reporting nothing. That is
    both a long spinner and a real risk of a proxy timeout with the spend already incurred.

    Progress names the issue being read, not just a count: "12 of 40" cannot distinguish a
    batch that is working from one that is stuck on issue 12.

    The synchronous route is kept — it is the documented surface, and this route is
    deliberately absent from `legacy_guard`'s product map for the reason the other 202
    routes are (see `_start_review_job`): the work happens after the response is sent, so
    `guarded_call` inside the job is what meters it.
    """
    if not req.github_repo_url.strip():
        raise HTTPException(status_code=400, detail="github_repo_url is required")
    _require_llm()

    caller, rejection = legacy_guard.resolve_caller(request.headers)
    if rejection is not None:
        return rejection
    caller["_subject"] = subject_service.resolve_subject(caller, request.headers)
    check_project_rate_limit(caller["_subject"], request.url.path)

    # One call per issue, capped by `limit`. An upper bound rather than the real count: the
    # real count needs a GitHub round trip, which is work this route exists to move off the
    # request thread. The first progress report replaces it with the number actually found,
    # before the bar has moved, so it can only revise downward.
    expected = max(int(req.limit or 10), 1)
    with agent_guard.expect_calls(expected):
        agent_guard.preflight(caller["_subject"], "issue-triage-batch-github")

    project_name = req.project_name or req.github_repo_url

    def _run(on_progress):
        with agent_guard.expect_calls(expected):
            return agent_guard.guarded_call(
                caller, "issue-triage-batch-github", None, skip_preflight=True,
                project_name=project_name,
                fn=lambda: orchestrator.triage_github_repo_issues(
                    github_repo_url=req.github_repo_url, limit=req.limit,
                    team_roster=req.team_roster or "", context=req.context or "",
                    project_name=project_name, on_progress=on_progress,
                ),
            )

    job_id = job_service.start_job("issue-triage-batch-github",
                                   total_steps=expected, run_fn=_run)
    return {
        "job_id": job_id,
        "status": "accepted",
        "agent": "issue-triage-batch-github",
        "unit": "issue",
        "estimate": {"calls": expected},
        "poll_url": f"/api/review/jobs/{job_id}",
    }


# ── QA Intelligence — Test Scenario / Test Case Generation ──────────────────

@app.post("/api/review/test-scenario")
def review_test_scenario(req: ReviewRequest, project_name: str = ""):
    if not req.content.strip():
        raise HTTPException(status_code=400, detail="Content is required")
    _require_llm()
    try:
        result = orchestrator.generate_test_scenarios(
            requirement=req.content, context=req.context or "", project_name=project_name or "Unnamed Project",
        )
        return result
    except Exception as e:
        logger.exception("Unhandled error: %s", e)
        status, body = format_error_response(e)
        raise HTTPException(status_code=status, detail=body)


class TestCaseInternalRequest(BaseModel):
    content: str
    scenarios_context: Optional[str] = None
    context: Optional[str] = None
    project_name: Optional[str] = "Unnamed Project"


@app.post("/api/review/test-case")
def review_test_case(req: TestCaseInternalRequest):
    if not req.content.strip():
        raise HTTPException(status_code=400, detail="Content is required")
    _require_llm()
    try:
        result = orchestrator.generate_test_cases(
            requirement=req.content, scenarios_context=req.scenarios_context or "",
            context=req.context or "", project_name=req.project_name or "Unnamed Project",
        )
        return result
    except Exception as e:
        logger.exception("Unhandled error: %s", e)
        status, body = format_error_response(e)
        raise HTTPException(status_code=status, detail=body)


# ── Sensitive Data Detector ────────────────────────────────────────────────

class SensitiveDataRequest(BaseModel):
    content: str
    context: Optional[str] = None
    project_name: Optional[str] = "Unnamed"


@app.post("/api/review/sensitive-data")
def review_sensitive_data(req: SensitiveDataRequest):
    if not req.content.strip():
        raise HTTPException(status_code=400, detail="Content is required")
    _require_llm()
    try:
        result = orchestrator.scan_sensitive_data(
            content=req.content, context=req.context or "", project_name=req.project_name or "Unnamed Project",
        )
        return result
    except Exception as e:
        logger.exception("Unhandled error: %s", e)
        status, body = format_error_response(e)
        raise HTTPException(status_code=status, detail=body)


@app.post("/api/review/sensitive-data/source")
def review_sensitive_source(req: SensitiveDataSourceRequest, request: Request):
    """A sensitive-data scan over a whole codebase — zip, repository at a ref, PR, compare.

    Unlike Architecture and Security Review, this one is **not** given a structural summary:
    its question is per-file ("is there a credential in here") and a summary of a codebase
    cannot contain the hardcoded key that is the finding. So it fans out over chunks like
    Code Review and VAPT do. See `SensitiveDataAgent.scan`.
    """
    try:
        bundle = source_bundle.resolve(req, orchestrator.github,
                                       project_id=_project_id_for(request))
    except source_bundle.SourceError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    try:
        return _run_sensitive_bundle(bundle, req.project_name or "", req.context or "")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unhandled error: %s", e)
        status, body = format_error_response(e)
        raise HTTPException(status_code=status, detail=body)


@app.post("/api/review/sensitive-data/upload-zip")
async def review_sensitive_zip(
    file: UploadFile = File(..., description="A zip of the codebase, or a single source file."),
    project_name: str = Form(""),
    context: str = Form(""),
):
    """The archive door for the scan above — a zip cannot travel on a JSON route (D-234)."""
    bundle = _upload_bundle(await file.read(), file.filename or "")
    try:
        return await asyncio.to_thread(_run_sensitive_bundle, bundle, project_name, context)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unhandled error: %s", e)
        status, body = format_error_response(e)
        raise HTTPException(status_code=status, detail=body)


@app.post("/api/review/sensitive-data/preflight")
def preflight_sensitive_ui(req: SensitiveDataSourceRequest, request: Request):
    return _preflight(req, request, "sensitive-data")


@app.post("/api/review/sensitive-data/upload")
async def review_sensitive_data_file(
    file: UploadFile = File(...),
    project_name: str = Form(""),
    context: str = Form(""),
):
    content = await file.read()
    text = await asyncio.to_thread(_extract_text, content, file.filename or "")
    if not text:
        raise HTTPException(status_code=400, detail="Could not extract text from file")
    _require_llm()
    try:
        result = await asyncio.to_thread(
            orchestrator.scan_sensitive_data,
            content=text, context=context, project_name=project_name or file.filename or "Unnamed Project",
        )
        return result
    except Exception as e:
        logger.exception("Unhandled error: %s", e)
        status, body = format_error_response(e)
        raise HTTPException(status_code=status, detail=body)


# ── AI Inventory check (integration status) ────────────────────────────────

@app.get("/api/agents/info")
async def get_agents_info():
    from backend.routes.agents import VALID_AGENTS, AGENT_SOURCES
    from backend.services.jira_service import JiraService
    jira = JiraService()
    return {
        "agents": list(VALID_AGENTS.keys()),
        "github_configured": bool(config.GITHUB_TOKEN),
        "jira_configured": jira.is_configured(),
        "anthropic_configured": bool(config.ANTHROPIC_API_KEY),
    }


# ── Knowledge Base ─────────────────────────────────────────────────────────
# Every agent has its own KB category (see knowledge_base.KB_CATEGORIES). Docs are
# either global/shared (used as context for every project) or scoped to one
# project_name (used only when that same project is reviewed/generated for).
#
# **Every route here was unauthenticated — read, upload, overwrite and delete.** That was
# the most serious authorization gap in the platform, because a knowledge-base document is
# not passive data: `SDLCOrchestrator._kb()` folds it into the prompt of every subsequent
# review in its category, and several agents call `knowledge_base.get_context` directly.
# So an anonymous caller could upload a Markdown file instructing the reviewer to ignore a
# class of vulnerability, or to report a Green gate, and every later run in that project
# would read it and look entirely normal. Uploading with `project_name=""` writes a
# *global* doc, which reaches every project at once.
#
# Three rules, and the asymmetry between them is the point:
#
#   * **Reading** needs a signed-in caller, and returns global docs plus the docs of
#     projects they belong to.
#   * **Writing a project's doc** needs an admin *of that project*. It is not ordinary
#     work: it changes the standing instructions every future review in that project is
#     given, which is closer to setting a budget than to running a review.
#   * **Writing a global doc** needs a super admin, because it changes the instructions
#     given to every project in the deployment. This is the one bar that could not be
#     relaxed without recreating the hole.
#
# `project_name` is free text and KB paths are keyed by its slug, so the check maps the
# caller's project *ids* to slugs rather than trusting the string it was handed.

def _kb_slugs_for(project_ids: Optional[List[str]]) -> Optional[set]:
    """Project slugs a caller may touch. `None` (from `scope`) stays None — unrestricted."""
    from backend.services.knowledge_base import slugify_project
    if project_ids is None:
        return None
    slugs = set()
    for pid in project_ids:
        project = database.get_project(pid)
        if project and project.get("name"):
            slugs.add(slugify_project(project["name"]))
    return slugs


def _kb_readable(reader: dict, request: Request) -> Optional[set]:
    return _kb_slugs_for(scope.visible_project_ids(reader, request))


def _kb_require_writable(reader: dict, project_name: str) -> None:
    """Refuse a KB write the caller is not entitled to make.

    A global doc (`project_name` empty) is super-admin-only. A project doc requires a row
    in `project_admins` for that project — membership alone is not enough, for the reason
    in the section comment above.
    """
    from backend.services.knowledge_base import slugify_project
    user = reader.get("_user") or {}
    if is_super_admin(user):
        return
    if reader.get("type") != "jwt":
        # An API key has no notion of "admin of a project"; it carries one project and a
        # scope list of agents it may *call*. Editing standing review instructions is not
        # one of those, and silently allowing it would let a CI credential rewrite the
        # rules its own reviews are judged by.
        raise HTTPException(
            status_code=403,
            detail="Knowledge Base documents can only be changed by a signed-in project "
                   "admin, not by an API key.",
        )
    if not (project_name or "").strip():
        raise HTTPException(
            status_code=403,
            detail="Only a super admin can add or change a shared Knowledge Base document "
                   "— a shared document is used as context for every project in this "
                   "deployment. Set a project name to scope it to one project instead.",
        )
    target = slugify_project(project_name)
    admin_slugs = _kb_slugs_for(database.list_admin_project_ids(user.get("sub") or "")) or set()
    if target not in admin_slugs:
        raise HTTPException(
            status_code=403,
            detail=f"You are not an admin of '{project_name}'. A Knowledge Base document "
                   "sets the standing instructions every review in that project is given, "
                   "so changing one requires project admin.",
        )


def _kb_audit(event: str, reader: dict, category: str, name: str, project_name: str) -> None:
    """Record a Knowledge Base change in the audit trail.

    These changes were previously invisible: nothing anywhere recorded that the standing
    instructions given to an agent had been edited, so a review whose behaviour changed
    because somebody rewrote its knowledge base looked identical to one that changed
    because the code did. A shared document is stored with a NULL project (super-admin
    visible only), which is the right scope for a change that affects every project.
    """
    user = reader.get("_user") or {}
    actor = user.get("username") or reader.get("team") or "unknown"
    project = database.get_project_by_name(project_name) if project_name else None
    database.record_audit_event(
        event, "kb_document", f"{category}/{name}", actor,
        details=f"project={project_name or 'shared (all projects)'}",
        project_id=(project or {}).get("id") or "",
    )


@app.get("/api/knowledge-base/categories")
async def list_kb_categories(reader: dict = Depends(require_reader)):
    from backend.services.knowledge_base import KB_CATEGORIES
    return {"categories": KB_CATEGORIES}


@app.get("/api/knowledge-base")
async def list_kb_docs(request: Request, project_name: Optional[str] = None,
                       category: Optional[str] = None,
                       reader: dict = Depends(require_reader)):
    """Global docs plus the docs of projects this caller may see.

    `project_name` narrows further, to one project. It used to be the *only* filter, and
    omitting it returned every project's documents to anyone.
    """
    docs = knowledge_base.list_docs(project_name=project_name, category=category)
    allowed = _kb_readable(reader, request)
    if allowed is not None:
        docs = [d for d in docs if not d["project_name"] or d["project_name"] in allowed]
    return {"docs": docs, "total": len(docs)}


@app.get("/api/knowledge-base/{name}")
async def get_kb_doc(name: str, category: str, request: Request, project_name: str = "",
                     reader: dict = Depends(require_reader)):
    doc = knowledge_base.get_doc(name, category=category, project_name=project_name)
    allowed = _kb_readable(reader, request)
    # 404 rather than 403 on another project's document, as everywhere else: whether a
    # named document exists in someone else's project is itself information.
    if not doc or (doc.project_name and allowed is not None and doc.project_name not in allowed):
        raise HTTPException(status_code=404, detail=f"Document '{name}' not found in category '{category}'")
    return {
        "name": doc.name, "title": doc.title, "path": doc.path, "content": doc.content,
        "category": doc.category, "project_name": doc.project_name, "size_chars": doc.size_chars,
    }


@app.post("/api/knowledge-base")
async def upload_kb_doc(
    file: UploadFile = File(...),
    category: str = Form(...),
    project_name: str = Form(""),
    reader: dict = Depends(require_reader),
):
    from backend.services.knowledge_base import VALID_KB_CATEGORIES
    _kb_require_writable(reader, project_name)
    if category not in VALID_KB_CATEGORIES:
        raise HTTPException(status_code=400, detail=f"category must be one of {VALID_KB_CATEGORIES}")
    content_bytes = await file.read()
    text = content_bytes.decode("utf-8", errors="ignore")
    if not text.strip():
        raise HTTPException(status_code=400, detail="File is empty")
    if not (file.filename or "").endswith(".md"):
        raise HTTPException(status_code=400, detail="Only .md files are accepted")
    name = (file.filename or "doc").replace(".md", "")
    doc = knowledge_base.add_doc(name, text, category, project_name=project_name)
    _kb_audit("kb_document_uploaded", reader, doc.category, doc.name, project_name)
    return {
        "message": "Document uploaded", "name": doc.name, "title": doc.title,
        "category": doc.category, "path": doc.path, "project_name": doc.project_name,
    }


@app.put("/api/knowledge-base/{name}")
async def update_kb_doc(name: str, req: ReviewRequest, category: str, project_name: str = "",
                        reader: dict = Depends(require_reader)):
    _kb_require_writable(reader, project_name)
    doc = knowledge_base.get_doc(name, category=category, project_name=project_name)
    if not doc:
        raise HTTPException(status_code=404, detail=f"Document '{name}' not found in category '{category}'")
    updated = knowledge_base.add_doc(name, req.content, doc.category, project_name=doc.project_name)
    _kb_audit("kb_document_updated", reader, doc.category, updated.name, project_name)
    return {"message": "Document updated", "name": updated.name, "title": updated.title}


@app.delete("/api/knowledge-base/{name}")
async def delete_kb_doc(name: str, category: str, project_name: str = "",
                        reader: dict = Depends(require_reader)):
    _kb_require_writable(reader, project_name)
    deleted = knowledge_base.delete_doc(name, category=category, project_name=project_name)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Document '{name}' not found in category '{category}'")
    _kb_audit("kb_document_deleted", reader, category, name, project_name)
    return {"message": f"Document '{name}' deleted"}


# ── Controls Library ───────────────────────────────────────────────────────

@app.get("/api/controls")
async def get_controls():
    from backend.controls.control_library import (
        ALL_CONTROLS, OWASP_LLM_TOP10, NIST_AI_RMF_FUNCTIONS,
        OWASP_WEB_TOP10_2021, OWASP_API_TOP10_2023,
    )
    return {
        "controls": {k: vars(v) for k, v in ALL_CONTROLS.items()},
        "owasp_llm_top10": OWASP_LLM_TOP10,
        "owasp_web_top10": OWASP_WEB_TOP10_2021,
        "owasp_api_top10": OWASP_API_TOP10_2023,
        "nist_ai_rmf": NIST_AI_RMF_FUNCTIONS,
        "total": len(ALL_CONTROLS),
    }


# ── Utility ────────────────────────────────────────────────────────────────

@app.post("/api/documents/extract")
async def extract_document_text(file: UploadFile = File(...)):
    """Read a PDF, Word document or text file and hand back its text. No model, no charge.

    One endpoint for every screen whose input is prose — a requirement, a test basis, a
    stack brief. The alternative was an upload-and-run route per screen, three more entries
    in the metering map and three more ways for a screen to spend money on a document
    nobody had read yet.

    Extraction is deliberately separate from running: the text lands in the textarea, the
    person sees exactly what was pulled out of their document, and *then* they press the
    button. A scanned PDF that yields two words of OCR noise is a thing you want to notice
    before paying for a review of it, which is also why the character count comes back.

    Absent from `legacy_guard`'s product map for the same reason `/preflight` is: it makes
    no model call and spends nothing, so there is no product to meter it against.
    """
    raw = await file.read()
    text = await asyncio.to_thread(_extract_text, raw, file.filename or "")
    text = (text or "").strip()
    if not text:
        raise HTTPException(
            status_code=400,
            detail="No text could be read from that file. A scanned PDF with no text layer "
                   "needs OCR first — this reads text, it does not recognise images.")
    return {"filename": file.filename or "", "text": text, "chars": len(text)}


def _extract_text(content: bytes, filename: str) -> str:
    filename_lower = filename.lower()
    try:
        if filename_lower.endswith(".pdf"):
            import io
            import PyPDF2
            reader = PyPDF2.PdfReader(io.BytesIO(content))
            return "\n".join(page.extract_text() or "" for page in reader.pages)
        elif filename_lower.endswith((".docx", ".doc")):
            import io
            from docx import Document
            doc = Document(io.BytesIO(content))
            return "\n".join(para.text for para in doc.paragraphs)
        else:
            return content.decode("utf-8", errors="ignore")
    except Exception:
        return content.decode("utf-8", errors="ignore")
