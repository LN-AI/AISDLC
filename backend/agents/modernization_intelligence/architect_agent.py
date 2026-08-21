"""Decide what the source codebase becomes, once, before anything is converted.

The defect this exists to fix: every conversion call independently invented its own output
path from one sentence of the charter ("one project per layer"). File 1 wrote
`Api/Controllers/OrderController.cs`, file 200 wrote `src/Api/Controllers/OrderController.cs`,
and nothing in the system compared them. Worse, the mapping was forced to be 1:1 — which is
simply wrong for the common legacy shapes. A PHP page-script is markup, a query and
controller logic in one file and has to become three; thirty page-scripts sharing
`header.php` have to become *one* layout.

So the architect answers two questions the converter cannot:

  * **Grouping.** Which source files are converted together, as one unit, into several
    target files.
  * **Placement.** Which project, namespace and directory each of those groups lands in.

What it proposes is then *enforced deterministically* before a single token is spent —
see `architecture.materialize_components`. The model is a proposer here, not an authority
(D-04's principle, applied past the dependency graph). Repairing its answer is acceptable at
this stage precisely because it happens once, before spend, and the repaired result is
printed on a plan a human approves. The same repair at conversion time would be neither
visible nor auditable, which is why output paths there are structural instead.
"""
import logging
from typing import Any, Dict, List, Optional

from backend.agents.base_agent import BaseAgent
from backend.services import llm_gateway
from backend.services.modernization import charter as charter_module

logger = logging.getLogger(__name__)

# The whole inventory goes in and a whole partition comes out, so this is the top tier.
# `agent_guard.RESERVATION_MAX_TOKENS` is 32000 and D-18 requires that constant to move if
# any agent goes above it — nothing here needs to.
ARCHITECTURE_MAX_TOKENS = 32000

TARGET_ARCHITECTURE_TOOL_SCHEMA = {
    "name": "submit_target_architecture",
    "description": (
        "Decide the target project layout and group every source file into a component that "
        "will be converted as one unit."
    ),
    "input_schema": {
        "type": "object",
        "required": ["projects", "components", "summary"],
        "properties": {
            "projects": {
                "type": "array",
                "description": (
                    "The projects (assemblies/modules) the output is organised into, "
                    "following the charter's project layout. Keep this small — one project "
                    "per layer or per bounded context, not one per feature."
                ),
                "items": {
                    "type": "object",
                    "required": ["name", "purpose"],
                    "properties": {
                        "name": {"type": "string",
                                 "description": "Project name, e.g. 'Acme.Billing.Api'."},
                        "purpose": {"type": "string", "description": "One line."},
                        "depends_on": {
                            "type": "array", "items": {"type": "string"},
                            "description": (
                                "Other project names this one references. **This is "
                                "enforced.** Every file emitted into a project is checked "
                                "against this list, and a reference it does not permit fails "
                                "the component and is sent back. Two projects that reference "
                                "each other cannot be built at all, so state the direction "
                                "you mean: list only what this project genuinely needs, and "
                                "leave it empty for the innermost layer."
                            ),
                        },
                    },
                },
            },
            "components": {
                "type": "array",
                "description": (
                    "The conversion units. Every source file you were shown must appear in "
                    "exactly one component's `members`. A component is converted in a single "
                    "call, so keep the combined size of its members modest — group files "
                    "that genuinely belong together (a page and the fragments only it "
                    "includes; a class and its interface), not everything in a directory."
                ),
                "items": {
                    "type": "object",
                    "required": ["name", "project", "namespace", "output_dir", "members"],
                    "properties": {
                        "name": {"type": "string",
                                 "description": "Short identifier, e.g. 'order-admin'."},
                        "project": {"type": "string",
                                    "description": "Which project from `projects` it belongs to."},
                        "namespace": {"type": "string",
                                      "description": "Target namespace for everything it emits."},
                        "output_dir": {
                            "type": "string",
                            "description": (
                                "Directory the component's files are written to, relative to "
                                "the repository root and beginning with the project name — "
                                "e.g. 'Acme.Billing.Api/Controllers'. Must be unique."
                            ),
                        },
                        "members": {
                            "type": "array", "items": {"type": "string"},
                            "description": "Source paths, exactly as given to you.",
                        },
                        "role": {
                            "type": "string",
                            "description": "e.g. 'controller', 'view', 'entity', 'repository', "
                                           "'layout', 'service', 'config'.",
                        },
                        "rationale": {"type": "string",
                                      "description": "One line: why these files convert together."},
                        "expected_files": {
                            "type": "array",
                            "description": "What you expect the conversion to emit, as bare "
                                           "file names. A hint, not a contract.",
                            "items": {"type": "string"},
                        },
                    },
                },
            },
            "seams": {
                "type": "array",
                "description": (
                    "How a project gets behaviour that lives in a project it may not "
                    "reference. This is the answer to the only structural problem that "
                    "cannot be repaired after conversion: if `depends_on` says A may not "
                    "reference B, but code in A needs something B does, then A declares an "
                    "interface and B implements it. Declare each of those here and both "
                    "sides will be told. A dependency you leave unstated becomes a "
                    "forbidden reference the converter has no legitimate way to write."
                ),
                "items": {
                    "type": "object",
                    "required": ["interface", "declared_in", "implemented_in"],
                    "properties": {
                        "interface": {
                            "type": "string",
                            "description": "The interface name, e.g. 'IEmployeeRepository'.",
                        },
                        "declared_in": {
                            "type": "string",
                            "description": "The project that declares it — the *consumer's* "
                                           "project, the one that may not reference the other.",
                        },
                        "implemented_in": {
                            "type": "string",
                            "description": "The project whose component writes the class that "
                                           "implements it.",
                        },
                        "purpose": {"type": "string", "description": "One line."},
                    },
                },
            },
            "shared_conventions": {
                "type": "string",
                "description": (
                    "Rules every component must follow that the charter does not already "
                    "state — naming of generated types, where cross-cutting concerns live, "
                    "how the source's idioms map. This is shown to every conversion call."
                ),
            },
            "risks": {
                "type": "array", "items": {"type": "string"},
                "description": "What about this decomposition could go wrong, and where.",
            },
            "summary": {"type": "string", "description": "2-4 sentences on the target shape."},
        },
    },
}


ARCHITECT_SYSTEM_PROMPT = """You are the LM AI-SDLC Modernization Architect. You are given the
complete inventory of a legacy codebase — every file, its role, its size, and what it depends on —
and you decide what it becomes.

You are called **once per run**, before anything is converted. Everything you decide here binds
every conversion call that follows, and none of them can see each other. That is the whole reason
you exist: without you, five hundred independent calls each invent their own project layout from
one sentence, and the result is a codebase that disagrees with itself in five hundred places.

Two rules about the project layout, because they are the only decisions here that cannot be corrected later.

1. `depends_on` is **enforced against the emitted code**. Every file a component writes is checked against the list you give its project, and a reference the list does not permit fails that component and sends it back to be converted again. So the graph you declare has to be the graph you mean.

2. A dependency that points the wrong way has no legitimate expression. If a project needs behaviour from a project it may not reference, that is a **seam**: the consumer declares an interface, the other side implements it, and you list it in `seams`. If you leave it out, the converter is left with a choice between naming a type it is forbidden to name and not doing the work — and it will name the type. Prefer fewer projects over a layout whose seams you have not thought through: a single project cannot have this problem at all.

## What you must get right

1. **Every source file is assigned to exactly one component.** Not most of them. A file you leave
   out still gets converted — into a directory derived mechanically from its path, with no
   architectural thought applied — so leaving it out is a decision, not an omission.
2. **`output_dir` is unique per component and starts with the component's project name.** Two
   components writing to the same directory will collide on file names, and nothing downstream can
   tell which one was right.
3. **Group by what converts together, not by what is similar.** A component is converted in one
   call, and its members are shown to that call in full. The test is: would converting these files
   separately force one of them to guess about another? A page and a fragment only it includes
   belong together. Twelve unrelated entities do not — they are just twelve entities.
4. **Respect the source's dependency order.** You are shown which files depend on which. A
   component's dependencies are the union of its members' dependencies; if you group A and B such
   that something between them depends both ways, you have created a cycle that did not exist.

## What the target shape should be

Follow the charter's project layout exactly — it was decided by the team that owns this codebase
and is not yours to improve on. Within it:

- A legacy page/script that mixes markup, data access and logic becomes **several** files, not one.
  Say so in `expected_files`.
- Presentation fragments that many pages include become **one** shared component, converted once.
- Files that only declare types become entities or models, in the project the charter puts them in.
- Configuration and build files are components too — something has to carry them across.

Be concrete and be complete. A decomposition that covers 90% of the files is worse than a blunt one
that covers all of them, because the 10% land wherever a mechanical fallback puts them and nobody
finds out until the build fails.
"""


class TargetArchitectAgent(BaseAgent):
    def __init__(self):
        super().__init__(
            agent_name="Modernization Target Architect",
            system_prompt=ARCHITECT_SYSTEM_PROMPT,
        )

    def system_for(self, charter: Dict[str, Any], digest: str = "") -> str:
        """Byte-identical across every call in a run — there is only one, but the shape has
        to match the converter's so the same cache reasoning applies if it ever shards."""
        parts = [ARCHITECT_SYSTEM_PROMPT, charter_module.render(charter)]
        if digest:
            parts.append(digest)
        return "\n\n".join(parts) + "\n"

    def design(
        self,
        charter: Dict[str, Any],
        inventory: str,
        digest: str = "",
        cycles: Optional[List[Dict[str, Any]]] = None,
        llm_override: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        result = llm_gateway.call_tool(
            self.system_for(charter, digest),
            _build_user_message(inventory, cycles or []),
            TARGET_ARCHITECTURE_TOOL_SCHEMA,
            llm_override,
            max_tokens=ARCHITECTURE_MAX_TOKENS,
        )
        result["agent_name"] = self.agent_name
        return result


def _build_user_message(inventory: str, cycles: List[Dict[str, Any]]) -> str:
    parts = [
        "# The codebase to decompose",
        "",
        "One row per source file: path, language, the role a parser guessed, size in lines, and "
        "the paths it depends on. This is the complete inventory — there is nothing else.",
        "",
        inventory,
    ]
    if cycles:
        listed = "\n".join(
            f"- {c['size']} files: " + ", ".join(f"`{p}`" for p in c["paths"][:10])
            + (" …" if len(c["paths"]) > 10 else "")
            for c in cycles[:15]
        )
        parts.append(
            "\n## Circular dependencies already present in the source\n\n"
            "These files depend on each other, so no order exists between them. Grouping the "
            "members of one cycle into a single component is usually the right move — it turns a "
            "cycle into an internal detail of one conversion. Do not split a cycle across "
            "components unless you have a reason.\n\n" + listed
        )
    parts.append(
        "\nDecompose this now. Every path above must appear in exactly one component's `members`."
    )
    return "\n".join(parts)
