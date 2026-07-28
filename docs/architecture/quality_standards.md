# Architecture Quality Standards

## Quality Attribute Scoring Guide

Score each dimension 1–10. Use this guide to calibrate scoring.

---

### Scalability (Can the system handle 10× more load?)

| Score | Criteria |
|---|---|
| 9–10 | Stateless services, horizontal auto-scaling, no shared mutable state, database sharding strategy defined, CDN for static assets, async processing for spikes |
| 7–8 | Mostly stateless, horizontal scaling possible with minor changes, read replicas in place, some caching |
| 5–6 | Can scale but requires manual intervention, some stateful components, database is primary bottleneck |
| 3–4 | Vertical scaling only, shared in-process state, session stickiness required, no caching |
| 1–2 | Cannot scale at all, single node, no path to scaling identified |

**Minimum standard:** Score ≥6 before production launch for any user-facing system.

---

### Resilience (Can the system survive component failures?)

| Score | Criteria |
|---|---|
| 9–10 | Circuit breakers on all external calls, retry with exponential backoff, bulkhead pattern, graceful degradation, no SPOFs, multi-AZ/multi-region, chaos testing performed |
| 7–8 | Circuit breakers in place, retries implemented, graceful degradation for non-critical features, multi-AZ |
| 5–6 | Some retries, partial graceful degradation, single-AZ but with snapshots |
| 3–4 | No circuit breakers, synchronous chains can cascade, partial SPOFs |
| 1–2 | Multiple SPOFs, no retry logic, entire system fails if one component fails |

**Minimum standard:** Score ≥7 for systems handling financial data or healthcare data. Score ≥5 for internal tools.

---

### Maintainability (How easy is it to change the system safely?)

| Score | Criteria |
|---|---|
| 9–10 | Clear bounded contexts, low coupling, high cohesion, comprehensive test suite (>80% coverage), automated linting/formatting, well-documented APIs, ADRs maintained |
| 7–8 | Reasonable separation of concerns, good test coverage, documented, manageable tech debt |
| 5–6 | Some coupling, moderate test coverage, documentation gaps, some tech debt |
| 3–4 | High coupling between modules, low test coverage, poorly documented, significant tech debt |
| 1–2 | Big ball of mud, no tests, impossible to change without breaking other things |

---

### Security (Is the system protected against attacks?)

| Score | Criteria |
|---|---|
| 9–10 | Defence in depth, all inputs validated, RBAC fully enforced, secrets in vault, TLS everywhere, security headers, WAF, regular penetration testing, no known CVEs |
| 7–8 | Authentication and authorisation in place, TLS, secrets managed, most inputs validated |
| 5–6 | Basic auth, TLS, but gaps in authorisation, some inputs not validated |
| 3–4 | Auth present but weak, some endpoints unprotected, secrets in config files |
| 1–2 | No meaningful security controls, critical vulnerabilities present |

**Minimum standard:** Score ≥7 for any system handling PII, PHI, or financial data.

---

### Performance (Does the system meet latency/throughput requirements?)

| Score | Criteria |
|---|---|
| 9–10 | P99 <500ms for all user-facing APIs, caching at multiple layers, database queries optimised (no N+1), CDN, async processing for heavy operations |
| 7–8 | P99 <1s for most operations, basic caching, no obvious N+1 queries |
| 5–6 | P99 2–5s for some operations, some caching, occasional slow queries |
| 3–4 | P99 >5s, no caching, known N+1 queries, database is always at 80%+ CPU |
| 1–2 | Timeouts common, system is regularly unavailable due to performance, no optimisation |

---

### Observability (Can you understand what the system is doing in production?)

| Score | Criteria |
|---|---|
| 9–10 | Distributed tracing (OpenTelemetry), structured logs with correlation IDs, RED metrics (rate/errors/duration) per service, dashboards, alerting on SLOs, on-call runbooks |
| 7–8 | Structured logs, basic metrics, some tracing, dashboards exist |
| 5–6 | Logs exist but not structured, basic metrics (CPU/memory only), no tracing |
| 3–4 | Logs are print statements, no centralised logging, debugging requires production access |
| 1–2 | No logging, no metrics, impossible to understand what is happening in production |

**Minimum standard:** Score ≥6 before production. Score ≥8 for regulated environments (HIPAA, GxP).

---

### Deployability (How safe and fast is the deployment process?)

| Score | Criteria |
|---|---|
| 9–10 | Fully automated CI/CD, feature flags, canary/blue-green deployments, automated rollback, <15min deploy time, deploy any time including Friday |
| 7–8 | CI/CD pipeline, automated tests in pipeline, rollback procedure, <30min deploy |
| 5–6 | Partial automation, manual steps in deployment, rollback is manual |
| 3–4 | Manual deployments, no rollback automation, deployment requires downtime |
| 1–2 | Big-bang deployments, no CI/CD, deployment is a high-risk event requiring maintenance windows |

---

### Cost Efficiency (Is the system spending money wisely?)

| Score | Criteria |
|---|---|
| 9–10 | Right-sized compute, autoscaling configured, serverless for bursty workloads, reserved/spot instances for predictable load, cost alerting, AI token budgets set, monthly cost reviewed |
| 7–8 | Autoscaling in place, reasonable right-sizing, basic cost monitoring |
| 5–6 | Some over-provisioning, manual right-sizing, irregular cost review |
| 3–4 | Significant over-provisioning, no autoscaling, costs increasing without visibility |
| 1–2 | Always-on large instances doing very little, no cost controls, surprise bills monthly |

---

## Architecture Review Thresholds

| Threshold | Action Required |
|---|---|
| Any dimension ≤2 | Must fix before production |
| Any dimension ≤3 | Fix within current sprint |
| Any dimension ≤5 for HIPAA/GxP systems | Must fix before production |
| Overall average <5 | Architecture review required before proceeding |
| Security score <6 | Security review mandatory |
| Resilience score <5 for financial/healthcare | Must fix before production |
