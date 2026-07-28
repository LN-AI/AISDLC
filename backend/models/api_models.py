"""Typed request / response models for LM AI-SDLC Agent API v1.

Every agent endpoint uses these models so external teams get:
  - Clear, self-documenting OpenAPI schemas
  - Predictable response envelopes
  - Consistent error shapes
"""
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


# ── Bring Your Own LLM — optional per-request override ────────────────────

class LLMOverrideFields(BaseModel):
    llm_provider: Optional[str] = Field(
        None,
        description="Route this single call through your own LLM: 'anthropic' or 'openai'. "
                    "Omit to use your API key's configured default, or the platform default.",
        examples=["anthropic", "openai"],
    )
    llm_api_key: Optional[str] = Field(
        None,
        description="Your own provider API key, used only for this call — never stored.",
    )
    llm_base_url: Optional[str] = Field(
        None,
        description="Custom endpoint, e.g. an AWS Bedrock/GCP Vertex Anthropic-compatible "
                    "gateway or an Azure OpenAI resource URL.",
    )
    llm_model: Optional[str] = Field(
        None,
        description="Model name override, e.g. 'claude-opus-4-1' or 'gpt-4o'.",
    )


# ── Shared source-routing fields ──────────────────────────────────────────

class _SourceBase(LLMOverrideFields):
    project_name: Optional[str] = Field(
        None, description="Project / product name stored in the audit trail",
        examples=["Patient Portal API", "Risk Scoring Engine v3"]
    )
    context: Optional[str] = Field(
        None, description="Free-text context injected into the review prompt",
        examples=["Healthcare SaaS — HIPAA env — v3.2 release branch"]
    )
    post_back: bool = Field(
        False,
        description="When true, posts the review result as a comment on the originating GitHub PR/issue or Jira ticket"
    )


# ── Per-agent request models ──────────────────────────────────────────────

class RequirementReviewRequest(_SourceBase):
    """Review a user story, BRD, or acceptance criteria for risk, ambiguity, and compliance gaps."""
    text: Optional[str] = Field(
        None,
        description="Raw requirement text, user story, or BRD to review",
        examples=["As a clinician I want to export patient data to CSV so that..."]
    )
    jira_ticket: Optional[str] = Field(
        None,
        description="Jira ticket ID (e.g. PROJ-123) or full Atlassian URL",
        examples=["PROJ-123", "https://company.atlassian.net/browse/PROJ-123"]
    )
    github_issue_url: Optional[str] = Field(
        None,
        description="GitHub issue URL",
        examples=["https://github.com/owner/repo/issues/45"]
    )


class CodeReviewRequest(_SourceBase):
    """Security and quality review of code, diffs, or pull requests."""
    code: Optional[str] = Field(
        None, description="Source code, git diff, or file content to review"
    )
    github_pr_url: Optional[str] = Field(
        None,
        description="GitHub Pull Request URL — fetches the PR diff automatically",
        examples=["https://github.com/owner/repo/pull/88"]
    )
    github_file_url: Optional[str] = Field(
        None,
        description="GitHub file blob URL",
        examples=["https://github.com/owner/repo/blob/main/src/api/auth.py"]
    )
    language: Optional[str] = Field(
        None,
        description="Programming language hint (improves accuracy)",
        examples=["python", "typescript", "java", "go"]
    )


class PromptSafetyRequest(_SourceBase):
    """Review AI system prompts, tool definitions, and RAG configs for injection and safety risks."""
    system_prompt: Optional[str] = Field(
        None, description="AI system prompt to audit"
    )
    user_prompt: Optional[str] = Field(
        None, description="Example user message or prompt template"
    )
    tool_definitions: Optional[str] = Field(
        None, description="Tool/function definitions exposed to the model (JSON or YAML)"
    )
    rag_config: Optional[str] = Field(
        None, description="RAG pipeline configuration or knowledge-base description"
    )
    github_file_url: Optional[str] = Field(
        None,
        description="GitHub file URL pointing to prompt configuration or agent code",
        examples=["https://github.com/owner/repo/blob/main/prompts/system.yaml"]
    )


class ComplianceRequest(_SourceBase):
    """Map an artifact to NIST AI-RMF, OWASP LLM, HIPAA, GDPR, ISO 27001, GxP, PCI DSS."""
    text: Optional[str] = Field(
        None,
        description="Artifact to map — requirement, architecture doc, policy, or system design"
    )
    jira_ticket: Optional[str] = Field(
        None, description="Jira ticket ID or URL"
    )
    frameworks: Optional[List[str]] = Field(
        None,
        description="Restrict mapping to specific frameworks (leave empty for auto-detect)",
        examples=[["HIPAA", "GDPR"], ["NIST-AI-RMF", "OWASP-LLM"]]
    )


class VAPTRequest(_SourceBase):
    """Full vulnerability assessment with CVSS scoring and OWASP mapping."""
    code: Optional[str] = Field(
        None, description="Code, config, or infrastructure-as-code to assess"
    )
    github_pr_url: Optional[str] = Field(
        None,
        description="GitHub PR URL",
        examples=["https://github.com/owner/repo/pull/92"]
    )
    github_file_url: Optional[str] = Field(
        None, description="GitHub file URL"
    )
    github_repo_url: Optional[str] = Field(
        None,
        description="GitHub repository URL for full-repo assessment",
        examples=["https://github.com/owner/repo"]
    )
    scope: Optional[str] = Field(
        None,
        description="Scope notes for the assessor",
        examples=["API endpoints only, skip frontend bundle", "Focus on auth and payment flows"]
    )


class SecurityReviewRequest(_SourceBase):
    """STRIDE threat model, trust boundary analysis, and attack surface assessment."""
    artifact: Optional[str] = Field(
        None, description="Architecture description, design doc, or code to review"
    )
    github_repo_url: Optional[str] = Field(
        None, description="GitHub repository URL"
    )
    github_file_url: Optional[str] = Field(
        None, description="GitHub file URL"
    )
    jira_ticket: Optional[str] = Field(
        None, description="Jira ticket ID or URL"
    )
    system_type: Optional[str] = Field(
        None,
        description="System type hint — helps the reviewer focus on relevant threats",
        examples=["Healthcare API with PHI", "Payment processing microservice", "LLM-powered chatbot"]
    )


class ArchitectureReviewRequest(_SourceBase):
    """Architectural quality scores, anti-pattern detection, SPOF analysis, and alternatives."""
    artifact: Optional[str] = Field(
        None, description="Architecture description, ADR, C4 diagram notes, or design document"
    )
    github_repo_url: Optional[str] = Field(
        None, description="GitHub repository URL (reads README + ARCHITECTURE docs)"
    )
    github_file_url: Optional[str] = Field(
        None, description="GitHub file URL"
    )
    jira_ticket: Optional[str] = Field(
        None, description="Jira ticket ID or URL"
    )
    requirements: Optional[str] = Field(
        None, description="Non-functional requirements or system constraints the architecture must satisfy"
    )
    review_mode: Optional[str] = Field(
        "review",
        description="review: assess current design | recommend: suggest improvements | compare: score multiple options"
    )


class SensitiveDataRequest(_SourceBase):
    """Scan code, configs, schemas, or logs for PII, PHI, PCI, credentials, and location data."""
    content: Optional[str] = Field(
        None, description="Code, configuration file, database schema, API response, or log sample to scan"
    )
    github_file_url: Optional[str] = Field(
        None,
        description="GitHub file URL — fetches and scans the file automatically",
        examples=["https://github.com/owner/repo/blob/main/models/patient.py"]
    )


class ReleaseGateRequest(LLMOverrideFields):
    """Evaluate whether a release should proceed based on prior review findings."""
    project_name: str = Field(
        ...,
        description="Project / product name",
        examples=["Patient Portal v3.2"]
    )
    review_ids: Optional[List[str]] = Field(
        None,
        description="Prior review IDs to aggregate findings from (fetched from DB). Leave empty to provide findings directly."
    )
    release_version: Optional[str] = Field(
        None,
        description="Version tag or release name",
        examples=["v3.2.0", "2026-Q3-release"]
    )
    evidence_summary: Optional[str] = Field(
        None,
        description="Evidence summary: test pass rates, pen-test report refs, sign-off notes, etc."
    )
    context: Optional[str] = Field(
        None, description="Additional release context"
    )


class AIProvenanceRequest(_SourceBase):
    """Assess the AI-origin likelihood of a code artifact — probabilistic evidence, not a verdict.
    Combines an optional developer disclosure with LLM-based style-signal analysis into a scored report."""
    code: Optional[str] = Field(None, description="Source code or diff to assess")
    github_pr_url: Optional[str] = Field(None, description="GitHub PR URL — fetches the diff automatically")
    github_file_url: Optional[str] = Field(None, description="GitHub file URL")

    ai_used: Optional[str] = Field(
        None, description="Developer's self-declared AI usage", examples=["Yes", "No", "Partially"]
    )
    ai_tool: Optional[str] = Field(None, description="Tool used, if any", examples=["GitHub Copilot", "ChatGPT", "Claude", "Cursor"])
    purpose: Optional[str] = Field(None, description="What the AI was used for", examples=["Generate", "Refactor", "Debug", "Document", "Test"])
    percent_ai_assisted: Optional[str] = Field(
        None, description="Developer's estimate of how much of the change was AI-assisted",
        examples=["0-25%", "25-50%", "50-75%", ">75%"]
    )
    sensitive_data_shared: Optional[bool] = Field(None, description="Was sensitive/proprietary data shared with the AI tool?")
    prompt_stored: Optional[bool] = Field(None, description="Was the prompt/session log captured and stored?")
    human_review_completed: Optional[bool] = Field(None, description="Has a human completed review of this artifact?")


class MigrationAssessmentRequest(_SourceBase):
    """Assess the complexity, risk, and phased plan for migrating code from one stack to another."""
    code: Optional[str] = Field(None, description="Source code or artifact to assess for migration")
    github_pr_url: Optional[str] = Field(None, description="GitHub PR URL")
    github_file_url: Optional[str] = Field(None, description="GitHub file URL")
    migration_target: str = Field(
        ..., description="What you're migrating to",
        examples=["Python 2.7 to Python 3.12", "Flask to FastAPI", "AngularJS to React 18", "MySQL to PostgreSQL", "Monolith to Microservices"]
    )
    constraints: Optional[str] = Field(None, description="Scale, timeline, team, or compliance constraints on the migration")


class MigrationExecutionRequest(_SourceBase):
    """Transform a specific file/artifact from its current stack to a target stack."""
    code: Optional[str] = Field(None, description="Source code to migrate")
    github_pr_url: Optional[str] = Field(None, description="GitHub PR URL")
    github_file_url: Optional[str] = Field(None, description="GitHub file URL")
    migration_target: str = Field(..., description="What you're migrating to", examples=["Flask to FastAPI"])
    file_path: Optional[str] = Field(None, description="Original file path, reused for the migrated output")
    requirement: Optional[str] = Field(None, description="Why this migration is happening — improves relevance")
    auto_review: bool = Field(
        False, description="Run the Code Review and Sensitive Data agents against the migrated code before returning it"
    )


class CodebaseFile(BaseModel):
    path: str = Field(..., description="Relative file path, e.g. backend/services/audit.py")
    content: str = Field(..., description="Full file content")


class CFR21VerificationRequest(_SourceBase):
    """Verify a codebase against FDA 21 CFR Part 11 (electronic records / electronic signatures) —
    audit trail, e-signature, access control, and data integrity (ALCOA+), with a full findings list."""
    files: Optional[List[CodebaseFile]] = Field(
        None, description="Multiple files for a real codebase scan — the primary way to use this agent"
    )
    code: Optional[str] = Field(None, description="Single code snippet, if not passing `files`")
    github_pr_url: Optional[str] = Field(None, description="GitHub PR URL")
    github_file_url: Optional[str] = Field(None, description="GitHub file URL")
    system_description: Optional[str] = Field(
        None, description="What this system does — helps calibrate applicability (e.g. e-signature checks are skipped if there's no approval workflow)",
        examples=["Clinical decision support system for oncology dosing", "Lab data management system (LIMS)"]
    )


class GxPDocument(BaseModel):
    name: str = Field(..., description="Document name/filename")
    content: str = Field(..., description="Full document text")


class GxPDocumentCheckRequest(_SourceBase):
    """Check a GxP document (SOP, validation protocol, batch record, deviation, CAPA, change control, etc.)
    for completeness and 21 CFR Part 11 compliance — required elements, signatures, version control, traceability."""
    documents: Optional[List[GxPDocument]] = Field(
        None, description="Multiple related documents checked as a set (e.g. URS + OQ protocol) — traceability is checked across them"
    )
    content: Optional[str] = Field(None, description="Single document text, if not passing `documents`")
    document_type_hint: Optional[str] = Field(
        None, description="What kind of document this is, if known — the agent verifies rather than just trusting it",
        examples=["SOP", "OQ Protocol", "Batch Record", "Deviation Report", "CAPA", "Change Control"]
    )


class GxPValidationPlanRequest(_SourceBase):
    """Point at an application and plan its GxP validation document package — which documents apply
    (URS, FRS, Risk Assessment, IQ/OQ/PQ, Traceability Matrix, Validation Summary, SOP) and an outline for each."""
    files: Optional[List[CodebaseFile]] = Field(None, description="The application's files — the primary way to use this agent")
    code: Optional[str] = Field(None, description="Single code snippet, if not passing `files`")
    github_pr_url: Optional[str] = Field(None, description="GitHub PR URL")
    github_file_url: Optional[str] = Field(None, description="GitHub file URL")
    system_description: Optional[str] = Field(
        None, description="What this application does",
        examples=["Lab data management system (LIMS) with approval workflow"]
    )


class GxPDocumentGenerateRequest(_SourceBase):
    """Generate one complete GxP validation document for an application (URS, FRS, Risk Assessment,
    Validation Plan, IQ/OQ/PQ Protocol, Traceability Matrix, Validation Summary Report, or SOP)."""
    document_type: str = Field(
        ..., description="Which document to generate",
        examples=["User Requirement Specification", "OQ Protocol", "Risk Assessment"]
    )
    files: Optional[List[CodebaseFile]] = Field(None, description="The application's files")
    code: Optional[str] = Field(None, description="Single code snippet, if not passing `files`")
    github_pr_url: Optional[str] = Field(None, description="GitHub PR URL")
    github_file_url: Optional[str] = Field(None, description="GitHub file URL")
    system_description: Optional[str] = Field(None, description="What this application does")
    outline: Optional[List[str]] = Field(None, description="Section outline for this document — typically copied from the planning step's output")
    plan_context: Optional[str] = Field(None, description="Free-text context from the planning step, for cross-referencing other documents in the same package")


class IssueTriageRequest(_SourceBase):
    """Classify, prioritize, assign, and enrich a defect/incident/support ticket/security finding/test
    failure. Provide as much structured detail as you have — the agent explicitly marks anything it can't
    determine as unavailable rather than inventing it."""
    title: Optional[str] = Field(None, description="Ticket/issue title", examples=["Login fails for all users in production"])
    description: Optional[str] = Field(None, description="Full issue description / reporter's account of the problem")
    github_issue_url: Optional[str] = Field(None, description="GitHub Issue URL — fetched as the issue content if `title`/`description` are omitted")
    logs: Optional[str] = Field(None, description="Relevant application/server logs")
    stack_trace: Optional[str] = Field(None, description="Stack trace, if any")
    error_code: Optional[str] = Field(None, description="Error code or exception type, if known")
    environment: Optional[str] = Field(None, description="Environment where this occurred", examples=["Production", "Staging", "QA", "Development"])
    application: Optional[str] = Field(None, description="Affected application/service name")
    module: Optional[str] = Field(None, description="Affected module/component, if known")
    reporter: Optional[str] = Field(None, description="Who reported this (name, role, or 'customer')")
    build_version: Optional[str] = Field(None, description="Build or release version where this was observed")
    test_case_ref: Optional[str] = Field(None, description="Related test case or requirement ID, if any")
    customer_impact_notes: Optional[str] = Field(None, description="Number of users/customers affected, business-process or revenue impact, SLA commitments")
    team_roster: Optional[str] = Field(
        None,
        description="Optional team/ownership mapping (e.g. a pasted CODEOWNERS file, or 'auth-service: @team-identity, "
                    "billing: @team-payments') used to recommend an owner/team. Without this, the agent flags ownership "
                    "data as insufficient rather than guessing.",
    )


class IssueTriageBatchGitHubRequest(LLMOverrideFields):
    """Pull open GitHub issues for a repo and triage each one."""
    github_repo_url: str = Field(..., description="GitHub repo URL", examples=["https://github.com/acme/patient-portal"])
    limit: int = Field(10, ge=1, le=25, description="Max number of open issues to triage (most recently updated first)")
    project_name: Optional[str] = Field(None, description="Project name stored in the audit trail")
    context: Optional[str] = Field(None, description="Free-text context injected into every issue's triage prompt")
    team_roster: Optional[str] = Field(None, description="Optional team/ownership mapping applied to every issue in this batch")


class TestScenarioRequest(_SourceBase):
    """Convert a requirement, user story, or acceptance criteria into a business-level test scenario catalogue —
    happy path, negative, boundary value, role-based, integration, and exception handling."""
    text: Optional[str] = Field(
        None, description="Requirement, user story, or acceptance criteria to generate test scenarios from",
        examples=["As a clinician I want to export patient data to CSV so that I can share it with another provider."]
    )
    jira_ticket: Optional[str] = Field(None, description="Jira ticket ID or full Atlassian URL", examples=["PROJ-123"])
    github_issue_url: Optional[str] = Field(None, description="GitHub issue URL", examples=["https://github.com/owner/repo/issues/45"])


class TestCaseRequest(_SourceBase):
    """Generate detailed, executable manual test cases with step-by-step actions, expected results, and test data."""
    text: Optional[str] = Field(
        None, description="Requirement or user story to generate test cases from",
        examples=["As a clinician I want to export patient data to CSV so that I can share it with another provider."]
    )
    scenarios_context: Optional[str] = Field(
        None, description="A prior test scenario catalogue (e.g. from POST /api/v1/agents/test-scenarios) to write test cases against, instead of deriving cases directly from the requirement"
    )
    jira_ticket: Optional[str] = Field(None, description="Jira ticket ID or full Atlassian URL", examples=["PROJ-123"])
    github_issue_url: Optional[str] = Field(None, description="GitHub issue URL", examples=["https://github.com/owner/repo/issues/45"])


class CodeGenAdvisoryRequest(LLMOverrideFields):
    """Recommend language, stack, architecture pattern, and design patterns for a new system."""
    requirement: str = Field(
        ...,
        description="The system or feature requirement to design for",
        examples=["A patient appointment booking system for a mid-size clinic network"]
    )
    constraints: Optional[str] = Field(
        None,
        description="Scale, compliance, team, or technical constraints",
        examples=["Expect 50k MAU, must be HIPAA compliant, team knows Python"]
    )
    context: Optional[str] = Field(None, description="Additional free-text context")
    project_name: Optional[str] = Field(None, description="Project name stored in the audit trail")
    pin_language: Optional[str] = Field(None, description="Force this language regardless of the AI's recommendation")
    pin_backend_framework: Optional[str] = Field(None, description="Force this backend framework")
    pin_frontend_framework: Optional[str] = Field(None, description="Force this frontend framework")
    pin_architecture_pattern: Optional[str] = Field(
        None, description="Force this architecture pattern",
        examples=["Modular Monolith", "Microservices", "Serverless", "Event-Driven"]
    )
    pin_database: Optional[str] = Field(None, description="Force this database choice")


class CodeGenScaffoldRequest(LLMOverrideFields):
    """Generate a runnable starter project for a chosen stack and architecture pattern."""
    project_name: str = Field(..., description="Project name — also used as the generated folder/zip name")
    language: str = Field(..., examples=["Python", "TypeScript", "Java", "Go"])
    backend_framework: str = Field(..., examples=["FastAPI", "Express", "Spring Boot", "Django"])
    frontend_framework: Optional[str] = Field(None, examples=["React + TypeScript", "Vue 3", "Angular", "None"])
    database: Optional[str] = Field(None, examples=["PostgreSQL", "MongoDB", "DynamoDB"])
    architecture_pattern: Optional[str] = Field("Modular Monolith", examples=["Modular Monolith", "Microservices", "Serverless"])
    requirement: Optional[str] = Field(None, description="The requirement this scaffold serves — improves generated code relevance")
    context: Optional[str] = Field(None, description="Additional free-text context")
    include_auth: bool = Field(True, description="Include a JWT auth stub (login endpoint + protected route example)")
    include_docker: bool = Field(True, description="Include a Dockerfile for the backend")
    include_tests: bool = Field(True, description="Include one example test file")
    auto_review: bool = Field(
        False,
        description="Run the Code Review and Sensitive Data agents against the generated scaffold before returning it"
    )


class FullReviewRequest(_SourceBase):
    """Run all relevant agents in one pass — requirement, compliance, architecture, code — and produce a release gate decision."""
    text: Optional[str] = Field(
        None, description="Requirement or code artifact (used as a fallback if `requirement`/`code` aren't passed via github_pr_url/jira_ticket)"
    )
    github_pr_url: Optional[str] = Field(
        None,
        description="GitHub PR URL — runs code + VAPT + compliance",
        examples=["https://github.com/owner/repo/pull/88"]
    )
    jira_ticket: Optional[str] = Field(
        None,
        description="Jira ticket — runs requirement + compliance",
        examples=["PROJ-123"]
    )
    compliance_artifact: Optional[str] = Field(
        None, description="Artifact to map to compliance frameworks (NIST AI-RMF, OWASP LLM, HIPAA, GDPR, GxP, etc.) — runs the Compliance Mapper alongside other agents"
    )
    architecture: Optional[str] = Field(
        None, description="Architecture / system description — runs a real Architecture Review (quality scoring, anti-patterns, SPOFs) alongside other agents"
    )
    prompt_config: Optional[Dict[str, str]] = Field(
        None,
        description="Prompt configuration — keys: system_prompt, user_prompt, tool_definitions, rag_config"
    )
    test_evidence: Optional[str] = Field(
        None, description="Test results or sign-off evidence for the release gate"
    )


class GithubAutoFixV1Request(LLMOverrideFields):
    """Review a GitHub PR, then commit fixes for its findings to a new branch off the PR's head
    branch and open a follow-up PR back into it. Requires a GITHUB_TOKEN with write (repo) scope."""
    pr_url: str = Field(..., description="GitHub PR URL", examples=["https://github.com/acme/patient-portal/pull/42"])
    jira_ticket: Optional[str] = Field(None, description="Jira ticket ID for audit trail context")
    max_files: int = Field(5, ge=1, le=15, description="Max number of changed files to attempt to patch")


# ── Standard finding (shared across all agents) ───────────────────────────

class Finding(BaseModel):
    control_id: str = Field(..., examples=["SEC-003", "AI-001", "VAPT-007"])
    severity: int = Field(..., ge=1, le=5, description="1=Low → 5=Critical")
    risk_level: str = Field(..., examples=["Critical", "High", "Moderate", "Minor", "Low"])
    title: str
    description: str
    recommendation: str
    is_blocking: bool
    owasp_ref: Optional[str] = Field(None, examples=["A03:2021 — Injection"])
    nist_ref: Optional[str] = Field(None, examples=["GOVERN 1.1"])


class ReportGenerateRequest(LLMOverrideFields):
    """Synthesize prior review findings into a comprehensive, audit-ready governance report —
    without re-running a full review."""
    project_name: str = Field(..., examples=["Patient Portal API"])
    review_ids: Optional[List[str]] = Field(
        None, description="Review IDs (from prior agent calls) whose findings to aggregate into the report"
    )
    findings: Optional[List[Finding]] = Field(
        None, description="Findings to report on directly, instead of (or in addition to) review_ids"
    )
    gate_status: Optional[str] = Field(None, description="Overall gate status; computed from findings if omitted", examples=["Green", "Amber", "Red"])
    overall_risk_score: Optional[float] = Field(None, ge=0, le=100, description="Overall risk score; computed from findings if omitted")
    context: Optional[str] = Field(None, description="Free-text context injected into the report prompt")


# ── Standard response envelope ─────────────────────────────────────────────

class AgentResponse(BaseModel):
    """Standard response returned by every v1 agent endpoint."""
    success: bool = True
    agent: str = Field(..., examples=["code", "vapt", "compliance"])
    agent_title: str = Field(..., examples=["Code & PR Security Reviewer"])
    agent_version: str = "1.0"
    review_id: Optional[str] = None
    project_name: Optional[str] = None
    gate_status: str = Field(..., examples=["Green", "Amber", "Red"])
    risk_score: float = Field(..., ge=0, le=100)
    total_findings: int
    critical_count: int
    high_count: int
    summary: str
    findings: List[Dict[str, Any]]
    recommendations: List[str]
    compliance_flags: List[str]
    post_back_status: str = Field("not_requested", examples=["not_requested", "posted", "failed"])
    source_type: Optional[str] = Field(None, examples=["github_pr", "jira", "text"])
    source_ref: Optional[str] = None
    created_at: str


class ErrorResponse(BaseModel):
    success: bool = False
    agent: Optional[str] = None
    error: str
    detail: Optional[str] = None
