"""Split a code artifact into reviewable chunks.

Until now the platform's answer to a large artifact was HTTP 413 with the message
"Split the artifact into smaller pieces" — manual work handed back to the caller, and the
reason a 200KB PR could only ever be reviewed on its first quarter.

Two things make chunking non-trivial here, and both are handled below:

1. **The binding constraint is output, not input.** The model has a large context window,
   but `max_tokens` caps a tool response at roughly 10-15 findings. Sizing chunks to fill
   the context would produce chunks whose findings cannot fit in one response. Chunks are
   therefore sized by *expected findings*, which in practice means a target measured in
   tens of thousands of input tokens, not hundreds.

2. **A chunk that doesn't know what it can't see invents findings.** "Missing auth check"
   when the check lives in a file that wasn't in this chunk is the archetypal false
   positive. Every chunk carries a header naming the files that exist elsewhere in the
   same artifact and instructing the model not to assert anything about them.

Splits never cut inside a diff hunk or a top-level definition, and every unit keeps its
absolute line numbers so a finding's line reference still means something in the original
artifact.
"""
import re
from dataclasses import dataclass, field
from typing import Callable, List, Optional

# Roughly 15-25K input tokens per chunk. Below this the per-chunk fixed cost (system
# prompt, schema, header) dominates; above it the findings stop fitting in one response.
DEFAULT_CHUNK_TOKENS = 20000

# When one unit is itself too big to fit, it is cut into line windows that overlap by this
# much, so a vulnerability spanning the cut is fully visible in at least one window.
DEFAULT_OVERLAP_LINES = 30

# Cheap, deterministic, and good enough for packing decisions. The real count (from
# llm_gateway.count_tokens) is used when a counter is supplied.
_CHARS_PER_TOKEN = 3.5

_DIFF_FILE_RE = re.compile(r"^diff --git a/(?P<a>.+?) b/(?P<b>.+?)$", re.MULTILINE)
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<new_start>\d+)(?:,\d+)? @@", re.MULTILINE)

# Column-0 declaration starts across the languages this platform actually sees. Not a
# parser — just a boundary detector, so a wrong guess costs packing efficiency, never
# correctness.
_DECL_RE = re.compile(
    r"^(?:@|def |class |async def |func |function |public |private |protected |internal |"
    r"static |const |export |import |package |type |interface |struct |impl |module |"
    r"procedure |unit |namespace |var |let )",
)


@dataclass
class Unit:
    """One indivisible piece of the artifact — a diff hunk, a file, or a declaration."""
    text: str
    path: str = ""
    start_line: int = 1
    end_line: int = 1

    @property
    def line_count(self) -> int:
        return self.end_line - self.start_line + 1


@dataclass
class Chunk:
    index: int          # 1-based
    total: int
    units: List[Unit] = field(default_factory=list)
    body: str = ""      # unit text only, without the header

    @property
    def paths(self) -> List[str]:
        seen, out = set(), []
        for u in self.units:
            if u.path and u.path not in seen:
                seen.add(u.path)
                out.append(u.path)
        return out


def estimate_tokens(text: str) -> int:
    return int(len(text) / _CHARS_PER_TOKEN) + 1


def estimate_chunks(text: str, max_tokens: int = DEFAULT_CHUNK_TOKENS) -> int:
    """How many model calls reviewing this artifact will take, without splitting it.

    Used by the route to size the budget reservation (`agent_guard.expect_calls`), which
    has to happen *before* the guard is taken and therefore before the real split. Division
    rather than a real chunking pass because the answer only has to be close: a reservation
    that is one chunk out changes when an already-marginal caller gets a 402, never whether
    the review is correct. Structural boundaries mean the real count is never lower than
    this, so it errs toward under-reserving by at most a chunk or two — never over.
    """
    if not text:
        return 1
    return max(1, -(-estimate_tokens(text) // max(max_tokens, 1)))


def is_unified_diff(text: str) -> bool:
    head = text[:4000]
    return bool(_DIFF_FILE_RE.search(head)) or ("--- a/" in head and "+++ b/" in head)


def _split_diff(text: str) -> List[Unit]:
    """Per-file, then per-hunk. Line numbers are the *new-file* numbers from the @@ header,
    which is what a reviewer citing a line in a PR actually means."""
    lines = text.splitlines()
    units: List[Unit] = []
    cur_path = ""
    header: List[str] = []      # the `diff --git`/`index`/`---`/`+++` preamble for cur_path
    hunk: List[str] = []
    hunk_start = 1

    def flush():
        if hunk:
            units.append(Unit(
                text="\n".join(header + hunk),
                path=cur_path,
                start_line=hunk_start,
                end_line=hunk_start + max(len(hunk) - 1, 0),
            ))

    for line in lines:
        m_file = _DIFF_FILE_RE.match(line)
        if m_file:
            flush()
            hunk = []
            cur_path = m_file.group("b")
            header = [line]
            continue
        m_hunk = _HUNK_RE.match(line)
        if m_hunk:
            flush()
            hunk = [line]
            hunk_start = int(m_hunk.group("new_start"))
            continue
        if hunk:
            hunk.append(line)
        else:
            header.append(line)
    flush()

    # A "diff" with no recognisable hunks (truncated mid-header, or a format we don't
    # know) is better reviewed whole than dropped.
    return units or [Unit(text=text, start_line=1, end_line=max(len(lines), 1))]


def _split_flat(text: str, path: str = "") -> List[Unit]:
    """Split plain source at column-0 declaration boundaries, falling back to blank-line
    paragraphs. Preamble before the first declaration (imports, module docstring) stays
    attached to the first unit rather than becoming a stray fragment."""
    lines = text.splitlines()
    if not lines:
        return [Unit(text=text, path=path, start_line=1, end_line=1)]

    starts = [i for i, l in enumerate(lines) if _DECL_RE.match(l)]
    if len(starts) < 2:
        return [Unit(text=text, path=path, start_line=1, end_line=len(lines))]

    boundaries = [0] + [s for s in starts if s > 0]
    boundaries = sorted(set(boundaries))
    units = []
    for i, b in enumerate(boundaries):
        end = boundaries[i + 1] if i + 1 < len(boundaries) else len(lines)
        if end <= b:
            continue
        units.append(Unit(
            text="\n".join(lines[b:end]),
            path=path,
            start_line=b + 1,
            end_line=end,
        ))
    return units


def _window(unit: Unit, max_tokens: int, overlap_lines: int, count: Callable[[str], int]) -> List[Unit]:
    """Cut a single oversized unit into overlapping line windows."""
    lines = unit.text.splitlines()
    if len(lines) <= 1:
        return [unit]
    # Size the window from the unit's own token density rather than a global guess — a
    # minified line and a comment line are worlds apart.
    tokens = max(count(unit.text), 1)
    lines_per_window = max(int(len(lines) * max_tokens / tokens), 10)
    if lines_per_window >= len(lines):
        return [unit]

    out, start = [], 0
    while start < len(lines):
        end = min(start + lines_per_window, len(lines))
        out.append(Unit(
            text="\n".join(lines[start:end]),
            path=unit.path,
            start_line=unit.start_line + start,
            end_line=unit.start_line + end - 1,
        ))
        if end >= len(lines):
            break
        start = max(end - overlap_lines, start + 1)
    return out


def split_units(text: str, path: str = "") -> List[Unit]:
    if not text:
        return []
    if is_unified_diff(text):
        return _split_diff(text)
    return _split_flat(text, path)


def _manifest(chunk: Chunk, all_paths: List[str], diff_mode: bool) -> str:
    """The header that tells a chunk what it is and — more importantly — what it isn't.

    Without the "elsewhere" list a chunk cannot distinguish "this protection is missing"
    from "this protection is in a file I wasn't shown", and it reliably guesses the former.
    """
    if chunk.total == 1 and not chunk.paths:
        return ""

    # One row per file, not per unit. A chunk holding twelve hunks of one file, or a file
    # that split at every declaration, used to produce twelve near-identical rows — which
    # buries the part of this header that actually does work (the "not shown to you" list)
    # under noise the model has to read past. Ranges are merged only where consecutive units
    # are contiguous; a genuine gap is kept, because "lines 1-40, 900-980" and "lines 1-980"
    # tell the model different things about what it may assert.
    here = []
    for u in chunk.units:
        label = u.path or "(artifact)"
        if here and here[-1][0] == label and u.start_line <= here[-1][2] + 1:
            here[-1][2] = max(here[-1][2], u.end_line)
            continue
        if here and here[-1][0] == label:
            here[-1][3].append((u.start_line, u.end_line))
            continue
        here.append([label, u.start_line, u.end_line, []])

    rendered = []
    for label, start, end, extra in here:
        spans = ", ".join([f"{start}-{end}"] + [f"{a}-{b}" for a, b in extra])
        rendered.append(f"  - {label} (lines {spans})")
    here = rendered
    elsewhere = [p for p in all_paths if p not in chunk.paths]

    parts = [f"REVIEW CHUNK {chunk.index} OF {chunk.total}", "", "In this chunk:"]
    parts.extend(here)
    if elsewhere:
        parts += [
            "",
            "Also part of this change but NOT shown to you here:",
            "  " + ", ".join(elsewhere),
        ]
    parts += [
        "",
        "Rules for this chunk:",
        "- Report only findings you can point to in the code shown above. Quote the exact "
        "offending line(s) as your evidence.",
        "- Line numbers are absolute in the original file — use the numbers shown, do not "
        "renumber from the top of this chunk.",
        "- If an issue depends on code you cannot see, do NOT assert it. A control that "
        "lives in another file is not a missing control.",
    ]
    if diff_mode:
        parts += [
            "- This is a diff. Lines starting with '+' are the change under review. Report a "
            "finding only if the change introduces it, or fails to fix it. Pre-existing "
            "problems on unchanged context lines are out of scope — if you report one, set "
            "is_pre_existing true.",
        ]
    return "\n".join(parts) + "\n\n---\n\n"


def chunk_artifact(
    text: str,
    path: str = "",
    max_tokens: int = DEFAULT_CHUNK_TOKENS,
    overlap_lines: int = DEFAULT_OVERLAP_LINES,
    token_counter: Optional[Callable[[str], int]] = None,
) -> List[Chunk]:
    """Split `text` into chunks, each small enough that its findings fit one response.

    Returns a single chunk (with no header) when the artifact already fits, so the common
    small-input case is byte-for-byte what it was before chunking existed.
    """
    count = token_counter or estimate_tokens
    if not (text or "").strip():
        return []

    if count(text) <= max_tokens:
        units = [Unit(text=text, path=path, start_line=1, end_line=max(len(text.splitlines()), 1))]
        return [Chunk(index=1, total=1, units=units, body=text)]

    diff_mode = is_unified_diff(text)
    units: List[Unit] = []
    for u in split_units(text, path):
        units.extend(_window(u, max_tokens, overlap_lines, count) if count(u.text) > max_tokens else [u])

    # Bin-pack in order. Order is preserved deliberately: a reviewer reading chunk 3 of 7
    # should be reading the third part of the file, not an arbitrary subset.
    packed: List[List[Unit]] = []
    current: List[Unit] = []
    current_tokens = 0
    for u in units:
        t = count(u.text)
        if current and current_tokens + t > max_tokens:
            packed.append(current)
            current, current_tokens = [], 0
        current.append(u)
        current_tokens += t
    if current:
        packed.append(current)

    all_paths = []
    for u in units:
        if u.path and u.path not in all_paths:
            all_paths.append(u.path)

    chunks = []
    for i, group in enumerate(packed, start=1):
        c = Chunk(index=i, total=len(packed), units=group)
        c.body = "\n\n".join(u.text for u in group)
        chunks.append(c)
    for c in chunks:
        c.body = _manifest(c, all_paths, diff_mode) + c.body
    return chunks


# ── Multi-file artifacts ──────────────────────────────────────────────────
#
# Everything above assumes the artifact is one string. That was true while the only inputs
# were a paste, one file, or one PR diff. A repository or a zip is a *list* of files, and
# concatenating it before splitting throws away the two things that make a finding usable:
#
# * **The real path.** A model reading a concatenated blob infers `file_path` from a header
#   it saw somewhere above; here every unit carries its path as a fact, so the finding does
#   too, and the PR comment and the inline anchor can rely on it.
# * **Per-file line numbers.** In a concatenated blob, line 42 of `auth.py` is line 1,847 of
#   the artifact, and the two are indistinguishable afterwards. Units built per file keep
#   the file's own numbering, which is what a reviewer, a diff anchor and
#   `finding_pipeline.verify_evidence_per_file` all need.


def unit_header(path: str, start_line: int, end_line: int) -> str:
    """How a unit announces which file and which lines it is.

    Carried on every unit rather than only in the chunk manifest: a chunk holding units from
    six files needs the boundary marked at each unit, or the model cannot tell which text
    belongs to which path — and it will guess, which is how a finding gets filed against the
    wrong file.
    """
    return f"----- {path or '(artifact)'} lines {start_line}-{end_line} -----\n"


def units_for_files(files: List[dict]) -> List[Unit]:
    """Split each file independently, so line numbers stay file-relative."""
    units: List[Unit] = []
    for f in files:
        path = f.get("path") or ""
        content = f.get("content") or ""
        if not content.strip():
            continue
        units.extend(_split_flat(content, path))
    return units


def chunk_files(
    files: List[dict],
    max_tokens: int = DEFAULT_CHUNK_TOKENS,
    overlap_lines: int = DEFAULT_OVERLAP_LINES,
    token_counter: Optional[Callable[[str], int]] = None,
) -> List[Chunk]:
    """Pack a list of `{path, content}` into chunks whose findings fit one response each.

    Same packing rules as `chunk_artifact` — order preserved, structural boundaries
    respected, oversized units windowed with overlap — with one addition: a unit's text is
    prefixed with `unit_header`, so the path travels with the code instead of only with the
    manifest.
    """
    count = token_counter or estimate_tokens
    units = units_for_files(files)
    if not units:
        return []

    expanded: List[Unit] = []
    for u in units:
        expanded.extend(
            _window(u, max_tokens, overlap_lines, count) if count(u.text) > max_tokens else [u]
        )

    # Stamp the header on after windowing, so a window's header names the window's own line
    # range rather than the whole unit's.
    for u in expanded:
        u.text = unit_header(u.path, u.start_line, u.end_line) + u.text

    packed: List[List[Unit]] = []
    current: List[Unit] = []
    current_tokens = 0
    for u in expanded:
        t = count(u.text)
        if current and current_tokens + t > max_tokens:
            packed.append(current)
            current, current_tokens = [], 0
        current.append(u)
        current_tokens += t
    if current:
        packed.append(current)

    all_paths: List[str] = []
    for u in expanded:
        if u.path and u.path not in all_paths:
            all_paths.append(u.path)

    chunks = []
    for i, group in enumerate(packed, start=1):
        c = Chunk(index=i, total=len(packed), units=group)
        c.body = "\n\n".join(u.text for u in group)
        chunks.append(c)
    for c in chunks:
        c.body = _manifest(c, all_paths, diff_mode=False) + c.body
    return chunks


def estimate_chunks_for_files(files: List[dict], max_tokens: int = DEFAULT_CHUNK_TOKENS) -> int:
    """How many model calls a file list will take, without doing the split.

    Same contract as `estimate_chunks`: close enough to size a budget reservation and a
    quote, never used for correctness. Sums per-file rather than over the concatenation
    because a file is never packed across a chunk boundary without being counted, and the
    per-unit header adds real tokens that the raw character count misses.
    """
    if not files:
        return 1
    total = 0
    for f in files:
        content = f.get("content") or ""
        if not content.strip():
            continue
        total += estimate_tokens(content) + estimate_tokens(unit_header(f.get("path") or "", 1, 1))
    return max(1, -(-total // max(max_tokens, 1)))
