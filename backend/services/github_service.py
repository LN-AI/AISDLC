import httpx
from typing import Dict, Any, Optional
from backend import config


class GitHubService:
    BASE_URL = "https://api.github.com"

    def __init__(self):
        self.token = config.GITHUB_TOKEN
        self.headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github.v3+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _parse_pr_url(self, pr_url: str) -> tuple[str, str, int]:
        """Parse https://github.com/owner/repo/pull/123 into (owner, repo, pr_number)."""
        parts = pr_url.rstrip("/").split("/")
        try:
            pull_idx = parts.index("pull")
            owner = parts[pull_idx - 2]
            repo = parts[pull_idx - 1]
            pr_number = int(parts[pull_idx + 1])
            return owner, repo, pr_number
        except (ValueError, IndexError) as e:
            raise ValueError(f"Invalid GitHub PR URL: {pr_url}") from e

    def get_pr_diff(self, pr_url: str) -> Dict[str, Any]:
        """Fetch PR metadata and diff content."""
        if not self.token:
            raise ValueError("GITHUB_TOKEN is not configured")

        owner, repo, pr_number = self._parse_pr_url(pr_url)

        with httpx.Client(headers=self.headers, timeout=30) as client:
            pr_resp = client.get(f"{self.BASE_URL}/repos/{owner}/{repo}/pulls/{pr_number}")
            pr_resp.raise_for_status()
            pr_data = pr_resp.json()

            diff_headers = {**self.headers, "Accept": "application/vnd.github.v3.diff"}
            diff_resp = client.get(
                f"{self.BASE_URL}/repos/{owner}/{repo}/pulls/{pr_number}",
                headers=diff_headers,
            )
            diff_resp.raise_for_status()
            diff_content = diff_resp.text

            files_resp = client.get(
                f"{self.BASE_URL}/repos/{owner}/{repo}/pulls/{pr_number}/files"
            )
            files_resp.raise_for_status()
            files_data = files_resp.json()

        return {
            "pr_number": pr_number,
            "title": pr_data.get("title", ""),
            "description": pr_data.get("body", ""),
            "author": pr_data.get("user", {}).get("login", ""),
            "base_branch": pr_data.get("base", {}).get("ref", ""),
            "head_branch": pr_data.get("head", {}).get("ref", ""),
            "diff": diff_content[:50000],  # limit diff size
            "files_changed": [f["filename"] for f in files_data],
            "additions": pr_data.get("additions", 0),
            "deletions": pr_data.get("deletions", 0),
            "url": pr_url,
        }

    def parse_pr_url(self, pr_url: str) -> tuple[str, str, int]:
        """Public wrapper for _parse_pr_url — owner, repo, pr_number."""
        return self._parse_pr_url(pr_url)

    def get_branch_head_sha(self, owner: str, repo: str, branch: str) -> str:
        with httpx.Client(headers=self.headers, timeout=30) as client:
            resp = client.get(f"{self.BASE_URL}/repos/{owner}/{repo}/git/ref/heads/{branch}")
            resp.raise_for_status()
            return resp.json()["object"]["sha"]

    def create_branch(self, owner: str, repo: str, new_branch: str, from_branch: str) -> Dict[str, Any]:
        """Create a new branch pointing at the current head of from_branch. Idempotent if it already exists."""
        if not self.token:
            raise ValueError("GITHUB_TOKEN is not configured")
        sha = self.get_branch_head_sha(owner, repo, from_branch)
        with httpx.Client(headers=self.headers, timeout=30) as client:
            resp = client.post(
                f"{self.BASE_URL}/repos/{owner}/{repo}/git/refs",
                json={"ref": f"refs/heads/{new_branch}", "sha": sha},
            )
            if resp.status_code == 422 and "already exists" in resp.text:
                return {"branch": new_branch, "sha": sha, "already_existed": True}
            resp.raise_for_status()
        return {"branch": new_branch, "sha": sha, "already_existed": False}

    def get_file_content_at_ref(self, owner: str, repo: str, path: str, ref: str) -> Dict[str, Any]:
        """Fetch a single file's content directly by owner/repo/path/ref (avoids the URL parser's
        assumption that a branch name has no slashes)."""
        if not self.token:
            raise ValueError("GITHUB_TOKEN is not configured")
        import base64
        with httpx.Client(headers=self.headers, timeout=30) as client:
            resp = client.get(
                f"{self.BASE_URL}/repos/{owner}/{repo}/contents/{path}",
                params={"ref": ref},
            )
            resp.raise_for_status()
            data = resp.json()
        content = base64.b64decode(data["content"]).decode("utf-8", errors="ignore")
        return {"path": path, "content": content, "name": data.get("name", ""), "size": data.get("size", 0)}

    def commit_file(self, owner: str, repo: str, branch: str, path: str, content: str, message: str) -> Dict[str, Any]:
        """Create or update a single file on the given branch via the Contents API."""
        if not self.token:
            raise ValueError("GITHUB_TOKEN is not configured")
        import base64
        existing_sha = None
        with httpx.Client(headers=self.headers, timeout=30) as client:
            existing_resp = client.get(
                f"{self.BASE_URL}/repos/{owner}/{repo}/contents/{path}",
                params={"ref": branch},
            )
            if existing_resp.status_code == 200:
                existing_sha = existing_resp.json().get("sha")

            body = {
                "message": message,
                "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
                "branch": branch,
            }
            if existing_sha:
                body["sha"] = existing_sha

            resp = client.put(f"{self.BASE_URL}/repos/{owner}/{repo}/contents/{path}", json=body)
            resp.raise_for_status()
            data = resp.json()

        return {
            "path": path,
            "commit_sha": data.get("commit", {}).get("sha", ""),
            "content_sha": data.get("content", {}).get("sha", ""),
        }

    def create_pull_request(self, owner: str, repo: str, title: str, body: str, head: str, base: str) -> Dict[str, Any]:
        """Open a PR from head -> base. Returns its number and URL."""
        if not self.token:
            raise ValueError("GITHUB_TOKEN is not configured")
        with httpx.Client(headers=self.headers, timeout=30) as client:
            resp = client.post(
                f"{self.BASE_URL}/repos/{owner}/{repo}/pulls",
                json={"title": title, "body": body, "head": head, "base": base},
            )
            resp.raise_for_status()
            data = resp.json()
        return {"number": data.get("number"), "url": data.get("html_url", ""), "state": data.get("state", "")}

    def post_pr_comment(self, pr_url: str, comment_body: str) -> bool:
        """Post a review comment on a PR."""
        if not self.token:
            return False

        try:
            owner, repo, pr_number = self._parse_pr_url(pr_url)
            with httpx.Client(headers=self.headers, timeout=30) as client:
                resp = client.post(
                    f"{self.BASE_URL}/repos/{owner}/{repo}/issues/{pr_number}/comments",
                    json={"body": comment_body},
                )
                resp.raise_for_status()
            return True
        except Exception:
            return False

    def _parse_url_type(self, url: str) -> Dict[str, Any]:
        """Detect GitHub URL type: pr | issue | file | repo | commit."""
        parts = url.rstrip("/").split("/")
        gh_idx = next((i for i, p in enumerate(parts) if "github.com" in p), -1)
        if gh_idx < 0:
            raise ValueError(f"Not a GitHub URL: {url}")
        rel = parts[gh_idx + 1:]
        if len(rel) < 2:
            raise ValueError("URL must include owner/repo")
        owner, repo = rel[0], rel[1]
        if len(rel) >= 4 and rel[2] == "pull":
            return {"type": "pr", "owner": owner, "repo": repo, "number": int(rel[3])}
        if len(rel) >= 4 and rel[2] == "issues":
            return {"type": "issue", "owner": owner, "repo": repo, "number": int(rel[3])}
        if len(rel) >= 5 and rel[2] in ("blob", "raw", "tree"):
            return {"type": "file", "owner": owner, "repo": repo, "branch": rel[3], "path": "/".join(rel[4:])}
        if len(rel) >= 4 and rel[2] == "commit":
            return {"type": "commit", "owner": owner, "repo": repo, "sha": rel[3]}
        return {"type": "repo", "owner": owner, "repo": repo}

    def get_issue(self, issue_url: str) -> Dict[str, Any]:
        """Fetch a GitHub issue (title, body, comments)."""
        if not self.token:
            raise ValueError("GITHUB_TOKEN is not configured")
        p = self._parse_url_type(issue_url)
        owner, repo, number = p["owner"], p["repo"], p["number"]
        with httpx.Client(headers=self.headers, timeout=30) as client:
            resp = client.get(f"{self.BASE_URL}/repos/{owner}/{repo}/issues/{number}")
            resp.raise_for_status()
            data = resp.json()
            cr = client.get(f"{self.BASE_URL}/repos/{owner}/{repo}/issues/{number}/comments")
            comments = cr.json() if cr.is_success else []
        body = data.get("body") or ""
        comments_text = "\n\n".join(
            f"**{c['user']['login']}:** {c['body']}" for c in comments[:5]
        )
        return {
            "title": data.get("title", ""),
            "body": body,
            "labels": [lb["name"] for lb in data.get("labels", [])],
            "author": data.get("user", {}).get("login", ""),
            "state": data.get("state", ""),
            "number": number,
            "repo": f"{owner}/{repo}",
            "content": f"# {data['title']}\n\n{body}" + (f"\n\n## Comments\n{comments_text}" if comments_text else ""),
            "url": issue_url,
            "fetch_type": "issue",
        }

    def get_file_content(self, file_url: str) -> Dict[str, Any]:
        """Fetch a single file from a GitHub repo."""
        if not self.token:
            raise ValueError("GITHUB_TOKEN is not configured")
        import base64
        p = self._parse_url_type(file_url)
        owner, repo = p["owner"], p["repo"]
        branch = p.get("branch", "main")
        path = p.get("path", "")
        with httpx.Client(headers=self.headers, timeout=30) as client:
            resp = client.get(
                f"{self.BASE_URL}/repos/{owner}/{repo}/contents/{path}",
                params={"ref": branch},
            )
            resp.raise_for_status()
            data = resp.json()
        content = base64.b64decode(data["content"]).decode("utf-8", errors="ignore")
        return {
            "path": path, "content": content,
            "name": data.get("name", ""), "size": data.get("size", 0),
            "url": file_url, "fetch_type": "file",
        }

    def get_repo_overview(self, repo_url: str) -> Dict[str, Any]:
        """Fetch README + architecture docs from a repo for architecture/security review."""
        if not self.token:
            raise ValueError("GITHUB_TOKEN is not configured")
        import base64
        p = self._parse_url_type(repo_url)
        owner, repo = p["owner"], p["repo"]
        docs = []
        CANDIDATE_PATHS = [
            "README.md", "readme.md", "ARCHITECTURE.md", "docs/architecture.md",
            "docs/ARCHITECTURE.md", "docs/design.md", "ADR.md", "SECURITY.md",
        ]
        with httpx.Client(headers=self.headers, timeout=30) as client:
            repo_resp = client.get(f"{self.BASE_URL}/repos/{owner}/{repo}")
            repo_data = repo_resp.json() if repo_resp.is_success else {}
            for path in CANDIDATE_PATHS:
                r = client.get(f"{self.BASE_URL}/repos/{owner}/{repo}/contents/{path}")
                if r.is_success:
                    raw = base64.b64decode(r.json()["content"]).decode("utf-8", errors="ignore")
                    docs.append((path, raw[:6000]))
                    if len(docs) >= 4:
                        break
        combined = (
            f"Repository: {owner}/{repo}\n"
            f"Description: {repo_data.get('description', 'N/A')}\n"
            f"Primary Language: {repo_data.get('language', 'N/A')}\n\n"
        )
        for path, content in docs:
            combined += f"--- {path} ---\n{content}\n\n"
        return {
            "repo": f"{owner}/{repo}",
            "description": repo_data.get("description", ""),
            "language": repo_data.get("language", ""),
            "content": combined,
            "files_fetched": [p for p, _ in docs],
            "url": repo_url,
            "fetch_type": "repo",
        }

    def list_open_issues(self, repo_url: str, limit: int = 10) -> list:
        """List open issues (not PRs) for a repo, most recently updated first."""
        if not self.token:
            raise ValueError("GITHUB_TOKEN is not configured")
        p = self._parse_url_type(repo_url)
        owner, repo = p["owner"], p["repo"]
        limit = max(1, min(limit, 25))
        with httpx.Client(headers=self.headers, timeout=30) as client:
            resp = client.get(
                f"{self.BASE_URL}/repos/{owner}/{repo}/issues",
                params={"state": "open", "sort": "updated", "direction": "desc", "per_page": limit},
            )
            resp.raise_for_status()
            data = resp.json()
        issues = []
        for item in data:
            if "pull_request" in item:
                continue
            body = item.get("body") or ""
            issues.append({
                "number": item.get("number"),
                "title": item.get("title", ""),
                "body": body,
                "labels": [lb["name"] for lb in item.get("labels", [])],
                "author": item.get("user", {}).get("login", ""),
                "url": item.get("html_url", ""),
                "content": f"# {item.get('title','')}\n\n{body}",
            })
        return issues

    def fetch_for_agent(self, ref: str, agent_name: str = "") -> Dict[str, Any]:
        """Detect URL type and return the right content dict for any agent."""
        p = self._parse_url_type(ref)
        t = p["type"]
        if t == "pr":
            return {**self.get_pr_diff(ref), "fetch_type": "pr"}
        if t == "issue":
            return self.get_issue(ref)
        if t == "file":
            return self.get_file_content(ref)
        if t == "repo":
            return self.get_repo_overview(ref)
        raise ValueError(f"Cannot handle GitHub URL type: {t}")

    def post_issue_comment(self, issue_url: str, comment_body: str) -> bool:
        """Post a comment on any GitHub issue or PR (both use the issues API)."""
        if not self.token:
            return False
        try:
            p = self._parse_url_type(issue_url)
            owner, repo, number = p["owner"], p["repo"], p["number"]
            with httpx.Client(headers=self.headers, timeout=30) as client:
                resp = client.post(
                    f"{self.BASE_URL}/repos/{owner}/{repo}/issues/{number}/comments",
                    json={"body": comment_body},
                )
                resp.raise_for_status()
            return True
        except Exception:
            return False

    def format_pr_review_comment(self, review_result: Dict[str, Any]) -> str:
        """Format review result as a GitHub PR comment."""
        gate = review_result.get("gate_status", "Unknown")
        score = review_result.get("risk_score", 0)
        findings = review_result.get("findings", [])

        gate_emoji = {"Green": "✅", "Amber": "⚠️", "Red": "🔴"}.get(gate, "❓")

        severity_map = {5: "🔴 Critical", 4: "🟠 High", 3: "🟡 Moderate", 2: "🔵 Minor", 1: "⚪ Low"}

        lines = [
            f"## {gate_emoji} LM AI-SDLC Review — Gate: **{gate}** | Risk Score: **{score:.0f}/100**",
            "",
            f"**Summary:** {review_result.get('summary', 'No summary.')}",
            "",
        ]

        if findings:
            lines.append(f"### Findings ({len(findings)} total)")
            lines.append("")
            for f in findings:
                sev_label = severity_map.get(f.get("severity", 1), "⚪ Unknown")
                blocking = " ⛔ BLOCKING" if f.get("is_blocking") else ""
                lines.append(f"**{sev_label}{blocking}** — `{f.get('control_id', 'N/A')}` {f.get('title', '')}")
                lines.append(f"> {f.get('description', '')}")
                lines.append(f"**Fix:** {f.get('recommendation', '')}")
                lines.append("")

        recs = review_result.get("recommendations", [])
        if recs:
            lines.append("### Recommendations")
            for i, r in enumerate(recs, 1):
                lines.append(f"{i}. {r}")
            lines.append("")

        lines.append("---")
        lines.append("*Reviewed by [LM AI-SDLC ReviewOps Agent](https://learnedmachine.ai)*")

        return "\n".join(lines)
