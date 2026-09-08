from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
from enum import Enum
from datetime import datetime


class RiskLevel(str, Enum):
    LOW = "Low"
    MINOR = "Minor"
    MODERATE = "Moderate"
    HIGH = "High"
    CRITICAL = "Critical"


class GateStatus(str, Enum):
    GREEN = "Green"
    AMBER = "Amber"
    RED = "Red"


class ReviewType(str, Enum):
    REQUIREMENT = "requirement"
    CODE = "code"
    PROMPT = "prompt"
    COMPLIANCE = "compliance"
    RELEASE = "release"
    FULL = "full"
    VAPT = "vapt"
    SECURITY = "security"
    ARCHITECTURE = "architecture"


class Finding(BaseModel):
    control_id: str = Field(description="Control ID e.g. SEC-001, AI-003, GOV-005")
    severity: int = Field(ge=1, le=5, description="1=Low, 2=Minor, 3=Moderate, 4=High, 5=Critical")
    risk_level: RiskLevel
    title: str
    description: str
    recommendation: str
    is_blocking: bool
    owasp_ref: Optional[str] = None
    nist_ref: Optional[str] = None


class AgentReviewResult(BaseModel):
    review_type: ReviewType
    agent_name: str
    findings: List[Finding]
    risk_score: float = Field(ge=0, le=100, description="0-100 composite risk score")
    gate_status: GateStatus
    compliance_flags: List[str]
    summary: str
    recommendations: List[str]


class DerivedFromMixin(BaseModel):
    """Declares `derived_from` on the request models the browser posts to.

    The field is **not read by any handler**, which is deliberate and worth knowing before
    you go looking for the reader. `save_review` is called from nineteen places that know
    nothing about where their artifact came from, so the ids travel by contextvar the same
    way `project_id` and the actor do (`backend/context.py`) — and the one place on this
    path that sees the request body before the handler is `legacy_guard`, which already
    reads it to size the budget reservation. It binds the claim; `database.save_review`
    keeps only the ids that exist and belong to the same project, then writes them.

    Declared here anyway, rather than left as an undocumented extra field Pydantic happens
    to ignore, so that it appears in `/docs` and so a field that changes what gets stored
    is part of the published contract rather than a coupling somebody has to discover.
    """

    derived_from: Optional[List[str]] = Field(
        None,
        description="Ids of earlier reviews this run was built from — recorded on the "
                    "stored review as provenance and shown when the run is reopened. Ids "
                    "that do not exist, or belong to another project, are dropped.",
    )


class ReviewRequest(DerivedFromMixin):
    content: str
    context: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


class CodeReviewRequest(BaseModel):
    code: str
    language: Optional[str] = None
    pr_url: Optional[str] = None
    jira_ticket: Optional[str] = None
    context: Optional[str] = None


class PromptReviewRequest(BaseModel):
    system_prompt: Optional[str] = None
    user_prompt: Optional[str] = None
    tool_definitions: Optional[str] = None
    rag_config: Optional[str] = None
    context: Optional[str] = None


class FullReviewRequest(DerivedFromMixin):
    requirement: Optional[str] = None
    compliance_artifact: Optional[str] = None
    architecture: Optional[str] = None
    code: Optional[str] = None
    prompt: Optional[str] = None
    test_evidence: Optional[str] = None
    context: Optional[str] = None
    project_name: Optional[str] = "Unnamed Project"
    pr_url: Optional[str] = None
    jira_ticket: Optional[str] = None
    # Opt-in stages over artifacts already supplied above — STRIDE reads `architecture`,
    # VAPT and the sensitive-data scan read `code`. Default off so an existing caller's
    # run, and its cost, are unchanged. See backend/workflows/full_review.py.
    include_security: bool = False
    include_vapt: bool = False
    include_sensitive_data: bool = False


class ReleaseGateRequest(DerivedFromMixin):
    review_ids: Optional[List[str]] = None
    findings: Optional[List[Finding]] = None
    project_name: Optional[str] = None
    release_version: Optional[str] = None


class ReviewRecord(BaseModel):
    id: str
    review_type: ReviewType
    project_name: Optional[str]
    input_summary: str
    findings_count: int
    critical_count: int
    high_count: int
    risk_score: float
    gate_status: GateStatus
    created_at: str
    result: Optional[AgentReviewResult] = None


class DashboardStats(BaseModel):
    total_reviews: int
    open_critical: int
    open_high: int
    green_gates: int
    amber_gates: int
    red_gates: int
    recent_reviews: List[ReviewRecord]
    compliance_coverage: Dict[str, int]


class GithubPRRequest(BaseModel):
    pr_url: str
    jira_ticket: Optional[str] = None
    include_prompt_review: bool = False
    # The screen has offered these three since the post-back options landed, and the route
    # silently dropped them -- so "Do not post" still posted, and the merge-blocking check
    # was always on regardless of the checkbox. Defaults match the previous behaviour
    # exactly, so an existing caller that sends none of them is unaffected.
    post_mode: str = Field(
        "summary",
        description="Where findings go: 'summary' (one comment, edited in place on a "
                    "re-run), 'inline' (positioned on the diff lines, with the rest in the "
                    "summary), or 'none' (review it and return the comment without writing "
                    "anything to GitHub).")
    set_status: bool = Field(
        True,
        description="Report the gate as a commit status, so a Red verdict blocks the merge "
                    "button under branch protection. Needs `repo:status` on the token.")
    request_changes: bool = Field(
        False,
        description="Also submit the review as REQUEST_CHANGES (inline mode only). Off by "
                    "default: on many repositories that blocks the author until a human "
                    "dismisses it, which is the customer's policy call.")


class GithubAutoFixRequest(BaseModel):
    pr_url: str
    jira_ticket: Optional[str] = None
    max_files: int = 5


class GovernanceReport(BaseModel):
    report_id: str
    project_name: str
    review_date: str
    overall_risk_score: float
    gate_status: GateStatus
    executive_summary: str
    findings_by_category: Dict[str, List[Finding]]
    compliance_mapping: Dict[str, List[str]]
    controls_checked: List[str]
    controls_passed: List[str]
    controls_failed: List[str]
    recommendations: List[str]
    approval_required: bool
    approvers_needed: List[str]
    audit_trail: List[Dict[str, Any]]


# --- VAPT Schemas ---

class VAPTFinding(BaseModel):
    vuln_id: str
    control_id: str
    severity: int = Field(ge=1, le=5)
    risk_level: RiskLevel
    cvss_score: float = Field(ge=0.0, le=10.0)
    cvss_vector: Optional[str] = None
    owasp_category: Optional[str] = None
    cwe_id: Optional[str] = None
    title: str
    description: str
    affected_component: str
    attack_scenario: str
    proof_of_concept: Optional[str] = None
    recommendation: str
    is_blocking: bool


class VAPTReviewRequest(BaseModel):
    artifact: str = Field(description="Code, API spec, architecture description, or URL list to assess")
    scope: Optional[str] = None
    context: Optional[str] = None
    project_name: Optional[str] = "Unnamed Project"


class VAPTReviewResult(BaseModel):
    agent_name: str
    findings: List[VAPTFinding]
    attack_surface_summary: str
    risk_score: float
    gate_status: GateStatus
    compliance_flags: List[str]
    summary: str
    recommendations: List[str]


# --- Security Review Schemas ---

class STRIDEThreat(BaseModel):
    threat_type: str
    component: str
    threat_description: str
    mitigation: str
    risk_level: RiskLevel


class TrustBoundary(BaseModel):
    boundary: str
    from_zone: str
    to_zone: str
    data_crossing: str
    controls_present: List[str]
    gaps: List[str]


class SecurityReviewRequest(DerivedFromMixin):
    artifact: str = Field(description="Architecture diagram description, system design doc, or component description")
    system_type: Optional[str] = None
    context: Optional[str] = None
    project_name: Optional[str] = "Unnamed Project"


class SecurityReviewResult(BaseModel):
    agent_name: str
    findings: List[Finding]
    stride_threats: List[STRIDEThreat]
    trust_boundaries: List[TrustBoundary]
    attack_vectors: List[str]
    risk_score: float
    gate_status: GateStatus
    compliance_flags: List[str]
    summary: str
    recommendations: List[str]


# --- Architecture Review Schemas ---

class ArchitectureQualityScores(BaseModel):
    scalability:     int = Field(ge=1, le=10)
    resilience:      int = Field(ge=1, le=10)
    maintainability: int = Field(ge=1, le=10)
    security:        int = Field(ge=1, le=10)
    performance:     int = Field(ge=1, le=10)
    observability:   int = Field(ge=1, le=10)
    deployability:   int = Field(ge=1, le=10)
    cost_efficiency: int = Field(ge=1, le=10)


class ArchitectureAntiPattern(BaseModel):
    name:           str
    severity:       str
    description:    str
    impact:         str
    recommendation: str


class ArchitectureSPOF(BaseModel):
    component:        str
    failure_scenario: str
    mitigation:       str


class ArchitectureAlternative(BaseModel):
    pattern:              str
    fit_score:            int = Field(ge=1, le=10)
    justification:        str
    advantages:           List[str]
    disadvantages:        List[str]
    migration_complexity: str
    when_to_choose:       Optional[str] = None


class ArchitectureReviewRequest(DerivedFromMixin):
    artifact: str = Field(description="Architecture description, system design doc, or diagram description")
    requirements: Optional[str] = Field(None, description="Functional and non-functional requirements")
    review_mode: Optional[str] = Field("review", description="review | recommend | compare")
    context: Optional[str] = None
    project_name: Optional[str] = "Unnamed Project"


class ArchitectureReviewResult(BaseModel):
    agent_name:                  str
    architecture_type:           str
    architecture_description:    Optional[str] = None
    quality_scores:              ArchitectureQualityScores
    anti_patterns:               List[ArchitectureAntiPattern]
    single_points_of_failure:    List[ArchitectureSPOF]
    scalability_bottlenecks:     List[str]
    alternative_recommendations: List[ArchitectureAlternative]
    findings:                    List[Finding]
    risk_score:                  float
    gate_status:                 GateStatus
    compliance_flags:            List[str]
    summary:                     str
    recommendations:             List[str]


# ── Source-capable request bodies for the UI surface ──────────────────────
#
# New models rather than extra fields on `CodeReviewRequest` / `VAPTReviewRequest`. Those
# two are what every existing screen and integration posts to, `code`/`artifact` is required
# on them, and 380-odd offline checks assert their behaviour — widening them would put all
# of that at risk to save two classes. These carry the full source set for the new
# `/source`, `/upload-zip` and `/preflight` routes.


class SourceFile(BaseModel):
    path: str = Field(description="Repo-relative path, e.g. src/api/auth.py")
    content: str = Field(description="The file's full text")


class _UISourceBase(BaseModel):
    project_name: Optional[str] = ""
    context: Optional[str] = None
    files: Optional[List[SourceFile]] = Field(
        None, description="Explicit file list — highest precedence source")
    github_pr_url: Optional[str] = Field(None, description="Review a pull request diff")
    github_repo_url: Optional[str] = Field(
        None, description="Review a repository's source at `ref`, or a `base_ref...head_ref` diff")
    ref: Optional[str] = Field(None, description="Branch, tag or commit for github_repo_url")
    base_ref: Optional[str] = Field(None, description="With head_ref: review a branch comparison")
    head_ref: Optional[str] = Field(None, description="The branch being proposed")
    github_file_url: Optional[str] = Field(None, description="Review a single file")
    github_token: Optional[str] = Field(
        None, description="A token for this call only — never stored. Overrides the saved "
                          "credential and the deployment default.")
    retention_days: Optional[int] = Field(
        None,
        description="How long to keep the reviewed source so files can be opened and fixes "
                    "applied afterwards: 0 (keep nothing), 1, 3, 7, 14 or 30 days. Defaults "
                    "to 7. Ignored for GitHub sources, which are re-read from the repository "
                    "at the commit the review recorded rather than copied. The list of which "
                    "files were read is always kept, whatever this is set to.",
        examples=[7, 0],
    )


class CodeReviewSourceRequest(_UISourceBase):
    code: Optional[str] = Field(None, description="Pasted code or a unified diff")
    language: Optional[str] = None


class VAPTSourceRequest(_UISourceBase):
    artifact: Optional[str] = Field(
        None, description="Pasted code, API spec, architecture description, or URL list")
    scope: Optional[str] = None
