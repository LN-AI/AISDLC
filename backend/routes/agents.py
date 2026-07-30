"""Universal agent runner — POST /api/agents/{name}/run.

Every agent is reachable from GitHub (PR / issue / file / repo) or Jira ticket,
in addition to plain text input.
"""
import logging
from typing import Optional
from fastapi import APIRouter, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi import Depends
from pydantic import BaseModel

from backend.services.github_service import GitHubService
from backend.services.jira_service import JiraService
from backend.services.orchestrator import SDLCOrchestrator
from backend.services.auth_service import decode_token
from backend import config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agents", tags=["Agent Hub"])

_orch = SDLCOrchestrator()
_github = GitHubService()
_jira = JiraService()
_security = HTTPBearer(auto_error=False)

VALID_AGENTS = {
    "requirement": "Requirement Risk Reviewer",
    "code":        "Code & PR Security Reviewer",
    "prompt":      "Prompt & Agent Safety Reviewer",
    "compliance":  "Compliance Mapper",
    "vapt":        "VAPT — Vulnerability Assessment",
    "security":    "Security Architecture Review",
    "architecture":"Architecture Review",
    "full":        "Full SDLC Review",
}

# Which sources each agent supports
AGENT_SOURCES = {
    "requirement": ["jira", "github_issue", "text"],
    "code":        ["github_pr", "github_file", "text"],
    "prompt":      ["github_file", "text"],
    "compliance":  ["jira", "text"],
    "vapt":        ["github_pr", "github_file", "github_repo", "text"],
    "security":    ["github_repo", "github_file", "jira", "text"],
    "architecture":["github_repo", "github_file", "jira", "text"],
    "full":        ["github_pr", "jira", "text"],
}


class AgentRunRequest(BaseModel):
    source: str               # github | jira | text
    ref: str                  # GitHub URL, Jira ticket ID, or raw content
    project_name: Optional[str] = ""
    context: Optional[str] = ""
    post_back: bool = False   # post results back to GitHub/Jira
    language: Optional[str] = None
    review_mode: Optional[str] = "review"
    scope: Optional[str] = None
    system_type: Optional[str] = None


def _get_user(creds: Optional[HTTPAuthorizationCredentials] = Depends(_security)):
    if creds:
        return decode_token(creds.credentials)
    return None


@router.get("")
def list_agents():
    return {
        "agents": [
            {
                "name": name,
                "title": title,
                "supported_sources": AGENT_SOURCES.get(name, ["text"]),
            }
            for name, title in VALID_AGENTS.items()
        ]
    }


@router.post("/{agent_name}/run")
def run_agent(agent_name: str, req: AgentRunRequest, user=Depends(_get_user)):
    if agent_name not in VALID_AGENTS:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown agent '{agent_name}'. Valid agents: {', '.join(sorted(VALID_AGENTS))}",
        )

    source_info: dict = {}
    artifact: str = ""
    post_back_target: Optional[tuple] = None

    # ── 1. Fetch content from source ─────────────────────────────────────
    try:
        if req.source == "github":
            fetched = _github.fetch_for_agent(req.ref, agent_name)
            ft = fetched.get("fetch_type", "")
            if ft == "pr":
                artifact = fetched.get("diff", "")
                source_info = {"type": "github_pr", "title": fetched.get("title", ""), "ref": req.ref, "fetch_type": ft}
                post_back_target = ("github", req.ref)
            elif ft == "issue":
                artifact = fetched.get("content", "")
                source_info = {"type": "github_issue", "title": fetched.get("title", ""), "ref": req.ref, "fetch_type": ft}
                post_back_target = ("github", req.ref)
            elif ft == "file":
                artifact = fetched.get("content", "")
                source_info = {"type": "github_file", "title": fetched.get("name", ""), "ref": req.ref, "fetch_type": ft}
            elif ft == "repo":
                artifact = fetched.get("content", "")
                source_info = {"type": "github_repo", "title": fetched.get("repo", ""), "ref": req.ref, "fetch_type": ft}
            else:
                raise HTTPException(status_code=422, detail=f"Unrecognised GitHub URL: {req.ref}")

        elif req.source == "jira":
            if not _jira.is_configured():
                raise HTTPException(status_code=503, detail="Jira not configured. Set JIRA_URL, JIRA_EMAIL, JIRA_API_TOKEN in .env")
            issue = _jira.get_issue(req.ref)
            artifact = issue.get("content", "")
            source_info = {
                "type": "jira",
                "title": issue.get("title", ""),
                "key": issue.get("key", ""),
                "ref": req.ref,
                "fetch_type": "jira",
                "jira_url": issue.get("url", ""),
            }
            post_back_target = ("jira", req.ref)

        elif req.source == "text":
            artifact = req.ref
            source_info = {"type": "text", "title": req.ref[:80] + "…" if len(req.ref) > 80 else req.ref, "ref": "inline"}

        else:
            raise HTTPException(status_code=400, detail=f"Invalid source '{req.source}'. Must be: github | jira | text")

        if not artifact or not artifact.strip():
            raise HTTPException(status_code=422, detail="No content could be fetched from the source — check the URL or ticket ID")

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to fetch from source=%s ref=%s", req.source, req.ref)
        raise HTTPException(status_code=502, detail=f"Failed to fetch from source: {str(e)}")

    project_name = (req.project_name or source_info.get("title", ""))[:120] or "Unnamed"
    actor = user["username"] if user else "api"

    # ── 2. Dispatch to the correct agent ─────────────────────────────────
    context = req.context or ""
    src_title = source_info.get("title", "")
    src_type = source_info.get("type", "")
    if src_title:
        context = f"Source: {src_type} — {src_title}\n{context}".strip()

    try:
        result = _dispatch(agent_name, artifact, req, project_name, context)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Agent '%s' dispatch failed for project=%s", agent_name, project_name)
        raise HTTPException(status_code=500, detail=f"Agent error: {str(e)}")

    # ── 3. Optionally post results back to GitHub / Jira ─────────────────
    post_back_status = "not_requested"
    if req.post_back and post_back_target:
        kind, target_ref = post_back_target
        try:
            review_payload = result.get("review_result", result) if "review_result" in result else result
            comment = _github.format_pr_review_comment(review_payload)
            if kind == "github":
                ok = _github.post_issue_comment(target_ref, comment)
                post_back_status = "posted" if ok else "failed"
            elif kind == "jira":
                plain = _format_for_jira(review_payload)
                ok = _jira.post_comment(target_ref, plain)
                post_back_status = "posted" if ok else "failed"
        except Exception as e:
            logger.exception("Post-back to %s failed for %s", kind, target_ref)
            post_back_status = f"failed: {str(e)[:100]}"

    return {
        "agent": agent_name,
        "agent_title": VALID_AGENTS[agent_name],
        "source": source_info,
        "actor": actor,
        "post_back_status": post_back_status,
        **result,
    }


def _dispatch(agent_name: str, artifact: str, req: AgentRunRequest, project_name: str, context: str) -> dict:
    if agent_name == "requirement":
        return _orch.review_requirement(artifact, context, project_name)
    if agent_name == "code":
        return _orch.review_code(artifact, req.language or "", context, project_name)
    if agent_name == "prompt":
        return _orch.review_prompt(system_prompt=artifact, context=context, project_name=project_name)
    if agent_name == "compliance":
        return _orch.review_compliance(artifact, context=context, project_name=project_name)
    if agent_name == "vapt":
        return _orch.review_vapt(artifact, req.scope or "", context, project_name)
    if agent_name == "security":
        return _orch.review_security(artifact, req.system_type or "", context, project_name)
    if agent_name == "architecture":
        return _orch.review_architecture(artifact, review_mode=req.review_mode or "review", context=context, project_name=project_name)
    if agent_name == "full":
        src_type = ""
        return _orch.full_review(
            requirement=artifact if src_type in ("jira", "github_issue") else None,
            code=artifact if src_type in ("github_pr", "github_file") else artifact,
            project_name=project_name,
            context=context,
        )
    raise ValueError(f"No dispatch handler for agent: {agent_name}")


def _format_for_jira(result: dict) -> str:
    gate = result.get("gate_status", "Unknown")
    score = result.get("risk_score", 0)
    findings = result.get("findings", [])
    summary = result.get("summary", "")
    SEV = ["", "Low", "Minor", "Moderate", "High", "Critical"]
    lines = [
        f"LM AI-SDLC Review — Gate: {gate} | Risk Score: {score:.0f}/100",
        f"Summary: {summary}",
        "",
        f"Findings: {len(findings)} total",
    ]
    for f in findings[:12]:
        lines.append(f"  [{SEV[f.get('severity', 1)]}] {f.get('control_id', '')} — {f.get('title', '')}")
        lines.append(f"    Fix: {f.get('recommendation', '')}")
    recs = result.get("recommendations", [])
    if recs:
        lines.append("\nRecommendations:")
        for r in recs[:5]:
            lines.append(f"  • {r}")
    return "\n".join(lines)
