"""Governance Swarm Service — parallel multi-agent orchestration.

Runs a set of agents concurrently (ThreadPoolExecutor), aggregates their
findings, enforces a SwarmPolicy, and returns a unified SwarmResponse.

Agent selection is automatic based on the source type supplied:

    github_pr_url  → code, vapt, compliance, sensitive-data
    github_file_url → code, vapt, sensitive-data
    github_repo_url → architecture, security, vapt
    jira_ticket    → requirement, compliance
    text           → requirement, compliance
    + architecture field → always adds architecture agent
    + system_prompt field → always adds prompt agent

Policy enforcement:
    required agent RED  → swarm_gate = BLOCKED
    optional agent RED  → swarm_gate = AMBER (unless already BLOCKED)
    all pass, avg score > policy.min_passing_score → swarm_gate = AMBER
    all pass, avg score <= policy.min_passing_score → swarm_gate = GREEN
"""
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from backend import config, database
from backend.models.swarm_models import (
    AgentSwarmResult,
    SwarmPolicy,
    SwarmRequest,
    SwarmResponse,
)
from backend.services import llm_gateway
from backend.services.api_key_service import resolve_effective_override
from backend.services.github_service import GitHubService
from backend.services.jira_service import JiraService
from backend.services.orchestrator import SDLCOrchestrator

logger = logging.getLogger(__name__)

# ── Built-in policies ──────────────────────────────────────────────────────

DEFAULT_POLICY = SwarmPolicy(
    required_agents=["code", "vapt", "compliance", "sensitive-data"],
    optional_agents=["architecture", "security"],
    min_passing_score=7.0,
)

BUILTIN_POLICIES: Dict[str, SwarmPolicy] = {
    "standard": SwarmPolicy(
        required_agents=["code", "vapt", "compliance"],
        optional_agents=["architecture", "security"],
        min_passing_score=7.0,
    ),
    "hipaa": SwarmPolicy(
        required_agents=["code", "vapt", "compliance", "sensitive-data"],
        optional_agents=["architecture", "security"],
        min_passing_score=8.0,
    ),
    "pci": SwarmPolicy(
        required_agents=["code", "vapt", "compliance", "sensitive-data"],
        optional_agents=["architecture"],
        min_passing_score=8.5,
    ),
    "ai-product": SwarmPolicy(
        required_agents=["code", "vapt", "compliance", "prompt", "sensitive-data"],
        optional_agents=["architecture", "security"],
        min_passing_score=7.0,
    ),
}


# ── Gate helpers ──────────────────────────────────────────────────────────

_RED_STATUSES = {"red", "blocked"}


def _is_red(gate_status: str) -> bool:
    return gate_status.lower().strip() in _RED_STATUSES


def _weighted_risk(scores: List[float]) -> float:
    """max×0.5 + mean×0.5 — same formula used by SDLCOrchestrator."""
    if not scores:
        return 0.0
    return round(min(max(scores) * 0.5 + sum(scores) / len(scores) * 0.5, 100.0), 1)


# ── Source resolution (mirrors logic in v1_agents._resolve) ───────────────

class _SourceResolver:
    """Thin wrapper that returns (artifact, source_type, source_ref, pb_ref)."""

    def __init__(self):
        self._github = GitHubService()
        self._jira = JiraService()

    def resolve(self, req: SwarmRequest) -> Tuple[str, str, str, str]:
        """Return (artifact_text, source_type, display_ref, postback_ref)."""
        if req.github_pr_url:
            d = self._github.fetch_for_agent(req.github_pr_url, "code")
            artifact = d.get("diff") or d.get("content", "")
            return artifact, "github_pr", d.get("title", req.github_pr_url), req.github_pr_url

        if req.github_file_url:
            d = self._github.fetch_for_agent(req.github_file_url, "code")
            artifact = d.get("content", "")
            return artifact, "github_file", d.get("name", req.github_file_url), ""

        if req.github_repo_url:
            d = self._github.fetch_for_agent(req.github_repo_url, "architecture")
            artifact = d.get("content", "")
            return artifact, "github_repo", d.get("repo", req.github_repo_url), ""

        if req.jira_ticket:
            if not self._jira.is_configured():
                raise ValueError("Jira not configured. Set JIRA_URL, JIRA_EMAIL, JIRA_API_TOKEN in .env")
            issue = self._jira.get_issue(req.jira_ticket)
            artifact = issue.get("content", "")
            return artifact, "jira", issue.get("title", req.jira_ticket), req.jira_ticket

        if req.text:
            return req.text, "text", req.text[:80].replace("\n", " "), ""

        # Architecture-only request (no primary source but architecture field present)
        if req.architecture:
            preview = req.architecture[:80].replace("\n", " ")
            return req.architecture, "text", preview, ""

        if req.system_prompt:
            preview = req.system_prompt[:80].replace("\n", " ")
            return req.system_prompt, "text", preview, ""

        raise ValueError(
            "No source provided. Supply a GitHub URL, Jira ticket ID, or text content."
        )


# ── Agent selector ────────────────────────────────────────────────────────

def _select_agents(req: SwarmRequest, source_type: str, policy: SwarmPolicy) -> List[str]:
    """
    Determine which agents to run based on source type and optional extras.
    Result is the union of policy.required_agents + policy.optional_agents
    intersected with what makes sense for the source type, plus any extras
    triggered by optional fields (architecture, system_prompt).
    """
    # Start from the source-type baseline
    source_defaults: Dict[str, List[str]] = {
        "github_pr":   ["code", "vapt", "compliance", "sensitive-data"],
        "github_file": ["code", "vapt", "sensitive-data"],
        "github_repo": ["architecture", "security", "vapt"],
        "jira":        ["requirement", "compliance"],
        "text":        ["requirement", "compliance"],
    }
    base = set(source_defaults.get(source_type, ["code", "compliance"]))

    # Always include all agents listed in the policy
    policy_agents = set(policy.required_agents) | set(policy.optional_agents)
    # Intersection: only run agents that are both policy-relevant and source-appropriate
    # Exception: always respect the full policy for required agents
    agents = (base & policy_agents) | set(policy.required_agents)

    # Optional extras
    if req.architecture:
        agents.add("architecture")
    if req.system_prompt:
        agents.add("prompt")

    # Ensure we don't include requirement/code agents for repo-level sources where
    # they receive useless content (repo manifest), unless explicitly in required_agents.
    if source_type == "github_repo":
        agents.discard("requirement")
        if "requirement" not in policy.required_agents:
            agents.discard("requirement")
        if "code" not in policy.required_agents:
            agents.discard("code")
        if "sensitive-data" not in policy.required_agents:
            agents.discard("sensitive-data")
        if "compliance" not in policy.required_agents:
            agents.discard("compliance")

    return sorted(agents)


# ── Per-agent runner ───────────────────────────────────────────────────────

def _run_one_agent(
    agent_name: str,
    artifact: str,
    req: SwarmRequest,
    orch: SDLCOrchestrator,
    ctx: str,
    caller: Optional[dict] = None,
) -> AgentSwarmResult:
    """
    Call the appropriate orchestrator method for one agent and normalise
    the result into an AgentSwarmResult.  Exceptions are caught and stored
    in AgentSwarmResult.error so the swarm can continue.

    Each agent resolves its own BYOL override (request-level override wins
    uniformly across all agents in the swarm; otherwise this agent's own
    per-agent config, then the caller's key-wide default, then platform).
    """
    try:
        override = resolve_effective_override(req, caller, agent_name)
    except ValueError as exc:
        return AgentSwarmResult(
            agent=agent_name, gate_status="Green", risk_score=0.0, summary="",
            error=f"Invalid BYOL config: {exc}",
        )

    try:
        project = req.project_name
        raw: Dict[str, Any] = {}

        with llm_gateway.with_override(override):
            if agent_name == "code":
                raw = orch.review_code(artifact, "", ctx, project)

            elif agent_name == "vapt":
                raw = orch.review_vapt(artifact, "", ctx, project)

            elif agent_name == "compliance":
                raw = orch.review_compliance(artifact, None, ctx, project)

            elif agent_name == "sensitive-data":
                raw = orch.scan_sensitive_data(artifact, ctx, project)

            elif agent_name == "security":
                raw = orch.review_security(artifact, "", ctx, project)

            elif agent_name == "architecture":
                arch_artifact = req.architecture or artifact
                raw = orch.review_architecture(
                    arch_artifact,
                    requirements=req.requirements or "",
                    review_mode="review",
                    context=ctx,
                    project_name=project,
                )

            elif agent_name == "requirement":
                raw = orch.review_requirement(artifact, ctx, project)

            elif agent_name == "prompt":
                raw = orch.review_prompt(
                    system_prompt=req.system_prompt or artifact,
                    user_prompt="",
                    tool_definitions="",
                    rag_config="",
                    context=ctx,
                    project_name=project,
                )

            else:
                return AgentSwarmResult(
                    agent=agent_name,
                    gate_status="Green",
                    risk_score=0.0,
                    summary=f"Agent '{agent_name}' is not supported by the swarm.",
                    error=f"Unknown agent: {agent_name}",
                )

        findings = raw.get("findings", [])
        return AgentSwarmResult(
            agent=agent_name,
            gate_status=raw.get("gate_status", "Green"),
            risk_score=float(raw.get("risk_score", 0.0)),
            critical_count=raw.get("critical_count", sum(1 for f in findings if f.get("severity") == 5)),
            high_count=raw.get("high_count", sum(1 for f in findings if f.get("severity") == 4)),
            summary=raw.get("summary", ""),
            findings=findings,
            compliance_flags=raw.get("compliance_flags", []),
            review_id=raw.get("review_id"),
            llm_used=llm_gateway.describe(override),
        )

    except Exception as exc:  # noqa: BLE001
        logger.exception("Swarm agent '%s' raised an exception: %s", agent_name, exc)
        return AgentSwarmResult(
            agent=agent_name,
            gate_status="Green",   # Don't let a single crash block the gate
            risk_score=0.0,
            summary="",
            error=str(exc)[:500],
        )


# ── Swarm Service ──────────────────────────────────────────────────────────

class SwarmService:
    """Runs multiple agents in parallel and returns a unified gate decision."""

    MAX_WORKERS = 6

    def __init__(self):
        self._resolver = _SourceResolver()
        self._jira = JiraService()
        self._github = GitHubService()

    # ── Public entry point ─────────────────────────────────────────────────

    def run_swarm(self, req: SwarmRequest, orch: SDLCOrchestrator, caller: Optional[dict] = None) -> SwarmResponse:
        """
        1. Resolve source → artifact text
        2. Determine policy (builtin shortcut → custom object → default)
        3. Select agents
        4. Run agents in parallel (ThreadPoolExecutor)
        5. Aggregate results
        6. Enforce policy → swarm_gate
        7. Optionally post back
        8. Save to DB
        9. Return SwarmResponse
        """
        # ── 1. Resolve source ──────────────────────────────────────────────
        try:
            artifact, source_type, source_ref, pb_ref = self._resolver.resolve(req)
        except ValueError as exc:
            raise exc  # bubble up to the route handler as a 400/503

        # ── 2. Determine policy ────────────────────────────────────────────
        policy = self._resolve_policy(req)

        # ── 3. Select agents ───────────────────────────────────────────────
        agents_to_run = _select_agents(req, source_type, policy)
        if not agents_to_run:
            agents_to_run = list(set(policy.required_agents))

        # ── 4. Run agents in parallel ──────────────────────────────────────
        ctx = self._build_context(req, source_type, source_ref)
        results: Dict[str, AgentSwarmResult] = {}

        # Each agent resolves and applies its own BYOL override inside _run_one_agent
        # (request-level override applies uniformly; otherwise per-agent > per-key > platform).
        with ThreadPoolExecutor(max_workers=self.MAX_WORKERS) as pool:
            future_to_agent = {
                pool.submit(_run_one_agent, name, artifact, req, orch, ctx, caller): name
                for name in agents_to_run
            }
            for future in as_completed(future_to_agent):
                agent_name = future_to_agent[future]
                try:
                    results[agent_name] = future.result()
                except Exception as exc:  # noqa: BLE001
                    # Should not happen (exceptions are caught inside _run_one_agent)
                    logger.error("Unexpected future exception for agent '%s': %s", agent_name, exc)
                    results[agent_name] = AgentSwarmResult(
                        agent=agent_name,
                        gate_status="Green",
                        risk_score=0.0,
                        summary="",
                        error=str(exc)[:500],
                    )

        # ── 5 & 6. Aggregate + enforce policy ─────────────────────────────
        response = self._aggregate(
            req=req,
            policy=policy,
            results=results,
            agents_run=agents_to_run,
            source_type=source_type,
            source_ref=source_ref,
        )

        # ── 7. Post back ───────────────────────────────────────────────────
        if req.post_back and pb_ref:
            self._do_postback(source_type, pb_ref, response)

        # ── 8. Save swarm result to DB ─────────────────────────────────────
        swarm_review_id = self._save(req, response, artifact)
        response.swarm_review_id = swarm_review_id

        return response

    # ── Policy resolution ──────────────────────────────────────────────────

    @staticmethod
    def _resolve_policy(req: SwarmRequest) -> SwarmPolicy:
        if req.policy:
            return req.policy
        if req.policy_name:
            name = req.policy_name.lower().strip()
            if name in BUILTIN_POLICIES:
                return BUILTIN_POLICIES[name]
            # Unknown policy name — fall through to default
            logger.warning("Unknown policy_name '%s'; using DEFAULT_POLICY.", req.policy_name)
        return DEFAULT_POLICY

    # ── Context builder ────────────────────────────────────────────────────

    @staticmethod
    def _build_context(req: SwarmRequest, source_type: str, source_ref: str) -> str:
        parts = [f"Source: {source_type} — {source_ref}"]
        if req.context:
            parts.append(req.context)
        return "\n".join(parts).strip()

    # ── Aggregation + gate logic ───────────────────────────────────────────

    def _aggregate(
        self,
        req: SwarmRequest,
        policy: SwarmPolicy,
        results: Dict[str, AgentSwarmResult],
        agents_run: List[str],
        source_type: str,
        source_ref: str,
    ) -> SwarmResponse:
        ordered_results = [results[a] for a in agents_run if a in results]

        agents_passed: List[str] = []
        agents_failed: List[str] = []   # produced Red gate
        agents_errored: List[str] = []  # raised an exception

        scores: List[float] = []
        all_blocking: List[Dict[str, Any]] = []
        total_critical = 0
        total_high = 0

        for res in ordered_results:
            if res.error and res.risk_score == 0.0:
                agents_errored.append(res.agent)
            else:
                scores.append(res.risk_score)
                total_critical += res.critical_count
                total_high += res.high_count
                blocking = [
                    {**f, "_source_agent": res.agent}
                    for f in res.findings
                    if f.get("severity", 0) >= 4 or f.get("is_blocking", False)
                ]
                all_blocking.extend(blocking)

                if _is_red(res.gate_status):
                    agents_failed.append(res.agent)
                else:
                    agents_passed.append(res.agent)

        swarm_risk_score = _weighted_risk(scores)

        # ── Gate decision ──────────────────────────────────────────────────
        required_failed = [a for a in agents_failed if a in policy.required_agents]
        optional_failed = [a for a in agents_failed if a in policy.optional_agents]

        if required_failed:
            swarm_gate = "BLOCKED"
        elif optional_failed or swarm_risk_score > policy.min_passing_score:
            swarm_gate = "AMBER"
        else:
            swarm_gate = "GREEN"

        # Sort blocking findings by severity desc
        all_blocking.sort(key=lambda f: f.get("severity", 0), reverse=True)

        return SwarmResponse(
            success=True,
            project_name=req.project_name,
            swarm_gate=swarm_gate,
            swarm_risk_score=swarm_risk_score,
            agents_run=agents_run,
            agents_passed=agents_passed,
            agents_failed=agents_failed,
            agents_errored=agents_errored,
            blocking_findings=all_blocking[:50],  # cap at 50 to keep response size sane
            total_critical=total_critical,
            total_high=total_high,
            results=ordered_results,
            policy_applied=policy,
            source_type=source_type,
            source_ref=source_ref,
            created_at=datetime.now(timezone.utc).isoformat(),
        )

    # ── DB persistence ─────────────────────────────────────────────────────

    @staticmethod
    def _save(req: SwarmRequest, resp: SwarmResponse, artifact: str) -> Optional[str]:
        try:
            result_payload = {
                "swarm_gate": resp.swarm_gate,
                "swarm_risk_score": resp.swarm_risk_score,
                "agents_run": resp.agents_run,
                "agents_passed": resp.agents_passed,
                "agents_failed": resp.agents_failed,
                "agents_errored": resp.agents_errored,
                "total_critical": resp.total_critical,
                "total_high": resp.total_high,
                "findings": resp.blocking_findings,
                "risk_score": resp.swarm_risk_score,
                "gate_status": resp.swarm_gate,
                "summary": (
                    f"Swarm gate: {resp.swarm_gate} | "
                    f"Agents run: {len(resp.agents_run)} | "
                    f"Risk score: {resp.swarm_risk_score:.1f} | "
                    f"Critical: {resp.total_critical} | High: {resp.total_high}"
                ),
                "compliance_flags": list({
                    flag
                    for r in resp.results
                    for flag in (r.compliance_flags or [])
                }),
                "policy_applied": resp.policy_applied.model_dump(),
                "per_agent_results": [r.model_dump() for r in resp.results],
            }
            review_id = database.save_review(
                review_type="swarm",
                project_name=req.project_name,
                input_summary=(
                    f"Swarm — {resp.source_type} | {len(resp.agents_run)} agents | "
                    f"gate={resp.swarm_gate}"
                ),
                input_content=artifact[:5000],
                result=result_payload,
            )
            return review_id
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to save swarm result to DB: %s", exc)
            return None

    # ── Post-back ──────────────────────────────────────────────────────────

    def _do_postback(self, source_type: str, ref: str, resp: SwarmResponse) -> None:
        try:
            if source_type in ("github_pr", "github_issue"):
                comment = self._format_github_comment(resp)
                self._github.post_issue_comment(ref, comment)
            elif source_type == "jira":
                comment = self._format_jira_comment(resp)
                self._jira.post_comment(ref, comment)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Swarm post-back failed: %s", exc)

    @staticmethod
    def _format_github_comment(resp: SwarmResponse) -> str:
        gate_emoji = {"GREEN": "✅", "AMBER": "⚠️", "RED": "🔴", "BLOCKED": "🚫"}.get(resp.swarm_gate, "❓")
        lines = [
            f"## {gate_emoji} LM AI-SDLC Governance Swarm — `{resp.swarm_gate}`",
            f"",
            f"| Metric | Value |",
            f"|--------|-------|",
            f"| Swarm Gate | **{resp.swarm_gate}** |",
            f"| Risk Score | {resp.swarm_risk_score:.1f}/100 |",
            f"| Agents Run | {len(resp.agents_run)} ({', '.join(resp.agents_run)}) |",
            f"| Critical Findings | {resp.total_critical} |",
            f"| High Findings | {resp.total_high} |",
            f"",
        ]
        if resp.agents_failed:
            lines.append(f"**Blocking agents:** {', '.join(resp.agents_failed)}")
            lines.append("")
        if resp.blocking_findings:
            lines.append("### Top Blocking Findings")
            for f in resp.blocking_findings[:5]:
                sev = f.get("risk_level", f"Sev{f.get('severity','?')}")
                title = f.get("title", "Unknown")
                agent = f.get("_source_agent", "")
                lines.append(f"- **[{sev}]** `{agent}` — {title}")
            lines.append("")
        lines.append(f"*Generated by LM AI-SDLC ReviewOps · {resp.created_at}*")
        return "\n".join(lines)

    @staticmethod
    def _format_jira_comment(resp: SwarmResponse) -> str:
        lines = [
            f"*LM AI-SDLC Governance Swarm* — Gate: {resp.swarm_gate} | "
            f"Score: {resp.swarm_risk_score:.1f}/100 | "
            f"Critical: {resp.total_critical} | High: {resp.total_high}",
            "",
        ]
        for f in resp.blocking_findings[:10]:
            sev = f.get("risk_level", f"Sev{f.get('severity','?')}")
            title = f.get("title", "Unknown")
            rec = f.get("recommendation", "")
            agent = f.get("_source_agent", "")
            lines.append(f"[{sev}] {agent} — {title}")
            if rec:
                lines.append(f"  Fix: {rec}")
        return "\n".join(lines)
