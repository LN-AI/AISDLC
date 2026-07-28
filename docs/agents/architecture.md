# Architecture Review

Evaluates system architecture: identifies the architecture pattern, scores it across 8 quality dimensions (scalability, resilience, maintainability, security, performance, observability, deployability, cost efficiency — each 1–10), detects 15+ structural/data/integration/AI anti-patterns, finds single points of failure, and recommends at least two alternative architectures with fit scores and migration complexity. Uses this project's Architecture Knowledge Base (uploaded standards docs, if any) as the authoritative reference.

**Module:** Architecture Intelligence
**Endpoint:** `POST /api/v1/agents/architecture`
**Agent name (for scopes/BYOL config):** `architecture`

## Authentication

Supply either:
- `X-API-Key: lmsdlc_<64 hex chars>` — for external/team access
- `Authorization: Bearer <jwt>` — for UI-authenticated calls

## Request

| Field | Type | Required | Description |
|---|---|---|---|
| `artifact` | string | one of artifact/github_repo_url/github_file_url/jira_ticket | Architecture description, ADR, C4 notes, or design document |
| `github_repo_url` | string | " | GitHub repository URL (reads README + ARCHITECTURE docs) |
| `github_file_url` | string | " | GitHub file URL |
| `jira_ticket` | string | " | Jira ticket ID or URL |
| `requirements` | string | no | Non-functional requirements/constraints the architecture must satisfy |
| `review_mode` | string | no (default `review`) | `review` — assess current design · `recommend` — suggest improvements for stated requirements · `compare` — score multiple options |
| `project_name`, `context`, `post_back` | — | no | Standard fields |
| `llm_provider`, `llm_api_key`, `llm_base_url`, `llm_model` | — | no | Per-call BYOL override |

**Sources accepted:** `artifact` · `github_repo_url` · `github_file_url` · `jira_ticket`. Resolution priority: `github_file_url` > `github_repo_url` > `jira_ticket` > `artifact`/`text`.

## Response

Custom envelope (via `_wrap`, extended with architecture-specific fields):

| Field | Description |
|---|---|
| `architecture_type` | Identified pattern, e.g. `Microservices`, `Modular Monolith`, `Event-Driven`, `Serverless`, `Hexagonal`, `CQRS+ES`, `AI Agent Architecture`, `Hybrid` |
| `architecture_description` | Brief explanation of what was identified and why |
| `quality_scores` | Object with `scalability`, `resilience`, `maintainability`, `security`, `performance`, `observability`, `deployability`, `cost_efficiency` — each 1–10 |
| `anti_patterns[]` | Each: `name` (e.g. Distributed Monolith, God Service, Chatty Services, Shared Database), `severity` (`Low`\|`Moderate`\|`High`\|`Critical`), `description`, `impact`, `recommendation` |
| `single_points_of_failure[]` | Each: `component`, `failure_scenario`, `mitigation` |
| `scalability_bottlenecks[]` | Strings |
| `alternative_recommendations[]` | Each: `pattern`, `fit_score` (1–10), `justification`, `advantages[]`, `disadvantages[]`, `migration_complexity` (`Low`\|`Medium`\|`High`\|`Very High`), `when_to_choose` |
| `findings[]` | `control_id` (`ARCH-001`–`ARCH-015`, `SEC-006`, `SEC-001`, `GOV-005`), `severity`, `risk_level`, `title`, `description`, `recommendation`, `is_blocking` |
| `anti_pattern_count`, `spof_count`, `alternative_count` | Counts added by the orchestrator |
| `gate_status`, `risk_score`, `summary`, `recommendations[]`, `compliance_flags[]` | Standard fields |
| `_budget_stats` | Top level |

Gate logic: **Red** — shared DB in a multi-tenant system, SPOF with no mitigation, distributed monolith with no improvement path, no DR. **Amber** — missing circuit breakers, no observability, tight coupling, no API versioning. **Green** — sound with minor improvements.

## Guardrails applied

- **Input size limit** — HTTP 413 over `MAX_AGENT_INPUT_CHARS` (400,000 chars)
- **Timeout & retry** — 120s timeout, up to 2 retries on transient errors
- **Prompt-injection scan** — non-blocking; flags surfaced as `guardrail_flags`
- **Untrusted content wrapping** — fetched repo/file/Jira content is wrapped before analysis

## Cost control

- **RBAC** — this key's `scopes` must include `architecture` or be `all`, else HTTP 403
- **Budget** — HTTP 402 once `monthly_budget_usd` is exhausted for the month
- `_budget_stats` appears at the top level of the response

## Example

```bash
curl -X POST https://your-host/api/v1/agents/architecture \
  -H "X-API-Key: lmsdlc_xxx" -H "Content-Type: application/json" \
  -d '{
    "artifact": "12 microservices, all reading and writing the same PostgreSQL database. Deploys happen together as one release train every 2 weeks. No circuit breakers between services.",
    "review_mode": "review",
    "project_name": "Order Management Platform"
  }'
```

Abbreviated response:

```json
{
  "success": true,
  "agent": "architecture",
  "architecture_type": "Distributed Monolith",
  "quality_scores": {"scalability": 3, "resilience": 2, "maintainability": 4, "security": 5, "performance": 4, "observability": 3, "deployability": 2, "cost_efficiency": 5},
  "anti_patterns": [
    {
      "name": "Shared Database",
      "severity": "Critical",
      "description": "All 12 services read/write the same PostgreSQL instance directly.",
      "impact": "Schema changes require coordinating across every service team; no service can scale its data layer independently.",
      "recommendation": "Give each service ownership of its own schema/database; expose data to other services only via API or events."
    }
  ],
  "single_points_of_failure": [
    {"component": "Shared PostgreSQL instance", "failure_scenario": "DB outage takes down all 12 services simultaneously", "mitigation": "Read replicas plus per-service database ownership"}
  ],
  "alternative_recommendations": [
    {"pattern": "Modular Monolith (interim step)", "fit_score": 8, "justification": "Lower migration cost than true microservices while fixing the coupling", "migration_complexity": "Medium"}
  ],
  "gate_status": "Red",
  "risk_score": 82
}
```

## Frameworks / controls referenced

`NIST-AI-RMF` · `SOC2`. Control IDs: `ARCH-001` (SPOF), `ARCH-002` (no circuit breaker), `ARCH-003` (tight sync coupling), `ARCH-004` (missing observability), `ARCH-005` (no DR design), `ARCH-006` (missing caching), `ARCH-007` (no API versioning), `ARCH-008` (missing async decoupling), `ARCH-009` (shared DB), `ARCH-010` (distributed monolith), `ARCH-011` (missing gateway/mesh), `ARCH-012` (no horizontal scale path), `ARCH-013` (missing multi-tenancy isolation), `ARCH-014` (AI/LLM integration not production-grade), `ARCH-015` (no cost architecture), plus `SEC-006`, `SEC-001`, `GOV-005`.
