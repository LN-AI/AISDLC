"""LM AI-SDLC — Agent API v1

Individual, typed endpoints for every review agent.
External teams authenticate with X-API-Key; UI users use Bearer JWT.

Endpoints
---------
GET  /api/v1/agents              — list all agents + capabilities
GET  /api/v1/agents/health       — health / config check
POST /api/v1/agents/requirement  — requirement risk review
POST /api/v1/agents/code         — code & PR security review
POST /api/v1/agents/prompt       — prompt & agent safety review
POST /api/v1/agents/compliance   — compliance framework mapping
POST /api/v1/agents/vapt         — vulnerability assessment (CVSS)
POST /api/v1/agents/security     — STRIDE threat model & trust boundaries
POST /api/v1/agents/architecture — architecture quality & anti-pattern review
POST /api/v1/agents/sensitive-data — PII/PHI/PCI/credential scanner
POST /api/v1/agents/release-gate — release readiness decision
POST /api/v1/agents/full         — full SDLC review (all agents, one call)
"""
import io
import logging
import zipfile
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, Security
from fastapi.responses import StreamingResponse
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

from backend import config, database
from backend.models.api_models import (
    AIProvenanceRequest,
    ArchitectureReviewRequest,
    CFR21VerificationRequest,
    CodeGenAdvisoryRequest,
    CodeGenScaffoldRequest,
    CodeReviewRequest,
    ComplianceRequest,
    FullReviewRequest,
    GithubAutoFixV1Request,
    GxPDocumentCheckRequest,
    GxPDocumentGenerateRequest,
    GxPValidationPlanRequest,
    IssueTriageBatchGitHubRequest,
    IssueTriageRequest,
    MigrationAssessmentRequest,
    MigrationExecutionRequest,
    PromptSafetyRequest,
    ReleaseGateRequest,
    ReportGenerateRequest,
    RequirementReviewRequest,
    SecurityReviewRequest,
    SensitiveDataRequest,
    TestCaseRequest,
    TestScenarioRequest,
    VAPTRequest,
)
from backend.services import agent_guard, llm_gateway, job_service
from backend.services.agent_guard import BudgetExceededError, ScopeDeniedError
from backend.services.api_key_service import (
    check_and_record_rate_limit,
    resolve_effective_override,
    validate_api_key,
)
from backend.services.auth_service import decode_token
from backend.services.github_service import GitHubService
from backend.services.jira_service import JiraService
from backend.services.orchestrator import SDLCOrchestrator

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/agents", tags=["Agents v1"])

_orch = SDLCOrchestrator()
_github = GitHubService()
_jira = JiraService()
_api_key_hdr = APIKeyHeader(name="X-API-Key", auto_error=False)
_bearer = HTTPBearer(auto_error=False)

AGENT_CATALOG = [
    {
        "name": "requirement",
        "title": "Requirement Risk Reviewer",
        "endpoint": "POST /api/v1/agents/requirement",
        "description": "Reviews user stories, BRDs, and acceptance criteria for risk, ambiguity, and compliance gaps.",
        "sources": ["text", "jira_ticket", "github_issue_url"],
        "frameworks": ["NIST-AI-RMF", "OWASP-LLM", "HIPAA", "GDPR"],
    },
    {
        "name": "code",
        "title": "Code & PR Security Reviewer",
        "endpoint": "POST /api/v1/agents/code",
        "description": "Security and quality review of code, git diffs, and pull requests (OWASP, CWE, injection, secrets).",
        "sources": ["code", "github_pr_url", "github_file_url"],
        "frameworks": ["OWASP-Web-Top10", "OWASP-LLM", "CWE", "NIST-AI-RMF"],
    },
    {
        "name": "prompt",
        "title": "Prompt & Agent Safety Reviewer",
        "endpoint": "POST /api/v1/agents/prompt",
        "description": "Reviews AI system prompts, tool definitions, and RAG configs for injection and safety risks.",
        "sources": ["system_prompt", "github_file_url"],
        "frameworks": ["OWASP-LLM", "NIST-AI-RMF"],
    },
    {
        "name": "compliance",
        "title": "Compliance Mapper",
        "endpoint": "POST /api/v1/agents/compliance",
        "description": "Maps any artifact to NIST AI-RMF, OWASP LLM, HIPAA, GDPR, ISO 27001, GxP, PCI DSS controls.",
        "sources": ["text", "jira_ticket"],
        "frameworks": ["NIST-AI-RMF", "OWASP-LLM", "HIPAA", "GDPR", "ISO27001", "GxP", "PCI-DSS"],
    },
    {
        "name": "vapt",
        "title": "VAPT — Vulnerability Assessment",
        "endpoint": "POST /api/v1/agents/vapt",
        "description": "Full vulnerability assessment with CVSS v3.1 scoring, OWASP API Top 10, and PoC attack scenarios.",
        "sources": ["code", "github_pr_url", "github_file_url", "github_repo_url"],
        "frameworks": ["OWASP-Web-Top10", "OWASP-API-Top10", "CWE", "CVSS-3.1"],
    },
    {
        "name": "security",
        "title": "Security Architecture Review",
        "endpoint": "POST /api/v1/agents/security",
        "description": "STRIDE threat modelling, trust boundary analysis, and attack surface assessment.",
        "sources": ["artifact", "github_repo_url", "github_file_url", "jira_ticket"],
        "frameworks": ["STRIDE", "NIST-AI-RMF", "HIPAA", "GDPR", "SOC2"],
    },
    {
        "name": "architecture",
        "title": "Architecture Review",
        "endpoint": "POST /api/v1/agents/architecture",
        "description": "Quality dimension scoring (8 axes), anti-pattern detection, SPOF analysis, and architectural alternatives.",
        "sources": ["artifact", "github_repo_url", "github_file_url", "jira_ticket"],
        "frameworks": ["NIST-AI-RMF", "SOC2"],
    },
    {
        "name": "sensitive-data",
        "title": "Sensitive Data Detector",
        "endpoint": "POST /api/v1/agents/sensitive-data",
        "description": "Scans code, configs, schemas, and logs for PII, PHI, PCI data, credentials, and location data.",
        "sources": ["content", "github_file_url"],
        "frameworks": ["HIPAA", "GDPR", "PCI-DSS", "NIST-AI-RMF"],
    },
    {
        "name": "release-gate",
        "title": "Release Gate",
        "endpoint": "POST /api/v1/agents/release-gate",
        "description": "Evaluates release readiness: aggregates prior findings, checks mandatory controls, issues Red/Amber/Green gate decision.",
        "sources": ["review_ids", "evidence_summary"],
        "frameworks": ["NIST-AI-RMF", "SOC2", "HIPAA"],
    },
    {
        "name": "full",
        "title": "Full SDLC Review",
        "endpoint": "POST /api/v1/agents/full",
        "description": "Runs requirement, code, compliance (via `compliance_artifact`), architecture (via `architecture`), and release gate agents in one call. Returns a consolidated gate decision.",
        "sources": ["text", "github_pr_url", "jira_ticket", "compliance_artifact", "architecture"],
        "frameworks": ["NIST-AI-RMF", "OWASP-LLM", "HIPAA", "GDPR", "ISO27001"],
    },
    {
        "name": "codegen-advisory",
        "title": "Code Generation Advisor",
        "endpoint": "POST /api/v1/agents/codegen-advisory",
        "description": "Recommends language, backend/frontend framework, database, architecture pattern, and design patterns for a new system given its requirement. Any field can be pinned to force that choice.",
        "sources": ["requirement"],
        "frameworks": ["NIST-AI-RMF"],
    },
    {
        "name": "codegen-scaffold",
        "title": "Code Generation Scaffold",
        "endpoint": "POST /api/v1/agents/codegen-scaffold",
        "description": "Generates a minimal, runnable starter project (8-16 files) for a chosen language/framework/pattern. Optionally auto-runs Code Review and Sensitive Data agents against the output before returning it.",
        "sources": ["project_name", "language", "backend_framework"],
        "frameworks": ["NIST-AI-RMF", "OWASP-LLM"],
    },
    {
        "name": "ai-provenance",
        "title": "AI Code Provenance & Risk",
        "endpoint": "POST /api/v1/agents/ai-provenance",
        "description": "Assesses the AI-origin likelihood of a code artifact as probabilistic evidence (Low/Possible/Likely/Strong evidence), never a verdict — combines an optional developer disclosure with LLM-based style-signal analysis into a scored, evidence-backed report.",
        "sources": ["code", "github_pr_url", "github_file_url"],
        "frameworks": ["NIST-AI-RMF"],
    },
    {
        "name": "migration-assessment",
        "title": "Migration Assessment",
        "endpoint": "POST /api/v1/agents/migration-assessment",
        "description": "Assesses migration complexity, breaking changes, risk areas, and a phased plan for moving code from a source stack to a target stack (language upgrade, framework swap, database migration, monolith-to-microservices, cloud migration).",
        "sources": ["code", "github_pr_url", "github_file_url"],
        "frameworks": ["NIST-AI-RMF"],
    },
    {
        "name": "migration-execute",
        "title": "Migration Execution",
        "endpoint": "POST /api/v1/agents/migration-execute",
        "description": "Transforms a specific file/artifact from its current stack to a target stack, preserving behavior. Lists every change and everything needing manual follow-up. Optionally auto-runs Code Review and Sensitive Data agents against the migrated output.",
        "sources": ["code", "github_pr_url", "github_file_url"],
        "frameworks": ["NIST-AI-RMF", "OWASP-LLM"],
    },
    {
        "name": "cfr21-verification",
        "title": "21 CFR Part 11 Verification",
        "endpoint": "POST /api/v1/agents/cfr21-verification",
        "description": "Verifies a codebase (multiple files, or a single snippet/PR/file) against FDA 21 CFR Part 11 — audit trail (§11.10(e)), electronic signatures (§11.50/70/100/200), access control (§11.10(d)/(g)/§11.300), and data integrity (ALCOA+). Returns exhaustive, evidence-cited findings plus a validation-readiness note for process-only controls.",
        "sources": ["files", "code", "github_pr_url", "github_file_url"],
        "frameworks": ["21CFR11", "GxP"],
    },
    {
        "name": "gxp-document-check",
        "title": "GxP Document Check",
        "endpoint": "POST /api/v1/agents/gxp-document-check",
        "description": "Checks GxP documents (SOPs, validation protocols IQ/OQ/PQ, batch records, deviations, CAPAs, change controls, risk assessments, URS/specifications, training records) for completeness against a type-specific required-elements checklist, signature/approval compliance (21 CFR 11.50), version control, and cross-document traceability. Accepts multiple related documents checked as a set.",
        "sources": ["documents", "content"],
        "frameworks": ["21CFR11", "GxP"],
    },
    {
        "name": "gxp-generate-plan",
        "title": "GxP Validation Plan",
        "endpoint": "POST /api/v1/agents/gxp-generate-plan",
        "description": "Points at an application (files/code/PR) and plans its GxP validation document package — GAMP 5 risk classification, which documents apply (URS, FRS, Risk Assessment, Validation Plan, IQ/OQ/PQ Protocols, Traceability Matrix, Validation Summary Report, SOP), and an application-specific outline for each, in generation order.",
        "sources": ["files", "code", "github_pr_url", "github_file_url"],
        "frameworks": ["21CFR11", "GxP"],
    },
    {
        "name": "gxp-generate-document",
        "title": "GxP Document Generation",
        "endpoint": "POST /api/v1/agents/gxp-generate-document",
        "description": "Generates one complete GxP validation document (URS, FRS, Risk Assessment, Validation Plan, IQ/OQ/PQ Protocol, Traceability Matrix, Validation Summary Report, or SOP) grounded in the actual application given. Call once per document type, typically driven by the plan from gxp-generate-plan.",
        "sources": ["files", "code", "github_pr_url", "github_file_url"],
        "frameworks": ["21CFR11", "GxP"],
    },
    {
        "name": "issue-triage",
        "title": "Issue Triaging Agent",
        "endpoint": "POST /api/v1/agents/issue-triage",
        "description": "Classifies, prioritizes, and recommends assignment for a defect/incident/support ticket/security finding/test failure. Detects duplicates against this project's own triage history, enriches incomplete tickets without inventing facts, and flags mandatory human review for Critical/P0/P1/security cases per a confidence-based operating model.",
        "sources": ["title", "description", "github_issue_url"],
        "frameworks": ["NIST-AI-RMF"],
    },
    {
        "name": "issue-triage-batch-github",
        "title": "Issue Triaging — GitHub Batch",
        "endpoint": "POST /api/v1/agents/issue-triage/batch-github",
        "description": "Pulls open issues from a GitHub repo (most recently updated first) and triages each one, returning per-issue reports plus a severity/escalation summary.",
        "sources": ["github_repo_url"],
        "frameworks": ["NIST-AI-RMF"],
    },
    {
        "name": "test-scenario",
        "title": "Test Scenario Generation",
        "endpoint": "POST /api/v1/agents/test-scenario",
        "description": "Converts a requirement/user story into a business-level test scenario catalogue — happy path, negative, boundary value, role-based, integration, and exception handling — each traced back to the requirement text it covers.",
        "sources": ["text", "jira_ticket", "github_issue_url"],
        "frameworks": [],
    },
    {
        "name": "test-case",
        "title": "Test Case Generation",
        "endpoint": "POST /api/v1/agents/test-case",
        "description": "Generates detailed, executable manual test cases — step-by-step actions, expected results, concrete test data — from a requirement, or from a prior Test Scenario catalogue via `scenarios_context`.",
        "sources": ["text", "jira_ticket", "github_issue_url", "scenarios_context"],
        "frameworks": [],
    },
    {
        "name": "auto-fix",
        "title": "Auto-Fix & Open PR",
        "endpoint": "POST /api/v1/agents/auto-fix",
        "description": "Reviews a GitHub PR, generates fixes for its findings, commits them to a new branch off the PR's head branch, and opens a follow-up PR back into it. Writes to a real repository — requires a GITHUB_TOKEN with write (repo) scope. Refuses to commit any file where its own secret scanner detects a newly-introduced hardcoded credential.",
        "sources": ["pr_url"],
        "frameworks": ["OWASP-Web-Top10", "CWE"],
    },
    {
        "name": "report-generate",
        "title": "Governance Report Generator",
        "endpoint": "POST /api/v1/agents/report-generate",
        "description": "Synthesizes prior review findings (by review_ids or supplied directly) into a comprehensive, audit-ready governance report — executive summary, findings by category, compliance coverage matrix, controls assessment, and release decision — without re-running a full review.",
        "sources": ["review_ids", "findings"],
        "frameworks": ["NIST-AI-RMF", "OWASP-LLM", "HIPAA", "GDPR", "ISO27001", "GxP"],
    },
]


# ── Auth ──────────────────────────────────────────────────────────────────

def get_caller(
    request: Request,
    response: Response,
    api_key: Optional[str] = Security(_api_key_hdr),
    bearer: Optional[HTTPAuthorizationCredentials] = Security(_bearer),
) -> dict:
    if api_key:
        record = validate_api_key(api_key)
        if not record:
            logger.warning("Rejected invalid/revoked API key on %s", request.url.path)
            raise HTTPException(status_code=401, detail="Invalid or revoked API key.")

        rl = check_and_record_rate_limit(record, request.url.path)
        response.headers["X-RateLimit-Limit-Minute"] = str(rl["limit_per_minute"])
        response.headers["X-RateLimit-Remaining-Minute"] = str(rl["remaining_per_minute"])
        response.headers["X-RateLimit-Limit-Day"] = str(rl["limit_per_day"])
        response.headers["X-RateLimit-Remaining-Day"] = str(rl["remaining_per_day"])
        if not rl["allowed"]:
            logger.warning("Rate limit exceeded for team '%s' on %s", record["team_name"], request.url.path)
            raise HTTPException(
                status_code=429,
                detail=(
                    f"Rate limit exceeded for team '{record['team_name']}': "
                    f"{rl['limit_per_minute']} req/min, {rl['limit_per_day']} req/day. "
                    f"Retry after {rl['retry_after_seconds']}s."
                ),
                headers={"Retry-After": str(rl["retry_after_seconds"])},
            )
        return {"type": "api_key", "team": record["team_name"], "key_id": record["id"], "_record": record}
    if bearer:
        user = decode_token(bearer.credentials)
        if user:
            return {"type": "jwt", "team": user.get("username", "ui"), "username": user.get("username")}
    logger.warning("Rejected unauthenticated request on %s", request.url.path)
    raise HTTPException(
        status_code=401,
        detail="Authentication required. Supply 'X-API-Key: lmsdlc_...' or 'Authorization: Bearer <jwt>'.",
        headers={"WWW-Authenticate": 'Bearer realm="LM AI-SDLC API v1"'},
    )


# ── Source resolver ───────────────────────────────────────────────────────

def _resolve(req) -> tuple:
    """Return (artifact_text, source_type, display_ref, postback_ref).
    Priority: github_pr > github_file > github_repo > jira > github_issue > text fields.
    """
    def _gh(url, hint):
        try:
            return _github.fetch_for_agent(url, hint)
        except Exception as e:
            logger.exception("GitHub fetch failed for %s", url)
            raise HTTPException(502, f"GitHub fetch failed: {e}")

    def _jr(ref):
        if not _jira.is_configured():
            raise HTTPException(503, "Jira not configured. Set JIRA_URL, JIRA_EMAIL, JIRA_API_TOKEN in .env")
        try:
            return _jira.get_issue(ref)
        except Exception as e:
            logger.exception("Jira fetch failed for %s", ref)
            raise HTTPException(502, f"Jira fetch failed: {e}")

    pr  = getattr(req, "github_pr_url",    None)
    gf  = getattr(req, "github_file_url",  None)
    gr  = getattr(req, "github_repo_url",  None)
    jt  = getattr(req, "jira_ticket",      None)
    gi  = getattr(req, "github_issue_url", None)

    if pr:
        d = _gh(pr, "code")
        return d.get("diff") or d.get("content", ""), "github_pr", d.get("title", pr), pr
    if gf:
        d = _gh(gf, "code")
        return d.get("content", ""), "github_file", d.get("name", gf), ""
    if gr:
        d = _gh(gr, "architecture")
        return d.get("content", ""), "github_repo", d.get("repo", gr), ""
    if jt:
        issue = _jr(jt)
        return issue.get("content", ""), "jira", issue.get("title", jt), jt
    if gi:
        d = _gh(gi, "requirement")
        return d.get("content", ""), "github_issue", d.get("title", gi), gi

    for attr in ("text", "code", "artifact", "content", "system_prompt"):
        val = getattr(req, attr, None)
        if val and str(val).strip():
            return str(val), "text", str(val)[:80].replace("\n", " "), ""

    raise HTTPException(
        400,
        "No source provided. Supply a GitHub URL, Jira ticket ID, or text content.",
    )


def _effective_llm_override(req, caller: dict, agent_name: str) -> Optional[dict]:
    """Bring Your Own LLM resolution, most specific wins: per-request fields >
    this key's override for `agent_name` > this key's global default > platform."""
    try:
        return resolve_effective_override(req, caller, agent_name)
    except ValueError as e:
        raise HTTPException(400, str(e))


def _do_postback(source_type: str, ref: str, result: dict) -> str:
    if not ref:
        return "not_requested"
    try:
        if source_type in ("github_pr", "github_issue"):
            comment = _github.format_pr_review_comment(result)
            ok = _github.post_issue_comment(ref, comment)
            return "posted" if ok else "failed"
        if source_type == "jira":
            SEV = ["", "Low", "Minor", "Moderate", "High", "Critical"]
            lines = [
                f"*LM AI-SDLC Review* — Gate: {result.get('gate_status','?')} | Score: {result.get('risk_score',0):.0f}/100",
                f"{result.get('summary','')}",
                "",
            ]
            for f in result.get("findings", [])[:10]:
                lines.append(f"[{SEV[f.get('severity',1)]}] {f.get('control_id','')} — {f.get('title','')}")
                lines.append(f"  Fix: {f.get('recommendation','')}")
            ok = _jira.post_comment(ref, "\n".join(lines))
            return "posted" if ok else "failed"
    except Exception as e:
        logger.exception("Post-back to %s failed for %s", source_type, ref)
        return f"failed: {str(e)[:100]}"
    return "not_requested"


def _wrap(agent: str, result: dict, source_type: str, source_ref: str,
          pb_status: str, project_name: str, llm_used: Optional[dict] = None) -> dict:
    # `full` (orchestrator.full_review) returns `top_findings` (top 10 only) /
    # `overall_risk_score` instead of `findings`/`risk_score` — every other agent
    # returns `findings`/`risk_score` directly, so the fallback only kicks in for
    # `full`. Without it, `total_findings`/`risk_score` silently reset to 0/0 here
    # even when the underlying review found real Critical findings — this was
    # invisible before because `full` never completed under the old synchronous
    # endpoint (always timed out), so this branch was never actually exercised.
    findings = result.get("findings", result.get("top_findings", []))
    risk_score = result.get("risk_score", result.get("overall_risk_score", 0))
    total_findings = result.get("total_findings", len(findings))
    CORE = {"findings", "risk_score", "gate_status", "summary", "recommendations",
            "compliance_flags", "review_id", "agent_name", "critical_count",
            "high_count", "total_findings"}
    return {
        "llm_used": llm_used or llm_gateway.describe(None),
        "success": True,
        "agent": agent,
        "agent_title": next((a["title"] for a in AGENT_CATALOG if a["name"] == agent), agent),
        "agent_version": "1.0",
        "review_id": result.get("review_id"),
        "project_name": project_name or "",
        "gate_status": result.get("gate_status", "Green"),
        "risk_score": float(risk_score),
        "total_findings": total_findings,
        "critical_count": result.get("critical_count", sum(1 for f in findings if f.get("severity") == 5)),
        "high_count": result.get("high_count",    sum(1 for f in findings if f.get("severity") == 4)),
        "summary": result.get("summary", ""),
        "findings": findings,
        "recommendations": result.get("recommendations", []),
        "compliance_flags": result.get("compliance_flags", []),
        "post_back_status": pb_status,
        "source_type": source_type,
        "source_ref": source_ref,
        "created_at": datetime.now(timezone.utc).isoformat(),
        **{k: v for k, v in result.items() if k not in CORE},
    }


# ── Catalogue & health ────────────────────────────────────────────────────

@router.get(
    "",
    summary="List all agents",
    description="Returns every agent's name, endpoint, supported sources, and mapped compliance frameworks.",
)
def list_agents():
    return {"agents": AGENT_CATALOG, "version": "1.0", "base_url": "/api/v1/agents"}


@router.get(
    "/health",
    summary="Health & configuration check",
    description="Reports whether each integration (Anthropic, GitHub, Jira) is configured and ready.",
)
def health():
    return {
        "status": "ok",
        "anthropic": bool(config.ANTHROPIC_API_KEY),
        "github":    bool(config.GITHUB_TOKEN),
        "jira":      _jira.is_configured(),
        "agents":    [a["name"] for a in AGENT_CATALOG],
        "version":   "1.0",
    }


# ── 1. Requirement Review ─────────────────────────────────────────────────

@router.post(
    "/requirement",
    summary="Review requirements for risk and compliance gaps",
    description="""Analyses user stories, BRDs, FRDs, and acceptance criteria for:

- **Completeness** — missing AC, non-functional requirements, data definitions
- **Data & privacy risk** — PHI, PII, financial data, GDPR/HIPAA/CCPA triggers
- **Security requirements** — auth, RBAC, audit logging, encryption gaps
- **AI/ML-specific risks** — autonomous decisions, bias, explainability gaps
- **Regulatory impact** — identifies applicable frameworks from the requirement text

**Sources:** `text` · `jira_ticket` · `github_issue_url`

**Returns:** Findings with control IDs, gate decision (Red/Amber/Green), risk score 0–100.
""",
)
def run_requirement(req: RequirementReviewRequest, caller: dict = Depends(get_caller)):
    artifact, src_type, src_ref, pb_ref = _resolve(req)
    ctx = f"Source: {src_type} — {src_ref}\n{req.context or ''}".strip()
    override = _effective_llm_override(req, caller, "requirement")
    result = agent_guard.guarded_call(
        caller, "requirement", override,
        lambda: _orch.review_requirement(artifact, ctx, req.project_name or src_ref[:60]),
    )
    pb = _do_postback(src_type, pb_ref, result) if req.post_back else "not_requested"
    return _wrap("requirement", result, src_type, src_ref, pb, req.project_name or "", llm_gateway.describe(override))


# ── 2. Code Review ────────────────────────────────────────────────────────

@router.post(
    "/code",
    summary="Security and quality review of code or pull requests",
    description="""Scans code, diffs, and PRs for:

- **Injection** — SQL, command, LDAP, XPath, template injection
- **Auth & access** — hardcoded credentials, JWT flaws, CSRF, BOLA/IDOR
- **Data exposure** — PHI/PII in logs, unencrypted storage, stack traces in responses
- **Cryptography** — weak algorithms (MD5/SHA-1), hardcoded keys, weak RNG
- **AI/LLM-specific** — unsanitised user input to LLM, missing output validation, over-permissioned tools
- **Dependencies** — known-vulnerable libraries, deprecated packages

**Sources:** `code` · `github_pr_url` · `github_file_url`

**Returns:** Findings with OWASP/CWE references, CVSS indicators, gate decision.
""",
)
def run_code(req: CodeReviewRequest, caller: dict = Depends(get_caller)):
    artifact, src_type, src_ref, pb_ref = _resolve(req)
    ctx = f"Source: {src_type} — {src_ref}\n{req.context or ''}".strip()
    override = _effective_llm_override(req, caller, "code")
    result = agent_guard.guarded_call(
        caller, "code", override,
        lambda: _orch.review_code(artifact, req.language or "", ctx, req.project_name or src_ref[:60]),
    )
    pb = _do_postback(src_type, pb_ref, result) if req.post_back else "not_requested"
    return _wrap("code", result, src_type, src_ref, pb, req.project_name or "", llm_gateway.describe(override))


# ── 3. Prompt Safety ──────────────────────────────────────────────────────

@router.post(
    "/prompt",
    summary="Review AI prompts and agent configs for injection and safety risks",
    description="""Audits AI system prompts, tool definitions, and RAG configurations for:

- **Prompt injection** (OWASP LLM01) — direct and indirect injection paths
- **Sensitive data disclosure** (OWASP LLM06) — PHI/PII leaking through prompts
- **Excessive agency** (OWASP LLM08) — over-permissioned tools, missing approval gates
- **Unsafe autonomy** — high-risk decisions without human escalation
- **Hallucination risk** (OWASP LLM09) — missing confidence thresholds
- **RAG security** — knowledge-base injection, cross-tenant data leakage
- **Tool definition security** — parameter injection vectors, scoping gaps

**Sources:** `system_prompt` · `user_prompt` · `tool_definitions` · `rag_config` · `github_file_url`

Supply one or more fields — all present fields are reviewed together.
""",
)
def run_prompt(req: PromptSafetyRequest, caller: dict = Depends(get_caller)):
    # Special handling: prompt agent takes 4 separate components, not one artifact
    src_type = "text"
    src_ref  = ""
    artifact_used = False

    if req.github_file_url:
        try:
            d = _github.fetch_for_agent(req.github_file_url, "code")
            # Treat file content as system_prompt if not already set
            if not req.system_prompt:
                req = req.model_copy(update={"system_prompt": d.get("content", "")})
            src_type = "github_file"
            src_ref  = d.get("name", req.github_file_url)
            artifact_used = True
        except Exception as e:
            logger.exception("GitHub file fetch failed for %s", req.github_file_url)
            raise HTTPException(502, f"GitHub fetch failed: {e}")

    if not any([req.system_prompt, req.user_prompt, req.tool_definitions, req.rag_config]):
        raise HTTPException(400, "Supply at least one of: system_prompt, user_prompt, tool_definitions, rag_config, github_file_url")

    ctx = f"Source: {src_type} — {src_ref}\n{req.context or ''}".strip() if src_ref else (req.context or "")
    override = _effective_llm_override(req, caller, "prompt")
    result = agent_guard.guarded_call(
        caller, "prompt", override,
        lambda: _orch.review_prompt(
            system_prompt=req.system_prompt or "",
            user_prompt=req.user_prompt or "",
            tool_definitions=req.tool_definitions or "",
            rag_config=req.rag_config or "",
            context=ctx,
            project_name=req.project_name or "",
        ),
    )
    pb = _do_postback(src_type, src_ref, result) if req.post_back and artifact_used else "not_requested"
    return _wrap("prompt", result, src_type, src_ref or "(inline)", pb, req.project_name or "", llm_gateway.describe(override))


# ── 4. Compliance Mapping ─────────────────────────────────────────────────

@router.post(
    "/compliance",
    summary="Map an artifact to NIST, OWASP, HIPAA, GDPR, ISO 27001, GxP, PCI DSS",
    description="""Maps requirements, architecture descriptions, or design documents to:

- **NIST AI Risk Management Framework** (GOVERN · MAP · MEASURE · MANAGE)
- **OWASP LLM Top 10** (LLM01–LLM10)
- **HIPAA** — PHI controls, audit logging, encryption, BAA, breach notification
- **GDPR** — lawful basis, data minimisation, Article 22, residency
- **ISO 27001** — Annex A controls, access control, cryptography, supplier risk
- **GxP / 21 CFR Part 11** — e-records, e-signatures, CSV, audit trail
- **PCI DSS** — card data protection, CVV prohibition, logging
- **SOC 2 Type II** — Trust Service Criteria

**Sources:** `text` · `jira_ticket`

Use `frameworks` to restrict the analysis to specific standards.
""",
)
def run_compliance(req: ComplianceRequest, caller: dict = Depends(get_caller)):
    artifact, src_type, src_ref, pb_ref = _resolve(req)
    ctx = f"Source: {src_type} — {src_ref}\n{req.context or ''}".strip()
    override = _effective_llm_override(req, caller, "compliance")
    result = agent_guard.guarded_call(
        caller, "compliance", override,
        lambda: _orch.review_compliance(artifact, req.frameworks, ctx, req.project_name or src_ref[:60]),
    )
    pb = _do_postback(src_type, pb_ref, result) if req.post_back else "not_requested"
    return _wrap("compliance", result, src_type, src_ref, pb, req.project_name or "", llm_gateway.describe(override))


# ── 5. VAPT ───────────────────────────────────────────────────────────────

@router.post(
    "/vapt",
    summary="Vulnerability assessment with CVSS scoring",
    description="""Full penetration-testing style assessment covering:

- **OWASP Web Top 10 (2021)** — A01 Broken Access through A10 SSRF
- **OWASP API Top 10 (2023)** — BOLA, broken auth, mass assignment, resource consumption
- **Additional** — XSS/CSRF, business logic, file uploads, info disclosure, security headers
- **AI/LLM-specific** — tool misuse, RAG poisoning, output manipulation
- **CVSS v3.1 scoring** with AV/AC/PR/UI/S/C/I/A vectors
- **Attack scenarios** — realistic PoC per finding
- **CWE mapping** — each finding linked to CWE

**Sources:** `code` · `github_pr_url` · `github_file_url` · `github_repo_url`

Supply `scope` to focus the assessment (e.g. "API endpoints only, skip frontend").
""",
)
def run_vapt(req: VAPTRequest, caller: dict = Depends(get_caller)):
    artifact, src_type, src_ref, pb_ref = _resolve(req)
    ctx = f"Source: {src_type} — {src_ref}\n{req.context or ''}".strip()
    override = _effective_llm_override(req, caller, "vapt")
    result = agent_guard.guarded_call(
        caller, "vapt", override,
        lambda: _orch.review_vapt(artifact, req.scope or "", ctx, req.project_name or src_ref[:60]),
    )
    pb = _do_postback(src_type, pb_ref, result) if req.post_back else "not_requested"
    return _wrap("vapt", result, src_type, src_ref, pb, req.project_name or "", llm_gateway.describe(override))


# ── 6. Security Review ────────────────────────────────────────────────────

@router.post(
    "/security",
    summary="STRIDE threat model, trust boundaries, and attack surface analysis",
    description="""Performs a security architecture review producing:

- **STRIDE threat matrix** — one entry per identified threat (Spoofing/Tampering/Repudiation/Information Disclosure/DoS/Elevation)
- **Trust boundary map** — from/to zones, data crossing, controls present, gaps
- **Attack surface analysis** — external entry points, auth surfaces, admin interfaces, data egress
- **Security architecture gaps** — secrets management, network segmentation, data classification, audit/logging
- **Compliance architecture** — HIPAA, GDPR, SOC2, NIST AI RMF, GxP/21 CFR Part 11

**Sources:** `artifact` · `github_repo_url` · `github_file_url` · `jira_ticket`

Use `system_type` to sharpen focus (e.g. "Healthcare API with PHI", "Payment microservice").
""",
)
def run_security(req: SecurityReviewRequest, caller: dict = Depends(get_caller)):
    artifact, src_type, src_ref, pb_ref = _resolve(req)
    ctx = f"Source: {src_type} — {src_ref}\n{req.context or ''}".strip()
    override = _effective_llm_override(req, caller, "security")
    result = agent_guard.guarded_call(
        caller, "security", override,
        lambda: _orch.review_security(artifact, req.system_type or "", ctx, req.project_name or src_ref[:60]),
    )
    pb = _do_postback(src_type, pb_ref, result) if req.post_back else "not_requested"
    return _wrap("security", result, src_type, src_ref, pb, req.project_name or "", llm_gateway.describe(override))


# ── 7. Architecture Review ────────────────────────────────────────────────

@router.post(
    "/architecture",
    summary="Architectural quality scores, anti-patterns, SPOFs, and alternatives",
    description="""Evaluates system architecture across:

- **8 quality dimensions** — scalability, resilience, maintainability, security, performance, observability, deployability, cost_efficiency (each scored 1–10)
- **Anti-pattern detection** — Distributed Monolith, God Service, Chatty Services, Shared DB, Nano-Services, and 15+ more
- **Single points of failure** — component, failure scenario, mitigation
- **Alternative recommendations** — up to 3 patterns with fit score, migration complexity, advantages/disadvantages
- **AI/LLM architecture** — RAG pipeline, agent orchestration, vector store, timeout/retry, cost controls

**Sources:** `artifact` · `github_repo_url` · `github_file_url` · `jira_ticket`

**review_mode:** `review` (assess current) · `recommend` (suggest improvements) · `compare` (score multiple options)
""",
)
def run_architecture(req: ArchitectureReviewRequest, caller: dict = Depends(get_caller)):
    artifact, src_type, src_ref, pb_ref = _resolve(req)
    ctx = f"Source: {src_type} — {src_ref}\n{req.context or ''}".strip()
    override = _effective_llm_override(req, caller, "architecture")
    result = agent_guard.guarded_call(
        caller, "architecture", override,
        lambda: _orch.review_architecture(
            artifact,
            requirements=req.requirements or "",
            review_mode=req.review_mode or "review",
            context=ctx,
            project_name=req.project_name or src_ref[:60],
        ),
    )
    pb = _do_postback(src_type, pb_ref, result) if req.post_back else "not_requested"
    return _wrap("architecture", result, src_type, src_ref, pb, req.project_name or "", llm_gateway.describe(override))


# ── 8. Sensitive Data ─────────────────────────────────────────────────────

@router.post(
    "/sensitive-data",
    summary="Scan for PII, PHI, PCI data, credentials, and location data",
    description="""Scans any text artifact for sensitive data exposure or mishandling:

- **PII** — names, emails, SSNs, DOBs, phone numbers, national IDs
- **PHI** — medical record numbers, diagnoses, health insurance IDs, lab results
- **PCI** — card numbers (Luhn-detectable patterns), CVV/CVC, bank account numbers
- **Credentials** — API keys, tokens, private keys, hardcoded passwords, connection strings
- **Location data** — GPS coordinates, precise addresses in unexpected contexts

Per finding: category, location, obfuscated sample, regulation reference (HIPAA §, GDPR Art., PCI DSS Req.), severity, and whether it blocks release.

**Sources:** `content` · `github_file_url`
""",
)
def run_sensitive_data(req: SensitiveDataRequest, caller: dict = Depends(get_caller)):
    artifact, src_type, src_ref, _ = _resolve(req)
    ctx = f"Source: {src_type} — {src_ref}\n{req.context or ''}".strip()
    override = _effective_llm_override(req, caller, "sensitive-data")
    result = agent_guard.guarded_call(
        caller, "sensitive-data", override,
        lambda: _orch.scan_sensitive_data(artifact, ctx, req.project_name or src_ref[:60]),
    )
    # sensitive data agent never posts back (output contains obfuscated data)
    return _wrap("sensitive-data", result, src_type, src_ref, "not_requested", req.project_name or "", llm_gateway.describe(override))


# ── 9. Release Gate ───────────────────────────────────────────────────────

@router.post(
    "/release-gate",
    summary="Evaluate release readiness and issue a gate decision",
    description="""Aggregates findings from prior reviews and issues a release gate decision:

- **Red** — any unresolved Critical, hardcoded secrets, auth bypass, PHI logging, SQL injection, missing encryption
- **Amber** — High findings mitigated but not fully resolved, test coverage < 70%, missing rollback/monitoring plan
- **Green** — All Critical/High resolved, mandatory controls satisfied, evidence documented

**Input:** Pass `review_ids` (UUIDs from previous agent calls) to aggregate findings automatically.
Or supply `evidence_summary` with manual evidence text.

**Returns:** Gate decision, mandatory control checklist, required approvers, and evidence items.
""",
)
def run_release_gate(req: ReleaseGateRequest, caller: dict = Depends(get_caller)):
    override = _effective_llm_override(req, caller, "release-gate")
    result = agent_guard.guarded_call(
        caller, "release-gate", override,
        lambda: _orch.run_release_gate(
            project_name=req.project_name,
            review_ids=req.review_ids or [],
            release_version=req.release_version or "",
            evidence_summary=req.evidence_summary or "",
            context=req.context or "",
        ),
    )
    return _wrap("release-gate", result, "internal", req.project_name, "not_requested", req.project_name, llm_gateway.describe(override))


# ── 10. Full SDLC Review ──────────────────────────────────────────────────

@router.post(
    "/full",
    summary="Run all relevant agents in one call",
    description="""Orchestrates multiple agents in a single request and produces a unified release gate decision:

| Input | Agents triggered |
|-------|-----------------|
| `github_pr_url` | Code Review |
| `jira_ticket` | Requirement Review |
| `text` | Requirement Review + Code Review |
| `compliance_artifact` | Compliance Mapper (always added when supplied) |
| `architecture` | Architecture Review (always added when supplied) |
| `prompt_config` | Prompt Safety Review (always added when supplied) |

All findings are aggregated and passed to the Release Gate agent. A consolidated report is generated.

This chains up to 7 sequential LLM calls (each potentially 60-150s+), so it does not run
synchronously — it returns a `job_id` immediately (202 Accepted). Poll
`GET /api/v1/agents/jobs/{job_id}` for live step progress (`current_step`/`total_steps`/
`step_label`) and, once `status` is `completed` or `failed`, the same result/error shape
this endpoint used to return directly.
""",
    status_code=202,
)
async def run_full(req: FullReviewRequest, caller: dict = Depends(get_caller)):
    requirement = None
    code = None
    src_type = "text"
    src_ref = ""
    pb_ref = ""

    if req.github_pr_url:
        try:
            d = _github.fetch_for_agent(req.github_pr_url, "code")
            code = d.get("diff") or d.get("content", "")
            src_type, src_ref, pb_ref = "github_pr", d.get("title", req.github_pr_url), req.github_pr_url
        except Exception as e:
            logger.exception("GitHub PR fetch failed for %s", req.github_pr_url)
            raise HTTPException(502, f"GitHub fetch failed: {e}")

    elif req.jira_ticket:
        if not _jira.is_configured():
            raise HTTPException(503, "Jira not configured")
        try:
            issue = _jira.get_issue(req.jira_ticket)
            requirement = issue.get("content", "")
            src_type, src_ref, pb_ref = "jira", issue.get("title", req.jira_ticket), req.jira_ticket
        except Exception as e:
            logger.exception("Jira fetch failed for %s", req.jira_ticket)
            raise HTTPException(502, f"Jira fetch failed: {e}")

    elif req.text:
        requirement = req.text
        code = req.text
        src_ref = req.text[:80].replace("\n", " ")

    if not requirement and not code and not req.compliance_artifact and not req.architecture and not req.prompt_config:
        raise HTTPException(400, "Supply at least one of: text, github_pr_url, jira_ticket, compliance_artifact, architecture, prompt_config")

    ctx = f"Source: {src_type} — {src_ref}\n{req.context or ''}".strip()
    override = _effective_llm_override(req, caller, "full")

    total_steps = sum([
        bool(requirement), bool(req.compliance_artifact), bool(req.architecture),
        bool(code), bool(req.prompt_config),
    ]) + 2  # release gate + report generation always run

    def _run(on_progress):
        result = agent_guard.guarded_call(
            caller, "full", override,
            lambda: _orch.full_review(
                requirement=requirement,
                compliance_artifact=req.compliance_artifact,
                architecture=req.architecture,
                code=code,
                prompt_config=req.prompt_config,
                test_evidence=req.test_evidence or "",
                project_name=req.project_name or src_ref[:60] or "Full Review",
                context=ctx,
                on_progress=on_progress,
            ),
        )
        pb = _do_postback(src_type, pb_ref, result) if req.post_back and pb_ref else "not_requested"
        return _wrap("full", result, src_type, src_ref, pb, req.project_name or "", llm_gateway.describe(override))

    job_id = job_service.start_job("full", total_steps, _run)
    return {"job_id": job_id, "status": "queued", "poll_url": f"/api/v1/agents/jobs/{job_id}"}


@router.get(
    "/jobs/{job_id}",
    summary="Poll status/progress of a background agent job (currently only /full uses this)",
    description="""Returns the current status of a job created by `POST /api/v1/agents/full`.

`status` is one of `queued` / `running` / `completed` / `failed`. While running,
`current_step`/`total_steps`/`step_label` show which of the sequential sub-agent
calls is in flight (e.g. "3 of 7: Architecture Review"). Once `completed`, `result`
holds the same payload the endpoint used to return directly. Once `failed`, `error`
holds a structured error body (same shape as the platform's other error responses:
`detail`, `error_type`, `request_id`, etc.).
""",
)
def get_job(job_id: str, caller: dict = Depends(get_caller)):
    job = job_service.get_job_status(job_id)
    if not job:
        raise HTTPException(404, f"No job found with id '{job_id}'")
    return job


# ── 11. Code Generation — Advisory ────────────────────────────────────────

@router.post(
    "/codegen-advisory",
    summary="Recommend language, stack, architecture pattern, and design patterns for a new system",
    description="""Given a requirement, recommends:

- **Language & frameworks** — backend language/framework, frontend framework, database
- **Architecture pattern** — Modular Monolith, Microservices, Serverless, Event-Driven, etc.
- **Design patterns** — concrete patterns (Repository, CQRS, Circuit Breaker, ...) tied to specific components
- **Alternatives** — other viable stack/pattern combinations with switch conditions
- **Compliance considerations** — build requirements implied by HIPAA/GDPR/PCI/etc.

Pin any field (`pin_language`, `pin_backend_framework`, `pin_frontend_framework`, `pin_architecture_pattern`,
`pin_database`) to force that choice — the agent adapts everything else around your pin instead of overriding it.

Feed this response's fields into `POST /api/v1/agents/codegen-scaffold` to generate a starter project for the
recommended (or your pinned) stack.
""",
)
def run_codegen_advisory(req: CodeGenAdvisoryRequest, caller: dict = Depends(get_caller)):
    pins = {
        "language": req.pin_language,
        "backend_framework": req.pin_backend_framework,
        "frontend_framework": req.pin_frontend_framework,
        "architecture_pattern": req.pin_architecture_pattern,
        "database": req.pin_database,
    }
    override = _effective_llm_override(req, caller, "codegen-advisory")
    result = agent_guard.guarded_call(
        caller, "codegen-advisory", override,
        lambda: _orch.generate_codegen_advisory(
            requirement=req.requirement,
            constraints=req.constraints or "",
            pins=pins,
            context=req.context or "",
            project_name=req.project_name or req.requirement[:60],
        ),
    )
    return {
        "success": True,
        "agent": "codegen-advisory",
        "agent_title": "Code Generation Advisor",
        "review_id": result.get("review_id"),
        "project_name": req.project_name or "",
        "recommendation": {k: v for k, v in result.items() if k not in ("review_id", "agent_name")},
        "llm_used": llm_gateway.describe(override),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


# ── 12. Code Generation — Scaffold ────────────────────────────────────────

@router.post(
    "/codegen-scaffold",
    summary="Generate a runnable starter project for a chosen stack",
    description="""Generates a minimal, runnable scaffold (8-16 files: entry point, model, route, config,
dependency manifest, Dockerfile, README, and a wired frontend page if a frontend framework is given) for the
specified language, backend/frontend framework, database, and architecture pattern.

Set `auto_review: true` to run the Code Review and Sensitive Data agents against the generated code before
returning it — so the scaffold ships pre-certified against this platform's governance controls.

Use `GET /api/v1/agents/codegen-scaffold/{review_id}/download` to fetch the generated files as a zip.
""",
)
def run_codegen_scaffold(req: CodeGenScaffoldRequest, caller: dict = Depends(get_caller)):
    override = _effective_llm_override(req, caller, "codegen-scaffold")
    result = agent_guard.guarded_call(
        caller, "codegen-scaffold", override,
        lambda: _orch.generate_codegen_scaffold(
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
        ),
    )
    review_id = result.get("review_id")
    return {
        "success": True,
        "agent": "codegen-scaffold",
        "agent_title": "Code Generation Scaffold",
        "review_id": review_id,
        "project_name": result.get("project_name", req.project_name),
        "files": result.get("files", []),
        "file_count": len(result.get("files", [])),
        "setup_instructions": result.get("setup_instructions", ""),
        "next_steps": result.get("next_steps", []),
        "included_controls": result.get("included_controls", []),
        "governance_review": result.get("governance_review"),
        "download_url": f"/api/v1/agents/codegen-scaffold/{review_id}/download" if review_id else None,
        "_budget_stats": result.get("_budget_stats"),
        "llm_used": llm_gateway.describe(override),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


# ── 13. Code Generation — Download scaffold as zip ────────────────────────

@router.get(
    "/codegen-scaffold/{review_id}/download",
    summary="Download a previously generated scaffold as a zip file",
)
def download_codegen_scaffold(review_id: str, caller: dict = Depends(get_caller)):
    review = database.get_review(review_id)
    if not review or review.get("review_type") != "codegen_scaffold":
        raise HTTPException(404, "Scaffold not found")
    files = review["result"].get("files", [])
    if not files:
        raise HTTPException(404, "No files stored for this scaffold")

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


# ── 14. AI Code Provenance & Risk ──────────────────────────────────────────

@router.post(
    "/ai-provenance",
    summary="Assess the AI-origin likelihood of a code artifact — evidence, not a verdict",
    description="""Direct AI-authorship detection is unreliable — human edits to AI code (and AI edits to
human code) remove most visible signals, and code watermarking is easily defeated by routine changes like
variable renaming. This agent instead produces **probabilistic evidence**:

- **`ai_origin_likelihood`** — Low / Possible / Likely / Strong evidence (never a definitive claim)
- **`score_breakdown`** — every factor that contributed to the score, with concrete evidence, not a black-box number
- **`style_signals`** — the specific weak stylistic signals checked (uniform style, generic names, boilerplate,
  over-commenting, etc.) and whether each was observed
- **`recommended_action`** — Normal review / Ask developer declaration / Enhanced security+license+test review /
  Require reviewer approval+evidence capture — scaled to the likelihood, never "block"

Pass an optional developer disclosure (`ai_used`, `ai_tool`, `purpose`, `percent_ai_assisted`,
`sensitive_data_shared`, `prompt_stored`, `human_review_completed`) — this is one weighted input into the
score, not treated as proof either way.

**Sources:** `code` · `github_pr_url` · `github_file_url`
""",
)
def run_ai_provenance(req: AIProvenanceRequest, caller: dict = Depends(get_caller)):
    artifact, src_type, src_ref, pb_ref = _resolve(req)
    ctx = f"Source: {src_type} — {src_ref}\n{req.context or ''}".strip()
    disclosure = {
        "ai_used": req.ai_used,
        "ai_tool": req.ai_tool,
        "purpose": req.purpose,
        "percent_ai_assisted": req.percent_ai_assisted,
        "sensitive_data_shared": req.sensitive_data_shared,
        "prompt_stored": req.prompt_stored,
        "human_review_completed": req.human_review_completed,
    }
    override = _effective_llm_override(req, caller, "ai-provenance")
    result = agent_guard.guarded_call(
        caller, "ai-provenance", override,
        lambda: _orch.analyze_ai_provenance(artifact, disclosure, ctx, req.project_name or src_ref[:60]),
    )
    return {
        "success": True,
        "agent": "ai-provenance",
        "agent_title": "AI Code Provenance & Risk",
        "review_id": result.get("review_id"),
        "project_name": req.project_name or "",
        "source_type": src_type,
        "source_ref": src_ref,
        "report": {k: v for k, v in result.items() if k not in ("review_id", "agent_name")},
        "llm_used": llm_gateway.describe(override),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


# ── 15. Migration Assessment ────────────────────────────────────────────────

@router.post(
    "/migration-assessment",
    summary="Assess complexity, risk, and a phased plan for a code migration",
    description="""Given existing code and a migration target, produces:

- **Source/target stack detection** and migration type classification
- **Complexity rating** and a realistic effort estimate — never sugar-coated
- **Breaking changes** — concrete APIs/syntax/behaviors that differ, with impact and mitigation
- **Risk areas** and a **phased plan** with concrete deliverables per phase
- **Recommended approach** (big-bang vs. incremental/strangler-fig) with justification, plus a rollback strategy

Uses this project's Knowledge Base (if `project_name` matches uploaded docs) as authoritative for target
stack/pattern preferences.

**Sources:** `code` · `github_pr_url` · `github_file_url`

Feed the result into `POST /api/v1/agents/migration-execute` to transform a specific file.
""",
)
def run_migration_assessment(req: MigrationAssessmentRequest, caller: dict = Depends(get_caller)):
    artifact, src_type, src_ref, pb_ref = _resolve(req)
    ctx = f"Source: {src_type} — {src_ref}\n{req.context or ''}".strip()
    override = _effective_llm_override(req, caller, "migration-assessment")
    result = agent_guard.guarded_call(
        caller, "migration-assessment", override,
        lambda: _orch.assess_migration(
            artifact, req.migration_target, req.constraints or "", ctx, req.project_name or src_ref[:60],
        ),
    )
    return {
        "success": True,
        "agent": "migration-assessment",
        "agent_title": "Migration Assessment",
        "review_id": result.get("review_id"),
        "project_name": req.project_name or "",
        "source_type": src_type,
        "source_ref": src_ref,
        "assessment": {k: v for k, v in result.items() if k not in ("review_id", "agent_name")},
        "llm_used": llm_gateway.describe(override),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


# ── 16. Migration Execution ─────────────────────────────────────────────────

@router.post(
    "/migration-execute",
    summary="Transform a file/artifact to the migration target",
    description="""Migrates one file/artifact to the target stack, preserving behavior exactly unless the
migration itself requires a change. Returns complete, runnable code — never a diff or partial snippet — plus
a change-by-change summary and an honest list of anything that needs manual follow-up.

Set `auto_review: true` to run the Code Review and Sensitive Data agents against the migrated code before
returning it.

**Sources:** `code` · `github_pr_url` · `github_file_url`
""",
)
def run_migration_execute(req: MigrationExecutionRequest, caller: dict = Depends(get_caller)):
    artifact, src_type, src_ref, pb_ref = _resolve(req)
    ctx = f"Source: {src_type} — {src_ref}\n{req.context or ''}".strip()
    override = _effective_llm_override(req, caller, "migration-execute")
    result = agent_guard.guarded_call(
        caller, "migration-execute", override,
        lambda: _orch.execute_migration(
            source=artifact,
            migration_target=req.migration_target,
            file_path=req.file_path or src_ref,
            requirement=req.requirement or "",
            auto_review=req.auto_review,
            context=ctx,
            project_name=req.project_name or src_ref[:60],
        ),
    )
    return {
        "success": True,
        "agent": "migration-execute",
        "agent_title": "Migration Execution",
        "review_id": result.get("review_id"),
        "project_name": req.project_name or "",
        "files": result.get("files", []),
        "file_count": len(result.get("files", [])),
        "changes_summary": result.get("changes_summary", []),
        "manual_followup": result.get("manual_followup", []),
        "test_recommendations": result.get("test_recommendations", []),
        "risk_notes": result.get("risk_notes", ""),
        "governance_review": result.get("governance_review"),
        "summary": result.get("summary", ""),
        "_budget_stats": result.get("_budget_stats"),
        "llm_used": llm_gateway.describe(override),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


# ── 17. 21 CFR Part 11 Verification ─────────────────────────────────────────

@router.post(
    "/cfr21-verification",
    summary="Verify a codebase against FDA 21 CFR Part 11",
    description="""Verifies a codebase against FDA 21 CFR Part 11 (Electronic Records; Electronic Signatures)
across four assessments — audit trail, electronic signatures, access control, and data integrity (ALCOA+) —
plus an exhaustive findings list, each with cited evidence (file/function/pattern), never a bare claim.

Pass `files` (an array of `{path, content}`) for a real multi-file codebase scan — this is the primary way to
use this agent, since audit trails and signature workflows are usually cross-cutting concerns a single file
rarely fully reveals. `code` / `github_pr_url` / `github_file_url` remain available as single-source fallbacks.

Process-only controls (§11.10(i)/(j)/(k): training, written policies, documentation control) are reported
separately in `validation_readiness_notes` — the agent will never claim a code-only pass/fail on those.

**Sources:** `files` (recommended) · `code` · `github_pr_url` · `github_file_url`
""",
)
def run_cfr21_verification(req: CFR21VerificationRequest, caller: dict = Depends(get_caller)):
    if req.files:
        artifact = "\n\n".join(f"--- FILE: {f.path} ---\n{f.content}" for f in req.files)
        src_type, src_ref, pb_ref = "files", f"{len(req.files)} files", ""
        files_payload = [{"path": f.path, "content": f.content} for f in req.files]
    else:
        artifact, src_type, src_ref, pb_ref = _resolve(req)
        files_payload = []

    ctx = f"Source: {src_type} — {src_ref}\n{req.context or ''}".strip()
    override = _effective_llm_override(req, caller, "cfr21-verification")
    result = agent_guard.guarded_call(
        caller, "cfr21-verification", override,
        lambda: _orch.verify_cfr21(
            files=files_payload,
            code=artifact if not files_payload else "",
            system_description=req.system_description or "",
            context=ctx,
            project_name=req.project_name or src_ref[:60],
        ),
    )
    return {
        "success": True,
        "agent": "cfr21-verification",
        "agent_title": "21 CFR Part 11 Verification",
        "review_id": result.get("review_id"),
        "project_name": req.project_name or "",
        "source_type": src_type,
        "source_ref": src_ref,
        "overall_gate_status": result.get("overall_gate_status", "Green"),
        "overall_risk_score": result.get("overall_risk_score", 0),
        "total_findings": result.get("total_findings", len(result.get("findings", []))),
        "critical_count": result.get("critical_count", 0),
        "high_count": result.get("high_count", 0),
        "findings": result.get("findings", []),
        "audit_trail_assessment": result.get("audit_trail_assessment"),
        "esignature_assessment": result.get("esignature_assessment"),
        "access_control_assessment": result.get("access_control_assessment"),
        "data_integrity_assessment": result.get("data_integrity_assessment"),
        "validation_readiness_notes": result.get("validation_readiness_notes", ""),
        "files_scanned": result.get("files_scanned", []),
        "compliance_flags": result.get("compliance_flags", []),
        "recommendations": result.get("recommendations", []),
        "summary": result.get("summary", ""),
        "_budget_stats": result.get("_budget_stats"),
        "llm_used": llm_gateway.describe(override),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


# ── 18. GxP Document Check ───────────────────────────────────────────────────

@router.post(
    "/gxp-document-check",
    summary="Check a GxP document (or document set) for completeness and 21 CFR Part 11 compliance",
    description="""Checks a GxP document against a type-specific required-elements checklist (SOP, Validation
Protocol IQ/OQ/PQ, Validation Report, Batch Record, Deviation Report, CAPA, Change Control, Risk Assessment,
URS/Specification, Training Record — auto-detected, not just trusted from a hint) plus:

- **Signature/approval compliance** (21 CFR 11.50) — printed name, date, and meaning for every required signoff
- **Version control** — document number, revision, effective date, revision history
- **Traceability** — does this document reference what it should (e.g. OQ → URS), and if multiple documents
  are passed as a set, are they consistent with each other

Pass `documents` (an array of `{name, content}`) to check a related set together — traceability across
documents is much stronger evidence than checking any one in isolation. `content` remains available for a
single document.

**Sources:** `documents` (recommended for a set) · `content` (single document)
""",
)
def run_gxp_document_check(req: GxPDocumentCheckRequest, caller: dict = Depends(get_caller)):
    if req.documents:
        artifact = "\n\n".join(f"--- DOCUMENT: {d.name} ---\n{d.content}" for d in req.documents)
        src_type, src_ref = "documents", f"{len(req.documents)} documents"
        documents_payload = [{"name": d.name, "content": d.content} for d in req.documents]
    else:
        artifact, src_type, src_ref, _ = _resolve(req)
        documents_payload = []

    ctx = f"Source: {src_type} — {src_ref}\n{req.context or ''}".strip()
    override = _effective_llm_override(req, caller, "gxp-document-check")
    result = agent_guard.guarded_call(
        caller, "gxp-document-check", override,
        lambda: _orch.check_gxp_documents(
            documents=documents_payload,
            content=artifact if not documents_payload else "",
            document_type_hint=req.document_type_hint or "",
            context=ctx,
            project_name=req.project_name or src_ref[:60],
        ),
    )
    return {
        "success": True,
        "agent": "gxp-document-check",
        "agent_title": "GxP Document Check",
        "review_id": result.get("review_id"),
        "project_name": req.project_name or "",
        "source_type": src_type,
        "source_ref": src_ref,
        "document_type": result.get("document_type", "Unknown"),
        "completeness_score": result.get("completeness_score", 0),
        "overall_gate_status": result.get("overall_gate_status", "Green"),
        "overall_risk_score": result.get("overall_risk_score", 0),
        "total_findings": result.get("total_findings", len(result.get("findings", []))),
        "critical_count": result.get("critical_count", 0),
        "high_count": result.get("high_count", 0),
        "findings": result.get("findings", []),
        "required_elements": result.get("required_elements", []),
        "signature_approval_check": result.get("signature_approval_check"),
        "version_control_check": result.get("version_control_check"),
        "traceability_check": result.get("traceability_check"),
        "missing_sections": result.get("missing_sections", []),
        "compliance_flags": result.get("compliance_flags", []),
        "recommendations": result.get("recommendations", []),
        "summary": result.get("summary", ""),
        "_budget_stats": result.get("_budget_stats"),
        "llm_used": llm_gateway.describe(override),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


# ── 19. GxP Validation Plan ─────────────────────────────────────────────────

@router.post(
    "/gxp-generate-plan",
    summary="Plan the GxP validation document package for an application",
    description="""Points at an application (files, a code snippet, or a GitHub PR/file) and plans its GxP
validation document package: GAMP 5 risk classification, which of the standard documents actually apply
(User Requirement Specification, Functional/Design Specification, Risk Assessment, Validation Plan, IQ/OQ/PQ
Protocols, Traceability Matrix, Validation Summary Report, SOP), and an application-specific outline for each,
in the correct generation order.

Feed each applicable document's `document_type` and `outline` into `POST /api/v1/agents/gxp-generate-document`
to write the full document — call it once per document type.

**Sources:** `files` (recommended) · `code` · `github_pr_url` · `github_file_url`
""",
)
def run_gxp_generate_plan(req: GxPValidationPlanRequest, caller: dict = Depends(get_caller)):
    if req.files:
        artifact = "\n\n".join(f"--- FILE: {f.path} ---\n{f.content}" for f in req.files)
        src_type, src_ref = "files", f"{len(req.files)} files"
        files_payload = [{"path": f.path, "content": f.content} for f in req.files]
    else:
        artifact, src_type, src_ref, _ = _resolve(req)
        files_payload = []

    ctx = f"Source: {src_type} — {src_ref}\n{req.context or ''}".strip()
    override = _effective_llm_override(req, caller, "gxp-generate-plan")
    result = agent_guard.guarded_call(
        caller, "gxp-generate-plan", override,
        lambda: _orch.plan_gxp_validation_package(
            files=files_payload,
            code=artifact if not files_payload else "",
            system_description=req.system_description or "",
            context=ctx,
            project_name=req.project_name or src_ref[:60],
        ),
    )
    return {
        "success": True,
        "agent": "gxp-generate-plan",
        "agent_title": "GxP Validation Plan",
        "review_id": result.get("review_id"),
        "project_name": req.project_name or "",
        "gxp_classification": result.get("gxp_classification"),
        "classification_rationale": result.get("classification_rationale"),
        "system_summary": result.get("system_summary"),
        "applicable_documents": result.get("applicable_documents", []),
        "recommended_generation_order": result.get("recommended_generation_order", []),
        "summary": result.get("summary", ""),
        "_budget_stats": result.get("_budget_stats"),
        "llm_used": llm_gateway.describe(override),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


# ── 20. GxP Document Generation ─────────────────────────────────────────────

@router.post(
    "/gxp-generate-document",
    summary="Generate one complete GxP validation document",
    description="""Generates one complete, application-grounded GxP validation document — not a
fill-in-the-blank template. Pass `document_type` (from the plan step's `applicable_documents`) and its
`outline`; `plan_context` (the plan's `system_summary`) helps keep documents in the same package consistent
with each other.

Honest `open_items` are returned for anything that genuinely requires human input (real names, real dates,
live test execution, organizational policy decisions) — the agent will not fabricate those.

**Sources:** `files` (recommended) · `code` · `github_pr_url` · `github_file_url`
""",
)
def run_gxp_generate_document(req: GxPDocumentGenerateRequest, caller: dict = Depends(get_caller)):
    if req.files:
        artifact = "\n\n".join(f"--- FILE: {f.path} ---\n{f.content}" for f in req.files)
        src_type, src_ref = "files", f"{len(req.files)} files"
        files_payload = [{"path": f.path, "content": f.content} for f in req.files]
    else:
        artifact, src_type, src_ref, _ = _resolve(req)
        files_payload = []

    ctx = f"Source: {src_type} — {src_ref}\n{req.context or ''}".strip()
    override = _effective_llm_override(req, caller, "gxp-generate-document")
    result = agent_guard.guarded_call(
        caller, "gxp-generate-document", override,
        lambda: _orch.generate_gxp_document(
            document_type=req.document_type,
            files=files_payload,
            code=artifact if not files_payload else "",
            system_description=req.system_description or "",
            outline=req.outline,
            plan_context=req.plan_context or "",
            context=ctx,
            project_name=req.project_name or src_ref[:60],
        ),
    )
    return {
        "success": True,
        "agent": "gxp-generate-document",
        "agent_title": "GxP Document Generation",
        "review_id": result.get("review_id"),
        "project_name": req.project_name or "",
        "document_type": result.get("document_type", req.document_type),
        "title": result.get("title", ""),
        "content": result.get("content", ""),
        "traceability_refs": result.get("traceability_refs", []),
        "included_controls": result.get("included_controls", []),
        "test_cases": result.get("test_cases", []),
        "open_items": result.get("open_items", []),
        "summary": result.get("summary", ""),
        "_budget_stats": result.get("_budget_stats"),
        "llm_used": llm_gateway.describe(override),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


# ── 19. Issue Triage ─────────────────────────────────────────────────────────

@router.post(
    "/issue-triage",
    summary="Classify, prioritize, and recommend assignment for one issue",
    description="""Given a defect, incident, support ticket, security finding, or test failure, produces:

- **Classification**: issue category, affected SDLC area, environment
- **Severity & priority**, with explicit reasoning tied to the factors that actually applied
- **Duplicate detection** against this project's own prior triage reports
- **Recommended owner/team** with a confidence score — flags "insufficient ownership data" rather than guessing
  when no team roster is supplied
- **Enrichment**: reproduction steps, expected/actual behavior, suggested labels, a missing-information checklist —
  never inventing facts
- **Root-cause hypotheses**, clearly split into confirmed facts / probable / possible causes
- **Escalation flags** and a deterministic human-in-the-loop gate: Critical severity, P0/P1 priority, security/
  compliance impact, or <70% confidence always requires human validation (`gate_status: Red`)

**Sources:** `title`+`description` (recommended) · `github_issue_url`
""",
)
def run_issue_triage(req: IssueTriageRequest, caller: dict = Depends(get_caller)):
    title, description = req.title or "", req.description or ""
    src_type, src_ref = "text", title[:80]
    if req.github_issue_url and not (title.strip() or description.strip()):
        try:
            gh = _github.get_issue(req.github_issue_url)
        except Exception as e:
            logger.exception("GitHub issue fetch failed for %s", req.github_issue_url)
            raise HTTPException(502, f"GitHub fetch failed: {e}")
        title, description = gh.get("title", ""), gh.get("body", "")
        src_type, src_ref = "github_issue", title[:80]
    elif not (title.strip() or description.strip()):
        raise HTTPException(400, "Supply `title`/`description`, or `github_issue_url`.")

    override = _effective_llm_override(req, caller, "issue-triage")
    result = agent_guard.guarded_call(
        caller, "issue-triage", override,
        lambda: _orch.triage_issue(
            title=title, description=description, logs=req.logs or "", stack_trace=req.stack_trace or "",
            error_code=req.error_code or "", environment=req.environment or "", application=req.application or "",
            module=req.module or "", reporter=req.reporter or "", build_version=req.build_version or "",
            test_case_ref=req.test_case_ref or "", customer_impact_notes=req.customer_impact_notes or "",
            team_roster=req.team_roster or "", context=req.context or "",
            project_name=req.project_name or src_ref or "Unnamed Project", llm_override=override,
        ),
    )
    return {
        "success": True,
        "agent": "issue-triage",
        "agent_title": "Issue Triaging Agent",
        "review_id": result.get("review_id"),
        "project_name": req.project_name or "",
        "source_type": src_type,
        "triage_report": {k: v for k, v in result.items() if k not in ("review_id", "agent_name")},
        "llm_used": llm_gateway.describe(override),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


@router.post(
    "/issue-triage/batch-github",
    summary="Pull open GitHub issues for a repo and triage each one",
    description="""Fetches up to `limit` open issues from a GitHub repo (most recently updated first, pull
requests excluded) and runs the full Issue Triage pipeline on each — classification, severity/priority, duplicate
detection against this project's history, ownership recommendation, enrichment, and escalation flags. Returns
one report per issue plus a severity breakdown and escalation count.
""",
)
def run_issue_triage_batch_github(req: IssueTriageBatchGitHubRequest, caller: dict = Depends(get_caller)):
    override = _effective_llm_override(req, caller, "issue-triage-batch-github")
    try:
        result = agent_guard.guarded_call(
            caller, "issue-triage-batch-github", override,
            lambda: _orch.triage_github_repo_issues(
                github_repo_url=req.github_repo_url, limit=req.limit, team_roster=req.team_roster or "",
                context=req.context or "", project_name=req.project_name or req.github_repo_url,
                llm_override=override,
            ),
        )
    except (HTTPException, ScopeDeniedError, BudgetExceededError):
        raise
    except Exception as e:
        logger.exception("GitHub repo issue batch fetch failed for %s", req.github_repo_url)
        raise HTTPException(502, f"GitHub fetch failed: {e}")
    return {
        "success": True,
        "agent": "issue-triage-batch-github",
        "agent_title": "Issue Triaging — GitHub Batch",
        "project_name": req.project_name or "",
        **result,
        "llm_used": llm_gateway.describe(override),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


# ── 20. Test Scenario Generation ─────────────────────────────────────────────

@router.post(
    "/test-scenario",
    summary="Generate a business-level test scenario catalogue from a requirement",
    description="""Converts a requirement, user story, or acceptance criteria into test scenarios covering:

- **Happy path**, **negative**, **boundary value**, **role-based**, **integration**, and **exception/error handling**
- Each scenario traced back to the specific requirement text it covers
- A requirement coverage map flagging anything not yet covered by a scenario
- An explicit gap list for missing acceptance criteria — never invented

**Sources:** `text` · `jira_ticket` · `github_issue_url`

Feed the result's scenario catalogue into `POST /api/v1/agents/test-case` (via `scenarios_context`) to generate executable test cases.
""",
)
def run_test_scenario(req: TestScenarioRequest, caller: dict = Depends(get_caller)):
    artifact, src_type, src_ref, pb_ref = _resolve(req)
    ctx = f"Source: {src_type} — {src_ref}\n{req.context or ''}".strip()
    override = _effective_llm_override(req, caller, "test-scenario")
    result = agent_guard.guarded_call(
        caller, "test-scenario", override,
        lambda: _orch.generate_test_scenarios(artifact, ctx, req.project_name or src_ref[:60]),
    )
    pb = _do_postback(src_type, pb_ref, result) if req.post_back else "not_requested"
    return {
        "success": True,
        "agent": "test-scenario",
        "agent_title": "Test Scenario Generation",
        "review_id": result.get("review_id"),
        "project_name": req.project_name or "",
        "source_type": src_type,
        "post_back": pb,
        "scenarios": result.get("scenarios", []),
        "requirement_coverage": result.get("requirement_coverage", []),
        "missing_acceptance_criteria": result.get("missing_acceptance_criteria", []),
        "test_data_requirements": result.get("test_data_requirements", []),
        "total_scenarios": result.get("total_scenarios", 0),
        "critical_count": result.get("critical_count", 0),
        "summary": result.get("summary", ""),
        "_budget_stats": result.get("_budget_stats"),
        "llm_used": llm_gateway.describe(override),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


# ── 21. Test Case Generation ─────────────────────────────────────────────────

@router.post(
    "/test-case",
    summary="Generate detailed, executable manual test cases",
    description="""Generates test cases with step-by-step actions, expected results, concrete test data, and
traceability — either directly from a requirement, or from a prior Test Scenario catalogue.

- Every step is a concrete, unambiguous action — never "verify it works"
- Preconditions state the exact system/data/user state needed
- Anything too vague to turn into a real test case is listed in `untestable_items`, not guessed at

**Sources:** `text` · `jira_ticket` · `github_issue_url` · `scenarios_context` (pass the `scenarios` array from
`POST /api/v1/agents/test-scenario`, JSON-stringified, to write cases against those scenarios instead of the raw requirement)
""",
)
def run_test_case(req: TestCaseRequest, caller: dict = Depends(get_caller)):
    artifact, src_type, src_ref, pb_ref = _resolve(req)
    ctx = f"Source: {src_type} — {src_ref}\n{req.context or ''}".strip()
    override = _effective_llm_override(req, caller, "test-case")
    result = agent_guard.guarded_call(
        caller, "test-case", override,
        lambda: _orch.generate_test_cases(artifact, req.scenarios_context or "", ctx, req.project_name or src_ref[:60]),
    )
    pb = _do_postback(src_type, pb_ref, result) if req.post_back else "not_requested"
    return {
        "success": True,
        "agent": "test-case",
        "agent_title": "Test Case Generation",
        "review_id": result.get("review_id"),
        "project_name": req.project_name or "",
        "source_type": src_type,
        "post_back": pb,
        "test_cases": result.get("test_cases", []),
        "traceability": result.get("traceability", []),
        "untestable_items": result.get("untestable_items", []),
        "total_test_cases": result.get("total_test_cases", 0),
        "critical_count": result.get("critical_count", 0),
        "summary": result.get("summary", ""),
        "_budget_stats": result.get("_budget_stats"),
        "llm_used": llm_gateway.describe(override),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


# ── 22. Auto-Fix & Open PR ────────────────────────────────────────────────

@router.post(
    "/auto-fix",
    summary="Review a GitHub PR, commit fixes, and open a follow-up PR",
    description="""Reviews the PR, generates corrected file contents for its blocking findings, commits them to a
new `lm-autofix/pr-N` branch off the PR's head branch, and opens a follow-up PR back into it.

**This writes real commits to GitHub** — requires a `GITHUB_TOKEN` with write (`repo`) scope, not just read access.
Every generated patch is scanned for newly-introduced hardcoded secrets before being committed; any file that
fails the scan is skipped and listed in `blocked_patches` instead of being written.

**Sources:** `pr_url`
""",
)
def run_auto_fix(req: GithubAutoFixV1Request, caller: dict = Depends(get_caller)):
    if not config.GITHUB_TOKEN:
        raise HTTPException(503, "GITHUB_TOKEN not configured in .env")
    override = _effective_llm_override(req, caller, "auto-fix")
    result = agent_guard.guarded_call(
        caller, "auto-fix", override,
        lambda: _orch.auto_fix_github_pr(pr_url=req.pr_url, jira_ticket=req.jira_ticket or "", max_files=req.max_files),
    )
    return {
        "success": True,
        "agent": "auto-fix",
        "agent_title": "Auto-Fix & Open PR",
        "pr_url": result.get("pr_url"),
        "pr_number": result.get("pr_number"),
        "pr_title": result.get("pr_title"),
        "fix_applied": result.get("fix_applied", False),
        "reason": result.get("reason"),
        "fix_branch": result.get("fix_branch"),
        "fix_pr_url": result.get("fix_pr_url"),
        "fix_pr_number": result.get("fix_pr_number"),
        "files_patched": result.get("files_patched", []),
        "blocked_patches": result.get("blocked_patches", []),
        "review_result": result.get("review_result", {}),
        "_budget_stats": result.get("_budget_stats"),
        "llm_used": llm_gateway.describe(override),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


# ── 23. Governance Report Generator ───────────────────────────────────────

@router.post(
    "/report-generate",
    summary="Synthesize prior findings into an audit-ready governance report",
    description="""Aggregates findings from `review_ids` and/or a directly-supplied `findings` list into a full
governance report — executive summary, findings by category, compliance coverage matrix, controls assessment,
prioritized recommendations, and release decision. Does not re-run any review agent.

**Sources:** `review_ids` · `findings`
""",
)
def run_report_generate(req: ReportGenerateRequest, caller: dict = Depends(get_caller)):
    override = _effective_llm_override(req, caller, "report-generate")
    findings = [f.model_dump() for f in req.findings] if req.findings else None
    result = agent_guard.guarded_call(
        caller, "report-generate", override,
        lambda: _orch.generate_standalone_report(
            project_name=req.project_name,
            review_ids=req.review_ids or [],
            findings=findings,
            gate_status=req.gate_status or "",
            overall_risk_score=req.overall_risk_score,
            context=req.context or "",
        ),
    )
    return {
        "success": True,
        "agent": "report-generate",
        "agent_title": "Governance Report Generator",
        "report_id": result.get("report_id"),
        "project_name": result.get("project_name"),
        "gate_status": result.get("gate_status"),
        "overall_risk_score": result.get("overall_risk_score"),
        "executive_summary": result.get("executive_summary"),
        "findings_by_category": result.get("findings_by_category"),
        "compliance_mapping": result.get("compliance_mapping"),
        "controls_checked": result.get("controls_checked"),
        "controls_passed": result.get("controls_passed"),
        "controls_failed": result.get("controls_failed"),
        "recommendations": result.get("recommendations"),
        "approval_required": result.get("approval_required"),
        "approvers_needed": result.get("approvers_needed"),
        "human_oversight_required": result.get("human_oversight_required"),
        "_budget_stats": result.get("_budget_stats"),
        "llm_used": llm_gateway.describe(override),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
