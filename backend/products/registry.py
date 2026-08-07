"""The product registry — one catalog of every agent and workflow this platform exposes.

Why this exists
---------------
Before this module there were four independent, hand-synced lists of agent names:

  * `v1_agents.AGENT_CATALOG`            — 25 entries, documentation + `_wrap()` titles
  * `api_key_service.VALID_AGENT_NAMES`  — 25 names, "kept in sync with AGENT_CATALOG by hand"
  * `routes/agents.py VALID_AGENTS`      — only 8 names
  * `swarm_service._run_one_agent`       — an 8-branch if/elif with per-agent call shapes

...plus an implicit fifth: the `reviews.review_type` strings each orchestrator method
writes, which use underscores where the API names use hyphens. Nothing validated any of
them against anything else, which is how `api_keys.scopes` ended up permanently set to
"all" — there was no authoritative list to check a scope value against.

This module is that authoritative list. Entitlement, metering, quota and the
`/api/v1/products` surface all resolve through it.

Product IDs
-----------
An ID is exactly the string already used on the wire and already persisted in
`api_keys.scopes`, `api_key_cost_log.agent_name` and `api_key_agent_llm_config.agent_name`
(which has a UNIQUE constraint on `(key_id, agent_name)`). Reusing them means existing
keys, per-agent BYOL configs and cost history keep resolving with no data migration.

Note the namespace is deliberately flat: composites like `full` and `auto-fix` have always
lived in the same namespace as single agents like `code`, so `kind` — not a name prefix —
is the discriminator between the two. Only products that never had a public name at all
(`swarm`, `github-pr-review`) get a new ID here.

Invocation
----------
Agent entrypoints cannot be called uniformly — there are 11 distinct verb names, 9
different names for "the thing to review", arities from 2 to 12, and three input shapes
(a string, a list of file dicts, or both). So `Product.invoke` is a per-product *adapter*
that maps a flat params dict onto the real call, rather than a bare method reference.

Every adapter goes through `SDLCOrchestrator` rather than touching the agent class
directly. That is load-bearing: 14 of the 20 agents have no KB lookup of their own and
depend on `SDLCOrchestrator._kb()` injecting it for them, and the orchestrator is also
where Headroom compression and `reviews` persistence happen.
"""
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

AGENT = "agent"
WORKFLOW = "workflow"

# A params key spec: either one key, or a tuple of candidates (first non-empty wins).
ParamRef = Union[str, Tuple[str, ...]]


@dataclass(frozen=True)
class Product:
    """One sellable/addressable unit: a single agent or a multi-agent workflow."""

    id: str
    kind: str                      # AGENT | WORKFLOW
    title: str
    module: str                    # the backend/agents/<module>/ it belongs to, "" for composites
    description: str
    # None only for products whose own service owns dispatch (currently `swarm`).
    invoke: Optional[Callable[[Any, Dict[str, Any]], Dict[str, Any]]]
    review_type: str = ""          # the reviews.review_type row(s) this writes, "" if none
    endpoint: str = ""             # canonical v1 endpoint, for docs
    sources: Tuple[str, ...] = ()
    frameworks: Tuple[str, ...] = ()
    swarm_capable: bool = False    # can be fanned out by SwarmService

    @property
    def is_workflow(self) -> bool:
        return self.kind == WORKFLOW


# ── Adapter construction ───────────────────────────────────────────────────

def _pick(params: Dict[str, Any], ref: ParamRef) -> Any:
    """Resolve a params key spec. A tuple means "first candidate that has a value"."""
    if isinstance(ref, tuple):
        for key in ref:
            value = params.get(key)
            if value:
                return value
        return params.get(ref[-1])
    return params.get(ref)


def _adapter(
    method: str,
    args: Optional[Dict[str, ParamRef]] = None,
    const: Optional[Dict[str, Any]] = None,
) -> Callable[[Any, Dict[str, Any]], Dict[str, Any]]:
    """Build `invoke(orch, params)` calling `orch.<method>(**kwargs)`.

    `args` maps an orchestrator kwarg name -> the params key it reads from.
    `const` supplies fixed kwargs. Kwargs whose params value is None are dropped so the
    orchestrator's own defaults apply (several of them default project_name to
    "Unnamed Project", which we must not stomp with an explicit None).
    """
    args = args or {}
    const = const or {}

    def _invoke(orch: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        kwargs = dict(const)
        for kwarg, ref in args.items():
            value = _pick(params, ref)
            if value is not None:
                kwargs[kwarg] = value
        return getattr(orch, method)(**kwargs)

    _invoke.__name__ = f"invoke_{method}"
    return _invoke


# Most agents take "the artifact" plus context/project. The params keys are normalised so
# one caller (the swarm, /products, a future generic dispatcher) can drive any product
# without knowing its individual signature.
_ARTIFACT = ("artifact", "content", "code", "text")
_CTX = "context"
_PROJECT = "project_name"


PRODUCTS: List[Product] = [
    # ── Requirement Intelligence ───────────────────────────────────────────
    Product(
        id="requirement",
        kind=AGENT,
        title="Requirement Risk Reviewer",
        module="requirement_intelligence",
        description="Reviews user stories, BRDs, and acceptance criteria for risk, ambiguity, and compliance gaps.",
        endpoint="POST /api/v1/agents/requirement",
        sources=("text", "jira_ticket", "github_issue_url"),
        frameworks=("NIST-AI-RMF", "OWASP-LLM", "HIPAA", "GDPR"),
        review_type="requirement",
        swarm_capable=True,
        invoke=_adapter("review_requirement", {"content": _ARTIFACT, "context": _CTX, "project_name": _PROJECT}),
    ),

    # ── Developer Intelligence ─────────────────────────────────────────────
    Product(
        id="code",
        kind=AGENT,
        title="Code & PR Security Reviewer",
        module="developer_intelligence",
        description="Security and quality review of code, git diffs, and pull requests (OWASP, CWE, injection, secrets).",
        endpoint="POST /api/v1/agents/code",
        sources=("code", "github_pr_url", "github_file_url"),
        frameworks=("OWASP-Web-Top10", "OWASP-LLM", "CWE", "NIST-AI-RMF"),
        review_type="code",
        swarm_capable=True,
        invoke=_adapter("review_code", {
            "code": _ARTIFACT, "language": "language", "context": _CTX,
            "project_name": _PROJECT, "pr_url": "pr_url",
        }),
    ),
    Product(
        id="codegen-advisory",
        kind=AGENT,
        title="Code Generation Advisor",
        module="developer_intelligence",
        description="Recommends language, backend/frontend framework, database, architecture pattern, and design patterns for a new system given its requirement. Any field can be pinned to force that choice.",
        endpoint="POST /api/v1/agents/codegen-advisory",
        sources=("requirement",),
        frameworks=("NIST-AI-RMF",),
        review_type="codegen_advisory",
        invoke=_adapter("generate_codegen_advisory", {
            "requirement": ("requirement",) + _ARTIFACT, "constraints": "constraints",
            "pins": "pins", "context": _CTX, "project_name": _PROJECT,
        }),
    ),
    Product(
        id="ai-provenance",
        kind=AGENT,
        title="AI Code Provenance & Risk",
        module="developer_intelligence",
        description="Assesses the AI-origin likelihood of a code artifact as probabilistic evidence (Low/Possible/Likely/Strong evidence), never a verdict — combines an optional developer disclosure with LLM-based style-signal analysis into a scored, evidence-backed report.",
        endpoint="POST /api/v1/agents/ai-provenance",
        sources=("code", "github_pr_url", "github_file_url"),
        frameworks=("NIST-AI-RMF",),
        review_type="ai_provenance",
        invoke=_adapter("analyze_ai_provenance", {
            "artifact": _ARTIFACT, "disclosure": "disclosure", "context": _CTX, "project_name": _PROJECT,
        }),
    ),
    Product(
        id="migration-assessment",
        kind=AGENT,
        title="Migration Assessment",
        module="developer_intelligence",
        description="Assesses migration complexity, breaking changes, risk areas, and a phased plan for moving code from a source stack to a target stack (language upgrade, framework swap, database migration, monolith-to-microservices, cloud migration).",
        endpoint="POST /api/v1/agents/migration-assessment",
        sources=("code", "github_pr_url", "github_file_url"),
        frameworks=("NIST-AI-RMF",),
        review_type="migration_assessment",
        invoke=_adapter("assess_migration", {
            "source": ("source",) + _ARTIFACT, "migration_target": "migration_target",
            "constraints": "constraints", "context": _CTX, "project_name": _PROJECT,
        }),
    ),

    # ── Security Intelligence ──────────────────────────────────────────────
    Product(
        id="prompt",
        kind=AGENT,
        title="Prompt & Agent Safety Reviewer",
        module="security_intelligence",
        description="Reviews AI system prompts, tool definitions, and RAG configs for injection and safety risks.",
        endpoint="POST /api/v1/agents/prompt",
        sources=("system_prompt", "github_file_url"),
        frameworks=("OWASP-LLM", "NIST-AI-RMF"),
        review_type="prompt",
        swarm_capable=True,
        invoke=_adapter("review_prompt", {
            "system_prompt": ("system_prompt",) + _ARTIFACT, "user_prompt": "user_prompt",
            "tool_definitions": "tool_definitions", "rag_config": "rag_config",
            "context": _CTX, "project_name": _PROJECT,
        }),
    ),
    Product(
        id="compliance",
        kind=AGENT,
        title="Compliance Mapper",
        module="security_intelligence",
        description="Maps any artifact to NIST AI-RMF, OWASP LLM, HIPAA, GDPR, ISO 27001, GxP, PCI DSS controls.",
        endpoint="POST /api/v1/agents/compliance",
        sources=("text", "jira_ticket"),
        frameworks=("NIST-AI-RMF", "OWASP-LLM", "HIPAA", "GDPR", "ISO27001", "GxP", "PCI-DSS"),
        review_type="compliance",
        swarm_capable=True,
        invoke=_adapter("review_compliance", {
            "content": _ARTIFACT, "frameworks": "frameworks", "context": _CTX, "project_name": _PROJECT,
        }),
    ),
    Product(
        id="vapt",
        kind=AGENT,
        title="VAPT — Vulnerability Assessment",
        module="security_intelligence",
        description="Full vulnerability assessment with CVSS v3.1 scoring, OWASP API Top 10, and PoC attack scenarios.",
        endpoint="POST /api/v1/agents/vapt",
        sources=("code", "github_pr_url", "github_file_url", "github_repo_url"),
        frameworks=("OWASP-Web-Top10", "OWASP-API-Top10", "CWE", "CVSS-3.1"),
        review_type="vapt",
        swarm_capable=True,
        invoke=_adapter("review_vapt", {
            "artifact": _ARTIFACT, "scope": "scope", "context": _CTX, "project_name": _PROJECT,
        }),
    ),
    Product(
        id="security",
        kind=AGENT,
        title="Security Architecture Review",
        module="security_intelligence",
        description="STRIDE threat modelling, trust boundary analysis, and attack surface assessment.",
        endpoint="POST /api/v1/agents/security",
        sources=("artifact", "github_repo_url", "github_file_url", "jira_ticket"),
        frameworks=("STRIDE", "NIST-AI-RMF", "HIPAA", "GDPR", "SOC2"),
        review_type="security",
        swarm_capable=True,
        invoke=_adapter("review_security", {
            "artifact": _ARTIFACT, "system_type": "system_type", "context": _CTX, "project_name": _PROJECT,
        }),
    ),
    Product(
        id="sensitive-data",
        kind=AGENT,
        title="Sensitive Data Detector",
        module="security_intelligence",
        description="Scans code, configs, schemas, and logs for PII, PHI, PCI data, credentials, and location data.",
        endpoint="POST /api/v1/agents/sensitive-data",
        sources=("content", "github_file_url"),
        frameworks=("HIPAA", "GDPR", "PCI-DSS", "NIST-AI-RMF"),
        review_type="sensitive_data",
        swarm_capable=True,
        invoke=_adapter("scan_sensitive_data", {
            "content": _ARTIFACT, "context": _CTX, "project_name": _PROJECT,
        }),
    ),
    Product(
        id="cfr21-verification",
        kind=AGENT,
        title="21 CFR Part 11 Verification",
        module="security_intelligence",
        description="Verifies a codebase (multiple files, or a single snippet/PR/file) against FDA 21 CFR Part 11 — audit trail (§11.10(e)), electronic signatures (§11.50/70/100/200), access control (§11.10(d)/(g)/§11.300), and data integrity (ALCOA+). Returns exhaustive, evidence-cited findings plus a validation-readiness note for process-only controls.",
        endpoint="POST /api/v1/agents/cfr21-verification",
        sources=("files", "code", "github_pr_url", "github_file_url"),
        frameworks=("21CFR11", "GxP"),
        review_type="cfr21_verification",
        invoke=_adapter("verify_cfr21", {
            "files": "files", "code": ("code", "artifact", "content"),
            "system_description": "system_description", "context": _CTX, "project_name": _PROJECT,
        }),
    ),
    Product(
        id="gxp-document-check",
        kind=AGENT,
        title="GxP Document Check",
        module="security_intelligence",
        description="Checks GxP documents (SOPs, validation protocols IQ/OQ/PQ, batch records, deviations, CAPAs, change controls, risk assessments, URS/specifications, training records) for completeness against a type-specific required-elements checklist, signature/approval compliance (21 CFR 11.50), version control, and cross-document traceability. Accepts multiple related documents checked as a set.",
        endpoint="POST /api/v1/agents/gxp-document-check",
        sources=("documents", "content"),
        frameworks=("21CFR11", "GxP"),
        review_type="gxp_document_check",
        invoke=_adapter("check_gxp_documents", {
            "documents": "documents", "content": ("content", "artifact", "text"),
            "document_type_hint": "document_type_hint", "context": _CTX, "project_name": _PROJECT,
        }),
    ),
    Product(
        id="gxp-generate-plan",
        kind=AGENT,
        title="GxP Validation Plan",
        module="security_intelligence",
        description="Points at an application (files/code/PR) and plans its GxP validation document package — GAMP 5 risk classification, which documents apply (URS, FRS, Risk Assessment, Validation Plan, IQ/OQ/PQ Protocols, Traceability Matrix, Validation Summary Report, SOP), and an application-specific outline for each, in generation order.",
        endpoint="POST /api/v1/agents/gxp-generate-plan",
        sources=("files", "code", "github_pr_url", "github_file_url"),
        frameworks=("21CFR11", "GxP"),
        review_type="gxp_validation_plan",
        invoke=_adapter("plan_gxp_validation_package", {
            "files": "files", "code": ("code", "artifact", "content"),
            "system_description": "system_description", "context": _CTX, "project_name": _PROJECT,
        }),
    ),
    Product(
        id="gxp-generate-document",
        kind=AGENT,
        title="GxP Document Generation",
        module="security_intelligence",
        description="Generates one complete GxP validation document (URS, FRS, Risk Assessment, Validation Plan, IQ/OQ/PQ Protocol, Traceability Matrix, Validation Summary Report, or SOP) grounded in the actual application given. Call once per document type, typically driven by the plan from gxp-generate-plan.",
        endpoint="POST /api/v1/agents/gxp-generate-document",
        sources=("files", "code", "github_pr_url", "github_file_url"),
        frameworks=("21CFR11", "GxP"),
        review_type="gxp_document_generated",
        invoke=_adapter("generate_gxp_document", {
            "document_type": "document_type", "files": "files",
            "code": ("code", "artifact", "content"), "system_description": "system_description",
            "outline": "outline", "plan_context": "plan_context", "context": _CTX, "project_name": _PROJECT,
        }),
    ),

    # ── Architecture Intelligence ──────────────────────────────────────────
    Product(
        id="architecture",
        kind=AGENT,
        title="Architecture Review",
        module="architecture_intelligence",
        description="Quality dimension scoring (8 axes), anti-pattern detection, SPOF analysis, and architectural alternatives.",
        endpoint="POST /api/v1/agents/architecture",
        sources=("artifact", "github_repo_url", "github_file_url", "jira_ticket"),
        frameworks=("NIST-AI-RMF", "SOC2"),
        review_type="architecture",
        swarm_capable=True,
        invoke=_adapter(
            "review_architecture",
            {
                # The swarm prefers a dedicated `architecture` field and falls back to the
                # generic artifact — preserved here so swarm behaviour is unchanged.
                "artifact": ("architecture",) + _ARTIFACT,
                "requirements": "requirements",
                "context": _CTX,
                "project_name": _PROJECT,
            },
            const={"review_mode": "review"},
        ),
    ),

    # ── QA Intelligence ────────────────────────────────────────────────────
    Product(
        id="test-scenario",
        kind=AGENT,
        title="Test Scenario Generation",
        module="qa_intelligence",
        description="Converts a requirement/user story into a business-level test scenario catalogue — happy path, negative, boundary value, role-based, integration, and exception handling — each traced back to the requirement text it covers.",
        endpoint="POST /api/v1/agents/test-scenario",
        sources=("text", "jira_ticket", "github_issue_url"),
        review_type="test_scenarios",
        invoke=_adapter("generate_test_scenarios", {
            "requirement": ("requirement",) + _ARTIFACT, "context": _CTX, "project_name": _PROJECT,
        }),
    ),
    Product(
        id="test-case",
        kind=AGENT,
        title="Test Case Generation",
        module="qa_intelligence",
        description="Generates detailed, executable manual test cases — step-by-step actions, expected results, concrete test data — from a requirement, or from a prior Test Scenario catalogue via `scenarios_context`.",
        endpoint="POST /api/v1/agents/test-case",
        sources=("text", "jira_ticket", "github_issue_url", "scenarios_context"),
        review_type="test_cases",
        invoke=_adapter("generate_test_cases", {
            "requirement": ("requirement",) + _ARTIFACT, "scenarios_context": "scenarios_context",
            "context": _CTX, "project_name": _PROJECT,
        }),
    ),

    # ── Workflows (multi-agent / multi-stage composites) ───────────────────
    Product(
        id="full",
        kind=WORKFLOW,
        title="Full SDLC Review",
        module="",
        description="Runs requirement, code, compliance (via `compliance_artifact`), architecture (via `architecture`), and release gate agents in one call. Returns a consolidated gate decision.",
        endpoint="POST /api/v1/agents/full",
        sources=("text", "github_pr_url", "jira_ticket", "compliance_artifact", "architecture"),
        frameworks=("NIST-AI-RMF", "OWASP-LLM", "HIPAA", "GDPR", "ISO27001"),
        review_type="full",
        invoke=_adapter("full_review", {
            "requirement": "requirement", "compliance_artifact": "compliance_artifact",
            "architecture": "architecture", "code": "code", "prompt_config": "prompt_config",
            "test_evidence": "test_evidence", "project_name": _PROJECT, "context": _CTX,
            "on_progress": "on_progress",
        }),
    ),
    Product(
        id="release-gate",
        kind=WORKFLOW,
        title="Release Gate",
        module="governance_intelligence",
        description="Evaluates release readiness: aggregates prior findings, checks mandatory controls, issues Red/Amber/Green gate decision.",
        endpoint="POST /api/v1/agents/release-gate",
        sources=("review_ids", "evidence_summary"),
        frameworks=("NIST-AI-RMF", "SOC2", "HIPAA"),
        review_type="release_gate",
        invoke=_adapter("run_release_gate", {
            "project_name": _PROJECT, "review_ids": "review_ids",
            "release_version": "release_version", "evidence_summary": "evidence_summary",
        }),
    ),
    Product(
        id="report-generate",
        kind=WORKFLOW,
        title="Governance Report Generator",
        module="governance_intelligence",
        description="Synthesizes prior review findings (by review_ids or supplied directly) into a comprehensive, audit-ready governance report — executive summary, findings by category, compliance coverage matrix, controls assessment, and release decision — without re-running a full review.",
        endpoint="POST /api/v1/agents/report-generate",
        sources=("review_ids", "findings"),
        frameworks=("NIST-AI-RMF", "OWASP-LLM", "HIPAA", "GDPR", "ISO27001", "GxP"),
        review_type="",  # writes a `reports` row, not a `reviews` row
        invoke=_adapter("generate_standalone_report", {
            "project_name": _PROJECT, "review_ids": "review_ids", "findings": "findings",
            "gate_status": "gate_status", "overall_risk_score": "overall_risk_score", "context": _CTX,
        }),
    ),
    Product(
        id="codegen-scaffold",
        kind=WORKFLOW,
        title="Code Generation Scaffold",
        module="developer_intelligence",
        description="Generates a minimal, runnable starter project (8-16 files) for a chosen language/framework/pattern. Optionally auto-runs Code Review and Sensitive Data agents against the output before returning it.",
        endpoint="POST /api/v1/agents/codegen-scaffold",
        sources=("project_name", "language", "backend_framework"),
        frameworks=("NIST-AI-RMF", "OWASP-LLM"),
        review_type="codegen_scaffold",
        invoke=_adapter("generate_codegen_scaffold", {
            "project_name": _PROJECT, "language": "language",
            "backend_framework": "backend_framework", "frontend_framework": "frontend_framework",
            "database_choice": "database_choice", "architecture_pattern": "architecture_pattern",
            "requirement": "requirement", "include_auth": "include_auth",
            "include_docker": "include_docker", "include_tests": "include_tests",
            "auto_review": "auto_review", "context": _CTX,
        }),
    ),
    Product(
        id="migration-execute",
        kind=WORKFLOW,
        title="Migration Execution",
        module="developer_intelligence",
        description="Transforms a specific file/artifact from its current stack to a target stack, preserving behavior. Lists every change and everything needing manual follow-up. Optionally auto-runs Code Review and Sensitive Data agents against the migrated output.",
        endpoint="POST /api/v1/agents/migration-execute",
        sources=("code", "github_pr_url", "github_file_url"),
        frameworks=("NIST-AI-RMF", "OWASP-LLM"),
        review_type="migration_execution",
        invoke=_adapter("execute_migration", {
            "source": ("source",) + _ARTIFACT, "migration_target": "migration_target",
            "file_path": "file_path", "requirement": "requirement", "auto_review": "auto_review",
            "context": _CTX, "project_name": _PROJECT,
        }),
    ),
    Product(
        id="issue-triage",
        kind=WORKFLOW,
        title="Issue Triaging Agent",
        module="operations_intelligence",
        description="Classifies, prioritizes, and recommends assignment for a defect/incident/support ticket/security finding/test failure. Detects duplicates against this project's own triage history, enriches incomplete tickets without inventing facts, and flags mandatory human review for Critical/P0/P1/security cases per a confidence-based operating model.",
        endpoint="POST /api/v1/agents/issue-triage",
        sources=("title", "description", "github_issue_url"),
        frameworks=("NIST-AI-RMF",),
        review_type="issue_triage",
        invoke=_adapter("triage_issue", {
            "title": "title", "description": "description", "logs": "logs",
            "stack_trace": "stack_trace", "error_code": "error_code", "environment": "environment",
            "application": "application", "module": "module", "reporter": "reporter",
            "build_version": "build_version", "test_case_ref": "test_case_ref",
            "customer_impact_notes": "customer_impact_notes", "team_roster": "team_roster",
            "context": _CTX, "project_name": _PROJECT, "llm_override": "llm_override",
        }),
    ),
    Product(
        id="issue-triage-batch-github",
        kind=WORKFLOW,
        title="Issue Triaging — GitHub Batch",
        module="operations_intelligence",
        description="Pulls open issues from a GitHub repo (most recently updated first) and triages each one, returning per-issue reports plus a severity/escalation summary.",
        endpoint="POST /api/v1/agents/issue-triage/batch-github",
        sources=("github_repo_url",),
        frameworks=("NIST-AI-RMF",),
        review_type="issue_triage",
        invoke=_adapter("triage_github_repo_issues", {
            "github_repo_url": "github_repo_url", "limit": "limit", "team_roster": "team_roster",
            "context": _CTX, "project_name": _PROJECT, "llm_override": "llm_override",
        }),
    ),
    Product(
        id="github-pr-review",
        kind=WORKFLOW,
        title="GitHub PR Review & Comment",
        module="developer_intelligence",
        description="Fetches a GitHub PR diff, runs the Code Review agent against it under prompt-injection guardrails, and posts the result back as a PR comment.",
        endpoint="POST /api/github/pr",
        sources=("pr_url",),
        frameworks=("OWASP-Web-Top10", "CWE"),
        review_type="code",
        invoke=_adapter("review_github_pr", {"pr_url": "pr_url", "jira_ticket": "jira_ticket"}),
    ),
    Product(
        id="auto-fix",
        kind=WORKFLOW,
        title="Auto-Fix & Open PR",
        module="developer_intelligence",
        description="Reviews a GitHub PR, generates fixes for its findings, commits them to a new branch off the PR's head branch, and opens a follow-up PR back into it. Writes to a real repository — requires a GITHUB_TOKEN with write (repo) scope. Refuses to commit any file where its own secret scanner detects a newly-introduced hardcoded credential.",
        endpoint="POST /api/v1/agents/auto-fix",
        sources=("pr_url",),
        frameworks=("OWASP-Web-Top10", "CWE"),
        review_type="code",
        invoke=_adapter("auto_fix_github_pr", {
            "pr_url": "pr_url", "jira_ticket": "jira_ticket", "max_files": "max_files",
        }),
    ),
    Product(
        id="modernization",
        kind=WORKFLOW,
        title="Legacy Modernization",
        module="modernization_intelligence",
        description=(
            "Converts a whole legacy codebase to a target stack as a durable, resumable run: "
            "ingests a repo (redacting secrets before any model call), derives a dependency "
            "graph with real parsers, converts file by file in topological order against a "
            "charter the team approves once, and keeps a symbol registry so later files stay "
            "consistent with earlier ones. Survives a restart — a run killed at file 390 "
            "resumes at 391. Metered per converted file."
        ),
        endpoint="POST /api/v1/modernization/runs",
        sources=("zip", "files", "github_repo_url"),
        frameworks=("NIST-AI-RMF",),
        review_type="",  # writes modernization_* rows, not a `reviews` row
        # Dispatch is owned by services/modernization/runner.py, which is a long-running
        # loop over a job rather than one call — the same reason `swarm` has no adapter.
        invoke=None,
    ),
    Product(
        id="swarm",
        kind=WORKFLOW,
        title="Agent Swarm",
        module="",
        description="Fans one artifact out to 3-5 review agents in parallel under a named policy (standard/hipaa/pci/ai-product) and collapses them into a single policy-enforced gate decision. Degrades gracefully — one agent crashing never blocks the gate.",
        endpoint="POST /api/v1/swarm",
        sources=("github_pr_url", "github_file_url", "github_repo_url", "jira_ticket", "text"),
        review_type="swarm",
        # Driven by SwarmService, which owns source resolution, policy and parallelism.
        # Registered here so it is entitleable and meterable like any other product.
        invoke=None,
    ),
]


# ── Lookup ─────────────────────────────────────────────────────────────────

_BY_ID: Dict[str, Product] = {p.id: p for p in PRODUCTS}


def get(product_id: str) -> Optional[Product]:
    return _BY_ID.get(product_id)


def require(product_id: str) -> Product:
    """Like `get`, but raises — use where an unknown ID is a programming error."""
    product = _BY_ID.get(product_id)
    if product is None:
        raise KeyError(f"Unknown product id: {product_id!r}")
    return product


def exists(product_id: str) -> bool:
    return product_id in _BY_ID


def all_ids() -> List[str]:
    return [p.id for p in PRODUCTS]


def list_products(kind: str = "", module: str = "", swarm_capable: Optional[bool] = None) -> List[Product]:
    items = PRODUCTS
    if kind:
        items = [p for p in items if p.kind == kind]
    if module:
        items = [p for p in items if p.module == module]
    if swarm_capable is not None:
        items = [p for p in items if p.swarm_capable == swarm_capable]
    return list(items)


def unknown_ids(candidates: Sequence[str]) -> List[str]:
    """Which of `candidates` are not real products. Used to validate plans and scopes —
    nothing validated `api_keys.scopes` before, which is why "all" became its only value."""
    return [c for c in candidates if c not in _BY_ID]


def to_public_dict(product: Product) -> Dict[str, Any]:
    """Shape served by GET /api/v1/products and GET /api/v1/agents."""
    return {
        "id": product.id,
        "name": product.id,          # back-compat: AGENT_CATALOG called this "name"
        "kind": product.kind,
        "title": product.title,
        "module": product.module,
        "description": product.description,
        "endpoint": product.endpoint,
        "sources": list(product.sources),
        "frameworks": list(product.frameworks),
    }
