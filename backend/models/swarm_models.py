"""Pydantic models for the Governance Swarm Layer.

SwarmRequest   — incoming request to POST /api/v1/swarm
SwarmPolicy    — gate-enforcement rules (required vs optional agents, min score)
AgentSwarmResult — per-agent result stored in SwarmResponse.results
SwarmResponse  — unified gate decision returned to the caller
"""
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

from backend.models.api_models import LLMOverrideFields


# ── Built-in policy names accepted as shortcut strings ────────────────────

BUILTIN_POLICY_NAMES = ("standard", "hipaa", "pci", "ai-product")


# ── Policy ─────────────────────────────────────────────────────────────────

class SwarmPolicy(BaseModel):
    """Defines which agents must pass (blocking) vs are advisory (optional)."""

    required_agents: List[str] = Field(
        ...,
        description="Agents that must produce a non-RED gate; any RED here escalates the overall swarm to BLOCKED.",
        examples=[["code", "vapt", "compliance", "sensitive-data"]],
    )
    optional_agents: List[str] = Field(
        default_factory=list,
        description="Agents that run and contribute to the risk score but do NOT block the gate.",
        examples=[["architecture", "security"]],
    )
    min_passing_score: float = Field(
        7.0,
        ge=0.0,
        le=100.0,
        description=(
            "Minimum average risk score (0–100) below which the swarm gate becomes AMBER. "
            "Scores are inverted internally — a score of 7 means 'medium risk'. "
            "Agents with risk_score > this threshold contribute to an AMBER or RED escalation."
        ),
    )


# ── Request ────────────────────────────────────────────────────────────────

class SwarmRequest(LLMOverrideFields):
    """Single-artifact request that triggers the full Governance Swarm."""

    project_name: str = Field(
        ...,
        description="Project / product name stored in the audit trail.",
        examples=["Patient Portal API v4", "Risk Scoring Engine"],
    )

    # ── Source (exactly one should be supplied) ────────────────────────────
    github_pr_url: Optional[str] = Field(
        None,
        description="GitHub Pull Request URL — triggers: code, vapt, compliance, sensitive-data.",
        examples=["https://github.com/owner/repo/pull/88"],
    )
    github_file_url: Optional[str] = Field(
        None,
        description="GitHub file blob URL — triggers: code, vapt, sensitive-data.",
        examples=["https://github.com/owner/repo/blob/main/src/api/auth.py"],
    )
    github_repo_url: Optional[str] = Field(
        None,
        description="GitHub repository URL — triggers: architecture, security, vapt.",
        examples=["https://github.com/owner/repo"],
    )
    jira_ticket: Optional[str] = Field(
        None,
        description="Jira ticket ID or Atlassian URL — triggers: requirement, compliance.",
        examples=["PROJ-123", "https://company.atlassian.net/browse/PROJ-123"],
    )
    text: Optional[str] = Field(
        None,
        description="Free-text artifact — triggers: requirement, compliance (+ architecture if architecture field also supplied).",
    )

    # ── Optional extras that add agents ────────────────────────────────────
    architecture: Optional[str] = Field(
        None,
        description="Architecture description — adds the architecture agent when supplied.",
    )
    system_prompt: Optional[str] = Field(
        None,
        description="AI system prompt — adds the prompt-safety agent when supplied.",
    )
    requirements: Optional[str] = Field(
        None,
        description="Non-functional requirements injected into the architecture agent.",
    )
    context: Optional[str] = Field(
        None,
        description="Additional free-text context injected into every agent's prompt.",
    )

    # ── Governance options ─────────────────────────────────────────────────
    post_back: bool = Field(
        False,
        description="When true, posts the swarm summary as a comment on the originating GitHub PR/issue or Jira ticket.",
    )
    policy_name: Optional[str] = Field(
        None,
        description=(
            "Shortcut to a built-in policy. One of: standard | hipaa | pci | ai-product. "
            "Ignored when `policy` is also supplied."
        ),
        examples=["hipaa", "pci"],
    )
    policy: Optional[SwarmPolicy] = Field(
        None,
        description="Custom policy object. Takes precedence over `policy_name`. When neither is supplied the DEFAULT_POLICY is used.",
    )


# ── Per-agent result ───────────────────────────────────────────────────────

class AgentSwarmResult(BaseModel):
    """The result produced by one agent within the swarm run."""

    agent: str = Field(..., examples=["code", "vapt", "compliance"])
    gate_status: str = Field(..., examples=["Green", "Amber", "Red"])
    risk_score: float = Field(..., ge=0.0, le=100.0)
    critical_count: int = Field(0, ge=0)
    high_count: int = Field(0, ge=0)
    summary: str = ""
    findings: List[Dict[str, Any]] = Field(default_factory=list)
    compliance_flags: List[str] = Field(default_factory=list)
    review_id: Optional[str] = None
    error: Optional[str] = Field(
        None, description="Set when this agent failed; other agents still ran."
    )
    llm_used: Optional[Dict[str, str]] = Field(
        None, description="Which LLM served this specific agent — provider, model, and source "
                           "(platform | key_default | agent_default | request_override)."
    )


# ── Swarm response ─────────────────────────────────────────────────────────

class SwarmResponse(BaseModel):
    """Unified gate decision returned by POST /api/v1/swarm."""

    success: bool = True
    project_name: str

    # ── Overall verdict ────────────────────────────────────────────────────
    swarm_gate: str = Field(
        ...,
        description=(
            "Overall gate decision. "
            "GREEN = all required agents passed and average risk is below threshold. "
            "AMBER = optional agents flagged issues or risk score near threshold. "
            "RED = one or more required agents returned Red but threshold still met. "
            "BLOCKED = one or more required agents returned Red AND risk is critical."
        ),
        examples=["GREEN", "AMBER", "RED", "BLOCKED"],
    )
    swarm_risk_score: float = Field(
        ..., ge=0.0, le=100.0,
        description="Weighted average risk score across all agents that ran (max×0.5 + mean×0.5).",
    )

    # ── Execution summary ──────────────────────────────────────────────────
    agents_run: List[str] = Field(default_factory=list)
    agents_passed: List[str] = Field(default_factory=list)
    agents_failed: List[str] = Field(
        default_factory=list,
        description="Agents that produced a Red gate status.",
    )
    agents_errored: List[str] = Field(
        default_factory=list,
        description="Agents that raised an exception during execution.",
    )

    # ── Aggregated findings ────────────────────────────────────────────────
    blocking_findings: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Critical and High severity findings aggregated across all agents.",
    )
    total_critical: int = 0
    total_high: int = 0

    # ── Per-agent detail ───────────────────────────────────────────────────
    results: List[AgentSwarmResult] = Field(default_factory=list)

    # ── Meta ───────────────────────────────────────────────────────────────
    policy_applied: SwarmPolicy
    source_type: str = Field(..., examples=["github_pr", "jira", "text", "github_repo"])
    source_ref: str = ""
    swarm_review_id: Optional[str] = None
    created_at: str
    llm_used: Optional[Dict[str, str]] = Field(
        None, description="Which LLM actually served this swarm run — provider, model, and source "
                           "(platform | key_default | request_override)."
    )
