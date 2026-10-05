"""
KnowledgeBaseLoader — loads all .md files from docs/<category>/ for every known agent
category, caches them in memory, and provides them as context for that category's agents.

Docs are either:
  - Global/shared: docs/<category>/<name>.md — used as context for every project.
  - Project-specific: docs/<category>/projects/<project-slug>/<name>.md — only used
    as context when that same project name is passed in.
"""

import os
import re
from pathlib import Path
from typing import Dict, List, Optional
from dataclasses import dataclass, field


DOCS_ROOT = Path(__file__).parent.parent.parent / "docs"
PROJECTS_SUBDIR = "projects"

# Max characters of KB injected per agent call (avoids blowing context window)
MAX_KB_CHARS = 24_000

# Every agent gets its own knowledge base category — closely related agents that
# should share the same organizational standards are grouped under one category.
# Only directories listed here are ever scanned (docs/n8n-workflows, docs/vscode-integration,
# etc. are platform documentation, not agent knowledge base content, and are ignored).
KB_CATEGORIES = [
    {"key": "requirement", "label": "Requirement Review", "agents": ["Requirement Reviewer"]},
    {"key": "code-review", "label": "Code Review", "agents": ["Code Review"]},
    {"key": "prompt-safety", "label": "Prompt & Agent Safety", "agents": ["Prompt & Agent Safety"]},
    {"key": "compliance", "label": "Compliance Mapping", "agents": ["Compliance Mapper"]},
    {"key": "vapt", "label": "VAPT", "agents": ["VAPT"]},
    {"key": "security", "label": "Security Architecture Review", "agents": ["Security Review"]},
    {"key": "architecture", "label": "Architecture & Engineering Standards", "agents": ["Architecture Review", "Code Migration", "Code Generation"]},
    {"key": "sensitive-data", "label": "Sensitive Data Detection", "agents": ["Sensitive Data Detector"]},
    {"key": "ai-provenance", "label": "AI Code Provenance", "agents": ["AI Code Provenance & Risk"]},
    {"key": "gxp", "label": "GxP / 21 CFR Part 11", "agents": ["21 CFR Part 11 Verification", "GxP Document Check", "GxP Document Generation"]},
    {"key": "issue-triage", "label": "Issue Triage", "agents": ["Issue Triaging Agent"]},
    {"key": "qa", "label": "QA / Test Standards", "agents": ["Test Scenario Generation", "Test Case Generation"]},
    {"key": "deployment-readiness", "label": "Deployment Readiness", "agents": ["Deployment Readiness"]},
    {"key": "cicd-pipeline", "label": "CI/CD Pipeline Standards", "agents": ["CI/CD Pipeline Review"]},
    {"key": "iac-review", "label": "Infrastructure-as-Code Standards", "agents": ["Infrastructure-as-Code Review"]},
    {"key": "incident-postmortem", "label": "Incident & Postmortem Standards", "agents": ["Incident & Postmortem"]},
    {"key": "monitoring-alerting", "label": "Monitoring & Alerting Standards", "agents": ["Monitoring & Alerting Config Review"]},
]
VALID_KB_CATEGORIES = tuple(c["key"] for c in KB_CATEGORIES)

# Architecture-category-only relevance boosting — these filenames get pulled to the
# front of the global docs when present, and certain keywords in the query pull in
# specific supplementary docs. Not meaningful for any other category.
_ARCH_PRIORITY_NAMES = ["patterns", "anti_patterns", "quality_standards", "checklist"]
_ARCH_CONDITIONAL_NAMES = {
    "ai": ["ai_architecture"], "llm": ["ai_architecture"], "agent": ["ai_architecture"], "rag": ["ai_architecture"],
    "cloud": ["cloud_patterns"], "aws": ["cloud_patterns"], "azure": ["cloud_patterns"],
    "docker": ["cloud_patterns"], "kubernetes": ["cloud_patterns"],
}


@dataclass
class KBDocument:
    name: str           # filename without extension, e.g. "patterns"
    title: str          # first H1 heading, e.g. "Architecture Patterns Reference"
    path: str           # relative path from docs root, e.g. "architecture/patterns.md"
    content: str        # full markdown content
    size_chars: int
    category: str       # top-level docs/ subdirectory, e.g. "architecture"
    project_name: str = ""   # "" = global/shared doc; otherwise the project slug it belongs to
    tags: List[str] = field(default_factory=list)  # keywords extracted from headings


def _extract_title(content: str) -> str:
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def _extract_tags(content: str) -> List[str]:
    """Extract H2 and H3 headings as searchable tags."""
    tags = []
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("## ") or line.startswith("### "):
            tag = re.sub(r"^#+\s+", "", line).lower()
            tags.append(tag)
    return tags


def slugify_project(project_name: str) -> str:
    return re.sub(r"[^a-z0-9_\-]", "_", project_name.strip().lower()).strip("_")


def _doc_key(category: str, project_slug: str, name: str) -> str:
    return f"{category}::{project_slug}::{name}"


class KnowledgeBaseLoader:
    def __init__(self, docs_root: Path = DOCS_ROOT):
        self.docs_root = docs_root
        self._docs: Dict[str, KBDocument] = {}
        self._loaded = False

    def load(self) -> None:
        """Load (or reload) all .md files from every known category directory."""
        self._docs = {}
        for category in VALID_KB_CATEGORIES:
            category_dir = self.docs_root / category
            if not category_dir.exists():
                category_dir.mkdir(parents=True, exist_ok=True)
                continue

            for root, _dirs, files in os.walk(category_dir):
                for filename in sorted(files):
                    if not filename.endswith(".md"):
                        continue
                    filepath = Path(root) / filename
                    try:
                        content = filepath.read_text(encoding="utf-8")
                    except Exception:
                        continue

                    rel_path = filepath.relative_to(self.docs_root)
                    parts = rel_path.parts  # ("architecture", "patterns.md")
                    #        or ("architecture", "projects", "clinic-api", "stack.md")
                    name = filepath.stem
                    project_slug = ""
                    if len(parts) >= 4 and parts[1] == PROJECTS_SUBDIR:
                        project_slug = parts[2]

                    doc = KBDocument(
                        name=name,
                        title=_extract_title(content) or name.replace("_", " ").title(),
                        path=str(rel_path).replace("\\", "/"),
                        content=content,
                        size_chars=len(content),
                        category=category,
                        project_name=project_slug,
                        tags=_extract_tags(content),
                    )
                    self._docs[_doc_key(category, project_slug, name)] = doc

        self._loaded = True

    def ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    def list_docs(self, project_name: Optional[str] = None, category: Optional[str] = None) -> List[Dict]:
        """List documents. With project_name: global docs + that project's docs only.
        Without it (None): every document, for the admin-facing "all docs" view.
        Optionally filter to one category."""
        self.ensure_loaded()
        docs = list(self._docs.values())
        if category:
            docs = [d for d in docs if d.category == category]
        if project_name is not None:
            slug = slugify_project(project_name)
            docs = [d for d in docs if not d.project_name or d.project_name == slug]
        return [
            {
                "name": d.name,
                "title": d.title,
                "path": d.path,
                "category": d.category,
                "project_name": d.project_name,
                "size_chars": d.size_chars,
                "tags": d.tags[:8],
            }
            for d in docs
        ]

    def get_doc(self, name: str, category: str, project_name: str = "") -> Optional[KBDocument]:
        self.ensure_loaded()
        slug = slugify_project(project_name) if project_name else ""
        return self._docs.get(_doc_key(category, slug, name))

    def add_doc(self, name: str, content: str, category: str, project_name: str = "") -> KBDocument:
        """Write a new or updated document to disk and reload it into cache.
        Pass project_name to scope this doc to one project instead of sharing it globally."""
        if category not in VALID_KB_CATEGORIES:
            raise ValueError(f"category must be one of {VALID_KB_CATEGORIES}")
        safe_name = re.sub(r"[^a-z0-9_\-]", "_", name.lower())
        slug = slugify_project(project_name) if project_name else ""
        target_dir = self.docs_root / category / PROJECTS_SUBDIR / slug if slug else self.docs_root / category
        target_dir.mkdir(parents=True, exist_ok=True)
        filepath = target_dir / f"{safe_name}.md"
        filepath.write_text(content, encoding="utf-8")

        doc = KBDocument(
            name=safe_name,
            title=_extract_title(content) or safe_name.replace("_", " ").title(),
            path=str(filepath.relative_to(self.docs_root)).replace("\\", "/"),
            content=content,
            size_chars=len(content),
            category=category,
            project_name=slug,
            tags=_extract_tags(content),
        )
        self._docs[_doc_key(category, slug, safe_name)] = doc
        return doc

    def delete_doc(self, name: str, category: str, project_name: str = "") -> bool:
        slug = slugify_project(project_name) if project_name else ""
        key = _doc_key(category, slug, name)
        doc = self._docs.get(key)
        if not doc:
            return False
        filepath = self.docs_root / doc.path
        if filepath.exists():
            filepath.unlink()
        del self._docs[key]
        return True

    def get_context(self, category: str, query: str = "", project_name: str = "", max_chars: int = MAX_KB_CHARS) -> str:
        """
        Build a KB context string for injection into an agent prompt. Project-specific
        docs for `project_name` (if any) are included first and treated as most
        authoritative, then global/shared docs for this category are added until
        max_chars is reached.
        """
        self.ensure_loaded()
        if not self._docs:
            return ""

        slug = slugify_project(project_name) if project_name else ""
        project_docs = [d for d in self._docs.values() if d.category == category and slug and d.project_name == slug]
        global_docs = {d.name: d for d in self._docs.values() if d.category == category and not d.project_name}
        if not project_docs and not global_docs:
            return ""

        ordered_names = list(global_docs.keys())
        if category == "architecture":
            priority = [n for n in _ARCH_PRIORITY_NAMES if n in global_docs]
            query_lower = query.lower()
            conditional = []
            for keyword, names in _ARCH_CONDITIONAL_NAMES.items():
                if keyword in query_lower:
                    conditional.extend(n for n in names if n in global_docs and n not in priority)
            rest = [n for n in ordered_names if n not in priority and n not in conditional]
            ordered_names = priority + conditional + rest

        parts: List[str] = []
        total = 0
        truncated = False

        def _add(doc: KBDocument, label: str = "") -> bool:
            """Returns False once the budget is exhausted (caller should stop)."""
            nonlocal total, truncated
            heading = f"## [{doc.title}]{label}"
            section = f"\n\n---\n{heading}\n\n{doc.content}"
            remaining = max_chars - total
            if len(section) > remaining:
                if remaining > 500:
                    parts.append(section[:remaining] + "\n\n[... truncated for length ...]")
                truncated = True
                return False
            parts.append(section)
            total += len(section)
            return True

        for doc in project_docs:
            if not _add(doc, label=f" — project: {project_name}"):
                break

        if not truncated:
            for name in ordered_names:
                doc = global_docs.get(name)
                if not doc:
                    continue
                if not _add(doc):
                    break

        if not parts:
            return ""

        label = next((c["label"] for c in KB_CATEGORIES if c["key"] == category), category.title())
        intro = f"# {label} Knowledge Base\n"
        if project_docs:
            intro += (
                f"The following documents include project-specific standards for '{project_name}' "
                "(marked as such) plus this organization's shared standards for this area. "
                "Treat the project-specific documents as most authoritative for this request.\n"
            )
        else:
            intro += "The following documents are the organization's shared standards for this area. Use them as authoritative reference.\n"
        return intro + "".join(parts)


# Module-level singleton — shared across all requests
knowledge_base = KnowledgeBaseLoader()
