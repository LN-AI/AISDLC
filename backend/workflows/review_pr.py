"""Review a GitHub PR and post the result back as a comment.

Three stages: fetch the diff, review it under a prompt-injection guardrail, post back.
The diff is untrusted third-party content, so it is wrapped before it reaches the model.
"""
from typing import Any, Dict

from backend import database
from backend.services import guardrail_service

WORKFLOW_ID = "github-pr-review"


def run(orch, pr_url: str, jira_ticket: str = "") -> Dict[str, Any]:
    pr_data = orch.github.get_pr_diff(pr_url)

    context = f"GitHub PR: {pr_data['title']}\nBranch: {pr_data['head_branch']} → {pr_data['base_branch']}"
    if jira_ticket:
        context += f"\nJira ticket: {jira_ticket}"

    code_result = orch.code_agent.review_code(
        code=guardrail_service.wrap_untrusted_content(pr_data["diff"], "GITHUB_PR_DIFF"),
        language="",
        context=context,
    )

    review_id = database.save_review(
        review_type="code",
        project_name=pr_data["title"],
        input_summary=f"PR #{pr_data['pr_number']}: {pr_data['title']}",
        input_content=pr_data["diff"][:5000],
        result=code_result,
    )
    # Every other workflow returns its review_id; this one used to discard it, leaving
    # callers unable to link the PR comment back to the stored review.
    code_result["review_id"] = review_id

    comment = orch.github.format_pr_review_comment(code_result)
    posted = orch.github.post_pr_comment(pr_url, comment)

    return {
        "pr_url": pr_url,
        "pr_title": pr_data["title"],
        "pr_number": pr_data["pr_number"],
        "review_id": review_id,
        "review_result": code_result,
        "comment_posted": posted,
    }
