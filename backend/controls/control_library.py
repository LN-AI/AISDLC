from typing import Dict, List
from dataclasses import dataclass


@dataclass
class Control:
    id: str
    category: str
    title: str
    description: str
    severity: int  # 1-5 default severity when violated
    frameworks: List[str]  # NIST, OWASP, HIPAA, GDPR, etc.
    blocking: bool  # whether violation blocks release by default


SECURITY_CONTROLS: Dict[str, Control] = {
    "SEC-001": Control(
        id="SEC-001",
        category="Security",
        title="Authentication Required",
        description="All endpoints and resources must require authentication. Anonymous access to sensitive data is prohibited.",
        severity=4,
        frameworks=["NIST-AI-RMF", "OWASP-LLM", "SOC2"],
        blocking=True,
    ),
    "SEC-002": Control(
        id="SEC-002",
        category="Security",
        title="Role-Based Access Control",
        description="Access to data and functions must be governed by RBAC. Users must only access what their role permits.",
        severity=4,
        frameworks=["NIST-AI-RMF", "HIPAA", "GDPR", "SOC2"],
        blocking=True,
    ),
    "SEC-003": Control(
        id="SEC-003",
        category="Security",
        title="No Secrets in Code",
        description="API keys, passwords, tokens, or credentials must not be hardcoded in source code or committed to version control.",
        severity=5,
        frameworks=["OWASP-LLM", "SOC2"],
        blocking=True,
    ),
    "SEC-004": Control(
        id="SEC-004",
        category="Security",
        title="No Sensitive Data in Logs",
        description="PHI, PII, passwords, tokens, and confidential data must never be written to application logs.",
        severity=5,
        frameworks=["HIPAA", "GDPR", "SOC2"],
        blocking=True,
    ),
    "SEC-005": Control(
        id="SEC-005",
        category="Security",
        title="API Rate Limiting Required",
        description="All public-facing APIs must implement rate limiting to prevent abuse and denial-of-service attacks.",
        severity=3,
        frameworks=["OWASP-LLM", "NIST-AI-RMF"],
        blocking=False,
    ),
    "SEC-006": Control(
        id="SEC-006",
        category="Security",
        title="Encryption in Transit and at Rest",
        description="All sensitive data must be encrypted using TLS 1.2+ in transit and AES-256 or equivalent at rest.",
        severity=4,
        frameworks=["HIPAA", "GDPR", "SOC2", "NIST-AI-RMF"],
        blocking=True,
    ),
    "SEC-007": Control(
        id="SEC-007",
        category="Security",
        title="Dependency Vulnerability Scan Required",
        description="All third-party dependencies must be scanned for known CVEs before deployment. Critical/High CVEs must be resolved.",
        severity=4,
        frameworks=["OWASP-LLM", "NIST-AI-RMF", "SOC2"],
        blocking=False,
    ),
    # SEC-008..012 were added on 2026-09-02 after the first real eval run, which surfaced the
    # gap: the Code Review prompt lists injection **first** under what to detect and grades it
    # Critical, and there was no control id for it. Anything the model found had to be filed
    # under an unrelated code — the corpus itself guessed `SEC-001` ("Authentication
    # Required") for a string-concatenated SQL query. Three consequences, all worse than the
    # missing row: compliance mapping files injection under authentication, the Governance
    # Report groups it wrong, and — because `finding_pipeline.fingerprint` includes
    # `control_id` — the *same* SQL injection can get a different fingerprint on the next run
    # and read as new forever, which quietly defeats the baseline and every waiver on it.
    #
    # Adding ids is safe where changing one would not be: nothing persisted moves, because no
    # existing row carries these.
    "SEC-008": Control(
        id="SEC-008",
        category="Security",
        title="No Injection Vectors",
        description="User-controlled data must never reach an interpreter unparameterized. Covers SQL, NoSQL, OS command, LDAP, XPath, XML and template injection. Use parameterized queries, argument lists rather than shells, and never build a query or command by concatenation. CWE-89, CWE-77, CWE-78, CWE-917.",
        severity=5,
        frameworks=["OWASP-Web-Top10", "OWASP-LLM", "SOC2", "PCI-DSS"],
        blocking=True,
    ),
    "SEC-009": Control(
        id="SEC-009",
        category="Security",
        title="Output Encoding Against XSS",
        description="Data rendered into a page, response or template must be encoded for its context. Covers reflected, stored and DOM-based cross-site scripting, and any use of an escaping bypass (raw/safe/innerHTML/dangerouslySetInnerHTML) on untrusted data. CWE-79.",
        severity=4,
        frameworks=["OWASP-Web-Top10", "SOC2"],
        blocking=True,
    ),
    "SEC-010": Control(
        id="SEC-010",
        category="Security",
        title="Cross-Site Request Forgery Protection",
        description="State-changing requests must carry an anti-forgery token or an equivalent origin check. A control the framework provides by default (Django's CsrfViewMiddleware, Rails' protect_from_forgery) satisfies this and must not be reported as missing. CWE-352.",
        severity=4,
        frameworks=["OWASP-Web-Top10", "SOC2"],
        blocking=True,
    ),
    "SEC-011": Control(
        id="SEC-011",
        category="Security",
        title="Safe Deserialization",
        description="Untrusted input must not be deserialized into arbitrary objects. Covers pickle, yaml.load without SafeLoader, PHP unserialize, Java ObjectInputStream and equivalents, and any path where a payload chooses the type it becomes. CWE-502.",
        severity=5,
        frameworks=["OWASP-Web-Top10", "SOC2"],
        blocking=True,
    ),
    "SEC-012": Control(
        id="SEC-012",
        category="Security",
        title="Server-Side Request and Path Safety",
        description="A URL or file path derived from user input must be validated against an allow-list before it is fetched or opened. Covers server-side request forgery — including cloud metadata endpoints — and path traversal in file reads, writes and archive extraction. CWE-918, CWE-22.",
        severity=4,
        frameworks=["OWASP-Web-Top10", "SOC2"],
        blocking=True,
    ),
    # SEC-013..016 were added on 2026-09-03 for the same reason as SEC-008..012 above, found
    # the same way — a real finding wearing a control id that contradicted it. A code review
    # of a healthcare API reported "appointment data returned without field-level access audit
    # logging" under SEC-004, which is *sensitive data in logs*: the opposite defect. Too
    # little logging filed under too much.
    #
    # The cause was not one missing row. `ALL_CONTROLS` holds 89 controls across nine
    # families and the Code Review prompt offered the model sixteen of them from two, so five
    # finding classes its own detection list explicitly asks for had nowhere to go: audit
    # logging, information disclosure through errors, CORS, object-level authorization, and
    # input size limits — the last of which the prompt names as an example of a *mis-filed*
    # id without saying where it should go instead.
    #
    # These four stay in SEC-* rather than reusing the VAPT-* equivalents (VAPT-011, -013,
    # -017, -018) on purpose. `finding_pipeline.fingerprint` includes `control_id`, so a
    # finding that a code review calls VAPT-018 and a VAPT scan calls VAPT-018 would share an
    # identity across two agents with different evidence standards and different gates — one
    # agent's waiver would silently suppress the other's finding. Same reasoning that keeps
    # ids stable: identity is a promise, not a label.
    "SEC-013": Control(
        id="SEC-013",
        category="Security",
        title="Access Logging for Sensitive Data",
        description="Reads, writes, deletes and exports of sensitive or regulated data must emit an access log carrying who, when, and which record — not merely that the endpoint was called. Distinct from SEC-004, which is the opposite defect: this is too little logging, SEC-004 is too much. CWE-778.",
        severity=4,
        frameworks=["HIPAA", "SOC2", "GDPR", "PCI-DSS"],
        blocking=False,
    ),
    "SEC-014": Control(
        id="SEC-014",
        category="Security",
        title="No Internal Detail in Responses",
        description="Errors returned to a caller must not carry stack traces, SQL text, file paths, dependency versions or internal hostnames, and a successful response must not return fields the caller has no need for. Covers debug mode left enabled in a deployed configuration. CWE-209, CWE-497.",
        severity=3,
        frameworks=["OWASP-Web-Top10", "SOC2"],
        blocking=False,
    ),
    "SEC-015": Control(
        id="SEC-015",
        category="Security",
        title="Origin Restriction and Security Headers",
        description="Cross-origin access must name its allowed origins — a wildcard, a reflected Origin header, or a wildcard sent with credentials is a finding. Covers absent Content-Security-Policy, HSTS, X-Content-Type-Options and frame-ancestors on responses that render in a browser. CWE-942, CWE-693.",
        severity=3,
        frameworks=["OWASP-Web-Top10", "SOC2"],
        blocking=False,
    ),
    "SEC-016": Control(
        id="SEC-016",
        category="Security",
        title="Object-Level Authorization",
        description="An identifier supplied by the caller must be checked against what that caller may see before the record is returned or modified. Distinct from SEC-002: the caller's role can be entirely correct and the object still not theirs. Covers insecure direct object reference and tenant bleed in a multi-tenant query. CWE-639, CWE-863.",
        severity=4,
        frameworks=["OWASP-Web-Top10", "OWASP-API", "SOC2", "HIPAA"],
        blocking=True,
    ),
}

AI_CONTROLS: Dict[str, Control] = {
    "AI-001": Control(
        id="AI-001",
        category="AI Safety",
        title="Prompt Injection Testing Required",
        description="System prompts must be tested against prompt injection attacks. External input must not override system instructions.",
        severity=5,
        frameworks=["OWASP-LLM", "NIST-AI-RMF"],
        blocking=True,
    ),
    "AI-002": Control(
        id="AI-002",
        category="AI Safety",
        title="Model Output Validation Required",
        description="AI model outputs must be validated before being used in downstream systems or shown to users.",
        severity=4,
        frameworks=["OWASP-LLM", "NIST-AI-RMF"],
        blocking=True,
    ),
    "AI-003": Control(
        id="AI-003",
        category="AI Safety",
        title="Human Approval for High-Risk Decisions",
        description="AI systems must not autonomously make high-risk decisions (medical, financial, legal, employment). Human review is mandatory.",
        severity=5,
        frameworks=["NIST-AI-RMF", "EU-AI-Act", "HIPAA"],
        blocking=True,
    ),
    "AI-004": Control(
        id="AI-004",
        category="AI Safety",
        title="Model Limitations Documented",
        description="A model card must document the model's purpose, limitations, known failure modes, and performance benchmarks.",
        severity=2,
        frameworks=["NIST-AI-RMF", "EU-AI-Act"],
        blocking=False,
    ),
    "AI-005": Control(
        id="AI-005",
        category="AI Safety",
        title="Training and Retrieval Data Source Documented",
        description="The sources, consent status, and quality of training data and RAG knowledge bases must be documented.",
        severity=3,
        frameworks=["NIST-AI-RMF", "GDPR", "EU-AI-Act"],
        blocking=False,
    ),
    "AI-006": Control(
        id="AI-006",
        category="AI Safety",
        title="Hallucination Mitigation Required",
        description="Workflows requiring factual accuracy must include citation requirements, confidence thresholds, or human validation.",
        severity=4,
        frameworks=["NIST-AI-RMF", "OWASP-LLM"],
        blocking=False,
    ),
    "AI-007": Control(
        id="AI-007",
        category="AI Safety",
        title="Agent Tool Permissions Restricted",
        description="AI agents must operate with least-privilege tool access. Destructive or sensitive tools must require explicit approval.",
        severity=5,
        frameworks=["OWASP-LLM", "NIST-AI-RMF"],
        blocking=True,
    ),
    "AI-008": Control(
        id="AI-008",
        category="AI Safety",
        title="RAG Source Citations Required",
        description="RAG-based systems must cite the source documents used in responses to enable verification and auditability.",
        severity=3,
        frameworks=["NIST-AI-RMF", "OWASP-LLM"],
        blocking=False,
    ),
    "AI-009": Control(
        id="AI-009",
        category="AI Safety",
        title="Output Must Not Expose Confidential Data",
        description="Model outputs must be checked to ensure they do not inadvertently disclose training data, system prompts, or confidential information.",
        severity=5,
        frameworks=["OWASP-LLM", "GDPR", "HIPAA"],
        blocking=True,
    ),
    "AI-010": Control(
        id="AI-010",
        category="AI Safety",
        title="Red-Team Testing Required Before Release",
        description="AI features must undergo adversarial/red-team testing before production release to identify misuse scenarios.",
        severity=3,
        frameworks=["NIST-AI-RMF", "EU-AI-Act"],
        blocking=False,
    ),
}

GOVERNANCE_CONTROLS: Dict[str, Control] = {
    "GOV-001": Control(
        id="GOV-001",
        category="Governance",
        title="Business Owner Assigned",
        description="Every AI feature or system must have a named business owner accountable for its use and outcomes.",
        severity=2,
        frameworks=["NIST-AI-RMF", "SOC2"],
        blocking=False,
    ),
    "GOV-002": Control(
        id="GOV-002",
        category="Governance",
        title="Technical Owner Assigned",
        description="Every AI feature must have a named technical owner responsible for maintenance, monitoring, and incident response.",
        severity=2,
        frameworks=["NIST-AI-RMF", "SOC2"],
        blocking=False,
    ),
    "GOV-003": Control(
        id="GOV-003",
        category="Governance",
        title="Risk Owner Assigned",
        description="A risk owner must be designated to track, review, and accept or mitigate risks associated with the AI system.",
        severity=3,
        frameworks=["NIST-AI-RMF"],
        blocking=False,
    ),
    "GOV-004": Control(
        id="GOV-004",
        category="Governance",
        title="Approval Workflow Completed",
        description="All required stakeholder approvals (security, legal, compliance, business) must be obtained before production release.",
        severity=4,
        frameworks=["NIST-AI-RMF", "GxP", "21-CFR-Part-11"],
        blocking=True,
    ),
    "GOV-005": Control(
        id="GOV-005",
        category="Governance",
        title="Audit Trail Maintained",
        description="All reviews, approvals, changes, and decisions must be logged with timestamps and actor identities.",
        severity=4,
        frameworks=["HIPAA", "SOC2", "GxP", "21-CFR-Part-11", "GDPR"],
        blocking=True,
    ),
    "GOV-006": Control(
        id="GOV-006",
        category="Governance",
        title="Release Evidence Stored",
        description="All governance artifacts (reviews, test reports, approvals) must be stored and linked to the release version.",
        severity=3,
        frameworks=["GxP", "21-CFR-Part-11", "SOC2"],
        blocking=False,
    ),
    "GOV-007": Control(
        id="GOV-007",
        category="Governance",
        title="Monitoring Plan Defined",
        description="A monitoring plan must define what metrics, alerts, and drift indicators will be tracked post-deployment.",
        severity=3,
        frameworks=["NIST-AI-RMF", "SOC2"],
        blocking=False,
    ),
    "GOV-008": Control(
        id="GOV-008",
        category="Governance",
        title="Incident Response Plan Defined",
        description="An incident response plan must cover AI-specific failure scenarios including model misbehavior and data breaches.",
        severity=3,
        frameworks=["NIST-AI-RMF", "HIPAA", "SOC2"],
        blocking=False,
    ),
}

VAPT_CONTROLS: Dict[str, Control] = {
    # OWASP Web Application Top 10 (2021)
    "VAPT-001": Control(
        id="VAPT-001", category="VAPT",
        title="Broken Access Control (OWASP A01:2021)",
        description="Restrictions on authenticated users are not enforced. Attackers can access unauthorized functions or data (IDOR, path traversal, privilege escalation, CORS misconfiguration).",
        severity=5, frameworks=["OWASP-Web-Top10", "SOC2"], blocking=True,
    ),
    "VAPT-002": Control(
        id="VAPT-002", category="VAPT",
        title="Cryptographic Failures (OWASP A02:2021)",
        description="Sensitive data exposed due to weak or absent cryptography. Includes plain-text transmission, weak algorithms (MD5, SHA1, DES), missing HSTS, unencrypted storage.",
        severity=5, frameworks=["OWASP-Web-Top10", "HIPAA", "GDPR", "PCI-DSS"], blocking=True,
    ),
    "VAPT-003": Control(
        id="VAPT-003", category="VAPT",
        title="Injection (OWASP A03:2021)",
        description="SQL, NoSQL, OS, LDAP, XML, template, and command injection. Hostile data tricks the interpreter into unintended execution. CWE-89, CWE-77, CWE-917.",
        severity=5, frameworks=["OWASP-Web-Top10"], blocking=True,
    ),
    "VAPT-004": Control(
        id="VAPT-004", category="VAPT",
        title="Insecure Design (OWASP A04:2021)",
        description="Missing or ineffective security controls by design. Includes missing rate limiting on business logic, insecure direct object references in design, and absence of threat modeling.",
        severity=4, frameworks=["OWASP-Web-Top10", "NIST-AI-RMF"], blocking=False,
    ),
    "VAPT-005": Control(
        id="VAPT-005", category="VAPT",
        title="Security Misconfiguration (OWASP A05:2021)",
        description="Improperly configured permissions, unnecessary features enabled, default credentials, verbose error messages, missing security headers, open cloud storage.",
        severity=4, frameworks=["OWASP-Web-Top10", "SOC2"], blocking=False,
    ),
    "VAPT-006": Control(
        id="VAPT-006", category="VAPT",
        title="Vulnerable and Outdated Components (OWASP A06:2021)",
        description="Components (libraries, frameworks, OS, runtime) with known vulnerabilities. Includes unpatched CVEs, unsupported versions, and transitive dependency risks.",
        severity=4, frameworks=["OWASP-Web-Top10", "NIST-AI-RMF"], blocking=False,
    ),
    "VAPT-007": Control(
        id="VAPT-007", category="VAPT",
        title="Authentication and Session Failures (OWASP A07:2021)",
        description="Broken authentication: weak passwords, credential stuffing, missing MFA, insecure session tokens, session fixation, missing logout, JWT weaknesses.",
        severity=5, frameworks=["OWASP-Web-Top10", "HIPAA", "SOC2"], blocking=True,
    ),
    "VAPT-008": Control(
        id="VAPT-008", category="VAPT",
        title="Software and Data Integrity Failures (OWASP A08:2021)",
        description="Code and infrastructure that does not protect against integrity violations: unsigned updates, insecure CI/CD, insecure deserialization (CWE-502), untrusted plugins.",
        severity=4, frameworks=["OWASP-Web-Top10"], blocking=False,
    ),
    "VAPT-009": Control(
        id="VAPT-009", category="VAPT",
        title="Security Logging and Monitoring Failures (OWASP A09:2021)",
        description="Insufficient logging and monitoring: logins not logged, no alerting on brute-force, audit logs not protected, no incident detection capability.",
        severity=3, frameworks=["OWASP-Web-Top10", "HIPAA", "SOC2"], blocking=False,
    ),
    "VAPT-010": Control(
        id="VAPT-010", category="VAPT",
        title="Server-Side Request Forgery / SSRF (OWASP A10:2021)",
        description="Application fetches remote resources without validating the user-supplied URL. Allows attackers to scan internal networks, access metadata APIs, or exfiltrate data.",
        severity=4, frameworks=["OWASP-Web-Top10"], blocking=False,
    ),
    # OWASP API Security Top 10 (2023)
    "VAPT-011": Control(
        id="VAPT-011", category="VAPT",
        title="Broken Object Level Authorization (OWASP API1:2023)",
        description="API endpoints do not validate that the requesting user has permission to access a specific object. IDOR vulnerabilities allow horizontal privilege escalation.",
        severity=5, frameworks=["OWASP-API-Top10"], blocking=True,
    ),
    "VAPT-012": Control(
        id="VAPT-012", category="VAPT",
        title="Broken API Authentication (OWASP API2:2023)",
        description="Weak or absent authentication on API endpoints: missing token validation, algorithm=none in JWT, API keys in URLs, missing token expiry.",
        severity=5, frameworks=["OWASP-API-Top10", "SOC2"], blocking=True,
    ),
    "VAPT-013": Control(
        id="VAPT-013", category="VAPT",
        title="Unrestricted Resource Consumption / Rate Limiting (OWASP API4:2023)",
        description="No rate limiting on API operations leads to brute-force attacks, DoS, and financial abuse. Includes missing pagination limits, unbounded AI API calls.",
        severity=3, frameworks=["OWASP-API-Top10", "OWASP-LLM"], blocking=False,
    ),
    "VAPT-014": Control(
        id="VAPT-014", category="VAPT",
        title="Broken Function Level Authorization (OWASP API5:2023)",
        description="Complex access control policies with different hierarchies are not enforced. Attackers discover undocumented admin endpoints or access privileged functions.",
        severity=5, frameworks=["OWASP-API-Top10"], blocking=True,
    ),
    "VAPT-015": Control(
        id="VAPT-015", category="VAPT",
        title="Security Misconfiguration — Network & Infrastructure",
        description="Open ports, default credentials on services, unnecessary services running, missing firewall rules, unprotected admin interfaces, verbose server banners.",
        severity=4, frameworks=["OWASP-Web-Top10", "SOC2", "NIST-AI-RMF"], blocking=False,
    ),
    "VAPT-016": Control(
        id="VAPT-016", category="VAPT",
        title="SSL/TLS Weakness",
        description="Weak cipher suites (RC4, DES, 3DES, export-grade), SSLv2/SSLv3/TLS 1.0/1.1 supported, self-signed or expired certificates, missing HSTS, certificate pinning absent.",
        severity=4, frameworks=["OWASP-Web-Top10", "PCI-DSS", "HIPAA"], blocking=False,
    ),
    "VAPT-017": Control(
        id="VAPT-017", category="VAPT",
        title="Missing Security Headers",
        description="Absent or misconfigured headers: Content-Security-Policy, X-Frame-Options, X-Content-Type-Options, Referrer-Policy, Permissions-Policy, HSTS.",
        severity=2, frameworks=["OWASP-Web-Top10"], blocking=False,
    ),
    "VAPT-018": Control(
        id="VAPT-018", category="VAPT",
        title="Information Disclosure",
        description="Application leaks sensitive information: stack traces, internal paths, software versions, API keys in responses, verbose error messages, directory listings.",
        severity=3, frameworks=["OWASP-Web-Top10", "GDPR"], blocking=False,
    ),
    "VAPT-019": Control(
        id="VAPT-019", category="VAPT",
        title="Business Logic Vulnerability",
        description="Flaws in application logic: price manipulation, workflow bypass, mass assignment, account takeover via predictable tokens, negative quantity exploits.",
        severity=4, frameworks=["OWASP-Web-Top10", "OWASP-API-Top10"], blocking=False,
    ),
    "VAPT-020": Control(
        id="VAPT-020", category="VAPT",
        title="Cross-Site Scripting (XSS) / CSRF",
        description="XSS: attacker scripts execute in victim's browser. CSRF: forces authenticated users to perform unintended actions. Both enable account takeover and data theft.",
        severity=4, frameworks=["OWASP-Web-Top10"], blocking=False,
    ),
    # Security Architecture
    "SEC-ARCH-001": Control(
        id="SEC-ARCH-001", category="Security Architecture",
        title="Defense in Depth",
        description="Application relies on a single security control rather than layered defenses. A single control failure should not compromise the system.",
        severity=3, frameworks=["NIST-AI-RMF", "SOC2"], blocking=False,
    ),
    "SEC-ARCH-002": Control(
        id="SEC-ARCH-002", category="Security Architecture",
        title="Threat Model Not Defined",
        description="No formal threat model exists for the system. STRIDE or PASTA analysis has not been performed. Attack vectors and trust boundaries are undocumented.",
        severity=3, frameworks=["NIST-AI-RMF", "SOC2"], blocking=False,
    ),
    "SEC-ARCH-003": Control(
        id="SEC-ARCH-003", category="Security Architecture",
        title="Attack Surface Not Minimized",
        description="Unnecessary exposed endpoints, open ports, or enabled features expand the attack surface beyond what is required for functionality.",
        severity=3, frameworks=["NIST-AI-RMF", "SOC2"], blocking=False,
    ),
    "SEC-ARCH-004": Control(
        id="SEC-ARCH-004", category="Security Architecture",
        title="Insecure Third-Party Integration",
        description="External services (APIs, SaaS, cloud services, AI providers) are integrated without security review, data sharing agreements, or contractual security obligations.",
        severity=4, frameworks=["NIST-AI-RMF", "GDPR", "SOC2"], blocking=False,
    ),
    "SEC-ARCH-005": Control(
        id="SEC-ARCH-005", category="Security Architecture",
        title="Secrets Management Not Implemented",
        description="Application secrets (API keys, DB passwords, certs) are not managed through a secrets manager (Vault, AWS Secrets Manager, Azure Key Vault). Rotation is manual or absent.",
        severity=5, frameworks=["SOC2", "NIST-AI-RMF"], blocking=True,
    ),
}

OWASP_WEB_TOP10_2021 = {
    "A01:2021": "Broken Access Control",
    "A02:2021": "Cryptographic Failures",
    "A03:2021": "Injection",
    "A04:2021": "Insecure Design",
    "A05:2021": "Security Misconfiguration",
    "A06:2021": "Vulnerable and Outdated Components",
    "A07:2021": "Identification and Authentication Failures",
    "A08:2021": "Software and Data Integrity Failures",
    "A09:2021": "Security Logging and Monitoring Failures",
    "A10:2021": "Server-Side Request Forgery (SSRF)",
}

OWASP_API_TOP10_2023 = {
    "API1:2023": "Broken Object Level Authorization",
    "API2:2023": "Broken Authentication",
    "API3:2023": "Broken Object Property Level Authorization",
    "API4:2023": "Unrestricted Resource Consumption",
    "API5:2023": "Broken Function Level Authorization",
    "API6:2023": "Unrestricted Access to Sensitive Business Flows",
    "API7:2023": "Server Side Request Forgery",
    "API8:2023": "Security Misconfiguration",
    "API9:2023": "Improper Inventory Management",
    "API10:2023": "Unsafe Consumption of APIs",
}

OWASP_LLM_TOP10 = {
    "LLM01": "Prompt Injection",
    "LLM02": "Insecure Output Handling",
    "LLM03": "Training Data Poisoning",
    "LLM04": "Model Denial of Service",
    "LLM05": "Supply Chain Vulnerabilities",
    "LLM06": "Sensitive Information Disclosure",
    "LLM07": "Insecure Plugin Design",
    "LLM08": "Excessive Agency",
    "LLM09": "Overreliance",
    "LLM10": "Model Theft",
}

NIST_AI_RMF_FUNCTIONS = {
    "GOVERN": "Establish policies, processes, procedures, and practices across AI risk management",
    "MAP": "Identify AI risks in context of organizational risk and benefit",
    "MEASURE": "Analyze and assess AI risks using quantitative and qualitative methods",
    "MANAGE": "Prioritize and address AI risks based on risk tolerance and resources",
}

ARCHITECTURE_CONTROLS: Dict[str, Control] = {
    "ARCH-001": Control(
        id="ARCH-001", category="Architecture",
        title="Single Point of Failure (SPOF)",
        description="A component whose failure causes the entire system to fail. Common SPOFs: single DB instance, single API gateway, no redundancy in critical services.",
        severity=4, frameworks=["NIST-AI-RMF", "SOC2"], blocking=False,
    ),
    "ARCH-002": Control(
        id="ARCH-002", category="Architecture",
        title="No Circuit Breaker Pattern",
        description="Synchronous service calls have no circuit breaker. A downstream failure cascades and causes a system-wide outage instead of failing gracefully.",
        severity=3, frameworks=["NIST-AI-RMF"], blocking=False,
    ),
    "ARCH-003": Control(
        id="ARCH-003", category="Architecture",
        title="Tight Synchronous Coupling Between Services",
        description="Services are synchronously coupled — one service directly calls another at runtime. This creates availability dependency and prevents independent scaling.",
        severity=3, frameworks=["NIST-AI-RMF"], blocking=False,
    ),
    "ARCH-004": Control(
        id="ARCH-004", category="Architecture",
        title="Missing Observability (Logging / Tracing / Metrics)",
        description="No distributed tracing, structured logging, or metrics instrumentation. Impossible to diagnose production issues or measure SLA compliance.",
        severity=3, frameworks=["NIST-AI-RMF", "SOC2"], blocking=False,
    ),
    "ARCH-005": Control(
        id="ARCH-005", category="Architecture",
        title="No Disaster Recovery Design",
        description="No DR plan, RTO/RPO targets undefined, no multi-region or backup strategy, no tested restore procedure. System cannot survive zone-level failures.",
        severity=4, frameworks=["SOC2", "HIPAA", "NIST-AI-RMF"], blocking=False,
    ),
    "ARCH-006": Control(
        id="ARCH-006", category="Architecture",
        title="Missing Caching Strategy",
        description="Repeated expensive operations (DB queries, AI API calls, external API calls) are not cached. Results in unnecessary latency, cost, and load.",
        severity=2, frameworks=["NIST-AI-RMF"], blocking=False,
    ),
    "ARCH-007": Control(
        id="ARCH-007", category="Architecture",
        title="No API Versioning Strategy",
        description="APIs have no versioning scheme (URI, header, or query). Breaking changes will break all consumers simultaneously with no migration path.",
        severity=3, frameworks=["NIST-AI-RMF"], blocking=False,
    ),
    "ARCH-008": Control(
        id="ARCH-008", category="Architecture",
        title="Missing Async / Event-Driven Decoupling",
        description="All inter-service communication is synchronous. Long-running operations block threads. No message queue or event bus for resilient async processing.",
        severity=3, frameworks=["NIST-AI-RMF"], blocking=False,
    ),
    "ARCH-009": Control(
        id="ARCH-009", category="Architecture",
        title="Shared Database Anti-Pattern",
        description="Multiple services share a single database schema, creating hidden coupling. Schema changes in one service can break others and prevent independent deployment.",
        severity=4, frameworks=["NIST-AI-RMF"], blocking=False,
    ),
    "ARCH-010": Control(
        id="ARCH-010", category="Architecture",
        title="Distributed Monolith Anti-Pattern",
        description="System is deployed as microservices but services are so tightly coupled they must be deployed together. Worst of both worlds: distributed complexity without microservice benefits.",
        severity=4, frameworks=["NIST-AI-RMF"], blocking=False,
    ),
    "ARCH-011": Control(
        id="ARCH-011", category="Architecture",
        title="Missing API Gateway / Service Mesh",
        description="No API gateway for cross-cutting concerns (auth, rate limiting, routing, load balancing). Services communicate directly with no traffic management layer.",
        severity=3, frameworks=["NIST-AI-RMF", "SOC2"], blocking=False,
    ),
    "ARCH-012": Control(
        id="ARCH-012", category="Architecture",
        title="No Horizontal Scalability Path",
        description="Architecture cannot scale horizontally. Stateful components, hard-coded instance assumptions, or shared in-process state prevent running multiple instances.",
        severity=4, frameworks=["NIST-AI-RMF", "SOC2"], blocking=False,
    ),
    "ARCH-013": Control(
        id="ARCH-013", category="Architecture",
        title="Missing Multi-Tenancy Isolation",
        description="Multi-tenant system has no data or resource isolation between tenants. A bug or performance issue in one tenant affects all others (noisy neighbour, data leak risk).",
        severity=5, frameworks=["SOC2", "GDPR", "HIPAA"], blocking=True,
    ),
    "ARCH-014": Control(
        id="ARCH-014", category="Architecture",
        title="AI/LLM Integration Not Production-Grade",
        description="LLM/AI API calls lack timeout, retry-with-backoff, rate limit handling, fallback responses, and cost controls. A single LLM outage takes down the entire feature.",
        severity=4, frameworks=["NIST-AI-RMF", "OWASP-LLM"], blocking=False,
    ),
    "ARCH-015": Control(
        id="ARCH-015", category="Architecture",
        title="No Cost Architecture / FinOps Consideration",
        description="Architecture has no cost controls: no AI token budget, unbounded storage growth, no autoscaling limits, no cost alerting. Risk of surprise cloud bills or AI cost overruns.",
        severity=2, frameworks=["NIST-AI-RMF"], blocking=False,
    ),
}

ARCHITECTURE_PATTERNS = {
    "monolith": "Traditional Monolith — single deployable unit, shared DB, layered internally",
    "modular_monolith": "Modular Monolith — single deployment but well-separated internal modules with defined interfaces",
    "microservices": "Microservices — independently deployable services, each owning its data",
    "event_driven": "Event-Driven — services communicate via events/messages, decoupled producers and consumers",
    "serverless": "Serverless/FaaS — functions triggered by events, no server management, pay-per-invocation",
    "hexagonal": "Hexagonal / Clean Architecture — ports and adapters, domain-centric, framework-agnostic core",
    "cqrs_es": "CQRS + Event Sourcing — separate read/write models, full audit trail via event log",
    "layered": "N-Tier / Layered — presentation, business logic, data access layers",
    "soa": "Service-Oriented Architecture (SOA) — coarse-grained services, ESB-based integration",
    "data_mesh": "Data Mesh — domain-owned data products, federated governance, self-serve data platform",
    "ai_agent": "AI Agent Architecture — LLM orchestration, RAG pipeline, tool-use agents, vector stores",
    "hybrid": "Hybrid — combination of multiple patterns across different subsystems",
}

HIPAA_CONTROLS: Dict[str, Control] = {
    "HIPAA-001": Control(
        id="HIPAA-001", category="HIPAA",
        title="PHI Encryption at Rest",
        description="All Protected Health Information (PHI) stored in databases, file systems, or backups must be encrypted using AES-256 or equivalent. Unencrypted PHI violates HIPAA §164.312(a)(2)(iv).",
        severity=5, frameworks=["HIPAA", "NIST-AI-RMF", "SOC2"], blocking=True,
    ),
    "HIPAA-002": Control(
        id="HIPAA-002", category="HIPAA",
        title="PHI Encryption in Transit",
        description="PHI transmitted over any network must use TLS 1.2+ with strong cipher suites. Cleartext transmission of PHI is a Critical HIPAA violation under §164.312(e)(1).",
        severity=5, frameworks=["HIPAA", "NIST-AI-RMF"], blocking=True,
    ),
    "HIPAA-003": Control(
        id="HIPAA-003", category="HIPAA",
        title="Audit Logging for PHI Access",
        description="All access to PHI — reads, writes, deletes, exports — must be logged with user identity, timestamp, and data element accessed. Required by HIPAA §164.312(b).",
        severity=4, frameworks=["HIPAA", "SOC2", "NIST-AI-RMF"], blocking=True,
    ),
    "HIPAA-004": Control(
        id="HIPAA-004", category="HIPAA",
        title="Minimum Necessary Data Access",
        description="Systems and personnel must access only the minimum PHI necessary to perform their function. Role-based access must enforce minimum necessary under §164.514(d).",
        severity=4, frameworks=["HIPAA", "GDPR"], blocking=False,
    ),
    "HIPAA-005": Control(
        id="HIPAA-005", category="HIPAA",
        title="Business Associate Agreement Required",
        description="Any third-party service, AI model provider, or vendor that receives PHI must have a signed Business Associate Agreement (BAA). No PHI to BAA-less vendors per §164.308(b).",
        severity=5, frameworks=["HIPAA"], blocking=True,
    ),
    "HIPAA-006": Control(
        id="HIPAA-006", category="HIPAA",
        title="PHI De-identification Before AI Processing",
        description="PHI must be de-identified (Safe Harbor or Expert Determination) before being sent to AI models, analytics pipelines, or development environments per §164.514(b).",
        severity=5, frameworks=["HIPAA", "NIST-AI-RMF", "OWASP-LLM"], blocking=True,
    ),
    "HIPAA-007": Control(
        id="HIPAA-007", category="HIPAA",
        title="Breach Notification Readiness",
        description="System must have documented incident response and breach notification procedures meeting HIPAA §164.400–414 (notify affected individuals within 60 days, HHS within 60 days, media if >500 in a state).",
        severity=4, frameworks=["HIPAA", "SOC2"], blocking=False,
    ),
}

ISO27001_CONTROLS: Dict[str, Control] = {
    "ISO-001": Control(
        id="ISO-001", category="ISO 27001",
        title="Information Classification Policy",
        description="All information assets must be classified (Public, Internal, Confidential, Restricted) with handling rules defined per ISO 27001 Annex A.8.2. AI training data and outputs must be classified.",
        severity=3, frameworks=["ISO27001", "GDPR", "HIPAA"], blocking=False,
    ),
    "ISO-002": Control(
        id="ISO-002", category="ISO 27001",
        title="Access Control Policy Enforcement",
        description="Access rights must follow need-to-know and least-privilege principles. Access reviews must occur at least quarterly per ISO 27001 A.9.2.5. Privileged accounts must be individually assigned.",
        severity=4, frameworks=["ISO27001", "NIST-AI-RMF", "SOC2"], blocking=True,
    ),
    "ISO-003": Control(
        id="ISO-003", category="ISO 27001",
        title="Secure Development Lifecycle",
        description="Software development must follow a Secure SDLC with security requirements, threat modelling, code review, and security testing at each phase per ISO 27001 A.14. AI systems must include bias and safety testing.",
        severity=4, frameworks=["ISO27001", "NIST-AI-RMF", "OWASP-LLM"], blocking=False,
    ),
    "ISO-004": Control(
        id="ISO-004", category="ISO 27001",
        title="Change Management Controls",
        description="All changes to production systems must go through a formal change management process including risk assessment, approval, and rollback plan per ISO 27001 A.12.1.2.",
        severity=3, frameworks=["ISO27001", "SOC2"], blocking=False,
    ),
    "ISO-005": Control(
        id="ISO-005", category="ISO 27001",
        title="Cryptography Policy",
        description="Use of cryptographic controls must be governed by a policy covering approved algorithms, key lengths, key management, and certificate lifecycle per ISO 27001 A.10.1. Deprecated algorithms (MD5, SHA-1, DES, RC4) are prohibited.",
        severity=4, frameworks=["ISO27001", "NIST-AI-RMF", "HIPAA"], blocking=True,
    ),
    "ISO-006": Control(
        id="ISO-006", category="ISO 27001",
        title="Supplier and Third-Party Security",
        description="Third-party suppliers and SaaS providers must be assessed for security compliance before onboarding. AI model API providers must meet minimum security standards per ISO 27001 A.15.",
        severity=4, frameworks=["ISO27001", "HIPAA", "GDPR"], blocking=False,
    ),
    "ISO-007": Control(
        id="ISO-007", category="ISO 27001",
        title="Security Incident Management",
        description="Security incidents must be detected, reported, and managed through a documented procedure with defined roles, SLAs, and post-incident review per ISO 27001 A.16.",
        severity=3, frameworks=["ISO27001", "SOC2", "HIPAA"], blocking=False,
    ),
}

GXP_CONTROLS: Dict[str, Control] = {
    "GXP-001": Control(
        id="GXP-001", category="GxP / 21 CFR Part 11",
        title="Electronic Records Integrity",
        description="Electronic records must be accurate, complete, consistent, enduring, and available. Systems must prevent and detect record tampering per FDA 21 CFR Part 11 §11.10(a). AI-generated records must meet the same standard.",
        severity=5, frameworks=["21CFR11", "GxP", "ISO27001"], blocking=True,
    ),
    "GXP-002": Control(
        id="GXP-002", category="GxP / 21 CFR Part 11",
        title="Electronic Signature Controls",
        description="Electronic signatures used in GxP systems must be unique to one individual, non-repudiable, linked to their respective records, and meet 21 CFR Part 11 §11.50 requirements.",
        severity=5, frameworks=["21CFR11", "GxP"], blocking=True,
    ),
    "GXP-003": Control(
        id="GXP-003", category="GxP / 21 CFR Part 11",
        title="Audit Trail for GxP Systems",
        description="Computer systems used in GxP processes must maintain time-stamped, operator-specific audit trails for all record creation, modification, and deletion per 21 CFR Part 11 §11.10(e). Audit trail must be retained for required retention period.",
        severity=5, frameworks=["21CFR11", "GxP", "HIPAA"], blocking=True,
    ),
    "GXP-004": Control(
        id="GXP-004", category="GxP / 21 CFR Part 11",
        title="Computer System Validation",
        description="Software and AI systems used in regulated GxP activities must be validated per GAMP 5 or equivalent: IQ (Installation Qualification), OQ (Operational Qualification), PQ (Performance Qualification).",
        severity=4, frameworks=["21CFR11", "GxP", "ISO27001"], blocking=True,
    ),
    "GXP-005": Control(
        id="GXP-005", category="GxP / 21 CFR Part 11",
        title="AI Decision Traceability in Clinical Settings",
        description="AI-assisted clinical decisions must be fully traceable: inputs, model version, inference outputs, and human review actions must be captured. Black-box AI decisions without audit trail are not GxP compliant.",
        severity=5, frameworks=["21CFR11", "GxP", "NIST-AI-RMF", "HIPAA"], blocking=True,
    ),
}

PCI_DSS_CONTROLS: Dict[str, Control] = {
    "PCI-001": Control(
        id="PCI-001", category="PCI DSS",
        title="No Storage of Full PAN After Auth",
        description="Full Primary Account Numbers (PANs) must not be stored after authorization. If storage is required, PANs must be masked or tokenized per PCI DSS Req. 3.3. AI models must not be trained on or receive full PANs.",
        severity=5, frameworks=["PCI-DSS", "OWASP-LLM", "NIST-AI-RMF"], blocking=True,
    ),
    "PCI-002": Control(
        id="PCI-002", category="PCI DSS",
        title="No CVV / CVC Storage",
        description="Security codes (CVV2, CVC2, CID) must never be stored after transaction authorization per PCI DSS Req. 3.2.1. This is an absolute prohibition — no exceptions, including AI training data.",
        severity=5, frameworks=["PCI-DSS"], blocking=True,
    ),
    "PCI-003": Control(
        id="PCI-003", category="PCI DSS",
        title="Cardholder Data Encryption",
        description="Cardholder data transmitted over open, public networks must be encrypted with strong cryptography (TLS 1.2+ with approved cipher suites) per PCI DSS Req. 4.2. Encryption key management must follow Req. 3.6.",
        severity=5, frameworks=["PCI-DSS", "ISO27001"], blocking=True,
    ),
    "PCI-004": Control(
        id="PCI-004", category="PCI DSS",
        title="Access to Cardholder Data Restricted",
        description="Access to cardholder data must be restricted to individuals whose job requires it, with least-privilege access controls and a documented need-to-know policy per PCI DSS Req. 7. AI systems accessing payment data require specific justification.",
        severity=4, frameworks=["PCI-DSS", "ISO27001", "NIST-AI-RMF"], blocking=True,
    ),
    "PCI-005": Control(
        id="PCI-005", category="PCI DSS",
        title="Payment System Logging and Monitoring",
        description="All access to cardholder data systems must be logged with sufficient detail to reconstruct events and detect anomalies per PCI DSS Req. 10. Logs must be retained for at least 12 months with 3 months immediately available.",
        severity=4, frameworks=["PCI-DSS", "SOC2", "ISO27001"], blocking=False,
    ),
}

ALL_CONTROLS: Dict[str, Control] = {
    **SECURITY_CONTROLS,
    **AI_CONTROLS,
    **GOVERNANCE_CONTROLS,
    **VAPT_CONTROLS,
    **ARCHITECTURE_CONTROLS,
    **HIPAA_CONTROLS,
    **ISO27001_CONTROLS,
    **GXP_CONTROLS,
    **PCI_DSS_CONTROLS,
}


def get_control(control_id: str) -> Control | None:
    return ALL_CONTROLS.get(control_id)


def get_controls_by_framework(framework: str) -> List[Control]:
    return [c for c in ALL_CONTROLS.values() if framework in c.frameworks]


def get_blocking_controls() -> List[Control]:
    return [c for c in ALL_CONTROLS.values() if c.blocking]
