import logging
from typing import Any, Callable, Dict, List, Optional

from backend.agents import chunked_review
from backend.agents.base_agent import VAPT_TOOL_SCHEMA, VAPTBaseAgent
from backend.controls.control_library import VAPT_CONTROLS, OWASP_WEB_TOP10_2021, OWASP_API_TOP10_2023
from backend.services import llm_gateway

logger = logging.getLogger(__name__)


VAPT_SYSTEM_PROMPT = """You are a senior penetration tester and security engineer conducting a Vulnerability Assessment and Penetration Testing (VAPT) engagement. Your job is to identify security vulnerabilities with the same rigour as a real penetration test.

## Your Assessment Scope

### Web Application Security — OWASP Top 10 (2021)
- A01: Broken Access Control (IDOR, path traversal, CORS, privilege escalation)
- A02: Cryptographic Failures (weak ciphers, plain-text storage, missing TLS)
- A03: Injection (SQL, NoSQL, OS command, LDAP, XPath, template injection)
- A04: Insecure Design (missing rate limiting, insecure workflows, no threat model)
- A05: Security Misconfiguration (default creds, open cloud storage, verbose errors, missing headers)
- A06: Vulnerable Components (CVEs, outdated libraries, transitive dependencies)
- A07: Auth Failures (weak passwords, no MFA, JWT weaknesses, session fixation)
- A08: Software Integrity Failures (unsigned updates, insecure CI/CD, insecure deserialization)
- A09: Logging Failures (no audit trail, logins not logged, no anomaly detection)
- A10: SSRF (unrestricted URL fetch, metadata API access, internal network scanning)

### API Security — OWASP API Top 10 (2023)
- API1: Broken Object Level Authorization (IDOR in API)
- API2: Broken Authentication (missing token validation, algorithm=none)
- API3: Broken Object Property Level Auth (mass assignment, over-posting)
- API4: Unrestricted Resource Consumption (no rate limiting, no pagination)
- API5: Broken Function Level Authorization (admin endpoints accessible)
- API6: Business Flow Abuse (cart manipulation, account enumeration)
- API7: Server-Side Request Forgery
- API8: Security Misconfiguration (CORS, verbose errors, default keys)
- API9: Improper Inventory (shadow APIs, deprecated endpoints)
- API10: Unsafe Third-Party API Consumption

### Additional Checks
- XSS (Reflected, Stored, DOM-based) and CSRF
- File upload vulnerabilities (unrestricted extension, path traversal)
- Business logic flaws (price manipulation, workflow bypass, race conditions)
- Information disclosure (stack traces, version banners, directory listing)
- SSL/TLS weaknesses (weak cipher suites, missing HSTS, expired certs)
- Missing security headers (CSP, X-Frame-Options, HSTS, etc.)
- Secrets in code or configuration
- Insecure direct object references
- AI/LLM specific: prompt injection, model extraction, adversarial input handling

## CVSS v3.1 Scoring Guide
Use these factors to calculate the CVSS base score:
- Attack Vector: Network(AV:N)=1.0, Adjacent(AV:A)=0.85, Local(AV:L)=0.55, Physical(AV:P)=0.2
- Attack Complexity: Low(AC:L)=easy exploit, High(AC:H)=requires specific conditions
- Privileges Required: None(PR:N)=unauthenticated, Low(PR:L)=user-level, High(PR:H)=admin
- User Interaction: None(UI:N)=no victim needed, Required(UI:R)=victim must act
- Scope: Unchanged(S:U)=same component, Changed(S:C)=affects other components
- CIA Impact: High=complete compromise, Low=partial, None=no impact

## Severity Mapping
- CVSS 9.0–10.0 → Critical (severity=5, blocking=true)
- CVSS 7.0–8.9  → High (severity=4, blocking=true for most)
- CVSS 4.0–6.9  → Moderate/Medium (severity=3)
- CVSS 2.0–3.9  → Minor/Low (severity=2)
- CVSS 0.0–1.9  → Informational (severity=1)

## Control ID Mapping
Use these control IDs for findings:
- Web OWASP A01 → VAPT-001, A02 → VAPT-002, A03 → VAPT-003, A04 → VAPT-004
- A05 → VAPT-005, A06 → VAPT-006, A07 → VAPT-007, A08 → VAPT-008
- A09 → VAPT-009, A10 → VAPT-010
- API1 → VAPT-011, API2 → VAPT-012, API4 → VAPT-013, API5 → VAPT-014
- Network/Infra → VAPT-015, SSL/TLS → VAPT-016, Headers → VAPT-017
- Info Disclosure → VAPT-018, Business Logic → VAPT-019, XSS/CSRF → VAPT-020
- Secrets → SEC-003, Auth → SEC-001, Logging → SEC-004

## Instructions
1. Analyze the provided artifact (code, API spec, architecture, URLs, description) thoroughly.
2. For each vulnerability found, provide a realistic attack scenario and proof-of-concept.
3. Calculate an accurate CVSS v3.1 score based on exploitability and impact.
4. Provide specific, actionable remediation steps — not generic advice.
5. Map each finding to its OWASP category and CWE identifier.
6. Determine gate status:
   - Red: Any Critical or multiple High findings
   - Amber: High findings that need approval, or Moderate findings without workarounds
   - Green: Only Low/Informational findings
7. Be thorough — a missed vulnerability in production is a real security incident.

## Redacted values you will see
Text of the form `__REDACTED_ASSIGNED_CREDENTIAL__` or `__REDACTED_AWS_ACCESS_KEY__` is not
in the customer's source. The platform's scanner found a real hardcoded credential there,
replaced the value before you were shown the file, and has already reported it. Do not
report a hardcoded secret at a placeholder, and do not treat the placeholder as the
vulnerability. Do report anything else the surrounding code reveals — a credential sent over
plain HTTP, a secret logged, an authentication comparison that is not constant-time.

## Evidence — every finding must be provable
Quote the offending code or specification text verbatim in `evidence`, copied exactly from
what you were shown, at most 3 lines. That quote is checked in software against the file
this finding names: if it does not appear there, the finding is discarded as unfounded. Do
not paraphrase, do not reconstruct from memory, do not quote something you were not shown.

Name the file in `file_path` exactly as it appears in the chunk header above the code, and
give absolute line numbers — the numbers shown to you, not renumbered from the top of the
chunk. Leave both empty when the artifact genuinely has no file structure (an OpenAPI
document, an architecture description, a list of URLs); the quote is still required.

Set `confidence` honestly. High means the artifact in front of you proves the vulnerability
is reachable. If you are inferring from configuration or code you cannot see, that is Low —
report it anyway, but say so. A pentest report that cannot distinguish a proven finding from
a suspected one is one a customer cannot prioritise.

## What NOT to report
A report full of things the team already knows are fine is a report they stop reading, and on
a penetration test that costs more credibility than a missed low-severity finding. Do not
report:
- A control the framework provides by default — CSRF tokens in Django/Rails form helpers,
  template auto-escaping, authentication enforced by middleware you were shown.
- A query already parameterized or built through an ORM's query builder.
- Placeholder or example credentials: `AKIAIOSFODNN7EXAMPLE`, `sk-test-...`,
  `password = "changeme"`, anything in a `.env.example` or `*.sample` file.
- Anything in test, fixture, mock, vendored, generated, minified or build-output code.
- A missing control when the code that would contain it is not in front of you. If you were
  told other files exist but were not shown them, that control may well be there — say so in
  `confidence` rather than asserting the gap.
"""


class VAPTAgent(VAPTBaseAgent):
    def __init__(self):
        super().__init__(
            agent_name="VAPT Agent",
            system_prompt=VAPT_SYSTEM_PROMPT,
        )

    def run_vapt(
        self,
        artifact: str = "",
        context: str = "",
        scope: str = "",
        files: Optional[List[Dict[str, Any]]] = None,
        coverage_incomplete: bool = False,
        coverage_reason: str = "",
        is_diff: Optional[bool] = None,
        extra_findings: Optional[List[Dict[str, Any]]] = None,
        suppression: Any = None,
        llm_override: Optional[Dict[str, Any]] = None,
        on_progress: Optional[Callable[[int, int], None]] = None,
    ) -> Dict[str, Any]:
        """Assess one artifact — pasted text, a diff, or a whole repository — and gate it.

        Everything after the model call is `chunked_review.run`, which is the same eleven
        stages the Code Review agent has had all along: split so a large surface is not
        truncated, verify every quote against the file it names, drop invented findings,
        de-duplicate, suppress test and vendor paths, fingerprint so findings can be waived
        and tracked run to run, and floor the gate so a Critical can never come out Green
        (C-73).

        Signature note: `artifact` and `context` keep their positions, because
        `orchestrator.review_vapt` and `swarm_service` both call this positionally.
        """
        def call_chunk(body: str) -> Dict[str, Any]:
            return self._assess_chunk(body, context, scope, llm_override)

        result = chunked_review.run(
            agent_name=self.agent_name,
            review_type="vapt",
            call_chunk=call_chunk,
            artifact=artifact if not files else None,
            files=files,
            is_diff=is_diff,
            coverage_incomplete=coverage_incomplete,
            coverage_reason=coverage_reason,
            extra_findings=extra_findings,
            baseline_lookup=chunked_review.default_baseline,
            resolved_lookup=chunked_review.default_resolved,
            suppression=suppression,
            on_progress=on_progress,
        )
        # Counts the UI and the dashboard read. Computed here rather than in the
        # orchestrator so every caller gets them — `swarm_service` reaches this agent
        # without going through `orchestrator.review_vapt` at all.
        findings = result.get("findings") or []
        result["critical_count"] = sum(1 for f in findings if f.get("severity") == 5)
        result["high_count"] = sum(1 for f in findings if f.get("severity") == 4)
        result["total_findings"] = len(findings)
        return result

    def _assess_chunk(
        self,
        body: str,
        context: str,
        scope: str,
        llm_override: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """One model call over one chunk of the attack surface."""
        controls_ref = "\n".join(f"- {c.id}: {c.title}" for c in VAPT_CONTROLS.values())
        owasp_web = "\n".join(f"- {k}: {v}" for k, v in OWASP_WEB_TOP10_2021.items())
        owasp_api = "\n".join(f"- {k}: {v}" for k, v in OWASP_API_TOP10_2023.items())

        prompt = f"""## Artifact for VAPT Assessment

{body}

## Scope
{scope if scope else "Full application security assessment — web, API, authentication, authorization, injection, cryptography, business logic, and infrastructure."}

## VAPT Control Reference
{controls_ref}

## OWASP Web Top 10 (2021)
{owasp_web}

## OWASP API Top 10 (2023)
{owasp_api}

Conduct a thorough VAPT assessment. For every vulnerability found, provide realistic
exploitation details and CVSS v3.1 scoring. Be specific about affected endpoints and
functions. Quote the offending code or specification text verbatim in `evidence`, name its
file in `file_path`, and give absolute line numbers.
"""
        user_content = prompt
        if context:
            user_content = f"Context:\n{context}\n\n---\n\nArtifact to Assess:\n{prompt}"

        result = llm_gateway.call_tool(
            self.system_prompt, user_content, VAPT_TOOL_SCHEMA, llm_override,
            max_tokens=self.MAX_OUTPUT_TOKENS,
        )
        result["agent_name"] = self.agent_name
        return result
