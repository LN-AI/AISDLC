"""Triage every open issue in a GitHub repo.

Fan-out over `issue_triage`, one issue at a time. Serial by design: each iteration
re-reads the project's triage history, so later issues can be matched as duplicates of
earlier ones from the same run.

Failure policy: all-or-nothing, matching `full_review`. One failing issue aborts the
batch.
"""
from typing import Any, Dict, Optional

from backend.workflows import issue_triage

WORKFLOW_ID = "issue-triage-batch-github"


def run(
    orch,
    github_repo_url: str,
    limit: int = 10,
    team_roster: str = "",
    context: str = "",
    project_name: str = "Unnamed Project",
    llm_override: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    issues = orch.github.list_open_issues(github_repo_url, limit)
    results = []
    for issue in issues:
        r = issue_triage.run(
            orch,
            title=issue.get("title", ""),
            description=issue.get("body", ""),
            context=f"GitHub issue #{issue.get('number')} — {issue.get('url', '')}\n{context}".strip(),
            team_roster=team_roster,
            project_name=project_name,
            llm_override=llm_override,
        )
        r["github_issue_number"] = issue.get("number")
        r["github_issue_url"] = issue.get("url", "")
        results.append(r)

    return {
        "repo": github_repo_url,
        "issues_fetched": len(issues),
        "issues_triaged": len(results),
        "results": results,
        "severity_breakdown": {
            s: sum(1 for r in results if r.get("severity") == s)
            for s in ("Critical", "High", "Medium", "Low")
        },
        "escalations_required": sum(1 for r in results if r.get("human_approval_required")),
    }
