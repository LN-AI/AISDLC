"""Serving `docs/agents/*.md` to the screen the agent runs on.

Every product already has a reference document — 26 of them, all on the same template —
and until now they existed only in the repository. Someone using the product had no way to
find out what the agent does beyond a one-line description on a card.

Two judgements are worth stating, because both could reasonably have gone the other way.

**Narrative sections only.** The template's `## Request`, `## Response`, `## Example` and
`## Authentication` sections document the **`/api/v1` endpoint** — its field names, its JSON
shape, a curl invocation. A screen does not send those fields. Code Review's documented
sources are `code` / `github_pr_url` / `github_file_url` while the screen visibly offers a
zip upload, a repository at a ref and a branch comparison as well, so a user reading the
Request table on the Code Review screen would be reading about a different interface than
the one in front of them. This is exactly the trap D-291 recorded for `Product.sources`,
which the frontend deliberately refuses to render for the same reason. Dropping four
sections is cheaper than maintaining a second set of documents, and the response names what
it dropped rather than quietly serving less.

**Resolved from the registry, never from the URL.** The filename is derived from a product
id that must already exist in `registry`, which is a closed set of 28 literals — so no
caller-supplied string ever reaches the filesystem. The realpath containment check below is
belt and braces on top of that, not the actual defence.

The 1:1 `<product id>.md` convention holds for 26 of 28 products. The two exceptions are
declared in `NO_DOC` with the reason, so "this product has no document" is a stated fact
that an offline check can hold us to, rather than a missing file nobody notices.
"""
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from backend.products import registry

DOCS_DIR = Path(__file__).parent.parent.parent / "docs" / "agents"

# Sections that describe the HTTP API rather than the agent. Dropped before the document
# reaches a screen — see the module docstring.
API_SECTIONS = ("Authentication", "Request", "Response", "Example")

# Products with no reference document, and why. An offline check asserts that every other
# product has one, so this set is the only way a missing document stays acceptable.
NO_DOC: Dict[str, str] = {
    "github-pr-review": (
        "The GitHub PR screen was retired and folded into Code Review (D-267). The product "
        "still exists because a run that writes back to GitHub is metered and entitled "
        "separately, but what it does is documented in code.md."
    ),
    "swarm": (
        "The Agent Hub runs other products rather than being one; there is no single agent "
        "behaviour to document. Each agent it dispatches to has its own document."
    ),
}


@lru_cache(maxsize=None)
def doc_path(product_id: str) -> Optional[Path]:
    """The document for a product, or None. Only registry ids resolve to a path.

    Cached because `GET /api/v1/products` reports `has_doc` for all 28 products and the
    browser calls it on every page load: uncached that is ~12ms of `resolve()` and
    `is_file()` per request, for an answer that cannot change while the process runs. The
    cost of the cache is that adding a document needs a restart to show up, which the
    reloader does anyway for any Python edit.
    """
    if not registry.exists(product_id) or product_id in NO_DOC:
        return None
    candidate = (DOCS_DIR / f"{product_id}.md").resolve()
    # The id came from a closed set, so this cannot currently fail. Kept because the cost
    # is one comparison and the failure it guards against is reading an arbitrary file.
    if os.path.commonpath([str(candidate), str(DOCS_DIR.resolve())]) != str(DOCS_DIR.resolve()):
        return None
    return candidate if candidate.is_file() else None


def _split_sections(body: str) -> Tuple[str, List[Tuple[str, str]]]:
    """Markdown -> (everything before the first `## `, [(heading, section text), ...])."""
    parts = re.split(r"^## +(.+?)\s*$", body, flags=re.MULTILINE)
    preamble = parts[0]
    sections: List[Tuple[str, str]] = []
    for i in range(1, len(parts) - 1, 2):
        sections.append((parts[i].strip(), parts[i + 1]))
    return preamble, sections


# Paragraphs worth keeping even when the section around them is dropped. Narrow on
# purpose: one phrasing, used identically by the six documents that have it. A looser
# pattern would start dragging API field names back in through the side door.
_RESCUE_PREFIXES = ("Gate logic:",)


def _rescue(text: str) -> List[str]:
    return [
        para.strip() for para in re.split(r"\n\s*\n", text)
        if para.strip().startswith(_RESCUE_PREFIXES)
    ]


def render(product_id: str) -> Optional[Dict[str, Any]]:
    """The document a screen shows, with the API-reference sections removed."""
    path = doc_path(product_id)
    if path is None:
        return None
    raw = path.read_text(encoding="utf-8")

    preamble, sections = _split_sections(raw)
    kept, omitted, rescued = [], [], []
    for heading, text in sections:
        if heading not in API_SECTIONS:
            kept.append((heading, text))
            continue
        omitted.append((heading, text))
        rescued.extend(_rescue(text))
    # Six documents explain their Red/Amber/Green rule in a paragraph that happens to sit
    # at the end of `## Response`, because it describes the `gate_status` field. Dropping
    # the section wholesale would take with it the one thing a person looking at an Amber
    # result most wants to read, so it is lifted out and given a heading of its own.
    if rescued:
        # Placed first, not appended: what turns this agent's verdict Red matters more to a
        # reader than the guardrail list. The paragraph's own "Gate logic:" lead-in is
        # dropped rather than repeated directly under a heading that already says it.
        body = "\n\n".join(t[len("Gate logic:"):].strip() for t in rescued)
        kept.insert(0, ("Gate logic", "\n" + body + "\n"))

    title = ""
    first = preamble.lstrip().split("\n", 1)[0].strip()
    if first.startswith("# "):
        title = first[2:].strip()
        preamble = preamble.lstrip()[len(first):].lstrip("\n")

    markdown = preamble.rstrip()
    for heading, text in kept:
        markdown += f"\n\n## {heading}\n{text.rstrip()}"

    return {
        "product_id": product_id,
        "title": title or registry.get(product_id).title,
        "markdown": markdown.strip() + "\n",
        "sections": [h for h, _ in kept],
        # Named, not silently dropped: a reader who wants the field names should know they
        # exist and where they live, rather than concluding the agent has no API.
        "omitted_sections": [h for h, _ in omitted],
        "omitted_note": (
            "Request, Response, Example and Authentication describe the /api/v1 endpoint's "
            "own field names, which are not what this screen sends. See docs/agents/ or "
            "/docs for the API reference."
        ) if omitted else "",
        "source": f"docs/agents/{product_id}.md",
    }


def missing_docs() -> List[str]:
    """Registry products that should have a document and do not — for the offline check."""
    return sorted(
        p.id for p in registry.PRODUCTS
        if p.id not in NO_DOC and doc_path(p.id) is None
    )
