"""Per-language-pair conversion knowledge, as Python constants.

**Why constants and not the knowledge base.** The KB reaches a prompt through the *user*
message — that is X-01 in DECISIONS.md, a correction somebody already had to make once — so
putting a mapping corpus there would sit it after the prompt-cache breakpoint and charge full
price for it on every one of a 500-file run's calls. The KB is also runtime-editable, which
would let the system prompt change mid-run and break the byte-identical guarantee the whole
cost model rests on. Constants are versioned with the code, render in source order (so
`render()` is byte-stable by construction, the property `charter.render` is careful about),
diff in review, and cannot change under a running conversion.

**Scoping.** A playbook is a set of sections and only the ones a run actually needs are
included, selected from the run's own inventory: a codebase with no `mysql_*` call never ships
the EF Core section. The selection is made once and frozen at `start()`, for the same reason
the charter is — a system block that grows as the run learns things is a system block that is
never cached.

Deliberately **not** scoped per module type. Sending Razor guidance only to UI units sounds
efficient and is not: *k* variants means *k* cache entries instead of one, and units interleave
by topological level, so each variant is likely evicted before its second read.
"""
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Section:
    key: str
    title: str
    body: str
    # Included only when this returns True for the run's aggregate facts. `None` = always.
    applies: Optional[Callable[[Dict[str, Any]], bool]] = None


@dataclass(frozen=True)
class Playbook:
    source: str                      # language label, matching `analyzers` output
    target: str                      # normalised target-stack fragment
    sections: Tuple[Section, ...] = field(default_factory=tuple)


_REGISTRY: Dict[Tuple[str, str], Playbook] = {}


def register(playbook: Playbook) -> None:
    _REGISTRY[(playbook.source.lower(), playbook.target.lower())] = playbook


def find(source_language: str, target_stack: str) -> Optional[Playbook]:
    source = (source_language or "").strip().lower()
    normalized = "".join(c for c in (target_stack or "").lower() if c.isalnum())
    if not source or not normalized:
        return None
    for (playbook_source, playbook_target), playbook in _REGISTRY.items():
        if playbook_source == source and playbook_target in normalized:
            return playbook
    return None


def select(playbook: Playbook, facts: Dict[str, Any]) -> List[str]:
    """Which sections this run needs. Deterministic given the same facts."""
    keys = []
    for section in playbook.sections:
        try:
            if section.applies is None or section.applies(facts):
                keys.append(section.key)
        except Exception:
            logger.exception("Playbook section %s failed its applicability check", section.key)
            keys.append(section.key)          # include rather than lose it
    return keys


def render(playbook: Playbook, keys: Sequence[str]) -> str:
    """The system-prompt block. Section order comes from the playbook, never from `keys` —
    a caller handing them back in a different order must not produce a different string."""
    wanted = set(keys or [])
    chosen = [s for s in playbook.sections if s.key in wanted]
    if not chosen:
        return ""
    lines = [
        f"## Converting {playbook.source} to {playbook.target} — known traps",
        "",
        "These are the transformations that are wrong in a way that compiles. Each one below "
        "has a right answer and a plausible wrong one, and the wrong one is what you get by "
        "translating the syntax rather than the behaviour. Where a rule here conflicts with "
        "the charter, the charter wins.",
        "",
    ]
    for section in chosen:
        lines.append(f"### {section.title}")
        lines.append("")
        lines.append(section.body.strip())
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def aggregate_facts(units: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Roll per-file facts up to the run, which is what section selection keys on."""
    out: Dict[str, Any] = {
        "db_apis": set(), "superglobals": set(), "hazard_kinds": set(),
        "html": False, "sessions": False, "uploads": False, "redirects": False,
        "json": False, "tables": set(), "languages": set(),
    }
    for unit in units:
        out["languages"].add(unit.get("language") or "")
        facts = (unit.get("meta") or {}).get("php") or {}
        out["db_apis"].update(facts.get("db_apis") or [])
        out["superglobals"].update((facts.get("superglobals") or {}).keys())
        out["hazard_kinds"].update(facts.get("hazard_kinds") or [])
        out["tables"].update(facts.get("tables") or [])
        out["html"] = out["html"] or bool(facts.get("has_html_document")) \
            or float(facts.get("html_ratio") or 0) > 0.15
        out["sessions"] = out["sessions"] or bool(facts.get("starts_session")) \
            or "$_SESSION" in (facts.get("superglobals") or {})
        out["uploads"] = out["uploads"] or bool(facts.get("handles_uploads"))
        out["redirects"] = out["redirects"] or bool(facts.get("redirects"))
        out["json"] = out["json"] or bool(facts.get("emits_json"))
    for key in ("db_apis", "superglobals", "hazard_kinds", "tables", "languages"):
        out[key] = sorted(x for x in out[key] if x)
    return out


from backend.services.modernization.playbooks import php_dotnet  # noqa: E402,F401
