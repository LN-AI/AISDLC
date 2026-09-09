"""Put generated code into a repository — a branch, and optionally a pull request.

Three agents produce whole files rather than findings: Codegen Scaffold, Migration Execute
and (through its own workflow) Legacy Modernization. Until now the first two could only be
downloaded as a zip, so the last mile of "generate code for this requirement" was a person
unzipping an archive and copying files by hand.

**This is not `fix_apply`, and the difference is the point.** Apply Fixes starts from a
review, runs two guardrails and a re-review over every patch, and only then writes. A
scaffold or a migrated file has had *no* review at all unless `auto_review` was ticked. So
the two are deliberately not equivalent doors:

- **A branch, never an existing one.** `open_pr` opens a pull request *from* the new branch;
  nothing here ever commits onto a branch somebody may have checked out. `fix_apply` has a
  `same_pr` delivery that does, and it is opt-in there because a review had already read
  that branch. There is no equivalent here.
- **The target branch is refused if it already exists**, rather than being appended to.
  Re-running a scaffold is cheap and easy to do twice; silently adding a second generation's
  files on top of the first is not something a person can see before it happens.
- **Whether the code was reviewed travels with it.** `reviewed` goes into the pull request
  body verbatim, so the person who has to merge it can see that nothing checked it.

Shared with `fix_apply`: `resolve_base_branch`. That single function is where **three**
separate live defects landed — a base branch derived by splitting a pull request's *title*
on `@` (D-315), the same split returning the *repository name* for a repo with no `@` in its
label (D-364), and a seven-character commit sha rejected as a bare 422 (also D-364). None
was visible offline, because every fix test stops before writing. A second copy of that
logic in a second writer is how all three come back, so there is one copy and `fix_apply`
calls it.

No model call happens here, so nothing is metered and no budget is reserved. What is
recorded is an `audit_event` **before** the write, for the reason `fix_apply._deliver` gives:
an *attempted* write to a customer's repository is itself the event, and a failure that
leaves no trace is worse than a failure.
"""
import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class DeliveryError(Exception):
    """A delivery could not be attempted. Carries a message meant for the person who asked."""


# Bounds on one delivery. Neither is a technical limit — both exist so a single click cannot
# push more than the person meant. A scaffold is 8-16 files by its own prompt and a
# single-file migration is one, so anything approaching these is a sign something else
# happened.
MAX_FILES = 200
MAX_TOTAL_CHARS = 4_000_000

# A branch name GitHub will accept and a human can read back. Deliberately narrower than
# git's own rules: the names this module generates are built from a review id and a project
# name, and a project name is free text.
_BRANCH_OK = re.compile(r"^[A-Za-z0-9._/-]{1,180}$")


def owner_repo(repo_url: str) -> tuple:
    """`https://github.com/owner/repo(.git)(/anything)` -> `("owner", "repo")`.

    Raises rather than returning a half-parsed pair: every caller is about to write, and an
    empty owner produces a 404 from a URL that looks almost right.
    """
    trimmed = (repo_url or "").strip().rstrip("/")
    tail = trimmed.split("github.com/")[-1].strip("/")
    owner, _, rest = tail.partition("/")
    repo = rest.split("/")[0]
    if repo.endswith(".git"):
        repo = repo[:-4]
    if not owner or not repo:
        raise DeliveryError(
            f"'{repo_url}' is not a GitHub repository URL. Expected something like "
            f"https://github.com/owner/repo.")
    return owner, repo


def resolve_base_branch(gh, owner: str, repo: str, candidate: str = "") -> str:
    """The branch a pull request should target, checked rather than assumed.

    `candidate` is whatever the caller derived — a ref off a source label, a branch the user
    typed, or nothing. It is used **only if GitHub agrees it is a branch**. That check is the
    whole function: a tag, a commit sha, a deleted branch and a plausible-looking string that
    was never a branch all fail identically, as a bare 422 from `POST /pulls` thirty lines
    away from whatever produced the name. Three separate defects in this codebase were that
    exact shape (D-314, D-315, D-364).

    Falls back to the repository's own default branch, asked for rather than guessed as
    "main" — a repository still on master fails the same opaque way.
    """
    candidate = (candidate or "").strip()
    if candidate and gh.branch_exists(owner, repo, candidate):
        return candidate
    if candidate:
        logger.info("'%s' is not a branch of %s/%s — falling back to the default branch",
                    candidate, owner, repo)
    return gh.get_default_branch(owner, repo)


def safe_path(path: str) -> str:
    """A repo-relative path a generated file may be written to.

    The Contents API takes the path straight into a URL, so an absolute path or a `..`
    segment is both a broken request and, on any caller that ever writes to disk, a
    traversal. Generated file paths come from a model, which is exactly the input that
    should not be trusted to stay inside the tree.
    """
    cleaned = (path or "").strip().replace("\\", "/").lstrip("/")
    if not cleaned:
        raise DeliveryError("A generated file has no path, so there is nowhere to write it.")
    parts = [p for p in cleaned.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise DeliveryError(f"'{path}' points outside the repository and was refused.")
    if re.match(r"^[A-Za-z]:", cleaned):
        raise DeliveryError(f"'{path}' is an absolute path and was refused.")
    return "/".join(parts)


def _check_files(files: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    if not files:
        raise DeliveryError("This run produced no files, so there is nothing to deliver.")
    if len(files) > MAX_FILES:
        raise DeliveryError(
            f"{len(files):,} files is more than one delivery will push ({MAX_FILES}). "
            f"Download the zip instead.")
    out, total = [], 0
    for f in files:
        content = f.get("content") or ""
        total += len(content)
        if total > MAX_TOTAL_CHARS:
            raise DeliveryError(
                f"These files total more than {MAX_TOTAL_CHARS:,} characters, which is more "
                f"than one delivery will push. Download the zip instead.")
        out.append({"path": safe_path(f.get("path") or ""), "content": content})
    seen = set()
    for f in out:
        if f["path"] in seen:
            raise DeliveryError(
                f"Two generated files claim the path '{f['path']}'. Refusing rather than "
                f"letting the second silently overwrite the first.")
        seen.add(f["path"])
    return out


def branch_name(prefix: str, review_id: str) -> str:
    """The branch one delivery writes to. Carries the run id so it can be traced back."""
    name = f"{prefix}/{(review_id or 'run')[:12]}"
    if not _BRANCH_OK.match(name):
        raise DeliveryError(f"'{name}' is not a usable branch name.")
    return name


def deliver(files: List[Dict[str, Any]], repo_url: str, *,
            project_id: Optional[str] = None,
            branch: str = "",
            base_branch: str = "",
            commit_message: str = "",
            open_pr: bool = False,
            pr_title: str = "",
            pr_body: str = "",
            token: str = "",
            review_id: str = "",
            review_type: str = "",
            actor: str = "system") -> Dict[str, Any]:
    """Commit `files` to a new branch of `repo_url`, and optionally open a pull request.

    Every failure before the first commit raises `DeliveryError` with a sentence. After the
    first commit there is nothing to roll back, so a later failure returns what did land
    rather than pretending none of it did — a half-written branch the caller does not know
    about is the worst outcome available.
    """
    from backend import database
    from backend.services import source_bundle

    owner, repo = owner_repo(repo_url)
    checked = _check_files(files)

    if not _BRANCH_OK.match(branch or ""):
        raise DeliveryError(
            f"'{branch}' is not a usable branch name. Letters, digits, dot, dash, "
            f"underscore and slash only.")

    try:
        if token:
            # A token supplied for this call only, the same override the review screens
            # accept. Routed through `for_project` rather than constructed directly so it is
            # labelled in logs the way every other per-call token is; never stored.
            from backend.services.github_service import GitHubService
            gh = GitHubService.for_project(project_id, f"{owner}/{repo}",
                                           override_token=token)
        else:
            gh = source_bundle.github_for_repo(repo_url, project_id)
    except Exception:
        raise DeliveryError(f"Could not reach {owner}/{repo} with any configured credential.")
    if not getattr(gh, "token", ""):
        raise DeliveryError(
            "No GitHub token is available for this repository. Save one for the project, "
            "supply one for this call, or set GITHUB_TOKEN.")

    base = resolve_base_branch(gh, owner, repo, base_branch)
    if branch == base:
        raise DeliveryError(
            f"'{branch}' is the branch this would be merged into. Generated code goes to a "
            f"new branch so somebody can read it first.")
    if gh.branch_exists(owner, repo, branch):
        raise DeliveryError(
            f"Branch '{branch}' already exists in {owner}/{repo}. Refusing rather than "
            f"committing a second generation on top of the first — delete it, or rename "
            f"this one, and try again.")

    # Before the write, at the one funnel every delivery passes through, for the reason
    # `fix_apply._deliver` records: an attempted write to a customer's repository is itself
    # the governance event, and a failure that leaves no record is worse than a failure.
    database.audit_event(
        "generated_code_delivered", "review", review_id or "unsaved",
        actor=actor, project_id=project_id,
        details=(f"repo={owner}/{repo} branch={branch} base={base} files={len(checked)} "
                 f"open_pr={bool(open_pr)} review_type={review_type or 'unknown'}"))

    head_sha = gh.get_branch_head_sha(owner, repo, base)
    gh.create_branch_at_sha(owner, repo, branch, head_sha)

    message = commit_message or f"Add generated code from run {(review_id or '')[:8]}".strip()
    committed, failed = [], []
    for f in checked:
        try:
            gh.commit_file(owner, repo, branch, f["path"], f["content"], message)
            committed.append(f["path"])
        except Exception as exc:  # noqa: BLE001
            logger.exception("Could not commit %s to %s/%s:%s", f["path"], owner, repo, branch)
            failed.append({"path": f["path"], "error": str(exc)})

    result = {
        "repo": f"{owner}/{repo}",
        "branch": branch,
        "base_branch": base,
        "branch_url": f"https://github.com/{owner}/{repo}/tree/{branch}",
        "files_committed": committed,
        "files_failed": failed,
        "pull_request": None,
    }
    if not committed:
        raise DeliveryError(
            f"The branch was created but no file could be committed to it. First error: "
            f"{failed[0]['error'] if failed else 'unknown'}")

    if open_pr:
        try:
            result["pull_request"] = gh.create_pull_request(
                owner, repo, pr_title or message, pr_body or "", branch, base)
        except Exception as exc:  # noqa: BLE001
            # The commits landed. Saying "delivery failed" here would send someone looking
            # for a branch they would be told does not exist.
            logger.exception("Committed to %s but could not open a pull request", branch)
            result["pull_request_error"] = str(exc)
    return result


def pr_body_for(review_type: str, review_id: str, reviewed: str, summary: str = "") -> str:
    """What the pull request says about where this code came from and what checked it.

    `reviewed` is the honest half. A scaffold run without `auto_review` has had nothing look
    at it, and the person being asked to merge it is entitled to know that before they read
    a line — so it is stated in the body rather than left to be inferred from the absence of
    a review link.
    """
    kind = {"codegen_scaffold": "Generated starter project",
            "migration_execute": "Migrated code"}.get(review_type, "Generated code")
    lines = [
        f"## {kind}",
        "",
        f"Produced by the LM AI-SDLC platform from run `{review_id}`.",
        "",
        f"**Review status:** {reviewed}",
        "",
        "This code was written by a language model. Nothing on this branch has been "
        "compiled or executed by the platform.",
    ]
    if summary:
        lines += ["", "---", "", summary]
    return "\n".join(lines)
