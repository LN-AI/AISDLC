import logging
from typing import Any, Dict, List, Optional

from backend.agents.base_agent import BaseAgent
from backend.services import llm_gateway

logger = logging.getLogger(__name__)

AUTOFIX_TOOL_SCHEMA = {
    "name": "submit_autofix",
    "description": "Submit corrected file contents that remediate the given code review findings",
    "input_schema": {
        "type": "object",
        "required": [
            "patches", "commit_message", "pr_title", "pr_description",
            "fixed_finding_titles", "unfixed_finding_titles",
        ],
        "properties": {
            "patches": {
                "type": "array",
                "description": "Full corrected content for each file that actually changed. Do not include unchanged files.",
                "items": {
                    "type": "object",
                    "required": ["path", "fixed_content", "explanation"],
                    "properties": {
                        "path": {"type": "string", "description": "Repo-relative file path, exactly as given in the input"},
                        "fixed_content": {"type": "string", "description": "The FULL corrected file content — not a diff fragment, not a placeholder"},
                        "explanation": {"type": "string", "description": "1-2 sentences on what changed and why"},
                    },
                },
            },
            "commit_message": {"type": "string", "description": "Conventional-commit style message for the fix commit, e.g. 'fix(security): parameterize SQL query in user lookup'"},
            "pr_title": {"type": "string"},
            "pr_description": {"type": "string", "description": "Markdown PR body explaining what was fixed and why, referencing the findings it resolves"},
            "fixed_finding_titles": {"type": "array", "items": {"type": "string"}, "description": "Titles of findings this patch set resolves"},
            "unfixed_finding_titles": {"type": "array", "items": {"type": "string"}, "description": "Titles of findings deliberately NOT fixed, each with a short reason (e.g. needs human judgment, insufficient context)"},
        },
    },
}

SYSTEM_PROMPT = """You are the LM AI-SDLC Auto-Fix Agent — a senior engineer who remediates specific,
already-identified code review findings by producing complete, corrected file contents ready to commit.

## Rules
- You are given the full current content of one or more files, plus a list of findings against them (from the
  Code Security Review Agent). Fix ONLY what the findings describe — do not refactor unrelated code, rename
  things, reformat, or "improve" style. Make minimal, surgical changes.
- Each patch's `fixed_content` must be the COMPLETE file content after your fix, not a diff fragment or
  placeholder — it will be committed to the file as-is.
- Preserve the file's existing structure, imports, and conventions exactly except where the fix requires a change.
- If a finding cannot be safely fixed without more context (e.g. "add rate limiting" with no rate-limiting
  infrastructure visible, or a design-level architecture concern), do NOT guess — list it in
  `unfixed_finding_titles` with a short reason instead of writing speculative code.
- Never introduce a new issue while fixing another (e.g. don't hardcode a "temporary" secret, don't silently
  swallow exceptions to remove an error path, don't disable a security check to make an error go away).
- Only include a file in `patches` if you actually changed it.
"""


class AutoFixAgent(BaseAgent):
    def __init__(self):
        super().__init__(agent_name="Auto-Fix Agent", system_prompt=SYSTEM_PROMPT)

    # One file per call. The original asked for the *complete* contents of up to five files
    # inside a single 16,000-token answer, which real source exceeds — the gateway then
    # correctly refuses to hand back a half-written file and raises, so on any realistic
    # repository Auto-Fix failed rather than helping (C-78). Splitting per file makes the
    # output budget a per-file budget, which is the shape the work actually has.
    MAX_OUTPUT_TOKENS = 32000

    # Above this a file cannot be rewritten whole within any sane output budget, and asking
    # anyway burns the input tokens before failing. Reported as unfixed instead, with the
    # reason, which is what the schema's `unfixed_finding_titles` is for.
    MAX_FILE_CHARS_FOR_REWRITE = 80_000

    def generate_fix(
        self,
        files: List[Dict[str, str]],
        findings: List[Dict[str, Any]],
        context: str = "",
        llm_override: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Remediate findings, one file per model call.

        Findings are routed to the file they name, using the `file_path` the review pipeline
        established and verified. The original handed every finding to every file with the
        location stripped out, so the agent had to infer which of five files each problem
        lived in — and this is the one agent that commits to a real repository, so a wrong
        inference becomes a wrong commit.
        """
        by_path = self._route(files, findings)
        patches: List[Dict[str, Any]] = []
        fixed: List[str] = []
        unfixed: List[str] = []
        commit_lines: List[str] = []
        per_file: List[Dict[str, Any]] = []

        for fl in files:
            path = fl["path"]
            targeted = by_path.get(path) or []
            if not targeted:
                continue
            if len(fl.get("content") or "") > self.MAX_FILE_CHARS_FOR_REWRITE:
                reason = (
                    f"{path} is {len(fl['content']):,} characters — too large to rewrite "
                    f"whole. Fix it by hand, or split the file first."
                )
                unfixed.extend(f"{f.get('title', 'finding')} ({reason})" for f in targeted)
                per_file.append({"path": path, "patched": False, "reason": reason})
                continue

            try:
                part = self._fix_one_file(fl, targeted, context, llm_override)
            except Exception as exc:
                logger.exception("Auto-Fix failed on %s", path)
                unfixed.extend(
                    f"{f.get('title', 'finding')} (Auto-Fix failed on {path}: "
                    f"{type(exc).__name__})" for f in targeted
                )
                per_file.append({"path": path, "patched": False, "reason": str(exc)[:200]})
                continue

            file_patches = part.get("patches") or []
            patches.extend(file_patches)
            fixed.extend(part.get("fixed_finding_titles") or [])
            unfixed.extend(part.get("unfixed_finding_titles") or [])
            if part.get("commit_message"):
                commit_lines.append(part["commit_message"])
            per_file.append({
                "path": path,
                "patched": bool(file_patches),
                "explanation": (file_patches[0].get("explanation", "") if file_patches else ""),
            })

        # A finding whose file was never in `files` at all cannot be fixed, and saying so is
        # more useful than its silent absence from both lists.
        routed = {id(f) for group in by_path.values() for f in group}
        for f in findings or []:
            if id(f) not in routed:
                unfixed.append(
                    f"{f.get('title', 'finding')} (no file content was available for "
                    f"{f.get('file_path') or 'this finding'})"
                )

        return {
            "patches": patches,
            "commit_message": self._commit_message(commit_lines, patches),
            "pr_title": (
                f"Auto-fix: remediate {len(fixed)} review finding(s)" if fixed
                else "Auto-fix: remediate review findings"
            ),
            "pr_description": self._pr_description(per_file, fixed, unfixed),
            "fixed_finding_titles": list(dict.fromkeys(fixed)),
            "unfixed_finding_titles": list(dict.fromkeys(unfixed)),
            "per_file": per_file,
            "agent_name": self.agent_name,
        }

    @staticmethod
    def _route(files: List[Dict[str, str]], findings: List[Dict[str, Any]]) -> Dict[str, List[dict]]:
        """Group findings by the file they name.

        A finding with no path, or one naming a file we were not given, is left unrouted and
        reported as unfixed. Guessing which file it meant is exactly the behaviour this
        replaces — the review pipeline already resolved and verified these paths, so a path
        that does not match here is genuinely absent, not merely spelled differently.
        """
        paths = {fl["path"] for fl in files}
        lowered = {p.lower(): p for p in paths}
        grouped: Dict[str, List[dict]] = {}
        for f in findings or []:
            claimed = (f.get("file_path") or "").lstrip("./").replace("\\", "/")
            match = claimed if claimed in paths else lowered.get(claimed.lower())
            if not match:
                suffix = [p for p in paths if p.lower().endswith("/" + claimed.lower())]
                match = suffix[0] if len(suffix) == 1 else None
            if match:
                grouped.setdefault(match, []).append(f)
        # A single-file change with findings that named nothing is the common PR case; there
        # is no ambiguity about where they belong, so route them rather than refusing.
        if len(files) == 1 and not grouped:
            unplaced = [f for f in findings or [] if not (f.get("file_path") or "").strip()]
            if unplaced:
                grouped[files[0]["path"]] = unplaced
        return grouped

    def _fix_one_file(
        self,
        fl: Dict[str, str],
        findings: List[Dict[str, Any]],
        context: str,
        llm_override: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """One model call: one file, and only the findings that name it."""
        findings_block = "\n\n".join(self._describe_finding(f) for f in findings)
        prompt = f"""## Findings to remediate in this file
{findings_block}

## Current content of {fl['path']}
{fl['content']}
{f"{chr(10)}## Additional context{chr(10)}{context}" if context else ""}

Return the COMPLETE corrected content of `{fl['path']}` in a single patch. Fix only what the
findings above describe. If a finding cannot be fixed safely from what you can see here, list
it in `unfixed_finding_titles` with a short reason instead of guessing.
"""
        result = llm_gateway.call_tool(
            SYSTEM_PROMPT, prompt, AUTOFIX_TOOL_SCHEMA, llm_override,
            max_tokens=self.MAX_OUTPUT_TOKENS,
        )
        # The model is asked for one file; hold it to that, so a stray patch against a path
        # we never supplied cannot reach `commit_file`.
        result["patches"] = [
            p for p in (result.get("patches") or [])
            if (p.get("path") or "").lstrip("./") in (fl["path"], fl["path"].lstrip("./"))
        ] or [
            {**p, "path": fl["path"]}
            for p in (result.get("patches") or [])[:1]
            if p.get("fixed_content")
        ]
        return result

    @staticmethod
    def _describe_finding(f: Dict[str, Any]) -> str:
        """A finding as the fixer needs to see it — including where it is.

        The location and the verbatim quote were both being dropped before this, which is
        what forced the agent to search the file for the problem instead of being pointed at
        it.
        """
        head = (
            f"- [{f.get('severity', '?')}/5 {f.get('risk_level', '')}] "
            f"{f.get('control_id', '')} {f.get('title', '')}"
        )
        parts = [head]
        line = f.get("line_start")
        if isinstance(line, int) and line > 0:
            end = f.get("line_end")
            parts.append(f"  Location: line {line}" + (f"-{end}" if isinstance(end, int) and end > line else ""))
        if f.get("evidence"):
            quoted = "\n".join("    " + l for l in str(f["evidence"]).strip().splitlines()[:3])
            parts.append(f"  Offending code (verbatim):\n{quoted}")
        if f.get("description"):
            parts.append(f"  Why: {f['description']}")
        if f.get("recommendation"):
            parts.append(f"  Recommended fix: {f['recommendation']}")
        if f.get("confidence") and f["confidence"] != "High":
            parts.append(
                f"  Confidence: {f['confidence']} — the reviewer was not certain. If the code "
                f"here does not actually have this problem, do not change it; report it unfixed."
            )
        return "\n".join(parts)

    @staticmethod
    def _commit_message(lines: List[str], patches: List[Dict[str, Any]]) -> str:
        if not patches:
            return "fix: no changes"
        if len(lines) == 1:
            return lines[0]
        subject = f"fix(security): remediate review findings across {len(patches)} file(s)"
        body = "\n".join(f"- {l}" for l in dict.fromkeys(lines))
        return f"{subject}\n\n{body}" if body else subject

    @staticmethod
    def _pr_description(per_file: List[Dict[str, Any]], fixed: List[str], unfixed: List[str]) -> str:
        lines = ["## What changed", ""]
        for entry in per_file:
            if entry.get("patched"):
                lines.append(f"- `{entry['path']}` — {entry.get('explanation', 'fixed')}")
        if not any(e.get("patched") for e in per_file):
            lines.append("_No files were changed._")
        skipped = [e for e in per_file if not e.get("patched")]
        if skipped:
            lines += ["", "## Not changed", ""]
            lines += [f"- `{e['path']}` — {e.get('reason', 'no patch produced')}" for e in skipped]
        if unfixed:
            lines += ["", "## Findings left for a human", ""]
            lines += [f"- {t}" for t in dict.fromkeys(unfixed)]
        return "\n".join(lines)
