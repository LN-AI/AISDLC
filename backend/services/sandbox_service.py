"""The Workshop — a team sandbox with two things in it: practicing a real built-in agent
with zero consequence, and running a custom agent the team wrote for themselves.

Custom agents belong to the project a member was working in when they made one (D-465) —
visible and runnable by every member of that team, editable/deletable by whoever created it
or an admin of that project. Practice itself stays personal: it is attributed to the caller
and counted against their own share of the daily cap, same as before.

Both share one property that makes them safe to leave uncapped by the real governance
system: neither spends project budget and neither writes a `reviews` row
(`context.practice_mode()` — see that module for why a flag here, not a parameter threaded
through 19 call sites). That is exactly why they need a cap of their own
(`SANDBOX_DAILY_CAP`, counted on `sandbox_runs`) — without one, this would be free,
unlimited real model access.

Practice mode reuses the *real* orchestrator call for the agent (`Product.invoke`), not a
re-implementation of it — the result a user sees in practice is the same code path, KB
context and compression a live call gets. It still respects the caller's real entitlement
(which agents they may reach at all); practice removes the cost/persistence, not the RBAC.

Only a curated, explicitly-safe subset of agents is practiceable (`PRACTICE_ELIGIBLE_AGENTS`):
every one of them is read-only (no GitHub/repo writes) and driven entirely by one artifact
blob + optional context/project name. Composite workflows (`full`, `release-gate`), anything
that writes somewhere real (`auto-fix`, `github-pr-review`, modernization), and anything
needing structured fields a generic text box can't supply (the GxP/21 CFR agents need a
file list; `migration-assessment` needs a required target stack) are deliberately excluded
— the risk of silently feeding a workflow agent garbage params is not worth the coverage.

A custom agent is deliberately NOT built on the findings/gate schema (`REVIEW_TOOL_SCHEMA`)
every built-in agent uses: an arbitrary user-written prompt ("summarize this meeting
transcript") does not naturally produce a control id or a Red/Amber/Green verdict, and
forcing one would just be a model inventing structure that means nothing. It gets a single
freeform text field instead (`FREEFORM_TOOL_SCHEMA`), via `llm_gateway.call_tool` — which is
also how it gets the same input-size guardrail, timeout and retry every other agent call
gets, for free, with no new code path in llm_gateway itself.
"""
from datetime import datetime
from typing import Any, Dict, List, Optional

from backend import config, context, database
from backend.products import registry
from backend.services import llm_gateway

# ── Practice mode ────────────────────────────────────────────────────────

PRACTICE_ELIGIBLE_AGENTS = frozenset({
    "requirement", "code", "prompt", "compliance", "vapt", "security",
    "sensitive-data", "architecture", "ai-provenance", "codegen-advisory",
    "test-scenario", "test-case",
    "deployment-readiness", "cicd-pipeline-review", "iac-review",
    "incident-postmortem", "monitoring-alerting-review",
})


class SandboxLimitError(Exception):
    """Raised when the daily practice/custom-agent cap is exhausted."""


class SandboxNotEligibleError(Exception):
    """Raised when an agent id is not on the practice-eligible allowlist."""


def check_daily_cap(user_id: str) -> None:
    used = database.count_sandbox_runs_today(user_id)
    if used >= config.SANDBOX_DAILY_CAP:
        raise SandboxLimitError(
            f"Workshop daily limit reached ({used}/{config.SANDBOX_DAILY_CAP} practice + "
            f"custom-agent runs today). Resets at midnight UTC."
        )


def run_practice(orch: Any, user_id: str, agent_id: str, content: str,
                 context_text: str = "") -> Dict[str, Any]:
    if agent_id not in PRACTICE_ELIGIBLE_AGENTS:
        raise SandboxNotEligibleError(
            f"'{agent_id}' is not available to practice — the Workshop only covers "
            f"read-only, single-artifact agents."
        )
    product = registry.get(agent_id)
    if not product or not product.invoke:
        raise SandboxNotEligibleError(f"'{agent_id}' has no direct call shape to practice.")

    check_daily_cap(user_id)

    # A generous superset covering every key variant the practice-eligible adapters read
    # (_ARTIFACT's four names, plus the three fields whose adapter prefers a more specific
    # key before falling back to _ARTIFACT) — see registry.py's Product entries for each.
    params = {
        "content": content, "artifact": content, "code": content, "text": content,
        "requirement": content, "system_prompt": content, "architecture": content,
        "context": context_text, "project_name": "Workshop practice",
    }
    with context.practice_mode():
        result = product.invoke(orch, params)
    database.record_sandbox_run(user_id, "practice", agent_id)
    result["_practice"] = True
    return result


def list_practice_agents() -> List[Dict[str, str]]:
    out = []
    for agent_id in sorted(PRACTICE_ELIGIBLE_AGENTS):
        product = registry.get(agent_id)
        if product:
            out.append({"id": product.id, "title": product.title, "description": product.description})
    return out


# ── Custom agents ────────────────────────────────────────────────────────

FREEFORM_TOOL_SCHEMA = {
    "name": "submit_response",
    "description": "Submit your response to the user's request",
    "input_schema": {
        "type": "object",
        "required": ["response"],
        "properties": {
            "response": {"type": "string", "description": "Your complete response, in plain text or markdown"},
        },
    },
}


def create_custom_agent(user_id: str, project_id: str, name: str, description: str,
                        system_prompt: str, input_label: str) -> Dict[str, Any]:
    """D-465: the cap is per team (project), not per person — it's a shared resource now,
    so one member filling it blocks the whole team rather than just themselves."""
    existing = database.list_custom_agents_for_project(project_id)
    if len(existing) >= config.SANDBOX_MAX_CUSTOM_AGENTS:
        raise SandboxLimitError(
            f"This team already has {len(existing)} custom agents — the Workshop limit is "
            f"{config.SANDBOX_MAX_CUSTOM_AGENTS} per team. Delete one before creating another."
        )
    if len(system_prompt) > config.SANDBOX_MAX_SYSTEM_PROMPT_CHARS:
        raise ValueError(
            f"System prompt is {len(system_prompt):,} characters — the Workshop limit is "
            f"{config.SANDBOX_MAX_SYSTEM_PROMPT_CHARS:,}."
        )
    return database.create_custom_agent(user_id, project_id, name, description, system_prompt, input_label)


def run_custom_agent(caller_user_id: str, custom_agent_id: str, input_text: str) -> Dict[str, Any]:
    """Team membership (any member may run any of their team's custom agents) is checked by
    the route before this is called — it already needed the project id to do that, so this
    only re-confirms the agent still exists and counts the run against the CALLER's own
    daily cap, not the creator's."""
    agent = database.get_custom_agent(custom_agent_id)
    if not agent or not agent.get("is_active"):
        raise SandboxNotEligibleError("Custom agent not found.")

    check_daily_cap(caller_user_id)

    result = llm_gateway.call_tool(agent["system_prompt"], input_text, FREEFORM_TOOL_SCHEMA)
    database.record_sandbox_run(caller_user_id, "custom", custom_agent_id)
    return {
        "response": result.get("response", ""),
        "guardrail_flags": result.get("guardrail_flags"),
        "agent_name": agent["name"],
        "_practice": True,
    }
