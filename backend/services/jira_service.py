import base64
import logging
from typing import Dict, Any, Optional
import httpx
from backend import config

logger = logging.getLogger(__name__)


class JiraService:
    def __init__(self):
        self.base_url = (config.JIRA_URL or "").rstrip("/")
        self.email = config.JIRA_EMAIL
        self.token = config.JIRA_API_TOKEN
        if self.email and self.token:
            creds = base64.b64encode(f"{self.email}:{self.token}".encode()).decode()
            self.headers = {
                "Authorization": f"Basic {creds}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        else:
            self.headers = {}

    def is_configured(self) -> bool:
        return bool(self.base_url and self.email and self.token)

    def _parse_ticket_key(self, ref: str) -> str:
        """Extract ticket key from 'PROJ-123' or 'https://company.atlassian.net/browse/PROJ-123'."""
        ref = ref.strip()
        if "/browse/" in ref:
            return ref.rstrip("/").split("/browse/")[-1].split("?")[0].split("#")[0]
        return ref

    def get_issue(self, ticket_ref: str) -> Dict[str, Any]:
        if not self.is_configured():
            raise ValueError("Jira not configured. Set JIRA_URL, JIRA_EMAIL, JIRA_API_TOKEN in .env")
        key = self._parse_ticket_key(ticket_ref)
        logger.info("Fetching Jira issue %s", key)
        with httpx.Client(headers=self.headers, timeout=30) as client:
            resp = client.get(
                f"{self.base_url}/rest/api/3/issue/{key}",
                params={"expand": "renderedFields,names,changelog"},
            )
            resp.raise_for_status()
            data = resp.json()

        fields = data.get("fields", {})
        title = fields.get("summary", "")
        issue_type = (fields.get("issuetype") or {}).get("name", "")
        status = (fields.get("status") or {}).get("name", "")
        priority = (fields.get("priority") or {}).get("name", "Medium")
        assignee = ((fields.get("assignee") or {}).get("displayName") or "Unassigned")
        reporter = ((fields.get("reporter") or {}).get("displayName") or "Unknown")

        description = self._extract_adf(fields.get("description"))

        # Common acceptance criteria field names used by different Jira configs
        ac = ""
        for ac_key in ["customfield_10016", "customfield_10014", "customfield_10028",
                       "customfield_10034", "acceptance_criteria"]:
            val = fields.get(ac_key)
            if val:
                ac = self._extract_adf(val) if isinstance(val, dict) else str(val)
                break

        # Labels and fix versions
        labels = fields.get("labels", [])
        fix_versions = [v.get("name", "") for v in (fields.get("fixVersions") or [])]
        components = [c.get("name", "") for c in (fields.get("components") or [])]

        # Comments
        comment_data = (fields.get("comment") or {}).get("comments", [])[:5]
        comments_text = ""
        for c in comment_data:
            author = (c.get("author") or {}).get("displayName", "?")
            body = self._extract_adf(c.get("body")) if isinstance(c.get("body"), dict) else (c.get("body") or "")
            comments_text += f"\n**{author}:** {body}\n"

        content = f"""# [{key}] {title}

**Type:** {issue_type}  **Status:** {status}  **Priority:** {priority}
**Reporter:** {reporter}  **Assignee:** {assignee}
{f'**Labels:** {", ".join(labels)}' if labels else ''}
{f'**Fix Versions:** {", ".join(fix_versions)}' if fix_versions else ''}
{f'**Components:** {", ".join(components)}' if components else ''}

## Description
{description}
"""
        if ac:
            content += f"\n## Acceptance Criteria\n{ac}\n"
        if comments_text:
            content += f"\n## Comments\n{comments_text}\n"

        return {
            "key": key,
            "title": title,
            "type": issue_type,
            "status": status,
            "priority": priority,
            "description": description,
            "acceptance_criteria": ac,
            "labels": labels,
            "content": content,
            "url": f"{self.base_url}/browse/{key}",
        }

    def post_comment(self, ticket_ref: str, comment_text: str) -> bool:
        if not self.is_configured():
            return False
        key = self._parse_ticket_key(ticket_ref)
        try:
            with httpx.Client(headers=self.headers, timeout=30) as client:
                resp = client.post(
                    f"{self.base_url}/rest/api/3/issue/{key}/comment",
                    json={
                        "body": {
                            "type": "doc",
                            "version": 1,
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [{"type": "text", "text": comment_text}],
                                }
                            ],
                        }
                    },
                )
                resp.raise_for_status()
            return True
        except Exception:
            logger.exception("Failed to post Jira comment on %s", ticket_ref)
            return False

    def search_issues(self, jql: str, max_results: int = 20) -> list:
        if not self.is_configured():
            return []
        try:
            with httpx.Client(headers=self.headers, timeout=30) as client:
                resp = client.get(
                    f"{self.base_url}/rest/api/3/search",
                    params={"jql": jql, "maxResults": max_results, "fields": "summary,status,priority,issuetype,assignee"},
                )
                resp.raise_for_status()
                data = resp.json()
            return [
                {
                    "key": issue["key"],
                    "title": issue["fields"].get("summary", ""),
                    "status": (issue["fields"].get("status") or {}).get("name", ""),
                    "type": (issue["fields"].get("issuetype") or {}).get("name", ""),
                    "url": f"{self.base_url}/browse/{issue['key']}",
                }
                for issue in data.get("issues", [])
            ]
        except Exception:
            logger.exception("Jira search failed for jql=%s", jql)
            return []

    def _extract_adf(self, node: Any) -> str:
        """Recursively extract plain text from Atlassian Document Format."""
        if node is None:
            return ""
        if isinstance(node, str):
            return node
        if isinstance(node, list):
            return " ".join(self._extract_adf(c) for c in node)
        if isinstance(node, dict):
            node_type = node.get("type", "")
            if node_type == "text":
                return node.get("text", "")
            if node_type in ("hardBreak", "rule"):
                return "\n"
            if node_type == "mention":
                return node.get("attrs", {}).get("text", "@user")
            children = node.get("content", [])
            sep = "\n" if node_type in ("paragraph", "heading", "listItem", "blockquote") else " "
            return sep.join(self._extract_adf(c) for c in children)
        return str(node)
