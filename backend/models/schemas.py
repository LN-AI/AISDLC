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


class ReviewRequest(BaseModel):
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


class FullReviewRequest(BaseModel):
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


class ReleaseGateRequest(BaseModel):
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


class SecurityReviewRequest(BaseModel):
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


class ArchitectureReviewRequest(BaseModel):
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
