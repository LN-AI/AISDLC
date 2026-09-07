"""One way to get code in front of a review agent, whatever the customer had.

Until now each agent had its own idea of what an input was. Code Review took a paste, one
file, or one PR diff. VAPT took a paste or a single uploaded file. `github_repo_url` on
VAPT/Security/Architecture went to `github_service.get_repo_overview`, which reads
`README.md` plus up to four documentation files and no source at all — while the request
model advertised it as a "full-repo assessment" (C-74). A zip could only be uploaded to
modernization.

This module is the single intake. Every source lands as the same `Bundle`, and every agent
reads a `Bundle`.

Three things it does that the old per-agent paths did not:

**It reuses the safe ingester rather than writing a second one.**
`modernization/repo_ingest.py` already refuses traversal paths and symlinks, skips
credential files outright, redacts hardcoded secrets *before* the text can reach a prompt,
and caps member size so a zip bomb is refused rather than decompressed. Reimplementing any
of that here would mean two ingesters that drift apart the first time either is fixed, and
the weaker one would be the one guarding code review. `repo_ingest` was written for
conversion, so its default extension list is legacy-heavy and includes config and schema
files; review wants a different default (see `REVIEW_EXTENSIONS`), which is a parameter it
already accepts.

**It says what it did not read.** `stats` and `skipped` travel with the bundle to the
caller and into the response. "We ignored 1,200 files" has to be visible, because a Green
gate over a subset is the worst output this product can produce and the reader cannot
otherwise tell it from a Green gate over everything.

**It keeps files as files.** `text` exists for evidence verification, which needs one
haystack to search, but `files` is what the chunker packs — so a finding's `file_path` is
the real path from the archive rather than something the model inferred from a header it
read in a concatenated blob.
"""
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from backend.services import suppression
from backend.services.modernization import repo_ingest

logger = logging.getLogger(__name__)

# What a security review reads. Narrower than `repo_ingest.DEFAULT_EXTENSIONS`, which is
# tuned for conversion and therefore carries `.sln`, `.csproj`, `.jcl` and friends — a
# reviewer has nothing to say about a solution file. Config formats stay in, because
# hardcoded credentials and permissive CORS live in them, and they are cheap to read.
REVIEW_EXTENSIONS = {
    # application source
    ".py", ".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs", ".java", ".go", ".rb", ".cs",
    ".vb", ".kt", ".kts", ".swift", ".scala", ".rs", ".c", ".h", ".cpp", ".hpp", ".cc",
    ".m", ".mm", ".php", ".php5", ".php7", ".phtml", ".pl", ".pm", ".sh", ".bash", ".ps1",
    ".lua", ".dart", ".ex", ".exs", ".erl", ".clj", ".groovy", ".r",
    # templates, where XSS and SSTI live
    ".html", ".htm", ".vue", ".svelte", ".jsp", ".asp", ".aspx", ".cshtml", ".erb",
    ".twig", ".hbs", ".ejs", ".tpl",
    # queries and migrations
    ".sql", ".pls", ".pks", ".pkb", ".prc",
    # configuration, where secrets and misconfigurations live
    ".json", ".yaml", ".yml", ".xml", ".ini", ".cfg", ".conf", ".toml", ".properties",
    ".tf", ".tfvars", ".gradle",
}

# The scan is capped before it starts rather than after it has spent. A repository over
# this size is a "narrow the scope" conversation, not a silent partial review.
MAX_REVIEW_FILES = 1_500
MAX_REVIEW_CHARS = 8_000_000

# Where each source type's content is data written by a third party rather than by the
# caller. Mirrors `v1_agents._UNTRUSTED_LABELS`; a bundle carries its own label so a caller
# cannot forget to wrap one.
UNTRUSTED_LABELS = {
    "github_pr": "GITHUB_PR_DIFF",
    "github_repo": "GITHUB_REPO",
    "github_file": "GITHUB_FILE",
    "github_compare": "GITHUB_PR_DIFF",
    "zip": "UPLOADED_ARCHIVE",
}


class SourceError(Exception):
    """A source could not be loaded. Carries a message meant for the person who asked."""


@dataclass
class Bundle:
    """Normalized source, plus an honest account of what was left out."""

    source_type: str                                   # text | zip | github_pr | github_repo | ...
    label: str = ""                                    # what to show a human: repo name, PR title
    ref: str = ""                                      # postback target (PR url) where one exists
    files: List[Dict[str, Any]] = field(default_factory=list)
    skipped: List[Dict[str, str]] = field(default_factory=list)
    stats: Dict[str, Any] = field(default_factory=dict)
    # Set when the source itself was cut before we saw all of it — a diff over the fetch
    # cap, a file list over MAX_REVIEW_FILES. Distinct from `skipped`, which is deliberate
    # filtering. The gate clamps on this; it does not clamp on deliberate filtering.
    truncated: bool = False
    truncation_reason: str = ""
    is_diff: bool = False
    # Extra facts a caller may want in the response but that are not part of the review:
    # PR number, head branch, commit sha.
    meta: Dict[str, Any] = field(default_factory=dict)
    # What the repository itself says not to gate on — its `.lmignore` and its inline
    # `lm-ignore` comments. A `suppression.Suppressions`, or None when the source has no
    # repository to have said anything (a paste). Carried here rather than looked up in the
    # agent because this is the only layer that knows what kind of source this is, and the
    # two source shapes find the ignore file two different ways: a file list already
    # contains it, a diff has to be asked for it.
    suppression: Any = None

    @property
    def total_chars(self) -> int:
        return sum(len(f.get("content") or "") for f in self.files)

    @property
    def untrusted_label(self) -> Optional[str]:
        return UNTRUSTED_LABELS.get(self.source_type)

    def source_ref(self) -> Dict[str, Any]:
        """Which repository, branch, commit and pull request this bundle came from.

        Every fact here is already in `meta` — but each GitHub constructor spells it
        slightly differently (`ref` on a repo, `head_branch` on a pull request, `head` on a
        compare), so a caller that wanted "which repo was this" had to know which of the
        three shapes it was holding. One method rather than that knowledge at every call
        site, which is the same reason `commit_sha` is normalized in `from_github_pr`.

        Empty strings, not None, for a source that has no repository (a paste, an upload) —
        `save_review` stores them as NULL, and a caller can treat the whole dict as absent
        by testing `repo_name`.
        """
        meta = self.meta or {}
        repo_url = (meta.get("repo_url") or "").rstrip("/")
        owner, name = "", ""
        if "github.com/" in repo_url:
            owner_repo = repo_url.split("github.com/")[-1].strip("/")
            owner, _, name = owner_repo.partition("/")
            name = name.split("/")[0].removesuffix(".git")
        # The branch a human would name this review by. A pull request is its head branch,
        # a compare is its head, a repository is whatever ref was asked for.
        branch = (meta.get("head_branch") or meta.get("head") or meta.get("ref") or "")
        pr_number = meta.get("pr_number")
        return {
            "repo_host": "github.com" if repo_url else "",
            "repo_owner": owner,
            "repo_name": name,
            "branch": branch or "",
            "commit_sha": meta.get("commit_sha") or "",
            "pr_number": int(pr_number) if str(pr_number or "").isdigit() else None,
        }

    def text(self) -> str:
        """One document, for the evidence verifier to search.

        `finding_pipeline.verify_evidence` needs a single haystack — a quote either appears
        in it or the finding is discarded. It must be assembled the same way the chunker
        assembles its units, or a genuine quote from chunk 7 would fail to verify against
        it. Both use `file_header()`, which is why that is a function and not an f-string
        in two places.
        """
        if len(self.files) == 1 and not self.files[0].get("path"):
            return self.files[0].get("content") or ""
        return "\n\n".join(
            file_header(f["path"]) + (f.get("content") or "") for f in self.files
        )

    @property
    def files_covered(self) -> int:
        """How many of the customer's files this source covers.

        Not `len(self.files)`, which is the number of *artifacts* the chunker is handed — and
        for a diff that is always 1, because a unified diff is a single artifact holding
        every changed file. Reporting that as "1 file(s)" on a twelve-file pull request is
        true about our internal representation and false about the only question being asked.
        """
        changed = (self.meta or {}).get("files_changed") or []
        if self.is_diff and changed:
            return len(changed)
        return len(self.files)

    def summary(self) -> Dict[str, Any]:
        """The shape that goes on an API response and onto the screen."""
        return {
            "source_type": self.source_type,
            "label": self.label,
            # The customer's files, not our artifacts — see `files_covered`.
            "files_reviewed": self.files_covered,
            # Kept separately because the two genuinely differ for a diff and the arithmetic
            # downstream cares: one artifact is one chunk is one model call, which is why the
            # quote can honestly say "12 files, 1 model call" without contradicting itself.
            "artifacts": len(self.files),
            "is_diff": self.is_diff,
            "files_skipped": len(self.skipped),
            "total_chars": self.total_chars,
            "truncated": self.truncated,
            "truncation_reason": self.truncation_reason,
            # `redaction_map` is excluded: it is a working list (path, line, category) that
            # `secret_findings` turns into findings, and echoing it beside them would report
            # the same secret twice in two different shapes.
            **{k: v for k, v in self.stats.items()
               if k not in ("files_ingested", "files_skipped", "redaction_map")},
            # Capped: the full list can be thousands of rows and nobody reads row 900. The
            # count above is the number that matters; this is the sample that makes it
            # concrete.
            "skipped_sample": self.skipped[:25],
            # Two counts, not the whole report: how many rules the repository brought is a
            # fact about the *source* and is known before any model runs, which is what
            # this dict is. Which rules actually fired is a fact about the *result* and
            # lands there instead (`chunked_review.run` -> `result["suppression"]`).
            "ignore_rules": len(getattr(self.suppression, "rules", []) or []),
            "ignore_inline": len(getattr(self.suppression, "inline", []) or []),
        }


def file_header(path: str) -> str:
    """The one definition of how a file is announced inside an artifact.

    Shared by `Bundle.text()` and `code_chunker.chunk_files()` on purpose — the verifier
    searches the former and the model reads the latter, and a byte of difference between
    them turns a real quote into a discarded finding.
    """
    return f"===== FILE: {path} =====\n"


# ── Sources ───────────────────────────────────────────────────────────────


def from_text(text: str, label: str = "") -> Bundle:
    """Text the caller typed or pasted. Never wrapped as untrusted — it came from them."""
    if not (text or "").strip():
        raise SourceError("Nothing to review — the supplied text is empty.")
    from backend.services import code_chunker
    return Bundle(
        source_type="text",
        label=label or (text[:80].replace("\n", " ")),
        files=[{"path": "", "content": text}],
        stats={"files_ingested": 1},
        is_diff=code_chunker.is_unified_diff(text),
    )


def from_files(files: List[Dict[str, str]], label: str = "",
               source_type: str = "files", extensions: Optional[set] = None) -> Bundle:
    """An explicit `[{path, content}]` list — the shape a zip and a repo both reduce to."""
    if not files:
        raise SourceError("No files were supplied.")
    result = repo_ingest.normalize_files(
        files, extensions=extensions if extensions is not None else REVIEW_EXTENSIONS
    )
    kept, skipped, stats = result["files"], result["skipped"], result["stats"]
    redactions = result["redactions"]
    if not kept:
        raise SourceError(
            f"None of the {len(files)} file(s) supplied could be reviewed. "
            f"They were filtered as binary, generated, credential files, or file types this "
            f"review does not read. First reason: "
            f"{skipped[0]['reason'] if skipped else 'unknown'}."
        )

    truncated, reason = False, ""
    if len(kept) > MAX_REVIEW_FILES:
        truncated = True
        reason = (
            f"This source has {len(kept):,} reviewable files; the scan was limited to the "
            f"first {MAX_REVIEW_FILES:,}. Narrow the scope to cover the rest."
        )
        kept = kept[:MAX_REVIEW_FILES]

    running, capped = 0, []
    for f in kept:
        running += len(f.get("content") or "")
        if running > MAX_REVIEW_CHARS:
            truncated = True
            reason = (
                f"{reason} " if reason else ""
            ) + (
                f"The source exceeds the {MAX_REVIEW_CHARS:,}-character scan limit; "
                f"{len(kept) - len(capped):,} file(s) were not reviewed."
            )
            break
        capped.append(f)

    # The redaction map rides along so `secret_findings` can turn it into findings. It
    # carries path, line and category — never a value; see `repo_ingest.redact`.
    # `files` (raw) for the ignore file, `capped` (reviewed) for the inline comments —
    # see `suppression.from_files` for why the two lists differ.
    rules = suppression.from_files(files, comment_files=capped)

    kept_paths = {f["path"] for f in capped}
    stats = {
        **stats,
        "files_ingested": len(capped),
        "redaction_map": [r for r in redactions if r.get("path") in kept_paths],
    }
    logger.info(
        "source_bundle: %s -> %d file(s), %d skipped, %d secret(s) redacted%s",
        source_type, len(capped), len(skipped), stats.get("secrets_redacted", 0),
        " (TRUNCATED)" if truncated else "",
    )
    return Bundle(
        source_type=source_type, label=label, files=capped, skipped=skipped, stats=stats,
        truncated=truncated, truncation_reason=reason.strip(), suppression=rules,
    )


def from_zip_bytes(content: bytes, label: str = "", extensions: Optional[set] = None) -> Bundle:
    """An uploaded archive.

    `strip_common_root` because both GitHub's zipball and most hand-made archives wrap
    everything in one directory, and that prefix would otherwise appear on every finding's
    file path and in every PR comment.
    """
    try:
        raw = repo_ingest.strip_common_root(repo_ingest.from_zip(content))
    except repo_ingest.IngestError as exc:
        raise SourceError(str(exc)) from exc
    return from_files(raw, label=label or "uploaded archive",
                      source_type="zip", extensions=extensions)


def sha_from_zipball(content: bytes) -> str:
    """The commit a GitHub zipball was built from, read out of the archive itself.

    GitHub names the single root directory `{owner}-{repo}-{sha}`, so the exact commit is
    already in the bytes we downloaded — no second request, and no chance of resolving a
    branch to a *different* commit than the one we actually read, which a follow-up API call
    would risk on a busy branch.

    Why this matters at all: showing a file back later means fetching it again, and fetching
    a *branch* a week later returns today's content against last week's line numbers, so
    every finding points at the wrong code. Recording the sha is what makes the re-read
    honest. Returns "" when the archive is not shaped like a GitHub zipball, which is not an
    error — it only means files cannot be re-read for that run.
    """
    import io
    import re
    import zipfile
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            names = zf.namelist()
    except Exception:
        return ""
    if not names:
        return ""
    root = names[0].split("/")[0]
    tail = root.rsplit("-", 1)[-1] if "-" in root else ""
    return tail.lower() if re.fullmatch(r"[0-9a-fA-F]{7,40}", tail or "") else ""


def from_github_repo(github, repo_url: str, ref: str = "",
                     extensions: Optional[set] = None) -> Bundle:
    """A whole repository at a ref — the thing `github_repo_url` always claimed to be.

    Goes through the zipball rather than walking the trees API: one request instead of
    hundreds against a 60/hour unauthenticated limit, and it hands back exactly the shape
    `from_zip` reads. This is the same call modernization has used since it shipped, so it
    is not new risk.
    """
    try:
        archive = github.get_repo_zipball(repo_url, ref)
    except Exception as exc:
        raise SourceError(f"Could not download {repo_url}: {exc}") from exc
    commit_sha = sha_from_zipball(archive)
    bundle = from_zip_bytes(archive, label=repo_url, extensions=extensions)
    bundle.source_type = "github_repo"
    bundle.label = repo_url.rstrip("/").split("/")[-1] + (f"@{ref}" if ref else "")
    bundle.ref = repo_url
    bundle.meta = {"repo_url": repo_url, "ref": ref, "commit_sha": commit_sha,
                   **(github.describe_credential() if hasattr(github, "describe_credential") else {})}
    return bundle


def github_for_repo(repo_url: str, project_id: Optional[str] = None) -> Any:
    """A GitHub client scoped to one repository, without a request object to read it from.

    `github_for` takes the whole request because it has several fields to look through.
    Re-reading a file for an already-finished review has only the repository, so this is the
    same credential resolution reached directly.
    """
    from backend.services.github_service import GitHubService
    return GitHubService.for_project(project_id, _owner_repo(repo_url))


def attach_repo_suppression(bundle: Bundle, github: Any) -> Bundle:
    """Read `.lmignore` out of the repository a **diff** came from.

    A pull request and a `base...head` compare are single unified diffs: there is no file
    list for the ignore file to be found in, so it has to be asked for. One extra Contents
    API call, on the commit the review is about — which is the same sha the source
    inventory records, so the rules are the ones that were in the tree at review time
    rather than whatever is on the branch now.

    Costs one request and fails open: a repository with no `.lmignore` answers 404, which
    is the overwhelmingly common case and must not turn into a failed review.

    Called from the constructors rather than from `resolve`, because `workflows/review_pr.py`
    and `workflows/autofix_pr.py` build their Bundle from a payload they already fetched
    and never go through `resolve` at all.
    """
    if bundle.suppression is not None or github is None:
        return bundle
    ref = bundle.source_ref()
    if not ref.get("repo_name"):
        return bundle
    bundle.suppression = suppression.fetch(
        github, ref["repo_owner"], ref["repo_name"],
        ref.get("commit_sha") or ref.get("branch") or "",
    )
    return bundle


def from_github_pr(github, pr_url: str) -> Bundle:
    """A pull request diff.

    One file, not many, and `is_diff` set: a unified diff is a single artifact with its own
    structure, and `code_chunker` already splits it per file and per hunk using the `@@`
    headers. Splitting it here into pseudo-files would throw that away and break the line
    numbers.
    """
    data = github.get_pr_diff(pr_url)
    return pr_bundle(data, pr_url, github=github)


def pr_bundle(data: Dict[str, Any], pr_url: str, github: Any = None) -> Bundle:
    """The same Bundle, from a payload somebody already fetched.

    Split out because `workflows/review_pr.py` and `workflows/autofix_pr.py` call
    `get_pr_diff` themselves and then need a Bundle to record a source inventory from —
    fetching the diff a second time to get one would double the request and, on a busy
    branch, could return a *different* diff than the one being reviewed.

    `github` is optional and only used to read `.lmignore` out of the repository. A caller
    that does not pass it gets a Bundle with no repository suppression rules — the review
    still runs, and inline `lm-ignore` comments inside the diff still work, because those
    are read out of the finding's own evidence quote.
    """
    diff = data.get("diff") or ""
    if not diff.strip():
        raise SourceError(f"{pr_url} has no reviewable diff — it may be empty or binary-only.")
    bundle = Bundle(
        source_type="github_pr",
        label=data.get("title") or pr_url,
        ref=pr_url,
        files=[{"path": "", "content": diff}],
        stats={"files_ingested": 1, "files_changed": len(data.get("files_changed") or [])},
        truncated=bool(data.get("diff_truncated")),
        truncation_reason=(
            f"The PR diff was truncated to {len(diff):,} of "
            f"{data.get('diff_full_chars', 0):,} characters; the remainder was not reviewed."
            if data.get("diff_truncated") else ""
        ),
        is_diff=True,
        meta={
            **{k: data.get(k) for k in
               ("pr_number", "title", "head_branch", "base_branch", "author",
                "files_changed", "additions", "deletions")},
            # `commit_sha` is the field every consumer reads (the inventory, the re-fetch,
            # the fix branch's base), so a PR names it the same way a repository does rather
            # than making each caller know which shape it is looking at.
            "commit_sha": data.get("head_sha") or "",
            "repo_url": pr_url.split("/pull/")[0],
            # What decides whether a fix may be committed into this PR rather than into a
            # new one. Carried on the bundle so the inventory records it and the choice can
            # be offered honestly later, without re-fetching the pull request.
            "head_branch": data.get("head_branch") or "",
            "from_fork": bool(data.get("from_fork")),
            "maintainer_can_modify": bool(data.get("maintainer_can_modify")),
        },
    )
    return attach_repo_suppression(bundle, github)


def from_branch_compare(github, repo_url: str, base: str, head: str) -> Bundle:
    """`base...head` as a diff — reviewing a branch before anyone opens a PR.

    The most-requested shape that had no route at all: a team wants the gate's answer
    *before* they raise the PR, and the only way to get one was to open the PR first.
    """
    data = github.get_compare_diff(repo_url, base, head)
    diff = data.get("diff") or ""
    if not diff.strip():
        raise SourceError(
            f"There is no difference between {base} and {head} in {repo_url}, "
            f"so there is nothing to review."
        )
    bundle = Bundle(
        source_type="github_compare",
        label=f"{repo_url.rstrip('/').split('/')[-1]} {base}...{head}",
        files=[{"path": "", "content": diff}],
        stats={"files_ingested": 1, "files_changed": len(data.get("files_changed") or [])},
        truncated=bool(data.get("diff_truncated")),
        truncation_reason=data.get("truncation_reason", ""),
        is_diff=True,
        meta={
            **{k: data.get(k) for k in ("base", "head", "files_changed", "commits")},
            "commit_sha": data.get("head_sha") or "",
            "repo_url": repo_url,
        },
    )
    return attach_repo_suppression(bundle, github)


def from_github_file(github, file_url: str) -> Bundle:
    """One file at a blob URL. Kept because it is already a documented input."""
    data = github.get_file_content(file_url)
    content = data.get("content") or ""
    if not content.strip():
        raise SourceError(f"{file_url} is empty.")
    return Bundle(
        source_type="github_file",
        label=data.get("name") or file_url,
        files=[{"path": data.get("path") or data.get("name") or "", "content": content}],
        stats={"files_ingested": 1},
    )


# ── Resolution ────────────────────────────────────────────────────────────


def _owner_repo(url: str) -> str:
    """`owner/repo` from any GitHub URL, for credential scoping. Best effort."""
    if not url:
        return ""
    parts = [p for p in url.split("//")[-1].split("/") if p]
    # parts[0] is the host; the two after it are owner and repo on every GitHub URL shape.
    return "/".join(parts[1:3]).lower() if len(parts) >= 3 else ""


def github_for(req, project_id: Optional[str], fallback) -> Any:
    """The GitHub client this request should use.

    Picks the credential scoped to the repository being reached, so a customer holding one
    token per organisation gets the right one — reaching for the wrong token produces a 404
    that reads exactly like "the repository does not exist" (C-76). Falls back to the shared
    client when the request names no GitHub source at all, so nothing changes for a paste.
    """
    target = _owner_repo(
        getattr(req, "github_repo_url", None)
        or getattr(req, "github_pr_url", None)
        or getattr(req, "github_file_url", None)
        or ""
    )
    override = (getattr(req, "github_token", None) or "").strip()
    if not target and not override:
        return fallback
    return type(fallback).for_project(project_id, target, override_token=override)


def resolve(req, github, extensions: Optional[set] = None,
            project_id: Optional[str] = None) -> Bundle:
    """Turn a request model into a Bundle, most specific source first.

    The order matters and matches the old `v1_agents._resolve` so no existing caller
    changes behaviour: an explicit PR beats a repo beats a file beats pasted text. `files`
    and `zip` are new and sit above the rest, because a caller who uploaded something meant
    it.

    `project_id` selects which GitHub credential is used. Passing it is what makes a
    repository review work for a customer whose repositories the deployment token cannot
    see; omitting it keeps the old single-token behaviour.
    """
    github = github_for(req, project_id, github)
    files = getattr(req, "files", None)
    if files:
        normalized = [
            {"path": getattr(f, "path", None) or (f.get("path") if isinstance(f, dict) else ""),
             "content": getattr(f, "content", None) or (f.get("content") if isinstance(f, dict) else "")}
            for f in files
        ]
        return from_files(normalized, label=f"{len(normalized)} file(s)", extensions=extensions)

    pr = getattr(req, "github_pr_url", None)
    if pr:
        return from_github_pr(github, pr)

    repo = getattr(req, "github_repo_url", None)
    if repo:
        base = getattr(req, "base_ref", None)
        head = getattr(req, "head_ref", None)
        if base and head:
            return from_branch_compare(github, repo, base, head)
        return from_github_repo(github, repo, getattr(req, "ref", "") or "",
                                extensions=extensions)

    gf = getattr(req, "github_file_url", None)
    if gf:
        return from_github_file(github, gf)

    for attr in ("code", "artifact", "text", "content"):
        val = getattr(req, attr, None)
        if val and str(val).strip():
            return from_text(str(val))

    raise SourceError(
        "No source provided. Supply pasted code, a file list, a zip, a GitHub PR URL, "
        "a GitHub repository URL, or a GitHub file URL."
    )


# ── Secrets: found by a parser, not asked of a model ──────────────────────
#
# `repo_ingest.normalize_files` redacts hardcoded credentials before the content is stored,
# so the value cannot reach a prompt, an event row or the usage ledger. That is exactly
# right for conversion, and on its own it would be *wrong* for a security review: a
# hardcoded credential is the single most valuable finding a code review produces, and
# redacting it means the reviewer looks at `password = "__REDACTED_ASSIGNED_CREDENTIAL__"`
# and has nothing to report.
#
# The resolution is not to stop redacting. It is that the redaction map is already a
# finding — better than the model's version of the same finding, in three ways:
#
#   * **Perfect precision on this class.** A regex either matched or it did not. There is no
#     hallucinated hardcoded secret.
#   * **It costs nothing.** The scan already ran, at ingest, before any model call.
#   * **The value never leaves the customer's repository.** The finding says *where* the
#     secret is and what kind it is. It never carries what it is.
#
# This is the same rule the modernization subsystem states as D-04 — never ask a model for
# what a parser answers exactly — arriving on the review side, where it also happens to be
# the cheapest accuracy improvement available.

# Category -> (control, severity, human title). Severity is per category on purpose: a live
# AWS key in source is not the same event as a password in a legacy `.ini`, and grading them
# alike is how a team learns to ignore both.
SECRET_FINDING_MAP = {
    "aws_access_key":     ("SEC-003", 5, "AWS access key hardcoded in source"),
    "private_key_block":  ("SEC-003", 5, "Private key embedded in source"),
    "github_token":       ("SEC-003", 5, "GitHub token hardcoded in source"),
    "provider_api_key":   ("SEC-003", 5, "Provider API key hardcoded in source"),
    "slack_token":        ("SEC-003", 5, "Slack token hardcoded in source"),
    "jwt":                ("SEC-003", 4, "Signed JWT embedded in source"),
    "assigned_credential": ("SEC-003", 4, "Credential assigned as a literal in source"),
    "connection_string":  ("SEC-003", 4, "Database password in a connection string"),
    "db_connect_password": ("SEC-003", 5, "Database password passed literally to a connect call"),
}

_RISK_LEVEL = {5: "Critical", 4: "High", 3: "Moderate", 2: "Minor", 1: "Low"}


def secret_findings(bundle: Bundle) -> List[Dict[str, Any]]:
    """Turn the ingest redaction map into findings the pipeline can carry.

    `evidence` is the *redacted* line — which is what the reviewer was shown and what the
    verifier checks against, so these verify like any other finding, and get fingerprints,
    baselines and waivers for free. It is also safe to store and to put in a PR comment,
    because the value is already gone from it.
    """
    redactions = bundle.stats.get("redaction_map") or []
    if not redactions:
        return []

    by_path: Dict[str, List[str]] = {
        f["path"]: (f.get("content") or "").splitlines() for f in bundle.files
    }
    out: List[Dict[str, Any]] = []
    for hit in redactions:
        path = hit.get("path") or ""
        category = hit.get("category") or "assigned_credential"
        control, severity, title = SECRET_FINDING_MAP.get(
            category, ("SEC-003", 4, "Credential hardcoded in source")
        )
        lines = by_path.get(path) or []
        line_no = hit.get("line") or 0
        evidence = ""
        if 0 < line_no <= len(lines) and "__REDACTED_" in lines[line_no - 1]:
            evidence = lines[line_no - 1].strip()[:300]
        else:
            # The line moved, which happens when a multi-line private-key block collapsed to
            # one placeholder above it. Find the placeholder rather than quoting the wrong
            # line; a wrong quote would be discarded by the verifier and the finding lost.
            marker = f"__REDACTED_{category.upper()}__"
            for i, text in enumerate(lines, start=1):
                if marker in text:
                    evidence = text.strip()[:300]
                    line_no = i
                    break

        out.append({
            "control_id": control,
            "severity": severity,
            "risk_level": _RISK_LEVEL[severity],
            "title": title,
            "description": (
                f"A {category.replace('_', ' ')} appears as a literal in {path}. The platform "
                f"redacted the value at ingest, so it is not stored here and was never sent "
                f"to a model — but it is still present in your repository, and in its history."
            ),
            "recommendation": (
                "Move the value into a secret manager or an environment variable, then "
                "**rotate it** — it must be treated as compromised, because it has been in "
                "source control. Removing the line does not remove it from git history."
            ),
            "is_blocking": severity >= 5,
            "file_path": path,
            "line_start": line_no or None,
            "evidence": evidence,
            "confidence": "High",
            # Marks this as found by a pattern rather than proposed by a model. The UI shows
            # it, because "a scanner found this" and "a model thinks this" deserve different
            # trust and the reader cannot otherwise tell.
            "detector": "pattern",
            "detector_rule": category,
        })
    logger.info("source_bundle: %d deterministic secret finding(s) from the ingest scan", len(out))
    return out


# Extensions that are archives rather than source. Checked by name *and* by magic bytes,
# because a caller renaming `app.py` to `app.zip` should still be told the truth.
_ZIP_MAGIC = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")


def looks_like_zip(content: bytes, filename: str = "") -> bool:
    """Whether these bytes are a zip archive, from the bytes themselves.

    The name is only a tiebreaker for an empty file. Trusting the extension is how a single
    source file named `.zip` produces "File is not a zip file" and a caller who did nothing
    wrong is told their upload is corrupt.
    """
    if content[:4] in _ZIP_MAGIC or any(content.startswith(m) for m in _ZIP_MAGIC):
        return True
    if not content.strip():
        return filename.lower().endswith(".zip")
    return False


def from_upload(content: bytes, filename: str = "", extensions: Optional[set] = None) -> Bundle:
    """One upload — an archive or a single source file — as a Bundle.

    Both go through here because the screen offers "Upload file" and "Upload zip" as separate
    modes and then posted both to the same endpoint, which read everything as an archive. A
    single file therefore failed with "That file is not a readable zip archive", which is
    true, unhelpful, and describes a mistake the caller did not make. Found by driving the
    real UI (`scripts/ui_examine.py`); no offline suite could see it, because the defect was
    in which endpoint the browser chose.

    A single file skips `REVIEW_EXTENSIONS` filtering. Someone who picked one file out of a
    file dialog has already said what they want reviewed, and refusing it because of its
    extension — after they chose it — is the tool arguing with an explicit instruction.
    """
    if looks_like_zip(content, filename):
        return from_zip_bytes(content, label=filename or "uploaded archive",
                              extensions=extensions)

    text = content.decode("utf-8", errors="replace")
    if not text.strip():
        raise SourceError(f"{filename or 'That file'} is empty.")
    if "\x00" in text[:8000]:
        raise SourceError(
            f"{filename or 'That file'} looks like a binary file, not source code. "
            f"Upload a text file, or a zip of the codebase."
        )
    path = (filename or "uploaded-file").replace("\\", "/").split("/")[-1]
    bundle = from_files([{"path": path, "content": text}], label=path,
                        source_type="upload", extensions=set())
    bundle.source_type = "upload"
    return bundle


def deterministic_findings(bundle: Bundle) -> Dict[str, Any]:
    """Everything a parser or a database can answer, before any model runs.

    Two contributors today, both following the same rule D-04 states for modernization —
    never ask a model for what an exact answer exists for:

    * **Hardcoded credentials**, from the ingest redaction scan. A regex matched or it did
      not, and the value never left the customer's repository.
    * **Known-vulnerable dependencies**, from the OSV database. Asking a model to recall a
      CVE database is asking it to be confidently wrong about versions and blind to anything
      published after its cutoff.

    Returns the findings plus `dependency`, which carries what *could not* be checked — a
    range with no lockfile to resolve it, or a database that could not be reached. That has
    to travel: a dependency scan that silently checked nothing is indistinguishable from one
    that found nothing, and only the first is a reason to worry.
    """
    from backend.services import dependency_scan

    findings = list(secret_findings(bundle))
    dependency = dependency_scan.scan(bundle.files)
    findings.extend(dependency.get("findings") or [])
    return {
        "findings": findings,
        "dependency": {
            "versions_checked": dependency.get("checked", 0),
            "manifests": dependency.get("manifests") or [],
            "vulnerable": len(dependency.get("findings") or []),
            "unresolved": dependency.get("unresolved") or [],
            "error": dependency.get("error", ""),
        },
    }
