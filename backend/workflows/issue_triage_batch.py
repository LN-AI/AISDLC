"""Triage every open issue in a GitHub repo.

Fan-out over `issue_triage`, one issue at a time. Serial by design: each iteration
re-reads the project's triage history, so later issues can be matched as duplicates of
earlier ones from the same run.

Failure policy: all-or-nothing, matching `full_review`. One failing issue aborts the
batch.
"""
from typing import Any, Callable, Dict, Optional

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
    on_progress: Optional[Callable[[int, int, str], None]] = None,
) -> Dict[str, Any]:
    issues = orch.github.list_open_issues(github_repo_url, limit)
    total = len(issues) or 1
    # Reported per issue, naming the one being read. This is serial by design and one model
    # call per issue, so a batch over forty issues is forty sequential calls -- the
    # difference between "working..." and "issue 12 of 40: Login times out" is the difference
    # between a screen that looks stuck and one that does not.
    if on_progress:
        on_progress(0, total, f"Found {len(issues)} open issue(s)")
    results = []
    for index, issue in enumerate(issues, start=1):
        if on_progress:
            on_progress(index - 1, total,
                        f"Triaging issue {index} of {total}"
                        + (f" — #{issue.get('number')} {(issue.get('title') or '')[:60]}"
                           if issue.get("number") else ""))
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
        if on_progress:
            on_progress(index, total, f"Triaged {index} of {total} issue(s)")

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
