import logging
import re
import os
import time

import httpx
from typing import Dict, Any, List, Optional
from backend import config

logger = logging.getLogger(__name__)


class GitHubService:
    BASE_URL = "https://api.github.com"

    # The old value here was 50,000, and the comment above it said why: "nothing downstream
    # chunks a diff yet". Something does — `code_chunker._split_diff` splits per file and
    # per hunk using the `@@` headers, and `chunked_review` fans the pieces out — so the cap
    # was outliving its reason and cutting large PRs to their first quarter (C-07).
    #
    # It is not removed, because a cap still does a job: it bounds one fetch, and therefore
    # bounds how much a single button press can spend. What changed is that the bound is now
    # generous enough to cover essentially every real PR (400,000 characters is roughly six
    # review chunks), and that the spend behind it is quoted to the caller before the review
    # runs — see `review_estimate`. Overridable so an operator can tighten or widen it
    # without a patch.
    MAX_DIFF_CHARS = int(os.getenv("MAX_DIFF_CHARS", "400000"))

    # GitHub returns 30 items per page by default. Nothing here asked for more, so
    # `files_changed` silently stopped at 30 on any larger PR — which matters less for
    # display than for accuracy: that list becomes the chunker's "files in this change you
    # were NOT shown" manifest, the thing that stops the model reporting a missing control
    # that lives in a file it never saw. A short list means more false positives.
    PER_PAGE = 100
    MAX_FILE_PAGES = 30          # 3,000 files; beyond that the manifest is not the problem

    def __init__(self, token: Optional[str] = None, token_source: str = "",
                 token_label: str = ""):
        """A client bound to one credential.

        `token=None` keeps the original behaviour — the deployment's `GITHUB_TOKEN` — so
        every existing caller is unchanged. `for_project` is the way to get a client that
        uses a customer's own credential (C-76): the deployment token was read once at
        import and shared by everyone, so two customers could not connect their own
        repositories and one tenant's access could not be revoked.

        `token_source` / `token_label` exist for the error messages. "Which credential did
        we even try" is the first question when a repository 404s, and it was previously
        unanswerable — a private repo and a typo produce the identical 404, because GitHub
        will not confirm that a repository exists to someone who cannot see it.
        """
        if token is None:
            self.token = config.GITHUB_TOKEN
            self.token_source = "deployment" if config.GITHUB_TOKEN else "none"
            self.token_label = "GITHUB_TOKEN from the environment" if config.GITHUB_TOKEN else ""
        else:
            self.token = token
            self.token_source = token_source or ("request" if token else "none")
            self.token_label = token_label
        self.headers = {
            "Accept": "application/vnd.github.v3+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            self.headers["Authorization"] = f"Bearer {self.token}"

    @classmethod
    def for_project(cls, project_id: Optional[str], target: str = "",
                    override_token: str = "") -> "GitHubService":
        """A client using this project's credential for `target` (an owner or owner/repo).

        Resolution order is `database.resolve_github_token`'s: an exact `owner/repo`
        credential, then one for the owner, then the project's default, then the
        deployment's. `override_token` wins over all of it — the one-off case, mirroring how
        a per-request `llm_api_key` beats a stored BYOL connection.
        """
        if override_token:
            return cls(token=override_token, token_source="request",
                       token_label="a token supplied with this request")
        from backend import database
        resolved = database.resolve_github_token(project_id, target)
        return cls(token=resolved["token"], token_source=resolved["source"],
                   token_label=resolved.get("label") or "")

    def describe_credential(self) -> Dict[str, Any]:
        """What served this call. Safe to put on a response — never the token itself."""
        return {
            "token_source": self.token_source,
            "token_label": self.token_label,
            "authenticated": bool(self.token),
        }

    def _credential_sentence(self) -> str:
        if not self.token:
            return "No GitHub credential was used (none is configured for this project)."
        where = {
            "project": "this project's stored GitHub credential",
            "deployment": "the deployment-wide GITHUB_TOKEN",
            "request": "the token supplied with this request",
        }.get(self.token_source, "the configured GitHub credential")
        return f"The request used {where}" + (
            f" ({self.token_label})." if self.token_label and self.token_source == "project" else "."
        )

    def _raise_friendly(self, resp: httpx.Response) -> None:
        """`resp.raise_for_status()`, but with an explanation a person can act on.

        The old version only helped when there was **no** token, which left the common
        multi-tenant case — a token that exists but cannot see this repository — surfacing a
        raw httpx string. Worse, GitHub answers "not visible to you" with **404, not 403**,
        deliberately, so it will not confirm a private repository exists to someone who
        cannot see it. That means a private repo, a revoked token, a token belonging to a
        different org and a plain typo are all the same status code, and the message has to
        name all four rather than guess.
        """
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            status = resp.status_code
            cred = self._credential_sentence()

            if status == 404:
                raise ValueError(
                    f"GitHub returned 404 for {resp.request.url.path}. {cred} "
                    f"GitHub answers 404 rather than 403 for anything the credential cannot "
                    f"see, so this means one of: the repository is private and this "
                    f"credential has no access to it; the credential has expired or been "
                    f"revoked; it belongs to a different account or organisation; or the "
                    f"owner/repository name is wrong. Check the URL first, then add a GitHub "
                    f"credential for this project under Settings."
                ) from e

            if status in (401, 403):
                remaining = resp.headers.get("x-ratelimit-remaining")
                if remaining == "0":
                    reset = resp.headers.get("x-ratelimit-reset", "")
                    raise ValueError(
                        f"GitHub's rate limit is exhausted for this credential"
                        f"{f' (resets at epoch {reset})' if reset else ''}. {cred} "
                        f"An authenticated credential gets 5,000 requests an hour where an "
                        f"anonymous one gets 60 — adding a GitHub credential for this project "
                        f"is the fix if none is configured."
                    ) from e
                raise ValueError(
                    f"GitHub refused the request ({status}). {cred} "
                    f"The credential is most likely missing a scope this operation needs — "
                    f"reading a private repository needs `Contents: Read`, posting a review "
                    f"needs `Pull requests: Write`, and reporting a merge check needs "
                    f"`Commit statuses: Write`."
                ) from e

            if status == 451:
                raise ValueError(
                    "GitHub has made this repository unavailable (DMCA or legal hold)."
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

            files_data = self._paged(
                client, f"{self.BASE_URL}/repos/{owner}/{repo}/pulls/{pr_number}/files"
            )

        # A diff over the cap used to be sliced silently, so a large PR was reviewed on its
        # first slice and the result was reported as covering the whole PR — a clean gate on
        # code nobody looked at. The cut is still made, because a cap bounds what one button
        # press can spend, but it is announced in the text the model sees and flagged on the
        # payload so callers refuse to report Green over it. See MAX_DIFF_CHARS for why the
        # bound is now high enough that real PRs no longer reach it.
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
            # The commit this diff is of. It was in the response and discarded, and it is
            # what lets a file be read back later at the version that was reviewed — a
            # branch name cannot do that, because the branch moves.
            "head_sha": pr_data.get("head", {}).get("sha", ""),
            "base_sha": pr_data.get("base", {}).get("sha", ""),
            # Whether a fix may be committed into this pull request's own branch. Both were
            # in the response and dropped. A fork's head branch belongs to the contributor,
            # so pushing to it is refused unless they allowed maintainer edits -- and finding
            # that out by attempting the push means discovering it after the money is spent.
            "head_repo": ((pr_data.get("head") or {}).get("repo") or {}).get("full_name", ""),
            "base_repo": ((pr_data.get("base") or {}).get("repo") or {}).get("full_name", ""),
            "from_fork": bool(
                ((pr_data.get("head") or {}).get("repo") or {}).get("full_name")
                and ((pr_data.get("head") or {}).get("repo") or {}).get("full_name")
                != ((pr_data.get("base") or {}).get("repo") or {}).get("full_name")),
            "maintainer_can_modify": bool(pr_data.get("maintainer_can_modify")),
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
        """Create a new branch pointing at the current head of from_branch. Idempotent if it already exists.

        **`from_branch` is a branch NAME, not a commit sha.** It is resolved through
        `/git/ref/heads/<from_branch>`, so a sha passed here produces a request for a branch
        literally named `61f9b8f…` and GitHub answers 404. Use `create_branch_at_sha` when
        you have a commit — which is what you want whenever the point is to branch off the
        exact commit something was reviewed at.
        """
        if not self.token:
            raise ValueError("GITHUB_TOKEN is not configured")
        sha = self.get_branch_head_sha(owner, repo, from_branch)
        result = self.create_branch_at_sha(owner, repo, new_branch, sha)
        if not result["already_existed"]:
            logger.info("Created branch %s/%s:%s from %s", owner, repo, new_branch, from_branch)
        return result

    def get_default_branch(self, owner: str, repo: str) -> str:
        """The repository's own default branch.

        Worth a request rather than assuming "main": a repository still on master, or one
        whose default is develop, otherwise fails as an opaque 422 from the pull-request
        endpoint with nothing naming the branch that did not exist. Falls back to "main"
        only if the lookup itself fails, which keeps a caller working where it used to.
        """
        try:
            with httpx.Client(headers=self.headers, timeout=30) as client:
                resp = client.get(f"{self.BASE_URL}/repos/{owner}/{repo}")
                resp.raise_for_status()
                return resp.json().get("default_branch") or "main"
        except Exception:
            logger.warning("Could not read the default branch for %s/%s — assuming main",
                           owner, repo)
            return "main"

    def create_branch_at_sha(self, owner: str, repo: str, new_branch: str,
                             sha: str) -> Dict[str, Any]:
        """Create a branch pointing at an exact commit. Idempotent if it already exists.

        Split out from `create_branch` because Apply Fixes needs to branch off the commit a
        review actually read, not off whatever the branch has since become — a patch computed
        against last week's file and committed on top of this week's silently reverts
        somebody else's change. It was calling `create_branch` with a sha in the `from_branch`
        slot, which asked GitHub for a branch named after a commit and 404'd every time, so
        the `pr` and `branch` deliveries had never once succeeded (D-314).
        """
        if not self.token:
            raise ValueError("GITHUB_TOKEN is not configured")
        if not re.fullmatch(r"[0-9a-fA-F]{7,40}", sha or ""):
            # Caught here rather than at GitHub, which would answer 422 with a message about
            # a malformed ref and leave the caller guessing which argument was wrong.
            raise ValueError(
                f"create_branch_at_sha needs a commit sha, got {sha!r}. For a branch name, "
                f"call create_branch instead.")
        with httpx.Client(headers=self.headers, timeout=30) as client:
            resp = client.post(
                f"{self.BASE_URL}/repos/{owner}/{repo}/git/refs",
                json={"ref": f"refs/heads/{new_branch}", "sha": sha},
            )
            if resp.status_code == 422 and "already exists" in resp.text:
                logger.info("Branch %s/%s:%s already exists - reusing", owner, repo, new_branch)
                return {"branch": new_branch, "sha": sha, "already_existed": True}
            resp.raise_for_status()
        logger.info("Created branch %s/%s:%s at %s", owner, repo, new_branch, sha[:8])
        return {"branch": new_branch, "sha": sha, "already_existed": False}

    def get_file_content_at_ref(self, owner: str, repo: str, path: str, ref: str) -> Dict[str, Any]:
        """Fetch a single file's content directly by owner/repo/path/ref (avoids the URL parser's
        assumption that a branch name has no slashes).

        No token is required: the Contents API answers unauthenticated for a public
        repository, and refusing to try was wrong for the case this is most used in — reading
        a file back out of the same public repo a review just read through the zipball, which
        also needs no token. `_raise_friendly` turns GitHub's 404-for-everything-unreadable
        into a sentence that distinguishes "no such file" from "this credential cannot see
        it", which `raise_for_status` could not.
        """
        import base64
        with httpx.Client(headers=self.headers, timeout=30) as client:
            resp = self._get_with_retry(
                client,
                f"{self.BASE_URL}/repos/{owner}/{repo}/contents/{path}",
                params={"ref": ref},
            )
            self._raise_friendly(resp)
            data = resp.json()
        if isinstance(data, list):
            raise ValueError(f"{path} is a directory in {owner}/{repo}, not a file.")
        if data.get("encoding") != "base64" or not data.get("content"):
            raise ValueError(
                f"{path} has no readable text content — it may be binary, or larger than "
                f"the 1 MB the Contents API will return inline.")
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
            # The repository lookup itself must raise. It used to be swallowed by
            # `if repo_resp.is_success`, so a repository that does not exist — or that the
            # credential cannot see — returned a perfectly well-formed overview containing
            # "Description: N/A" and no documents, and the Architecture and Security agents
            # then reviewed nothing and reported on it. It also made the credential test lie:
            # "can read this repository" was true for every repository, readable or not.
            #
            # A *missing candidate document* is still tolerated below, which is the case this
            # leniency was for: almost no repository has an ARCHITECTURE.md.
            self._raise_friendly(repo_resp)
            repo_data = repo_resp.json()
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

    # Beyond this many findings the comment becomes a wall nobody scrolls, so the tail is
    # summarised as a count and the reader is pointed at the full result. Findings arrive
    # sorted severity-first, so what gets cut is always the least important.
    MAX_COMMENT_FINDINGS = 25

    def format_pr_review_comment(self, review_result: Dict[str, Any],
                                 result_url: str = "") -> str:
        """Format a review result as a GitHub comment.

        What this used to omit is the whole point of the rewrite (C-75). It printed severity,
        control, title, description and fix — and dropped `file_path`, `line_start` and
        `evidence`, every one of which the pipeline had established and line-corrected. So a
        developer read "SQL injection in a query built by string concatenation" and then went
        looking for it. It also said nothing about how much of the PR was actually reviewed,
        nothing about which findings were new, and nothing about the findings the verifier
        threw away — which is the strongest evidence the tool polices itself.
        """
        gate = review_result.get("gate_status", "Unknown")
        score = review_result.get("risk_score", 0) or 0
        findings = review_result.get("findings", []) or []
        coverage = review_result.get("coverage") or {}

        gate_emoji = {"Green": "✅", "Amber": "⚠️", "Red": "🔴"}.get(gate, "❓")
        severity_map = {5: "🔴 Critical", 4: "🟠 High", 3: "🟡 Moderate", 2: "🔵 Minor", 1: "⚪ Low"}

        lines = [
            f"## {gate_emoji} LM AI-SDLC Review — Gate: **{gate}** · Risk **{score:.0f}/100**",
            "",
        ]

        # The counts row. "3 new, 41 already seen" is the difference between a comment a team
        # acts on and the tenth identical comment they mute.
        tally = []
        if review_result.get("new_findings_count") is not None:
            tally.append(f"**{review_result['new_findings_count']} new**")
        if review_result.get("pre_existing_count"):
            tally.append(f"{review_result['pre_existing_count']} pre-existing")
        if review_result.get("waived_count"):
            tally.append(f"{review_result['waived_count']} waived")
        if review_result.get("suppressed_findings"):
            tally.append(f"{len(review_result['suppressed_findings'])} in test/vendor paths")
        if review_result.get("discarded_findings"):
            tally.append(
                f"{len(review_result['discarded_findings'])} discarded (quoted code not "
                f"found in the source)"
            )
        if tally:
            lines += [" · ".join(tally), ""]

        # Coverage, stated before the findings. A gate over part of a PR is a different claim
        # from a gate over all of it, and the reader cannot tell them apart otherwise.
        if coverage:
            total = coverage.get("chunks_total", 0)
            ok = coverage.get("chunks_succeeded", 0)
            if coverage.get("chunks_failed"):
                lines += [
                    f"> ⚠️ **Partial review.** {ok} of {total} piece(s) of this change were "
                    f"reviewed; {coverage['chunks_failed']} failed, so part of the diff was "
                    f"never examined.",
                    "",
                ]
            elif total > 1:
                lines += [f"_Reviewed in {total} pieces, all successful._", ""]
        if review_result.get("coverage_complete") is False and not coverage.get("chunks_failed"):
            lines += [
                "> ⚠️ **Partial review.** Part of this change was not provided to the "
                "reviewer, so this gate does not cover the whole diff.",
                "",
            ]

        if review_result.get("summary"):
            lines += [f"**Summary:** {review_result['summary']}", ""]

        if findings:
            shown = findings[:self.MAX_COMMENT_FINDINGS]
            lines.append(f"### Findings ({len(findings)})")
            lines.append("")
            for f in shown:
                sev_label = severity_map.get(f.get("severity", 1), "⚪ Unknown")
                blocking = " ⛔ BLOCKING" if f.get("is_blocking") else ""
                new_tag = " 🆕" if f.get("is_new") else ""
                lines.append(
                    f"**{sev_label}{blocking}{new_tag}** — `{f.get('control_id', 'N/A')}` "
                    f"{f.get('title', '')}"
                )
                # The location. The single most important thing this comment was missing.
                where = self._format_location(f)
                if where:
                    lines.append(where)
                if f.get("evidence"):
                    quoted = str(f["evidence"]).strip().splitlines()[:3]
                    lines += ["", "```", *quoted, "```"]
                if f.get("description"):
                    lines.append(f"> {f['description']}")
                if f.get("recommendation"):
                    lines.append(f"**Fix:** {f['recommendation']}")
                # Say when a finding is not proven. A reader who cannot tell a verified
                # finding from an inferred one treats them all as inferred.
                marks = []
                if f.get("evidence_verified") is False:
                    marks.append("evidence not verified against the source")
                if f.get("confidence") and f["confidence"] != "High":
                    marks.append(f"confidence: {f['confidence'].lower()}")
                if f.get("file_path_corrected"):
                    marks.append(f"re-filed from `{f.get('reported_file_path', '?')}`")
                if f.get("is_pre_existing"):
                    marks.append("pre-existing, not introduced by this change")
                if marks:
                    lines.append(f"_{' · '.join(marks)}_")
                lines.append("")
            if len(findings) > len(shown):
                lines += [
                    f"_…and {len(findings) - len(shown)} lower-severity finding(s) not shown "
                    f"here._",
                    "",
                ]
        else:
            lines += ["_No findings._", ""]

        recs = review_result.get("recommendations", []) or []
        if recs:
            lines.append("### Recommendations")
            lines += [f"{i}. {r}" for i, r in enumerate(recs[:10], 1)]
            lines.append("")

        lines.append("---")
        footer = "*Reviewed by [LM AI-SDLC ReviewOps Agent](https://learnedmachine.ai)"
        if result_url:
            footer += f" — [full result]({result_url})"
        footer += ".*"
        lines.append(footer)
        if review_result.get("review_id"):
            lines.append(f"<sub>Review ID: `{review_result['review_id']}`</sub>")

        return "\n".join(lines)

    @staticmethod
    def _format_location(f: Dict[str, Any]) -> str:
        path = (f.get("file_path") or "").strip()
        line = f.get("line_start")
        if not path and not isinstance(line, int):
            # A finding with no location at all — an architecture-level observation, or one
            # the model declined to place. Saying nothing is better than "unknown:0".
            return ""
        if path and isinstance(line, int) and line > 0:
            end = f.get("line_end")
            span = f"{line}" if not isinstance(end, int) or end <= line else f"{line}-{end}"
            return f"`{path}:{span}`"
        return f"`{path or 'line ' + str(line)}`"

    # ── Paging, comparison, and the write side of a review ─────────────────

    def _paged(self, client: httpx.Client, url: str, params: Optional[dict] = None) -> List[dict]:
        """Every page of a list endpoint, not just the first thirty.

        Stops at `MAX_FILE_PAGES` rather than following `Link` forever: a pull request with
        3,000 changed files is a conversation about scope, not a paging problem, and an
        unbounded loop here is an unbounded number of API calls against a rate limit.
        """
        out: List[dict] = []
        for page in range(1, self.MAX_FILE_PAGES + 1):
            resp = self._get_with_retry(
                client, url, {**(params or {}), "per_page": self.PER_PAGE, "page": page})
            self._raise_friendly(resp)
            batch = resp.json()
            if not isinstance(batch, list) or not batch:
                break
            out.extend(batch)
            if len(batch) < self.PER_PAGE:
                break
        return out

    # A connect/read timeout to api.github.com is a fact of life, not a defect, and it cost a
    # real verification run on 2026-09-02: `upsert_pr_comment` failed on the *read* half of
    # its find-then-edit, so an already-paid-for review was not posted. Retried only for
    # GETs, which are idempotent — a POST retry risks a duplicate comment or a second commit,
    # which is worse than the failure it would be papering over.
    GET_RETRIES = 2
    GET_RETRY_BACKOFF_SECONDS = 1.5

    def _get_with_retry(self, client: httpx.Client, url: str,
                        params: Optional[dict] = None) -> httpx.Response:
        """A GET that survives one transient. Status codes are returned untouched — this
        retries *transport* failures only, so a 404 still reaches `_raise_friendly` and gets
        its explanation rather than being retried into a timeout."""
        last: Optional[Exception] = None
        for attempt in range(self.GET_RETRIES + 1):
            try:
                return client.get(url, params=params)
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout,
                    httpx.RemoteProtocolError) as exc:
                last = exc
                if attempt == self.GET_RETRIES:
                    break
                delay = self.GET_RETRY_BACKOFF_SECONDS * (attempt + 1)
                logger.warning(
                    "GitHub GET %s failed (%s) — retrying in %.1fs (attempt %d of %d)",
                    url, type(exc).__name__, delay, attempt + 2, self.GET_RETRIES + 1)
                time.sleep(delay)
        raise ValueError(
            f"Could not reach GitHub after {self.GET_RETRIES + 1} attempt(s): "
            f"{type(last).__name__}. This is a network failure rather than a rejection — "
            f"nothing was written, so the operation is safe to repeat."
        ) from last

    def get_compare_diff(self, repo_url: str, base: str, head: str) -> Dict[str, Any]:
        """`base...head` as a unified diff — a review before anyone opens a PR.

        The most-asked-for shape that had no route: a team wants the gate's answer *before*
        raising the pull request, and the only way to get one was to raise it first. GitHub's
        compare endpoint returns the same diff media type as a PR, so everything downstream —
        the hunk splitter, the line numbers, the inline anchors — works unchanged.
        """
        p = self._parse_url_type(repo_url)
        owner, repo = p["owner"], p["repo"]
        # `...` is the three-dot form: changes on `head` since it diverged from `base`, which
        # is what a PR shows. Two dots would also report everything that landed on `base`
        # afterwards and read as the branch's own work.
        spec = f"{base}...{head}"
        with httpx.Client(headers=self.headers, timeout=60) as client:
            meta_resp = client.get(f"{self.BASE_URL}/repos/{owner}/{repo}/compare/{spec}")
            self._raise_friendly(meta_resp)
            meta = meta_resp.json()

            diff_resp = client.get(
                f"{self.BASE_URL}/repos/{owner}/{repo}/compare/{spec}",
                headers={**self.headers, "Accept": "application/vnd.github.v3.diff"},
            )
            self._raise_friendly(diff_resp)
            diff = diff_resp.text

        full = len(diff)
        truncated = full > self.MAX_DIFF_CHARS
        text = diff[:self.MAX_DIFF_CHARS]
        if truncated:
            logger.warning("Compare %s %s diff truncated: %d of %d chars",
                           repo_url, spec, self.MAX_DIFF_CHARS, full)
            text += (
                f"\n\n[TRUNCATED: this diff was cut off at {self.MAX_DIFF_CHARS:,} of "
                f"{full:,} characters. The remainder has NOT been provided — do not treat "
                f"anything below this point as reviewed or absent.]"
            )
        # The head commit of the comparison, so a file can be read back at the version that
        # was reviewed rather than at whatever the branch has since become. Taken from the
        # commits the compare API already returned — the last one is the head — rather than
        # resolving the ref again, which on a busy branch could answer with a different
        # commit than the diff above is actually of.
        _commits = meta.get("commits") or []
        head_sha = (_commits[-1].get("sha") if _commits else "") or ""
        return {
            "base": base, "head": head, "diff": text,
            "head_sha": head_sha,
            "diff_truncated": truncated, "diff_full_chars": full,
            "truncation_reason": (
                f"The {base}...{head} diff was truncated to {self.MAX_DIFF_CHARS:,} of "
                f"{full:,} characters; the remainder was not reviewed." if truncated else ""
            ),
            "files_changed": [f.get("filename", "") for f in (meta.get("files") or [])],
            "commits": len(meta.get("commits") or []),
            "url": f"https://github.com/{owner}/{repo}/compare/{spec}",
            "fetch_type": "compare",
        }

    # An HTML comment carrying our identity. Invisible in the rendered comment, and the only
    # reliable way to find our own previous comment again: matching on the visible heading
    # would also match a human who quoted us.
    COMMENT_MARKER = "<!-- lm-ai-sdlc:review -->"

    def upsert_pr_comment(self, pr_url: str, comment_body: str) -> Dict[str, Any]:
        """Edit our previous comment on this PR, or post the first one.

        Posting was an unconditional create, so reviewing the same PR five times left five
        comments, each contradicting the last, and the thread became something people mute
        (C-75). An active PR gets reviewed on every push, so this is the normal case rather
        than an edge one.

        Falls back to creating when the previous comment cannot be found or edited — a failed
        lookup must not mean no review is posted at all.
        """
        if not self.token:
            return {"posted": False, "reason": "GITHUB_TOKEN is not configured"}
        body = f"{self.COMMENT_MARKER}\n{comment_body}"
        try:
            p = self._parse_url_type(pr_url)
            owner, repo, number = p["owner"], p["repo"], p["number"]
            with httpx.Client(headers=self.headers, timeout=30) as client:
                existing = self._paged(
                    client, f"{self.BASE_URL}/repos/{owner}/{repo}/issues/{number}/comments"
                )
                mine = [c for c in existing if self.COMMENT_MARKER in (c.get("body") or "")]
                if mine:
                    # The oldest is the one to keep: its permalink may already be referenced
                    # from a Slack thread or a Jira ticket, and moving the review to a new
                    # comment id breaks those.
                    target = mine[0]
                    resp = client.patch(
                        f"{self.BASE_URL}/repos/{owner}/{repo}/issues/comments/{target['id']}",
                        json={"body": body},
                    )
                    resp.raise_for_status()
                    logger.info("Updated existing review comment %s on %s", target["id"], pr_url)
                    return {"posted": True, "updated": True,
                            "comment_url": resp.json().get("html_url", ""),
                            "superseded": len(mine) - 1}
                resp = client.post(
                    f"{self.BASE_URL}/repos/{owner}/{repo}/issues/{number}/comments",
                    json={"body": body},
                )
                resp.raise_for_status()
                return {"posted": True, "updated": False,
                        "comment_url": resp.json().get("html_url", "")}
        except Exception as exc:
            logger.exception("Failed to upsert review comment on %s", pr_url)
            return {"posted": False, "reason": str(exc)[:200]}

    # GitHub rejects a review with a very large `comments` array, and forty inline comments
    # is worse for a reader than none — the diff becomes unreadable. Both limits are one
    # decision: comment inline on what is worth interrupting someone for, summarise the rest.
    MAX_INLINE_COMMENTS = 20
    MIN_INLINE_SEVERITY = 3

    def post_pr_review(
        self,
        pr_url: str,
        summary_body: str,
        findings: List[dict],
        commit_sha: str = "",
        request_changes: bool = False,
    ) -> Dict[str, Any]:
        """Post findings as line-level review comments, with a summary body above them.

        Only a finding that survived evidence verification is anchored. An unverified or
        re-filed finding may name a line that is not the line, and an inline comment on the
        wrong line is read as the tool being wrong about the code — far more damaging than
        the same finding appearing in the summary.

        `event` is COMMENT by default. REQUEST_CHANGES is available but is not the default
        even for a Red gate: on many repositories that blocks the author until a human
        dismisses it, which is a policy decision for the customer, not for us. Merge blocking
        belongs to the check run (`set_commit_status`), which is designed for it.
        """
        if not self.token:
            return {"posted": False, "reason": "GITHUB_TOKEN is not configured"}
        owner, repo, number = self._parse_pr_url(pr_url)

        anchored, unanchored = [], []
        for f in findings or []:
            path = (f.get("file_path") or "").lstrip("./")
            line = f.get("line_start")
            severity = f.get("severity") or 0
            if (path and isinstance(line, int) and line > 0
                    and f.get("evidence_verified")
                    and severity >= self.MIN_INLINE_SEVERITY
                    and not f.get("suppressed_reason")
                    and len(anchored) < self.MAX_INLINE_COMMENTS):
                anchored.append({
                    "path": path,
                    "line": line,
                    "side": "RIGHT",
                    "body": self._inline_body(f),
                })
            else:
                unanchored.append(f)

        payload: Dict[str, Any] = {
            "body": f"{self.COMMENT_MARKER}\n{summary_body}",
            "event": "REQUEST_CHANGES" if request_changes else "COMMENT",
        }
        if commit_sha:
            payload["commit_id"] = commit_sha
        if anchored:
            payload["comments"] = anchored

        try:
            with httpx.Client(headers=self.headers, timeout=60) as client:
                resp = client.post(
                    f"{self.BASE_URL}/repos/{owner}/{repo}/pulls/{number}/reviews",
                    json=payload,
                )
                if resp.status_code == 422 and anchored:
                    # A line that is not part of the diff is rejected for the whole review.
                    # Retrying without anchors still delivers the findings, which matters
                    # more than where they appear.
                    logger.warning(
                        "PR review rejected with %d inline comment(s) (%s) — retrying as a "
                        "summary-only review", len(anchored), resp.text[:200],
                    )
                    payload.pop("comments", None)
                    resp = client.post(
                        f"{self.BASE_URL}/repos/{owner}/{repo}/pulls/{number}/reviews",
                        json=payload,
                    )
                    resp.raise_for_status()
                    return {"posted": True, "inline_comments": 0,
                            "inline_rejected": len(anchored),
                            "summary_only": len(findings or []),
                            "review_url": resp.json().get("html_url", "")}
                resp.raise_for_status()
                return {"posted": True, "inline_comments": len(anchored),
                        "summary_only": len(unanchored),
                        "review_url": resp.json().get("html_url", "")}
        except Exception as exc:
            logger.exception("Failed to post PR review on %s", pr_url)
            return {"posted": False, "reason": str(exc)[:200]}

    def _inline_body(self, f: dict) -> str:
        sev = {5: "Critical", 4: "High", 3: "Moderate", 2: "Minor", 1: "Low"}.get(
            f.get("severity", 1), "Finding")
        parts = [f"**{sev} · `{f.get('control_id', 'N/A')}`** — {f.get('title', '')}"]
        if f.get("description"):
            parts.append(str(f["description"]))
        if f.get("recommendation"):
            parts.append(f"**Fix:** {f['recommendation']}")
        if f.get("cvss_score") is not None:
            vector = f.get("cvss_vector")
            parts.append(f"CVSS {f['cvss_score']}" + (f" `{vector}`" if vector else ""))
        if f.get("confidence"):
            parts.append(f"_Confidence: {f['confidence']}_")
        return "\n\n".join(parts)

    def set_commit_status(
        self,
        owner: str,
        repo: str,
        sha: str,
        gate_status: str,
        description: str = "",
        target_url: str = "",
        context_label: str = "lm-ai-sdlc/review",
    ) -> Dict[str, Any]:
        """Report the gate to GitHub as a commit status, so a Red verdict can block a merge.

        This is the difference between a governance *tool* and a governance *control*. The
        product calls its verdict a gate; on GitHub it was a paragraph anyone could scroll
        past (C-75). A status under branch protection greys out the merge button.

        Commit statuses rather than check runs on purpose: a check run requires a GitHub App
        installation token, and the platform authenticates with a personal access token
        today. Statuses work with a PAT, need only `repo:status`, and satisfy branch
        protection identically. When per-tenant GitHub App credentials land, this is the
        function that gains a check-run branch.
        """
        if not self.token:
            return {"posted": False, "reason": "GITHUB_TOKEN is not configured"}
        # Amber is deliberately `success`, not `pending` or `failure`. `pending` never
        # resolves and blocks the PR forever; `failure` would make every advisory finding
        # block a merge, which is how a team turns the check off altogether. Amber is
        # reported as a passing check whose description says it needs a look.
        state = {"Red": "failure", "Amber": "success", "Green": "success"}.get(gate_status, "success")
        try:
            with httpx.Client(headers=self.headers, timeout=30) as client:
                resp = client.post(
                    f"{self.BASE_URL}/repos/{owner}/{repo}/statuses/{sha}",
                    json={
                        "state": state,
                        "description": (description or f"Gate: {gate_status}")[:140],
                        "context": context_label,
                        **({"target_url": target_url} if target_url else {}),
                    },
                )
                resp.raise_for_status()
            logger.info("Set commit status %s=%s on %s/%s@%s",
                        context_label, state, owner, repo, sha[:8])
            return {"posted": True, "state": state, "gate_status": gate_status}
        except Exception as exc:
            logger.exception("Failed to set commit status on %s/%s@%s", owner, repo, sha)
            return {"posted": False, "reason": str(exc)[:200]}

    def get_pr_head_sha(self, pr_url: str) -> str:
        owner, repo, number = self._parse_pr_url(pr_url)
        with httpx.Client(headers=self.headers, timeout=30) as client:
            resp = client.get(f"{self.BASE_URL}/repos/{owner}/{repo}/pulls/{number}")
            self._raise_friendly(resp)
            return (resp.json().get("head") or {}).get("sha", "")
