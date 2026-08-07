"""Turn a customer codebase into rows — safely — before any model sees a byte.

Three sources, one normalizer: a zip upload, an explicit `files[]` list, or a GitHub repo
(fetched as a zipball and then treated exactly like an upload). Everything downstream
consumes the same normalized `[{path, content, ...}]` shape.

Two things happen here that do not happen anywhere else in the platform:

**Path safety.** The CFR21 zip reader (`main._extract_zip_files`) does not need it, because
it never materialises a tree — it reads names into a prompt and throws them away. A
modernization run *derives output paths from input paths*, so a member called
`../../etc/cron.d/x` or `C:\\Windows\\...` is a real traversal primitive. Anything that is
absolute, escapes the root, or is a symlink is dropped, not sanitised: a path we had to
repair is a path we do not understand.

**Secret redaction at ingest.** `guardrail_service.scan_for_hardcoded_secrets` already
exists and works; it was simply wired in at the wrong end of the pipeline — *after*
generation, in `autofix_pr`. Legacy codebases are precisely where hardcoded connection
strings live (`.ini`, `.dfm`, `web.config`, COBOL copybooks), and on the old
`migration-execute` path those went straight into the prompt. Here the value is replaced
before it is stored, so it cannot reach a prompt, an event row, an artifact, or the usage
ledger. The redaction map records **path, line and category — never the value.**
"""
import hashlib
import io
import logging
import os
import re
import zipfile
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Directories that are never source. Matched as path segments, so `src/vendor/x` is
# excluded but `vendored_logic.py` is not.
EXCLUDED_DIRS = {
    ".git", ".svn", ".hg", "node_modules", "__pycache__", ".venv", "venv", "env",
    "dist", "build", "target", "bin", "obj", "vendor", "packages", "third_party",
    ".idea", ".vs", ".vscode", "coverage", "htmlcov", ".pytest_cache", ".mypy_cache",
}

# Extensions worth converting. Deliberately broad and legacy-heavy — this is the list the
# CFR21 reader could not use, because it contains no legacy extensions at all. Overridable
# per run: a COBOL shop's copybooks are `.cpy`, and nobody should have to patch the
# platform to convert them.
DEFAULT_EXTENSIONS = {
    # modern
    ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".go", ".rb", ".php", ".cs", ".vb",
    ".kt", ".swift", ".scala", ".rs", ".c", ".h", ".cpp", ".hpp", ".cc",
    # legacy / mainframe / 4GL
    ".pas", ".dpr", ".dfm", ".dpk", ".inc",          # Delphi / Pascal
    ".cob", ".cbl", ".cpy", ".jcl",                   # COBOL
    ".pl", ".pm", ".f", ".for", ".f90",               # Perl / Fortran
    ".asp", ".cfm", ".jsp", ".vbs", ".bas", ".cls",   # classic web / VB
    ".pls", ".pks", ".pkb", ".prc", ".sql",           # PL/SQL, T-SQL
    # config & schema that a conversion has to carry across
    ".json", ".yaml", ".yml", ".xml", ".ini", ".cfg", ".conf", ".toml", ".properties",
    ".csproj", ".vbproj", ".sln", ".gradle", ".pom",
}

# Files whose *contents* are a secret rather than merely containing one. These are skipped
# entirely — not redacted — because there is nothing left after redaction and no reason to
# convert them. Ported from the pattern the delphi-to-dotnet skill's security guardrails use.
FORBIDDEN_NAMES = re.compile(
    r"(^|/)("
    r"\.env(\..*)?|"
    r"id_rsa(\.pub)?|id_dsa|id_ecdsa|id_ed25519|"
    r".*\.(pem|pfx|p12|key|keystore|jks|ppk|asc|gpg)|"
    r"secrets?\.(ya?ml|json|ini|txt)|credentials(\.json)?"
    r")$",
    re.IGNORECASE,
)
FORBIDDEN_DIRS = {"secrets", "secret", ".ssh", ".gnupg", ".aws", ".azure"}

# What gets replaced before storage. A superset of guardrail_service._SECRET_PATTERNS,
# categorised — the redaction map has to say *what kind* of secret was at a location
# without saying what it was, so a reviewer knows whether the gap matters.
#
# A pattern may mark the secret itself with a `(?P<value>...)` group. When it does, only
# that group is replaced and the surrounding syntax survives, so `password = "hunter2"`
# becomes `password = "__REDACTED_ASSIGNED_CREDENTIAL__"` rather than losing the key
# entirely — converted code missing a config key is a bug the redaction caused.
_REDACTION_PATTERNS: List[Tuple[str, "re.Pattern"]] = [
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("private_key_block", re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----.*?-----END [^-]*-----",
        re.DOTALL)),
    ("github_token", re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}")),
    ("provider_api_key", re.compile(r"sk-[A-Za-z0-9_\-]{20,}")),
    ("slack_token", re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}")),
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
    # Assignment forms, which is how legacy config actually carries credentials.
    # `\w*` on both sides on purpose: legacy config calls it DB_PASSWORD, MailApiKey,
    # sSecretKey. Anchoring on a word boundary would miss every one of them, which is
    # precisely the population this pattern exists for.
    ("assigned_credential", re.compile(
        r"(?i)\w*(?:api[_-]?key|apikey|secret|token|passwd|password|pwd)\w*"
        r"\s*(?::=|=|:)\s*['\"](?P<value>[^'\"\r\n]{8,})['\"]")),
    ("connection_string", re.compile(
        r"(?i)\b(?:pwd|password)\s*=\s*(?P<value>[^;'\"\r\n]{4,}?)(?=\s*(?:;|$))")),
]

REDACTION_PLACEHOLDER = "__REDACTED_{category}__"

# Bounds. Generous, because content goes into rows rather than into a prompt — the only
# thing these protect is the database and the request that carries the upload.
MAX_FILES = 5000
MAX_FILE_CHARS = 400_000
MAX_TOTAL_CHARS = 60_000_000
MAX_ZIP_MEMBER_BYTES = 10_000_000       # a single member; guards against a zip bomb
MAX_ZIP_TOTAL_BYTES = 500_000_000       # uncompressed total, same reason


class IngestError(Exception):
    """The archive could not be read at all — as opposed to a member being skipped."""


def _is_excluded_dir(path: str) -> bool:
    parts = path.split("/")[:-1]
    return any(part in EXCLUDED_DIRS for part in parts)


def _is_forbidden(path: str) -> bool:
    if FORBIDDEN_NAMES.search("/" + path):
        return True
    return any(part.lower() in FORBIDDEN_DIRS for part in path.split("/")[:-1])


def safe_relpath(raw: str) -> Optional[str]:
    """Normalize an archive member name, or None if it is not safe to use as a path.

    Rejected rather than repaired: absolute paths, drive letters, UNC paths, and **any**
    `..` segment at all — including one that would normalize back inside the root. A source
    archive has no legitimate reason to contain one, and "it happens to resolve safely" is a
    much weaker guarantee than "it isn't there". A name we had to fix is a name whose intent
    we cannot vouch for, and the output tree is derived from these.
    """
    if not raw:
        return None
    path = raw.replace("\\", "/").strip()
    if not path or path.endswith("/"):
        return None
    if path.startswith("/") or path.startswith("//"):
        return None
    if re.match(r"^[A-Za-z]:", path):
        return None
    if any(part == ".." for part in path.split("/")):
        return None
    # normpath collapses `./` and doubled slashes. `lstrip("./")` is deliberately NOT used
    # here — it strips *characters*, so it turns `.env` into `env` and quietly defeats the
    # credential-file check further down.
    normalized = os.path.normpath(path).replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    if not normalized or normalized in (".", "..") or normalized.startswith("/"):
        return None
    return normalized


def redact(text: str, path: str) -> Tuple[str, List[Dict[str, Any]]]:
    """Replace secrets in `text` and report where they were.

    Returns `(redacted_text, [{path, line, category}])`. The value never appears in the
    return, is never logged, and is never persisted — the whole point is that after this
    function returns, the secret exists only in the customer's own repository.

    Detection and substitution are two separate passes, deliberately. Doing both in one
    loop means later patterns match against a string whose offsets have already shifted —
    and the private-key pattern spans lines, so every line number after the first key block
    would be wrong. Locations are therefore always measured against the original file,
    which is the file a reviewer will open. The cost is that two patterns matching the same
    secret report it twice; an over-reported redaction map is harmless, a misreported line
    number is not.
    """
    if not text:
        return text, []

    hits: List[Dict[str, Any]] = []
    seen = set()
    for category, pattern in _REDACTION_PATTERNS:
        for match in pattern.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            if (line, category) in seen:
                continue
            seen.add((line, category))
            hits.append({"path": path, "line": line, "category": category})

    redacted = text
    for category, pattern in _REDACTION_PATTERNS:
        placeholder = REDACTION_PLACEHOLDER.format(category=category.upper())
        if "value" in (pattern.groupindex or {}):
            def _replace(match, ph=placeholder):
                whole, start = match.group(0), match.start(0)
                return whole[:match.start("value") - start] + ph + whole[match.end("value") - start:]
            redacted = pattern.sub(_replace, redacted)
        else:
            redacted = pattern.sub(placeholder, redacted)

    if hits:
        logger.info("Redacted %d secret(s) from %s before storage", len(hits), path)
    return redacted, hits


def _looks_binary(raw: bytes) -> bool:
    return b"\x00" in raw[:8000]


def normalize_files(
    files: List[Dict[str, str]],
    extensions: Optional[set] = None,
) -> Dict[str, Any]:
    """The one normalizer. Every source funnels through here.

    Returns `{files, skipped, redactions, stats}`. `files` entries carry the **redacted**
    content plus a hash of it, so a re-ingest of an unchanged file is detectable without
    keeping the original anywhere.
    """
    allowed = extensions if extensions is not None else DEFAULT_EXTENSIONS
    kept: List[Dict[str, Any]] = []
    skipped: List[Dict[str, str]] = []
    redactions: List[Dict[str, Any]] = []
    total_chars = 0

    for entry in files:
        raw_path = entry.get("path") or ""
        path = safe_relpath(raw_path)
        if not path:
            skipped.append({"path": raw_path, "reason": "unsafe_path"})
            continue
        if _is_excluded_dir(path):
            skipped.append({"path": path, "reason": "excluded_directory"})
            continue
        if _is_forbidden(path):
            # Deliberately not redacted — see FORBIDDEN_NAMES.
            skipped.append({"path": path, "reason": "credential_file"})
            continue
        ext = os.path.splitext(path)[1].lower()
        if allowed and ext not in allowed:
            skipped.append({"path": path, "reason": f"extension_not_converted ({ext or 'none'})"})
            continue
        content = entry.get("content") or ""
        if not content.strip():
            skipped.append({"path": path, "reason": "empty"})
            continue
        if len(content) > MAX_FILE_CHARS:
            skipped.append({"path": path, "reason": f"too_large ({len(content)} chars)"})
            continue
        if len(kept) >= MAX_FILES:
            skipped.append({"path": path, "reason": "file_limit_reached"})
            continue
        if total_chars + len(content) > MAX_TOTAL_CHARS:
            skipped.append({"path": path, "reason": "total_size_limit_reached"})
            continue

        clean, hits = redact(content, path)
        redactions.extend(hits)
        kept.append({
            "path": path,
            "content": clean,
            "loc": clean.count("\n") + 1,
            "extension": ext,
            "source_hash": hashlib.sha256(clean.encode("utf-8", "ignore")).hexdigest()[:32],
        })
        total_chars += len(clean)

    stats = {
        "files_ingested": len(kept),
        "files_skipped": len(skipped),
        "total_chars": total_chars,
        "secrets_redacted": len(redactions),
        "credential_files_skipped": sum(1 for s in skipped if s["reason"] == "credential_file"),
        "unsafe_paths_rejected": sum(1 for s in skipped if s["reason"] == "unsafe_path"),
    }
    logger.info(
        "Ingest: %d file(s) kept, %d skipped, %d secret(s) redacted, %d credential file(s) skipped",
        stats["files_ingested"], stats["files_skipped"], stats["secrets_redacted"],
        stats["credential_files_skipped"],
    )
    return {"files": kept, "skipped": skipped, "redactions": redactions, "stats": stats}


def from_zip(content: bytes) -> List[Dict[str, str]]:
    """Decode a zip upload into raw `[{path, content}]`. **Does no filtering.**

    Deliberately not the normalizer: filtering happens in exactly one place
    (`normalize_files`), because a run may override which extensions it converts and a
    filter here would drop a COBOL shop's `.cpy` files before the override was ever
    consulted. This function's only jobs are decoding and archive-level safety.

    Member size is checked from the *header* before reading, so a zip bomb is refused
    rather than decompressed. Symlink members are dropped: their content is a path, and a
    path that gets written somewhere is the whole traversal problem again.
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise IngestError(f"That file is not a readable zip archive: {exc}") from exc

    files: List[Dict[str, str]] = []
    total_bytes = 0
    with archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            # High 16 bits of external_attr are the Unix mode; 0xA000 is S_IFLNK.
            if (info.external_attr >> 16) & 0xF000 == 0xA000:
                continue
            if info.file_size > MAX_ZIP_MEMBER_BYTES:
                continue
            total_bytes += info.file_size
            if total_bytes > MAX_ZIP_TOTAL_BYTES:
                logger.warning("Zip exceeded %d uncompressed bytes — stopping", MAX_ZIP_TOTAL_BYTES)
                break
            try:
                raw = archive.read(info)
            except Exception:
                continue
            if _looks_binary(raw):
                continue
            files.append({
                "path": info.filename,
                "content": raw.decode("utf-8", errors="replace"),
            })

    if not files:
        raise IngestError(
            "No readable text files were found in that archive. Check that it contains "
            "source code rather than build output or binaries."
        )
    return files


def strip_common_root(files: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Drop a single wrapping directory (`myrepo-a1b2c3/...`), which is how GitHub's
    zipball and most hand-made archives are shaped. Paths are what a reviewer reads and
    what the output tree mirrors, so carrying a meaningless prefix through all of it is
    worth one pass to avoid."""
    if not files:
        return files
    roots = {f["path"].split("/", 1)[0] for f in files if "/" in f["path"]}
    if len(roots) != 1 or any("/" not in f["path"] for f in files):
        return files
    return [{**f, "path": f["path"].split("/", 1)[1]} for f in files]
