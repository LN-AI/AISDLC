# Architecture Decision Record (ADR) Template

## How to Use
Create one ADR file per major architecture decision. Name it: `ADR-NNNN-short-title.md`
Store in: `docs/architecture/decisions/`

---

# ADR-NNNN: [Short Decision Title]

**Date:** YYYY-MM-DD
**Status:** Proposed | Accepted | Deprecated | Superseded by ADR-NNNN
**Deciders:** [Names or roles]
**Reviewed by:** [Architect, Security, Compliance as applicable]

---

## Context

What is the issue that motivates this decision? What forces are at play (technical, business, regulatory, team capability)?

Describe the situation factually. Do not describe the decision itself here.

Example: *Our monolithic application is experiencing scaling issues at peak load. The team has grown to 15 developers working on the same codebase, causing merge conflicts and slow CI builds.*

---

## Decision Drivers

- [Driver 1: e.g. Must support 10× current load within 6 months]
- [Driver 2: e.g. 5-person team, limited DevOps capacity]
- [Driver 3: e.g. HIPAA compliance required]
- [Driver 4: e.g. Budget constraint — no new infrastructure spend >$5k/month]

---

## Considered Options

### Option A: [Name]
[1–2 sentence description]

**Pros:**
- [Pro 1]
- [Pro 2]

**Cons:**
- [Con 1]
- [Con 2]

### Option B: [Name]
...

### Option C: [Name]
...

---

## Decision

**We will: [Selected option]**

[Explain why this option was chosen over the others. Reference the decision drivers. Be specific.]

---

## Consequences

### Positive
- [What becomes easier or better]

### Negative
- [What becomes harder or worse — be honest]

### Risks
- [What could go wrong] — Mitigation: [how we address it]

---

## Implementation Notes

[Any specific implementation guidance, standards to follow, or pitfalls to avoid]

---

## Review Date

[When should this decision be revisited? e.g. After 6 months, after reaching 100k users, after team grows to 30 devs]
