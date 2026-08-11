import os
import io
import json
import logging
import time
import uuid
import zipfile
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Security, UploadFile, File, Form
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import APIKeyHeader, HTTPBearer, HTTPAuthorizationCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from backend.logging_config import configure_logging, request_id_ctx

configure_logging()
logger = logging.getLogger(__name__)

from backend import database, config
from backend.workflows import full_review as full_review_workflow
from backend.workflows.base import count_steps
from backend.routes.auth import router as auth_router
from backend.routes.approvals import router as approvals_router
from backend.routes.audit_trail import router as audit_router
from backend.routes.agents import router as agents_router
from backend.routes.inventory import router as inventory_router
from backend.routes.findings import router as findings_router
from backend.routes.modernization import router as modernization_router
from backend.routes.v1_agents import router as v1_agents_router
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
from backend.services.knowledge_base import knowledge_base
from backend.services import agent_guard, legacy_guard, llm_settings_service, subject_service
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
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
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
app.include_router(modernization_router)
app.include_router(api_keys_router)
app.include_router(plans_router)
app.include_router(products_router)
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
    selected = (request.headers.get("x-project-id") or "").strip()

    if is_super_admin(user):
        return [selected] if selected else None

    allowed = database.list_member_project_ids(user.get("sub") or "")
    if selected:
        # Not a member of it: show nothing rather than falling back to everything they
        # can see, which would quietly ignore a selection they can see on screen.
        return [selected] if selected in allowed else []
    return allowed


# ── Dashboard ──────────────────────────────────────────────────────────────

@app.get("/api/dashboard")
async def get_dashboard(request: Request, reader: dict = Depends(require_reader)):
    return database.get_dashboard_stats(project_ids=_visible_project_ids(reader, request))


@app.get("/api/reviews")
async def list_reviews(request: Request, limit: int = Query(20, ge=1, le=200),
                       offset: int = Query(0, ge=0),
                       reader: dict = Depends(require_reader)):
    """One page of reviews, newest first.

    `count` is how many rows this page holds; `total` is how many the caller may see in
    all, which is what a pager needs to know whether there is a next page.
    """
    visible = _visible_project_ids(reader, request)
    reviews = database.list_reviews(limit=limit, offset=offset, project_ids=visible)
    return {
        "reviews": reviews,
        "count": len(reviews),
        "total": database.count_reviews(project_ids=visible),
        "limit": limit,
        "offset": offset,
    }


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
async def review_requirement(req: ReviewRequest, project_name: str = ""):
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
    text = _extract_text(content, file.filename or "")
    if not text:
        raise HTTPException(status_code=400, detail="Could not extract text from file")
    try:
        result = orchestrator.review_requirement(
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
async def review_code(req: CodeReviewRequest, project_name: str = ""):
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
async def review_prompt(req: PromptReviewRequest, project_name: str = ""):
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
        with agent_guard.expect_calls(total_steps):
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
async def review_github_pr(req: GithubPRRequest):
    if not req.pr_url:
        raise HTTPException(status_code=400, detail="PR URL is required")
    _require_llm()
    try:
        result = orchestrator.review_github_pr(
            pr_url=req.pr_url,
            jira_ticket=req.jira_ticket or "",
            include_prompt_review=req.include_prompt_review,
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("Unhandled error: %s", e)
        status, body = format_error_response(e)
        raise HTTPException(status_code=status, detail=body)


@app.post("/api/github/pr/auto-fix")
async def auto_fix_github_pr(req: GithubAutoFixRequest):
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
async def release_gate(req: ReleaseGateRequest):
    _require_llm()

    findings = []
    if req.findings:
        findings = [f.model_dump() for f in req.findings]
    elif req.review_ids:
        for rid in req.review_ids:
            review = database.get_review(rid)
            if review and review.get("result"):
                findings.extend(review["result"].get("findings", []))

    try:
        result = orchestrator.gate_agent.evaluate_gate(
            all_findings=findings,
            project_name=req.project_name or "Unnamed",
            release_version=req.release_version or "",
        )
        return result
    except Exception as e:
        logger.exception("Unhandled error: %s", e)
        status, body = format_error_response(e)
        raise HTTPException(status_code=status, detail=body)


# ── Architecture Review ────────────────────────────────────────────────────

@app.post("/api/review/architecture")
async def review_architecture(req: ArchitectureReviewRequest):
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


@app.post("/api/review/architecture/upload")
async def review_architecture_file(
    file: UploadFile = File(...),
    project_name: str = Form(""),
    requirements: str = Form(""),
    review_mode: str = Form("review"),
    context: str = Form(""),
):
    content = await file.read()
    text = _extract_text(content, file.filename or "")
    if not text:
        raise HTTPException(status_code=400, detail="Could not extract text from file")
    _require_llm()
    try:
        result = orchestrator.review_architecture(
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
async def review_vapt(req: VAPTReviewRequest):
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
    text = _extract_text(content, file.filename or "")
    if not text:
        raise HTTPException(status_code=400, detail="Could not extract text from file")
    _require_llm()
    try:
        result = orchestrator.review_vapt(
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
async def review_security(req: SecurityReviewRequest):
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
    text = _extract_text(content, file.filename or "")
    if not text:
        raise HTTPException(status_code=400, detail="Could not extract text from file")
    _require_llm()
    try:
        result = orchestrator.review_security(
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

@app.post("/api/codegen/advisory")
async def codegen_advisory(req: CodeGenAdvisoryRequest):
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
async def codegen_scaffold(req: CodeGenScaffoldRequest):
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


# ── AI Code Provenance & Risk ────────────────────────────────────────────────

@app.post("/api/review/ai-provenance")
async def review_ai_provenance(req: AIProvenanceRequest):
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
async def review_migration_assessment(req: MigrationAssessmentRequest):
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


@app.post("/api/review/migration-execute")
async def review_migration_execute(req: MigrationExecutionRequest):
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


def _extract_zip_files(content: bytes) -> list:
    files = []
    total_chars = 0
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            path = info.filename.replace("\\", "/")
            if any(f"/{d}/" in f"/{path}" for d in _CFR21_EXCLUDED_DIRS):
                continue
            ext = os.path.splitext(path)[1].lower()
            if ext not in _CFR21_ALLOWED_EXTENSIONS:
                continue
            if len(files) >= _CFR21_MAX_FILES or total_chars >= _CFR21_MAX_TOTAL_CHARS:
                break
            try:
                text = zf.read(info).decode("utf-8", errors="ignore")
            except Exception:
                continue
            if not text.strip():
                continue
            remaining = _CFR21_MAX_TOTAL_CHARS - total_chars
            text = text[:remaining]
            files.append({"path": path, "content": text})
            total_chars += len(text)
    return files


@app.post("/api/review/cfr21-verification")
async def review_cfr21_verification(req: CFR21VerificationRequest):
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


@app.post("/api/review/cfr21-verification/upload-zip")
async def review_cfr21_verification_upload_zip(
    file: UploadFile = File(...),
    system_description: str = Form(""),
    project_name: str = Form(""),
    context: str = Form(""),
):
    if not (file.filename or "").endswith(".zip"):
        raise HTTPException(status_code=400, detail="Only .zip files are accepted")
    _require_llm()
    content = await file.read()
    try:
        files = _extract_zip_files(content)
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="Not a valid zip file")
    if not files:
        raise HTTPException(status_code=400, detail="No scannable source files found in the zip (check extensions / excluded dirs)")
    try:
        result = orchestrator.verify_cfr21(
            files=files,
            system_description=system_description,
            context=context,
            project_name=project_name or (file.filename or "Unnamed Project"),
        )
        result["files_extracted"] = len(files)
        return result
    except Exception as e:
        logger.exception("Unhandled error: %s", e)
        status, body = format_error_response(e)
        raise HTTPException(status_code=status, detail=body)


# ── GxP Document Check ────────────────────────────────────────────────────────

@app.post("/api/review/gxp-document-check")
async def review_gxp_document_check(req: GxPDocumentCheckRequest):
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
        text = _extract_text(content_bytes, f.filename or "")
        if text.strip():
            documents.append({"name": f.filename or "document", "content": text})
    if not documents:
        raise HTTPException(status_code=400, detail="Could not extract text from any uploaded file")
    try:
        result = orchestrator.check_gxp_documents(
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
async def review_gxp_generate_plan(req: GxPValidationPlanRequest):
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
    _require_llm()
    content = await file.read()
    try:
        files = _extract_zip_files(content)
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="Not a valid zip file")
    if not files:
        raise HTTPException(status_code=400, detail="No scannable source files found in the zip (check extensions / excluded dirs)")
    try:
        result = orchestrator.plan_gxp_validation_package(
            files=files,
            system_description=system_description,
            context=context,
            project_name=project_name or (file.filename or "Unnamed Project"),
        )
        # Return the extracted files too, so the frontend can reuse them for gxp-generate-document
        # calls without re-uploading the zip for every document.
        result["files_extracted"] = files
        return result
    except Exception as e:
        logger.exception("Unhandled error: %s", e)
        status, body = format_error_response(e)
        raise HTTPException(status_code=status, detail=body)


@app.post("/api/review/gxp-generate-document")
async def review_gxp_generate_document(req: GxPDocumentGenerateRequest):
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
async def review_issue_triage(req: IssueTriageRequest):
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
async def review_issue_triage_batch_github(req: IssueTriageBatchGitHubRequest):
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


# ── QA Intelligence — Test Scenario / Test Case Generation ──────────────────

@app.post("/api/review/test-scenario")
async def review_test_scenario(req: ReviewRequest, project_name: str = ""):
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
async def review_test_case(req: TestCaseInternalRequest):
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
async def review_sensitive_data(req: SensitiveDataRequest):
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


@app.post("/api/review/sensitive-data/upload")
async def review_sensitive_data_file(
    file: UploadFile = File(...),
    project_name: str = Form(""),
    context: str = Form(""),
):
    content = await file.read()
    text = _extract_text(content, file.filename or "")
    if not text:
        raise HTTPException(status_code=400, detail="Could not extract text from file")
    _require_llm()
    try:
        result = orchestrator.scan_sensitive_data(
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

@app.get("/api/knowledge-base/categories")
async def list_kb_categories():
    from backend.services.knowledge_base import KB_CATEGORIES
    return {"categories": KB_CATEGORIES}


@app.get("/api/knowledge-base")
async def list_kb_docs(project_name: Optional[str] = None, category: Optional[str] = None):
    """Without project_name: every document (admin view).
    With project_name: global docs + that project's docs only. Optionally filter by category."""
    docs = knowledge_base.list_docs(project_name=project_name, category=category)
    return {"docs": docs, "total": len(docs)}


@app.get("/api/knowledge-base/{name}")
async def get_kb_doc(name: str, category: str, project_name: str = ""):
    doc = knowledge_base.get_doc(name, category=category, project_name=project_name)
    if not doc:
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
):
    from backend.services.knowledge_base import VALID_KB_CATEGORIES
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
    return {
        "message": "Document uploaded", "name": doc.name, "title": doc.title,
        "category": doc.category, "path": doc.path, "project_name": doc.project_name,
    }


@app.put("/api/knowledge-base/{name}")
async def update_kb_doc(name: str, req: ReviewRequest, category: str, project_name: str = ""):
    doc = knowledge_base.get_doc(name, category=category, project_name=project_name)
    if not doc:
        raise HTTPException(status_code=404, detail=f"Document '{name}' not found in category '{category}'")
    updated = knowledge_base.add_doc(name, req.content, doc.category, project_name=doc.project_name)
    return {"message": "Document updated", "name": updated.name, "title": updated.title}


@app.delete("/api/knowledge-base/{name}")
async def delete_kb_doc(name: str, category: str, project_name: str = ""):
    deleted = knowledge_base.delete_doc(name, category=category, project_name=project_name)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Document '{name}' not found in category '{category}'")
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
