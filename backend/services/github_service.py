import logging

import httpx
from typing import Dict, Any, Optional
from backend import config

logger = logging.getLogger(__name__)


class GitHubService:
    BASE_URL = "https://api.github.com"
    # Nothing downstream chunks a diff yet, so a very large PR still has to be cut to fit
    # one agent call. Raise this only once the reviewer can split its input — a bigger
    # slice on its own just moves the cut, it doesn't make the review complete.
    MAX_DIFF_CHARS = 50000

    def __init__(self):
        self.token = config.GITHUB_TOKEN
        self.headers = {
            "Accept": "application/vnd.github.v3+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            self.headers["Authorization"] = f"Bearer {self.token}"

    def _raise_friendly(self, resp: httpx.Response) -> None:
        """Like resp.raise_for_status(), but explains 403/404s that are likely
        caused by GitHub's unauthenticated rate limit or a private repo when no
        GITHUB_TOKEN is configured, instead of a bare GitHub error body."""
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            if not self.token and resp.status_code in (401, 403, 404):
                raise ValueError(
                    f"GitHub request failed (status {resp.status_code}) without GITHUB_TOKEN — "
                    "this may be a private repo or GitHub's unauthenticated rate limit (60 req/hr) "
                    "was hit. Set GITHUB_TOKEN in .env for private repos or higher limits."
                ) from e
            raise

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
        """Fetch PR metadata and diff content. Works unauthenticated for public
        repos (GitHub allows anonymous reads at a lower rate limit)."""
        owner, repo, pr_number = self._parse_pr_url(pr_url)
        logger.info("Fetching PR diff: %s/%s#%d", owner, repo, pr_number)

        with httpx.Client(headers=self.headers, timeout=30) as client:
            pr_resp = client.get(f"{self.BASE_URL}/repos/{owner}/{repo}/pulls/{pr_number}")
            self._raise_friendly(pr_resp)
            pr_data = pr_resp.json()

            diff_headers = {**self.headers, "Accept": "application/vnd.github.v3.diff"}
            diff_resp = client.get(
                f"{self.BASE_URL}/repos/{owner}/{repo}/pulls/{pr_number}",
                headers=diff_headers,
            )
            self._raise_friendly(diff_resp)
            diff_content = diff_resp.text

            files_resp = client.get(
                f"{self.BASE_URL}/repos/{owner}/{repo}/pulls/{pr_number}/files"
            )
            self._raise_friendly(files_resp)
            files_data = files_resp.json()

        # A diff over the cap used to be sliced silently, so a large PR was reviewed on
        # its first 50k characters and the result was reported as covering the whole PR
        # — a clean gate on code nobody looked at. The cut is still made (nothing
        # downstream chunks yet), but it is now announced in the text the model sees and
        # flagged on the payload so callers can refuse to report Green over it.
        diff_full_chars = len(diff_content)
        diff_truncated = diff_full_chars > self.MAX_DIFF_CHARS
        diff_text = diff_content[:self.MAX_DIFF_CHARS]
        if diff_truncated:
            logger.warning(
                "PR #%s diff truncated for review: %d of %d characters kept",
                pr_number, self.MAX_DIFF_CHARS, diff_full_chars,
            )
            diff_text += (
                f"\n\n[TRUNCATED: this diff was cut off at {self.MAX_DIFF_CHARS:,} of "
                f"{diff_full_chars:,} characters. The remainder has NOT been provided — "
                f"do not treat anything below this point as reviewed or absent.]"
            )

        return {
            "pr_number": pr_number,
            "title": pr_data.get("title", ""),
            "description": pr_data.get("body", ""),
            "author": pr_data.get("user", {}).get("login", ""),
            "base_branch": pr_data.get("base", {}).get("ref", ""),
            "head_branch": pr_data.get("head", {}).get("ref", ""),
            "diff": diff_text,
            "diff_truncated": diff_truncated,
            "diff_full_chars": diff_full_chars,
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
                logger.info("Branch %s/%s:%s already exists - reusing", owner, repo, new_branch)
                return {"branch": new_branch, "sha": sha, "already_existed": True}
            resp.raise_for_status()
        logger.info("Created branch %s/%s:%s from %s", owner, repo, new_branch, from_branch)
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

        logger.info("Committed file %s to %s/%s:%s", path, owner, repo, branch)
        return {
            "path": path,
            "commit_sha": data.get("commit", {}).get("sha", ""),
            "content_sha": data.get("content", {}).get("sha", ""),
        }

    # GitHub rejects a very large `POST /git/trees` payload, and its own guidance for a big
    # tree is to build it in stages. A 500-file conversion is exactly that shape, so entries
    # go up in batches of this size, each batch passing the previous result as its
    # `base_tree` — the final tree still contains every file.
    TREE_ENTRY_BATCH = 500
    # One client serves every blob POST in a conversion, and a generated source file is a
    # larger body than any other write in this file, so the usual 30s is tight.
    GIT_DATA_TIMEOUT = 60

    def _tree_path(self, raw: str) -> str:
        """Normalize one repo-relative path, or raise if it isn't safe to commit.

        Rejected rather than repaired, matching the rule at the other end of the pipeline
        (`modernization.repo_ingest.safe_relpath`): no absolute path, no drive letter, and
        no `..` segment even when it would normalize back inside the tree. These paths are
        machine-generated, and a name we had to fix is a name whose intent we can't vouch for.
        """
        path = (raw or "").replace("\\", "/").strip()
        # A drive letter survives the backslash swap as `C:/…`; git has no such path.
        drive_letter = len(path) > 1 and path[1] == ":"
        # Empty segments cover a leading `/` and a doubled `//`; `.`/`..` cover traversal.
        bad_segment = any(seg in ("", ".", "..") for seg in path.split("/"))
        if not path or drive_letter or bad_segment:
            raise ValueError(f"Refusing to commit an unsafe path: {raw!r}")
        return path

    def commit_tree(self, owner: str, repo: str, branch: str, files: Dict[str, str],
                    message: str, base_branch: str = "") -> Dict[str, Any]:
        """Commit many files as ONE commit, via the Git Data API.

        `commit_file` is one GET plus one PUT per file and moves the branch each time, so a
        500-file conversion is 1,000 sequential requests and 500 commits — slow enough to be
        unusable and enough traffic to trip GitHub's secondary rate limits. This is one blob
        POST per file, then a single tree, commit and ref update, so the branch ends up with
        the whole conversion or with none of it.

        `base_branch` empty means: build on `branch` if that ref already exists (so a second
        build/fix round stacks onto the first instead of orphaning it), otherwise on the
        repository's default branch. The one it resolved to comes back as `base_branch`,
        because the caller opening the PR needs to name it and only this method knows it.

        Never force-pushes. A branch that has diverged from its base raises rather than
        discarding whatever moved it.
        """
        if not self.token:
            raise ValueError("GITHUB_TOKEN is not configured")
        if not files:
            raise ValueError("commit_tree was given no files")
        import base64

        # Validate every path before a single request goes out — a partially committed tree
        # is not something we can take back.
        items = [(self._tree_path(path), content or "") for path, content in files.items()]

        with httpx.Client(headers=self.headers, timeout=self.GIT_DATA_TIMEOUT) as client:
            base = base_branch or branch
            ref_resp = client.get(f"{self.BASE_URL}/repos/{owner}/{repo}/git/ref/heads/{base}")
            if ref_resp.status_code == 404 and not base_branch:
                repo_resp = client.get(f"{self.BASE_URL}/repos/{owner}/{repo}")
                repo_resp.raise_for_status()
                base = repo_resp.json().get("default_branch") or "main"
                ref_resp = client.get(f"{self.BASE_URL}/repos/{owner}/{repo}/git/ref/heads/{base}")
            ref_resp.raise_for_status()
            base_commit_sha = ref_resp.json()["object"]["sha"]

            commit_resp = client.get(
                f"{self.BASE_URL}/repos/{owner}/{repo}/git/commits/{base_commit_sha}"
            )
            commit_resp.raise_for_status()
            base_tree_sha = commit_resp.json()["tree"]["sha"]

            entries = []
            for path, content in items:
                # Always base64, never `utf-8`: generated source can legitimately contain
                # anything, and the utf-8 encoding rejects content GitHub can't round-trip.
                blob_resp = client.post(
                    f"{self.BASE_URL}/repos/{owner}/{repo}/git/blobs",
                    json={
                        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
                        "encoding": "base64",
                    },
                )
                blob_resp.raise_for_status()
                entries.append({"path": path, "mode": "100644", "type": "blob",
                                "sha": blob_resp.json()["sha"]})

            tree_sha = base_tree_sha
            for start in range(0, len(entries), self.TREE_ENTRY_BATCH):
                tree_resp = client.post(
                    f"{self.BASE_URL}/repos/{owner}/{repo}/git/trees",
                    json={"base_tree": tree_sha,
                          "tree": entries[start:start + self.TREE_ENTRY_BATCH]},
                )
                tree_resp.raise_for_status()
                tree_sha = tree_resp.json()["sha"]

            new_commit_resp = client.post(
                f"{self.BASE_URL}/repos/{owner}/{repo}/git/commits",
                json={"message": message, "tree": tree_sha, "parents": [base_commit_sha]},
            )
            new_commit_resp.raise_for_status()
            new_commit = new_commit_resp.json()
            commit_sha = new_commit["sha"]

            update_resp = client.patch(
                f"{self.BASE_URL}/repos/{owner}/{repo}/git/refs/heads/{branch}",
                json={"sha": commit_sha, "force": False},
            )
            if update_resp.status_code in (404, 422):
                create_resp = client.post(
                    f"{self.BASE_URL}/repos/{owner}/{repo}/git/refs",
                    json={"ref": f"refs/heads/{branch}", "sha": commit_sha},
                )
                if create_resp.status_code == 422 and "already exists" in create_resp.text:
                    # The PATCH was refused and the ref is there, so it isn't missing — it
                    # has moved off the base we built on. Saying so beats "Reference already
                    # exists", which reads like a race and hides the real answer (pass the
                    # branch as `base_branch`, or delete it).
                    raise ValueError(
                        f"{owner}/{repo}:{branch} exists but has diverged from '{base}', and "
                        f"commit_tree never force-pushes. Re-run with base_branch='{branch}'."
                    )
                create_resp.raise_for_status()
            else:
                update_resp.raise_for_status()

        # File count only. The contents are customer source and converted output; neither
        # belongs in a log line.
        logger.info("Committed %d file(s) to %s/%s:%s as %s (base %s)",
                    len(entries), owner, repo, branch, commit_sha[:7], base)
        return {
            "commit_sha": commit_sha,
            "tree_sha": tree_sha,
            "branch": branch,
            "base_branch": base,
            "files": len(entries),
            "html_url": new_commit.get("html_url", ""),
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
        logger.info("Opened PR %s/%s#%s: %s -> %s", owner, repo, data.get("number"), head, base)
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
            logger.exception("Failed to post PR comment on %s", pr_url)
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
        """Fetch a GitHub issue (title, body, comments). Works unauthenticated
        for public repos."""
        p = self._parse_url_type(issue_url)
        owner, repo, number = p["owner"], p["repo"], p["number"]
        with httpx.Client(headers=self.headers, timeout=30) as client:
            resp = client.get(f"{self.BASE_URL}/repos/{owner}/{repo}/issues/{number}")
            self._raise_friendly(resp)
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
        """Fetch a single file from a GitHub repo. Works unauthenticated for
        public repos."""
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
            self._raise_friendly(resp)
            data = resp.json()
        content = base64.b64decode(data["content"]).decode("utf-8", errors="ignore")
        return {
            "path": path, "content": content,
            "name": data.get("name", ""), "size": data.get("size", 0),
            "url": file_url, "fetch_type": "file",
        }

    def get_repo_overview(self, repo_url: str) -> Dict[str, Any]:
        """Fetch README + architecture docs from a repo for architecture/security
        review. Works unauthenticated for public repos."""
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

    # A modernization run needs the whole tree, not a handful of docs. One zipball request
    # beats walking the trees API file by file (hundreds of requests against a 60/hr
    # unauthenticated limit), and it hands back exactly the shape `repo_ingest.from_zip`
    # already reads. The archive is held in memory — see MAX_REPO_ZIP_BYTES.
    MAX_REPO_ZIP_BYTES = 200_000_000

    def get_repo_zipball(self, repo_url: str, ref: str = "") -> bytes:
        """Download a repository as a zip. Returns the raw archive bytes."""
        p = self._parse_url_type(repo_url)
        owner, repo = p["owner"], p["repo"]
        url = f"{self.BASE_URL}/repos/{owner}/{repo}/zipball"
        if ref:
            url = f"{url}/{ref}"
        # Long timeout and follow_redirects: GitHub answers this with a 302 to a
        # codeload.github.com URL, and a large repository takes real time to stream.
        with httpx.Client(headers=self.headers, timeout=300, follow_redirects=True) as client:
            with client.stream("GET", url) as resp:
                self._raise_friendly(resp)
                chunks, total = [], 0
                for chunk in resp.iter_bytes():
                    total += len(chunk)
                    if total > self.MAX_REPO_ZIP_BYTES:
                        raise ValueError(
                            f"{owner}/{repo} is larger than the {self.MAX_REPO_ZIP_BYTES // 1_000_000} MB "
                            f"import limit. Upload a zip of the subtree you want to convert instead."
                        )
                    chunks.append(chunk)
        logger.info("Fetched %s/%s zipball (%d bytes)", owner, repo, total)
        return b"".join(chunks)

    def list_open_issues(self, repo_url: str, limit: int = 10) -> list:
        """List open issues (not PRs) for a repo, most recently updated first.
        Works unauthenticated for public repos."""
        p = self._parse_url_type(repo_url)
        owner, repo = p["owner"], p["repo"]
        limit = max(1, min(limit, 25))
        with httpx.Client(headers=self.headers, timeout=30) as client:
            resp = client.get(
                f"{self.BASE_URL}/repos/{owner}/{repo}/issues",
                params={"state": "open", "sort": "updated", "direction": "desc", "per_page": limit},
            )
            self._raise_friendly(resp)
            data = resp.json()
        logger.info("Fetched %d open issues from %s/%s", len(data), owner, repo)
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
        logger.info("fetch_for_agent: ref=%s type=%s agent=%s", ref, t, agent_name)
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
            logger.exception("Failed to post issue comment on %s", issue_url)
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
