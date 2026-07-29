import os
import io
import json
import logging
import time
import uuid
import zipfile
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from backend.logging_config import configure_logging, request_id_ctx

configure_logging()
logger = logging.getLogger(__name__)

from backend import database, config
from backend.routes.auth import router as auth_router
from backend.routes.approvals import router as approvals_router
from backend.routes.audit_trail import router as audit_router
from backend.routes.agents import router as agents_router
from backend.routes.inventory import router as inventory_router
from backend.routes.v1_agents import router as v1_agents_router
from backend.routes.api_keys import router as api_keys_router
from backend.routes.swarm import router as swarm_router
from backend.routes.llm_config import router as llm_config_router
from backend.routes.projects import router as projects_router
from backend.routes.verticals import router as verticals_router
from backend.routes.issue_triage_dashboard import router as issue_triage_dashboard_router
from backend.routes.cost_optimization import router as cost_optimization_router
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
from backend.services.knowledge_base import knowledge_base
from backend.services.agent_guard import BudgetExceededError, ScopeDeniedError
from backend.services.guardrail_service import GuardrailViolation


def _init_default_admin():
    if not database.get_user_by_username("admin"):
        from backend.services.auth_service import hash_password
        pw_hash = hash_password(config.DEFAULT_ADMIN_PASSWORD)
        database.create_user("admin", "admin@sdlc.local", pw_hash, "admin", "System Administrator")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting up - env=%s model=%s", config.APP_ENV, config.MODEL)
    database.init_db()
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
    return JSONResponse(status_code=413, content={"detail": str(exc)})


@app.exception_handler(ScopeDeniedError)
async def _scope_denied_handler(request, exc: ScopeDeniedError):
    logger.warning("Scope denied on %s %s: %s", request.method, request.url.path, exc)
    return JSONResponse(status_code=403, content={"detail": str(exc)})


@app.exception_handler(BudgetExceededError)
async def _budget_exceeded_handler(request, exc: BudgetExceededError):
    logger.warning("Budget exceeded on %s %s: %s", request.method, request.url.path, exc)
    return JSONResponse(status_code=402, content={"detail": str(exc), **exc.budget})


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request, exc: Exception):
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


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
app.include_router(api_keys_router)
app.include_router(swarm_router)
app.include_router(llm_config_router)
app.include_router(projects_router)
app.include_router(verticals_router)
app.include_router(issue_triage_dashboard_router)
app.include_router(cost_optimization_router)

orchestrator = SDLCOrchestrator()

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


# ── Dashboard ──────────────────────────────────────────────────────────────

@app.get("/api/dashboard")
async def get_dashboard():
    stats = database.get_dashboard_stats()
    return stats


@app.get("/api/reviews")
async def list_reviews(limit: int = 20, offset: int = 0):
    reviews = database.list_reviews(limit=limit, offset=offset)
    return {"reviews": reviews, "count": len(reviews)}


@app.get("/api/reviews/{review_id}")
async def get_review(review_id: str):
    review = database.get_review(review_id)
    if not review:
        raise HTTPException(status_code=404, detail="Review not found")
    return review


@app.get("/api/reports/{report_id}")
async def get_report(report_id: str):
    report = database.get_report(report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    return report


# ── Requirement Review ─────────────────────────────────────────────────────

@app.post("/api/review/requirement")
async def review_requirement(req: ReviewRequest, project_name: str = ""):
    if not req.content.strip():
        raise HTTPException(status_code=400, detail="Content is required")
    if not config.ANTHROPIC_API_KEY:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not configured")
    try:
        result = orchestrator.review_requirement(
            content=req.content,
            context=req.context or "",
            project_name=project_name,
        )
        return result
    except Exception as e:
        logger.exception("Unhandled error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


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
        raise HTTPException(status_code=500, detail=str(e))


# ── Code Review ────────────────────────────────────────────────────────────

@app.post("/api/review/code")
async def review_code(req: CodeReviewRequest, project_name: str = ""):
    if not req.code.strip():
        raise HTTPException(status_code=400, detail="Code is required")
    if not config.ANTHROPIC_API_KEY:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not configured")
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
        raise HTTPException(status_code=500, detail=str(e))


# ── Prompt Safety Review ───────────────────────────────────────────────────

@app.post("/api/review/prompt")
async def review_prompt(req: PromptReviewRequest, project_name: str = ""):
    if not any([req.system_prompt, req.user_prompt, req.tool_definitions, req.rag_config]):
        raise HTTPException(status_code=400, detail="At least one prompt field is required")
    if not config.ANTHROPIC_API_KEY:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not configured")
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
        raise HTTPException(status_code=500, detail=str(e))


# ── Full SDLC Review ───────────────────────────────────────────────────────

@app.post("/api/review/full")
async def full_review(req: FullReviewRequest):
    if not any([req.requirement, req.compliance_artifact, req.architecture, req.code, req.prompt]):
        raise HTTPException(status_code=400, detail="At least one artifact is required")
    if not config.ANTHROPIC_API_KEY:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not configured")

    prompt_config = None
    if req.prompt:
        prompt_config = {"system_prompt": req.prompt}

    try:
        result = orchestrator.full_review(
            requirement=req.requirement,
            compliance_artifact=req.compliance_artifact,
            architecture=req.architecture,
            code=req.code,
            prompt_config=prompt_config,
            test_evidence=req.test_evidence,
            project_name=req.project_name or "Unnamed Project",
            context=req.context or "",
        )
        return result
    except Exception as e:
        logger.exception("Unhandled error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


# ── GitHub PR Review ───────────────────────────────────────────────────────

@app.post("/api/github/pr")
async def review_github_pr(req: GithubPRRequest):
    if not req.pr_url:
        raise HTTPException(status_code=400, detail="PR URL is required")
    if not config.GITHUB_TOKEN:
        raise HTTPException(status_code=503, detail="GITHUB_TOKEN not configured")
    if not config.ANTHROPIC_API_KEY:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not configured")
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
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/github/pr/auto-fix")
async def auto_fix_github_pr(req: GithubAutoFixRequest):
    if not req.pr_url:
        raise HTTPException(status_code=400, detail="PR URL is required")
    if not config.GITHUB_TOKEN:
        raise HTTPException(status_code=503, detail="GITHUB_TOKEN not configured")
    if not config.ANTHROPIC_API_KEY:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not configured")
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
        raise HTTPException(status_code=500, detail=str(e))


# ── Release Gate ───────────────────────────────────────────────────────────

@app.post("/api/release-gate")
async def release_gate(req: ReleaseGateRequest):
    if not config.ANTHROPIC_API_KEY:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not configured")

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
        raise HTTPException(status_code=500, detail=str(e))


# ── Architecture Review ────────────────────────────────────────────────────

@app.post("/api/review/architecture")
async def review_architecture(req: ArchitectureReviewRequest):
    if not req.artifact.strip():
        raise HTTPException(status_code=400, detail="Architecture description is required")
    if not config.ANTHROPIC_API_KEY:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not configured")
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
        raise HTTPException(status_code=500, detail=str(e))


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
    if not config.ANTHROPIC_API_KEY:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not configured")
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
        raise HTTPException(status_code=500, detail=str(e))


# ── VAPT ──────────────────────────────────────────────────────────────────

@app.post("/api/review/vapt")
async def review_vapt(req: VAPTReviewRequest):
    if not req.artifact.strip():
        raise HTTPException(status_code=400, detail="Artifact content is required")
    if not config.ANTHROPIC_API_KEY:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not configured")
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
        raise HTTPException(status_code=500, detail=str(e))


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
    if not config.ANTHROPIC_API_KEY:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not configured")
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
        raise HTTPException(status_code=500, detail=str(e))


# ── Security Review ────────────────────────────────────────────────────────

@app.post("/api/review/security")
async def review_security(req: SecurityReviewRequest):
    if not req.artifact.strip():
        raise HTTPException(status_code=400, detail="Artifact content is required")
    if not config.ANTHROPIC_API_KEY:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not configured")
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
        raise HTTPException(status_code=500, detail=str(e))


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
    if not config.ANTHROPIC_API_KEY:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not configured")
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
        raise HTTPException(status_code=500, detail=str(e))


# ── Code Generation ─────────────────────────────────────────────────────────

@app.post("/api/codegen/advisory")
async def codegen_advisory(req: CodeGenAdvisoryRequest):
    if not req.requirement.strip():
        raise HTTPException(status_code=400, detail="Requirement is required")
    if not config.ANTHROPIC_API_KEY:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not configured")
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
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/codegen/scaffold")
async def codegen_scaffold(req: CodeGenScaffoldRequest):
    if not config.ANTHROPIC_API_KEY:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not configured")
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
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/codegen/scaffold/{review_id}/download")
async def codegen_scaffold_download(review_id: str):
    review = database.get_review(review_id)
    if not review or review.get("review_type") != "codegen_scaffold":
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
    if not config.ANTHROPIC_API_KEY:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not configured")
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
        raise HTTPException(status_code=500, detail=str(e))


# ── Code Migration ─────────────────────────────────────────────────────────

@app.post("/api/review/migration-assessment")
async def review_migration_assessment(req: MigrationAssessmentRequest):
    if not (req.code or "").strip():
        raise HTTPException(status_code=400, detail="Code is required")
    if not req.migration_target.strip():
        raise HTTPException(status_code=400, detail="migration_target is required")
    if not config.ANTHROPIC_API_KEY:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not configured")
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
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/review/migration-execute")
async def review_migration_execute(req: MigrationExecutionRequest):
    if not (req.code or "").strip():
        raise HTTPException(status_code=400, detail="Code is required")
    if not req.migration_target.strip():
        raise HTTPException(status_code=400, detail="migration_target is required")
    if not config.ANTHROPIC_API_KEY:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not configured")
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
        raise HTTPException(status_code=500, detail=str(e))


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
    if not config.ANTHROPIC_API_KEY:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not configured")
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
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/review/cfr21-verification/upload-zip")
async def review_cfr21_verification_upload_zip(
    file: UploadFile = File(...),
    system_description: str = Form(""),
    project_name: str = Form(""),
    context: str = Form(""),
):
    if not (file.filename or "").endswith(".zip"):
        raise HTTPException(status_code=400, detail="Only .zip files are accepted")
    if not config.ANTHROPIC_API_KEY:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not configured")
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
        raise HTTPException(status_code=500, detail=str(e))


# ── GxP Document Check ────────────────────────────────────────────────────────

@app.post("/api/review/gxp-document-check")
async def review_gxp_document_check(req: GxPDocumentCheckRequest):
    documents = [{"name": d.name, "content": d.content} for d in (req.documents or [])]
    if not documents and not (req.content or "").strip():
        raise HTTPException(status_code=400, detail="Provide `documents` or `content`")
    if not config.ANTHROPIC_API_KEY:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not configured")
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
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/review/gxp-document-check/upload")
async def review_gxp_document_check_upload(
    files: List[UploadFile] = File(...),
    document_type_hint: str = Form(""),
    project_name: str = Form(""),
    context: str = Form(""),
):
    if not config.ANTHROPIC_API_KEY:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not configured")
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
        raise HTTPException(status_code=500, detail=str(e))


# ── GxP Validation Document Generation ───────────────────────────────────────

@app.post("/api/review/gxp-generate-plan")
async def review_gxp_generate_plan(req: GxPValidationPlanRequest):
    files = [{"path": f.path, "content": f.content} for f in (req.files or [])]
    if not files and not (req.code or "").strip():
        raise HTTPException(status_code=400, detail="Provide `files` or `code`")
    if not config.ANTHROPIC_API_KEY:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not configured")
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
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/review/gxp-generate-plan/upload-zip")
async def review_gxp_generate_plan_upload_zip(
    file: UploadFile = File(...),
    system_description: str = Form(""),
    project_name: str = Form(""),
    context: str = Form(""),
):
    if not (file.filename or "").endswith(".zip"):
        raise HTTPException(status_code=400, detail="Only .zip files are accepted")
    if not config.ANTHROPIC_API_KEY:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not configured")
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
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/review/gxp-generate-document")
async def review_gxp_generate_document(req: GxPDocumentGenerateRequest):
    files = [{"path": f.path, "content": f.content} for f in (req.files or [])]
    if not files and not (req.code or "").strip():
        raise HTTPException(status_code=400, detail="Provide `files` or `code`")
    if not req.document_type.strip():
        raise HTTPException(status_code=400, detail="document_type is required")
    if not config.ANTHROPIC_API_KEY:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not configured")
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
        raise HTTPException(status_code=500, detail=str(e))


# ── Issue Triaging Agent ─────────────────────────────────────────────────────

@app.post("/api/review/issue-triage")
async def review_issue_triage(req: IssueTriageRequest):
    title, description = req.title or "", req.description or ""
    if req.github_issue_url and not (title.strip() or description.strip()):
        if not config.GITHUB_TOKEN:
            raise HTTPException(status_code=503, detail="GITHUB_TOKEN not configured")
        from backend.services.github_service import GitHubService
        try:
            gh = GitHubService().get_issue(req.github_issue_url)
        except Exception as e:
            logger.exception("GitHub issue fetch failed for %s", req.github_issue_url)
            raise HTTPException(status_code=502, detail=f"GitHub fetch failed: {e}")
        title, description = gh.get("title", ""), gh.get("body", "")
    if not (title.strip() or description.strip()):
        raise HTTPException(status_code=400, detail="Supply `title`/`description`, or `github_issue_url`")
    if not config.ANTHROPIC_API_KEY:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not configured")
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
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/review/issue-triage/batch-github")
async def review_issue_triage_batch_github(req: IssueTriageBatchGitHubRequest):
    if not req.github_repo_url.strip():
        raise HTTPException(status_code=400, detail="github_repo_url is required")
    if not config.GITHUB_TOKEN:
        raise HTTPException(status_code=503, detail="GITHUB_TOKEN not configured")
    if not config.ANTHROPIC_API_KEY:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not configured")
    try:
        result = orchestrator.triage_github_repo_issues(
            github_repo_url=req.github_repo_url, limit=req.limit, team_roster=req.team_roster or "",
            context=req.context or "", project_name=req.project_name or req.github_repo_url,
        )
        return result
    except Exception as e:
        logger.exception("Unhandled error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


# ── QA Intelligence — Test Scenario / Test Case Generation ──────────────────

@app.post("/api/review/test-scenario")
async def review_test_scenario(req: ReviewRequest, project_name: str = ""):
    if not req.content.strip():
        raise HTTPException(status_code=400, detail="Content is required")
    if not config.ANTHROPIC_API_KEY:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not configured")
    try:
        result = orchestrator.generate_test_scenarios(
            requirement=req.content, context=req.context or "", project_name=project_name or "Unnamed Project",
        )
        return result
    except Exception as e:
        logger.exception("Unhandled error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


class TestCaseInternalRequest(BaseModel):
    content: str
    scenarios_context: Optional[str] = None
    context: Optional[str] = None
    project_name: Optional[str] = "Unnamed Project"


@app.post("/api/review/test-case")
async def review_test_case(req: TestCaseInternalRequest):
    if not req.content.strip():
        raise HTTPException(status_code=400, detail="Content is required")
    if not config.ANTHROPIC_API_KEY:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not configured")
    try:
        result = orchestrator.generate_test_cases(
            requirement=req.content, scenarios_context=req.scenarios_context or "",
            context=req.context or "", project_name=req.project_name or "Unnamed Project",
        )
        return result
    except Exception as e:
        logger.exception("Unhandled error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


# ── Sensitive Data Detector ────────────────────────────────────────────────

class SensitiveDataRequest(BaseModel):
    content: str
    context: Optional[str] = None
    project_name: Optional[str] = "Unnamed"


@app.post("/api/review/sensitive-data")
async def review_sensitive_data(req: SensitiveDataRequest):
    if not req.content.strip():
        raise HTTPException(status_code=400, detail="Content is required")
    if not config.ANTHROPIC_API_KEY:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not configured")
    try:
        result = orchestrator.scan_sensitive_data(
            content=req.content, context=req.context or "", project_name=req.project_name or "Unnamed Project",
        )
        return result
    except Exception as e:
        logger.exception("Unhandled error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


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
    if not config.ANTHROPIC_API_KEY:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not configured")
    try:
        result = orchestrator.scan_sensitive_data(
            content=text, context=context, project_name=project_name or file.filename or "Unnamed Project",
        )
        return result
    except Exception as e:
        logger.exception("Unhandled error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


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
