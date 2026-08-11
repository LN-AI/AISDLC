from typing import Any, Dict, List

from backend.agents.base_agent import BaseAgent
from backend.services import llm_gateway

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

    def generate_fix(
        self,
        files: List[Dict[str, str]],
        findings: List[Dict[str, Any]],
        context: str = "",
    ) -> Dict[str, Any]:
        findings_block = "\n".join(
            f"- [{f.get('severity', '?')}/5 {f.get('risk_level', '')}] {f.get('control_id', '')} "
            f"{f.get('title', '')}: {f.get('description', '')} — Fix: {f.get('recommendation', '')}"
            for f in findings
        ) or "None supplied."

        files_block = "\n\n".join(
            f"--- FILE: {fl['path']} ---\n{fl['content']}" for fl in files
        ) or "None supplied."

        prompt = f"""## Findings to remediate
{findings_block}

## Current file contents
{files_block}
{f"{chr(10)}## Additional context{chr(10)}{context}" if context else ""}

Produce corrected file contents for every finding you can safely fix. List any findings you deliberately did
not address and why.
"""
        result = llm_gateway.call_tool(SYSTEM_PROMPT, prompt, AUTOFIX_TOOL_SCHEMA, max_tokens=16000)
        result["agent_name"] = self.agent_name
        return result
