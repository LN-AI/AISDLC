import contextvars
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

from backend.agents.base_agent import CODE_REVIEW_TOOL_SCHEMA, BaseAgent
from backend.services import code_chunker, finding_gate, finding_pipeline, llm_gateway
from backend.services.llm_gateway import LLMTruncatedResponseError

logger = logging.getLogger(__name__)

# Concurrency for the fan-out. Kept modest on purpose: the point is to finish a large
# artifact in reasonable wall-clock time, not to burn a per-key rate limit and turn every
# chunk into a retry.
MAX_CHUNK_WORKERS = 4

# How many times a chunk may be halved after a truncated response. Truncation means "more
# findings than the output budget", so halving is the right answer — but a chunk that
# truncates after two halvings is pathological and should surface as an error.
MAX_TRUNCATION_SPLITS = 2

SYSTEM_PROMPT = """You are the LM AI-SDLC Code Security Review Agent — a senior application security engineer and code quality expert.

Your job is to review source code (PR diffs, files, snippets) for security vulnerabilities, secret exposure, dependency risks, and code quality issues.

## Security Vulnerabilities to Detect

### Injection Attacks
- SQL injection: string concatenation in queries, missing parameterization
- Command injection: os.system, subprocess with user input, shell=True
- LDAP injection, XPath injection, XML injection
- Template injection (Jinja2, Mako, Twig, etc.)

### Authentication & Authorization
- Hardcoded credentials, API keys, tokens, passwords, connection strings
- Missing authentication on sensitive endpoints
- JWT without signature verification (algorithm=none)
- Insecure session management
- Missing CSRF protection
- Broken object-level authorization (BOLA/IDOR)
- Missing role checks before privileged operations

### Data Exposure
- Logging of PHI, PII, passwords, tokens, API keys
- Sensitive data returned in API responses unnecessarily
- Unencrypted sensitive data storage
- Stack traces or debug info exposed to users
- Overly permissive CORS settings

### Cryptography
- MD5 or SHA1 for passwords (use bcrypt/argon2)
- Hardcoded crypto keys or IVs
- Weak random number generators for secrets (use secrets module)
- Missing TLS certificate validation

### Dependency Risks
- Pinned to known-vulnerable versions
- Direct use of deprecated/insecure libraries
- Suspicious or typosquatted package names

### API Security
- Missing rate limiting
- Missing input validation and size limits
- Unrestricted file upload
- Missing authentication middleware
- Verbose error messages exposing internals

### AI/LLM-Specific Code Risks
- User input passed directly to LLM without sanitization (prompt injection vector)
- System prompt embedded in client-side code (exposed)
- LLM output used in eval(), exec(), or subprocess without validation
- Agent tools with excessive permissions (file delete, email send, DB writes)
- Missing output validation before using LLM response in critical flows
- API keys for AI services hardcoded or in plaintext config
- No max_tokens or timeout on LLM API calls (DoS risk)

### Code Quality (blocking if severe)
- Overly complex functions (cyclomatic complexity > 15)
- Dead code, commented-out credentials
- Missing error handling in critical paths
- Unsafe deserialization
- Race conditions in concurrent code

## Control Mapping
- SEC-001: Missing auth on endpoint
- SEC-002: Missing RBAC/role check
- SEC-003: Hardcoded secret found
- SEC-004: PII/sensitive data in logs
- SEC-005: Missing rate limiting
- SEC-006: Missing encryption / weak crypto
- SEC-007: Vulnerable dependency
- AI-001: User input unsanitized before LLM
- AI-002: LLM output used without validation
- AI-007: Agent tool over-permissioned
- AI-009: Model output could expose confidential data

## Severity Guide
- Critical (5): Hardcoded secret, SQL injection, auth bypass, PHI in logs
- High (4): Missing auth, IDOR, XSS, command injection, LLM prompt injection vector
- Moderate (3): Missing rate limit, weak crypto, excessive permissions, poor error handling
- Minor (2): Code quality, missing input validation for low-risk fields
- Low (1): Style issues with minor security implications

## Gate Logic
- Red: any Critical finding (hardcoded secrets, SQL injection, auth bypass, PHI exposure)
- Amber: High severity findings present, or multiple Moderate issues
- Green: Only Minor/Low issues, or no findings

## What NOT to report
Precision matters as much as recall here — a review full of things the team already knows
are fine is a review they stop reading. Do not report:
- A query that is already parameterized, or built through an ORM's query builder. Passing a
  variable to `.filter()`, `.where()` or a `?`/`%s` placeholder is not SQL injection.
- A control the framework already provides by default — CSRF tokens in Django/Rails forms,
  escaping in a template engine's default output, auth enforced by middleware.
- Obvious placeholder or example credentials: `AKIAIOSFODNN7EXAMPLE`, `sk-test-...`,
  `password = "changeme"`, anything in a `.env.example` or `*.sample` file.
- Anything in test, fixture, mock, vendored, generated, minified or build-output code.
- A "missing" control when the code that would contain it is not in front of you. If you
  were told other files exist but were not shown them, that control may well be there.
- Style, formatting or naming with no security or correctness consequence.

## Evidence
Every finding must quote the offending code verbatim in `evidence`, copied exactly from
what you were shown — at most 3 lines. That quote is checked against the source: if it does
not appear there, the finding is discarded as unfounded. Do not paraphrase, do not
reconstruct from memory, do not quote code you were not shown.

Set `confidence` honestly. High means the code in front of you is enough to prove the
issue. If you are inferring from something you cannot see, that is Low — report it anyway,
but say so.
"""


class CodeReviewAgent(BaseAgent):
    def __init__(self):
        super().__init__(
            agent_name="Code Security Review Agent",
            system_prompt=SYSTEM_PROMPT,
        )

    def review_code(
        self,
        code: str,
        language: str = "",
        context: str = "",
        coverage_incomplete: bool = False,
        coverage_reason: str = "",
        llm_override: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Review one code artifact and return a gated result.

        The guards below live here, in the agent method, rather than in
        `orchestrator.review_code`, because four of the five callers reach this agent
        without going through the orchestrator at all — `workflows/review_pr.py`,
        `workflows/autofix_pr.py`, `workflows/full_review.py` and
        `workflows/shared.governance_review()` all call `orch.code_agent.review_code(...)`
        directly. AutoFix in particular commits to a real repository off this result, so
        it is the path that can least afford an ungated review.

        `coverage_incomplete` lets a caller say "the artifact I gave you was already cut
        short" (e.g. an oversized PR diff) so the gate can refuse to report Green over
        code it never saw.
        """
        chunks = code_chunker.chunk_artifact(code)
        if not chunks:
            raise RuntimeError("Code review was given an empty artifact — nothing to review.")

        if len(chunks) == 1:
            raw, failures = self._review_chunk(chunks[0].body, language, context, llm_override), []
            raw_findings = list(raw.get("findings") or [])
            model_gate, model_risk = raw.get("gate_status"), raw.get("risk_score")
            summary = raw.get("summary", "")
            recommendations = list(raw.get("recommendations") or [])
            compliance_flags = list(raw.get("compliance_flags") or [])
            guardrail_flags = list(raw.get("guardrail_flags") or [])
        else:
            logger.info("Code review fanning out over %d chunks", len(chunks))
            raw_findings, failures, model_gate, model_risk, summary, recommendations, \
                compliance_flags, guardrail_flags = self._review_chunks(chunks, language, context, llm_override)

        # Every chunk failing is indistinguishable from the agent not running at all —
        # that must be an error, not a clean report over nothing.
        if failures and len(failures) == len(chunks):
            raise RuntimeError(
                f"Code review failed on all {len(chunks)} chunk(s) — no part of the artifact "
                f"was reviewed. First error: {failures[0]['error']}"
            )

        result: Dict[str, Any] = {
            "findings": raw_findings,
            "gate_status": model_gate or "Green",
            "risk_score": model_risk or 0.0,
            "summary": summary,
            "recommendations": recommendations,
            "compliance_flags": compliance_flags,
            "agent_name": self.agent_name,
            "review_type": "code",
        }
        if guardrail_flags:
            result["guardrail_flags"] = guardrail_flags

        # Fail loud rather than persisting a truncated response as a clean Green — the
        # same refusal orchestrator.review_vapt makes. Without it a malformed response is
        # stored as "0 findings, Green" and, on the AutoFix path, is the basis for a
        # commit. Judged on `code`, not on the assembled prompt, so the size threshold
        # reflects the actual artifact.
        degenerate = finding_gate.looks_degenerate(result, code)
        if degenerate:
            raise RuntimeError(
                f"Code review returned an empty result — {degenerate}. "
                "Try again with a smaller artifact."
            )

        # Verify against the whole original artifact, never against a chunk: a chunk is a
        # substring of it, so a genuine quote still matches, and the position recovered is
        # absolute. Line correction is off for diffs — see verify_evidence's docstring.
        processed = finding_pipeline.process(
            result["findings"], code, correct_lines=not code_chunker.is_unified_diff(code)
        )
        # Baseline comparison has to follow `process`, because fingerprints only exist
        # after merging. Waived findings drop out here; everything else is labelled new or
        # pre-existing so a repeat run reads "3 new, 41 pre-existing" instead of "44
        # findings" for the tenth time.
        processed.update(
            finding_pipeline.apply_baseline(processed["findings"], self._baseline(processed["findings"]))
        )
        result.update(processed)

        result["coverage"] = {
            "chunks_total": len(chunks),
            "chunks_succeeded": len(chunks) - len(failures),
            "chunks_failed": len(failures),
            "failures": failures,
        }

        incomplete = coverage_incomplete or bool(failures)
        reason = coverage_reason
        if failures:
            failed_note = (
                f"{len(failures)} of {len(chunks)} chunk(s) of this artifact could not be "
                f"reviewed, so part of the code was never examined."
            )
            reason = f"{reason} {failed_note}".strip() if reason else failed_note

        finding_gate.reconcile_gate(result, coverage_incomplete=incomplete, coverage_reason=reason)
        return result

    @staticmethod
    def _baseline(findings: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        """What this project already decided about these findings.

        Imported lazily: the agent layer does not otherwise depend on the data layer, and
        a module-level import would make that dependency permanent for every agent that
        imports this one. A lookup failure degrades to "no baseline" — every finding then
        reads as new, which is the pre-baseline behaviour and never hides anything.
        """
        fingerprints = [f.get("fingerprint") for f in findings if f.get("fingerprint")]
        if not fingerprints:
            return {}
        try:
            from backend import context, database
            return database.get_finding_baseline(context.current_project_id(), fingerprints)
        except Exception:
            logger.exception("Finding baseline lookup failed — treating every finding as new")
            return {}

    def _review_chunk(
        self,
        body: str,
        language: str,
        context: str,
        llm_override: Optional[Dict[str, Any]] = None,
        depth: int = 0,
    ) -> Dict[str, Any]:
        """One model call over one chunk, halving and retrying on truncation.

        A truncated tool response means the findings did not fit the output budget, not
        that the call was transiently unlucky — so the useful retry is a smaller input,
        not the same one again. Raising max_tokens instead would only move the ceiling.
        """
        lang_hint = f"Language: {language}\n\n" if language else ""
        prompt = f"""Please perform a security and quality review of the following code.

{lang_hint}CODE:
{body}

Check for: security vulnerabilities, hardcoded secrets, PHI/PII exposure, dependency risks, AI/LLM-specific risks, and code quality issues.
Map each finding to the relevant control (SEC-xxx, AI-xxx, GOV-xxx).
Quote the offending code verbatim in `evidence` for every finding, and give its absolute line numbers.
"""
        user_content = prompt
        if context:
            user_content = f"Context:\n{context}\n\n---\n\nContent to Review:\n{prompt}"

        try:
            return llm_gateway.call_tool(
                self.system_prompt, user_content, CODE_REVIEW_TOOL_SCHEMA,
                llm_override, max_tokens=8000,
            )
        except LLMTruncatedResponseError:
            if depth >= MAX_TRUNCATION_SPLITS:
                raise
            lines = body.splitlines()
            if len(lines) < 4:
                raise
            mid = len(lines) // 2
            logger.warning(
                "Chunk truncated at depth %d — splitting %d lines in half and retrying",
                depth, len(lines),
            )
            halves = ["\n".join(lines[:mid]), "\n".join(lines[mid:])]
            merged: Dict[str, Any] = {"findings": [], "recommendations": [], "compliance_flags": []}
            summaries = []
            for half in halves:
                part = self._review_chunk(half, language, context, llm_override, depth + 1)
                merged["findings"].extend(part.get("findings") or [])
                merged["recommendations"].extend(part.get("recommendations") or [])
                merged["compliance_flags"].extend(part.get("compliance_flags") or [])
                if part.get("summary"):
                    summaries.append(part["summary"])
                merged["gate_status"] = finding_gate.stricter(
                    merged.get("gate_status", "Green"), part.get("gate_status", "Green")
                )
                merged["risk_score"] = max(merged.get("risk_score", 0.0), part.get("risk_score", 0.0) or 0.0)
            merged["summary"] = " ".join(summaries)
            return merged

    def _review_chunks(self, chunks, language, context, llm_override):
        """Fan out across chunks, isolating failures so one bad chunk doesn't lose the rest.

        Chunk 1 runs alone first. An Anthropic prompt-cache entry only becomes readable
        once the first response using that prefix has started, so firing all N at once
        means all N pay the full uncached input price; sequencing the first turns the
        remaining N-1 into cache reads.

        `contextvars.copy_context()` is load-bearing, not decoration: the BYOL override and
        the usage accumulator are both contextvars, and without copying them into the
        worker every chunk would bill to nobody. Same reason swarm_service does it.
        """
        results, failures = [], []

        def run(chunk):
            return self._review_chunk(chunk.body, language, context, llm_override)

        def guarded(chunk):
            try:
                return chunk, run(chunk), None
            except Exception as e:
                logger.exception("Code review chunk %d/%d failed", chunk.index, chunk.total)
                return chunk, None, e

        collected = [guarded(chunks[0])]
        if len(chunks) > 1:
            # .copy() per task, not one shared Context: a Context object cannot be entered
            # by two threads at once, and reusing one raises "is already entered". The copy
            # has to be taken here, on the calling thread — taking it inside the worker
            # would snapshot the worker's own empty context and record no usage at all.
            parent_context = contextvars.copy_context()
            with ThreadPoolExecutor(max_workers=MAX_CHUNK_WORKERS) as pool:
                futures = [
                    pool.submit(parent_context.copy().run, guarded, c)
                    for c in chunks[1:]
                ]
                collected += [f.result() for f in futures]

        findings, recommendations, compliance_flags, guardrail_flags = [], [], [], []
        summaries, gate, risk = [], "Green", 0.0
        for chunk, res, err in collected:
            if err is not None:
                failures.append({
                    "chunk": chunk.index,
                    "files": chunk.paths,
                    "error": f"{type(err).__name__}: {err}",
                })
                continue
            results.append(res)
            findings.extend(res.get("findings") or [])
            recommendations.extend(res.get("recommendations") or [])
            compliance_flags.extend(res.get("compliance_flags") or [])
            guardrail_flags.extend(res.get("guardrail_flags") or [])
            if res.get("summary"):
                summaries.append(res["summary"])
            gate = finding_gate.stricter(gate, res.get("gate_status", "Green") or "Green")
            try:
                risk = max(risk, float(res.get("risk_score") or 0.0))
            except (TypeError, ValueError):
                pass

        # Deduplicate the flat string lists — every chunk tends to return the same handful.
        recommendations = list(dict.fromkeys(recommendations))
        compliance_flags = list(dict.fromkeys(compliance_flags))
        guardrail_flags = list(dict.fromkeys(guardrail_flags))
        return (findings, failures, gate, risk, " ".join(summaries),
                recommendations, compliance_flags, guardrail_flags)
