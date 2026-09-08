"""What the compliance chips on an agent screen actually mean.

Every agent screen renders `Product.frameworks` as a row of chips — `SOC2`, `HIPAA`,
`21CFR11` — and until now they were inert text. A chip that says `21CFR11` to someone who
does not already know what 21 CFR Part 11 is conveys nothing, and a chip that says `SOC2`
to someone who does know may convey something worse: that running this agent has produced
an audit result. Neither is true, and the screen said nothing either way.

So this module is the glossary those chips resolve against, and it carries three things
the chip alone cannot:

  * a **display name**, because the stored tokens are wire identifiers and read badly.
    `21CFR11` is "21 CFR Part 11"; `SOC2` is "SOC 2"; `ISO27001` is "ISO/IEC 27001". The
    tokens are not renamed — they are persisted in `Product.frameworks` and matched against
    `Control.frameworks`, and renaming a load-bearing identifier to improve a label is how
    you get a chip that resolves to nothing. Display is a display concern.

  * a **kind**, because these are not the same category of thing and the UI drew them
    identically. HIPAA is a law. SOC 2 is an audit report a CPA firm issues. CWE is a
    catalogue of weakness types. STRIDE is a method you apply with a whiteboard. CVSS is
    arithmetic. Presenting "STRIDE" and "HIPAA" as the same kind of badge implies a
    regulatory obligation where there is only a technique.

  * the **controls**, when there are any. `control_library` already knows which control ids
    belong to which framework, so the panel can show the actual `HIPAA-004`s a finding from
    this agent could cite rather than a sentence claiming it could. Where there are none —
    CWE, CVSS, STRIDE — the honest answer is that this is a vocabulary rather than a
    control set, not an empty list left to look like a bug.

Alias resolution exists because the two sources drifted. `control_library` tags controls
with both `21-CFR-Part-11` and `21CFR11`, and with both `OWASP-API` and `OWASP-API-Top10`,
while the registry only ever uses the second of each pair. Looking up by the registry token
alone silently under-reports; the aliases below close that without editing 98 control rows.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

from backend.controls import control_library

# Said once, on the payload, rather than repeated in fourteen summaries: a mapping is a
# statement about vocabulary, not a statement about compliance. This is the same discipline
# `attest.coverage_claim` applies to a file list — say what is established, never let it be
# read as more.
MAPPING_CAVEAT = (
    "Mapping means this agent's findings carry control ids from these frameworks, so a "
    "report can cite them. It is not a certification, an audit, or evidence that the "
    "framework is satisfied — a Green gate here is this platform's verdict, not an "
    "auditor's."
)

# The vocabulary for `kind`. Kept short on purpose; the point is to stop a method and a law
# rendering as the same badge, not to build a taxonomy of taxonomies.
REGULATION = "regulation"      # has legal force
STANDARD = "standard"          # certifiable or contractually enforced
ATTESTATION = "attestation"    # an audit report, issued by a firm
FRAMEWORK = "framework"        # voluntary, structures how you manage a risk
TAXONOMY = "taxonomy"          # a catalogue of things that go wrong
METHOD = "method"              # a technique you apply
SCORING = "scoring"            # arithmetic that produces a number

KIND_LABELS = {
    REGULATION:  "Regulation — has legal force",
    STANDARD:    "Standard — certifiable or contractually required",
    ATTESTATION: "Attestation — an audit report issued by a firm",
    FRAMEWORK:   "Framework — voluntary, structures how risk is managed",
    TAXONOMY:    "Taxonomy — a catalogue of weakness types, not an obligation",
    METHOD:      "Method — a technique, not an obligation",
    SCORING:     "Scoring system — produces a severity number",
}


@dataclass(frozen=True)
class Framework:
    id: str                                  # the token stored in Product.frameworks
    name: str                                # how it should read on screen
    kind: str
    summary: str                             # one plain sentence, no jargon
    applies_to: str                          # who it binds, and why they care
    url: str = ""
    aliases: Tuple[str, ...] = field(default_factory=tuple)


FRAMEWORKS: Dict[str, Framework] = {
    "HIPAA": Framework(
        id="HIPAA", name="HIPAA", kind=REGULATION,
        summary="United States law governing protected health information — who may see a "
                "patient's data, how it must be secured, and what must happen when it leaks.",
        applies_to="Healthcare providers, insurers and clearinghouses, and any vendor "
                   "processing health data on their behalf (a 'business associate').",
        url="https://www.hhs.gov/hipaa/",
    ),
    "GDPR": Framework(
        id="GDPR", name="GDPR", kind=REGULATION,
        summary="European Union regulation on personal data: it must be collected for a "
                "stated purpose, kept no longer than needed, and the person it describes "
                "has rights over it.",
        applies_to="Anyone processing the personal data of people in the EU, wherever the "
                   "processing happens.",
        url="https://eur-lex.europa.eu/eli/reg/2016/679/oj",
    ),
    "SOC2": Framework(
        id="SOC2", name="SOC 2", kind=ATTESTATION,
        summary="A report on how a service company handles customer data, judged against "
                "five Trust Services Criteria: security, availability, processing "
                "integrity, confidentiality and privacy.",
        applies_to="Software vendors, usually because a customer's procurement asks for "
                   "one. It is not a law and there is no certificate — an accountancy firm "
                   "issues an opinion.",
        url="https://www.aicpa-cima.com/resources/landing/system-and-organization-controls-soc-suite-of-services",
    ),
    "ISO27001": Framework(
        id="ISO27001", name="ISO/IEC 27001", kind=STANDARD,
        summary="The international standard for running an information security management "
                "system — the documented process by which an organisation decides what to "
                "protect and checks that it did.",
        applies_to="Any organisation that wants to be certified as managing security "
                   "systematically. An accredited body audits and certifies.",
        url="https://www.iso.org/standard/27001",
    ),
    "PCI-DSS": Framework(
        id="PCI-DSS", name="PCI DSS", kind=STANDARD,
        summary="The payment card industry's security standard for handling cardholder "
                "data — network segmentation, encryption, access control and logging.",
        applies_to="Anyone who stores, processes or transmits payment card data. Enforced "
                   "contractually by the card brands and acquiring banks, not by statute.",
        url="https://www.pcisecuritystandards.org/",
    ),
    "21CFR11": Framework(
        id="21CFR11", name="21 CFR Part 11", kind=REGULATION,
        summary="The FDA's rule for keeping records electronically instead of on paper: an "
                "audit trail nobody can edit, signatures tied to a real identified person, "
                "access control, and copies that stay readable for as long as required.",
        applies_to="FDA-regulated companies — pharmaceutical, biotech, medical device — "
                   "whose quality records live in software.",
        url="https://www.ecfr.gov/current/title-21/chapter-I/subchapter-A/part-11",
        aliases=("21-CFR-Part-11",),
    ),
    "GxP": Framework(
        id="GxP", name="GxP", kind=REGULATION,
        summary="'Good x Practice' — the family of quality rules regulated life-sciences "
                "companies work under, where x is Manufacturing, Clinical or Laboratory. "
                "Software supporting them has to be validated and shown to stay validated.",
        applies_to="Pharmaceutical, biotech and medical device companies, and the systems "
                   "in their regulated processes.",
    ),
    "NIST-AI-RMF": Framework(
        id="NIST-AI-RMF", name="NIST AI Risk Management Framework", kind=FRAMEWORK,
        summary="A voluntary framework for managing the risks of an AI system across four "
                "functions — Govern, Map, Measure and Manage — rather than a list of "
                "controls to pass.",
        applies_to="Anyone building or operating AI systems. Increasingly cited by US "
                   "public-sector procurement and by internal AI governance policies.",
        url="https://www.nist.gov/itl/ai-risk-management-framework",
    ),
    "OWASP-LLM": Framework(
        id="OWASP-LLM", name="OWASP Top 10 for LLM Applications", kind=TAXONOMY,
        summary="The ten most critical security risks in applications built on large "
                "language models — prompt injection, insecure handling of model output, "
                "excessive agency, and so on.",
        applies_to="Anyone shipping an LLM-backed feature. An awareness document, not "
                   "something you certify against.",
        url="https://owasp.org/www-project-top-10-for-large-language-model-applications/",
    ),
    "OWASP-Web-Top10": Framework(
        id="OWASP-Web-Top10", name="OWASP Top 10", kind=TAXONOMY,
        summary="The ten most critical web application security risks, revised every few "
                "years from real breach data — broken access control, injection, "
                "cryptographic failures and the rest.",
        applies_to="Any web application. Widely referenced in contracts and security "
                   "questionnaires as a baseline, though it was written as awareness "
                   "material rather than a checklist.",
        url="https://owasp.org/www-project-top-ten/",
    ),
    "OWASP-API-Top10": Framework(
        id="OWASP-API-Top10", name="OWASP API Security Top 10", kind=TAXONOMY,
        summary="The ten most critical risks specific to APIs, where the leading entry is "
                "broken object level authorization — an endpoint that returns another "
                "user's record because it never checked whose record it was.",
        applies_to="Any HTTP API. The risks differ from the web Top 10 because an API has "
                   "no UI to constrain what a caller asks for.",
        url="https://owasp.org/www-project-api-security/",
        aliases=("OWASP-API",),
    ),
    "CWE": Framework(
        id="CWE", name="CWE", kind=TAXONOMY,
        summary="The Common Weakness Enumeration — a community catalogue of software "
                "weakness types, each with a stable number, so that CWE-89 always means SQL "
                "injection to everyone reading the report.",
        applies_to="A shared vocabulary for naming a defect precisely. There is nothing to "
                   "comply with; a finding either cites the right weakness type or it does "
                   "not.",
        url="https://cwe.mitre.org/",
    ),
    "CVSS-3.1": Framework(
        id="CVSS-3.1", name="CVSS v3.1", kind=SCORING,
        summary="The Common Vulnerability Scoring System — a published formula that turns a "
                "vulnerability's characteristics (how it is reached, what it needs, what it "
                "costs you) into a 0–10 score and a vector string showing the working.",
        applies_to="Comparing and ranking vulnerabilities consistently. It measures "
                   "technical severity, not business impact, and it is not a compliance "
                   "obligation.",
        url="https://www.first.org/cvss/v3-1/specification-document",
    ),
    "STRIDE": Framework(
        id="STRIDE", name="STRIDE", kind=METHOD,
        summary="A threat-modelling method: walk each trust boundary in a design and ask "
                "six questions — can it be Spoofed, Tampered with, Repudiated, can it leak "
                "information (Disclosure), be denied (Denial of service), or let someone "
                "gain privilege (Elevation)?",
        applies_to="Design review, before code exists. A technique rather than a standard — "
                   "there is no STRIDE certificate.",
        url="https://learn.microsoft.com/en-us/azure/security/develop/threat-modeling-tool-threats",
    ),
}


def _lookup_tokens(framework_id: str) -> Tuple[str, ...]:
    """Every string `control_library` might have tagged this framework with."""
    entry = FRAMEWORKS.get(framework_id)
    if not entry:
        return (framework_id,)
    return (entry.id,) + tuple(entry.aliases)


def controls_for(framework_id: str) -> List[Dict[str, Any]]:
    """The control ids a finding citing this framework could carry.

    De-duplicated across aliases and ordered by id, so the panel reads as a reference list
    rather than in whatever order the control dictionaries happen to merge.
    """
    seen: Dict[str, Dict[str, Any]] = {}
    for token in _lookup_tokens(framework_id):
        for control in control_library.get_controls_by_framework(token):
            seen[control.id] = {
                "id": control.id,
                "title": control.title,
                "category": control.category,
                "severity": control.severity,
                "blocking": control.blocking,
            }
    return [seen[k] for k in sorted(seen)]


def to_public_dict(framework: Framework) -> Dict[str, Any]:
    """Shape served by GET /api/v1/frameworks."""
    controls = controls_for(framework.id)
    return {
        "id": framework.id,
        "name": framework.name,
        "kind": framework.kind,
        "kind_label": KIND_LABELS.get(framework.kind, framework.kind),
        "summary": framework.summary,
        "applies_to": framework.applies_to,
        "url": framework.url,
        "controls": controls,
        "control_count": len(controls),
        # A taxonomy, a method and a scoring system have no controls, and that is a fact
        # about them rather than a gap in our data. Saying so stops an empty list reading
        # as a bug and stops anyone "fixing" it by inventing rows.
        "controls_note": (
            "" if controls else
            f"{framework.name} is a {framework.kind}, not a control set — findings "
            "reference it as vocabulary rather than citing numbered controls."
        ),
    }


def all_public() -> List[Dict[str, Any]]:
    return [to_public_dict(FRAMEWORKS[k]) for k in sorted(FRAMEWORKS)]


def unknown_tokens(tokens) -> List[str]:
    """Tokens with no glossary entry — what the offline check reports."""
    return sorted({t for t in tokens if t not in FRAMEWORKS})
