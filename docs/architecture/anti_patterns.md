# Architecture Anti-Patterns

## Structural Anti-Patterns

### Distributed Monolith
**What it is:** System deployed as separate services but services are so tightly coupled they must be deployed together, share a database, or chain synchronously.
**How to detect:** Services cannot be deployed independently. A change in Service A requires redeploying Service B. All services share one database schema.
**Impact:** Worst of both worlds — distributed system complexity without microservice independence.
**Fix:** Identify bounded contexts. Give each service its own data store. Introduce async messaging between services. Define explicit API contracts.

### God Service
**What it is:** One service that does everything — authentication, user management, business logic, reporting, notifications, integrations.
**How to detect:** One service has hundreds of endpoints. All teams depend on it. It's always the bottleneck. It has the most incidents.
**Impact:** Single point of failure, impossible to scale specific parts, team bottleneck, slow deployments.
**Fix:** Extract bounded contexts into separate services. Start with the highest-traffic or most independent domain.

### Chatty Services
**What it is:** Service A makes 10–50 synchronous HTTP calls to Service B, C, D on every user request.
**How to detect:** High latency on simple operations. Request traces show many sequential service calls. Adding Service E requires modifying A, B, C, D.
**Impact:** Latency multiplication, tight coupling, cascading failures, impossible to debug.
**Fix:** Aggregate calls into a purpose-built orchestration service. Use async events for non-critical data. Apply CQRS to pre-build read models.

### Shared Database (Shared Nothing violation)
**What it is:** Multiple services read and write to the same database tables or schema.
**How to detect:** Service A's ORM models include tables owned by Service B. A schema migration in Service B breaks Service A.
**Impact:** Hidden coupling, schema changes ripple across services, impossible to scale services independently, no service autonomy.
**Fix:** Each service owns its schema. Use events or APIs to share data. Accept eventual consistency where appropriate.

### Nano-Services
**What it is:** Services so small they have more overhead (deployment, monitoring, networking, auth) than value.
**How to detect:** A service is a single function. Deploying it requires 200 lines of Kubernetes YAML. The domain logic is 5 lines.
**Impact:** Operational explosion, latency overhead, complex debugging, high DevOps cost.
**Fix:** Merge related nano-services into a cohesive bounded context.

### Stovepipe System
**What it is:** Each team builds its own version of shared infrastructure (auth, logging, notifications, storage) with no reuse.
**How to detect:** 4 teams each have their own auth implementation. 3 different email sending libraries in use. No shared platform.
**Impact:** Inconsistent behaviour, security gaps in home-grown auth, wasted effort, impossible to apply organisation-wide policies.
**Fix:** Extract shared capabilities into platform services or libraries. Mandate use of shared auth, logging, and notification services.

---

## Data Anti-Patterns

### Single Database for Everything
**What it is:** One relational database used for all data needs — full-text search, time-series metrics, document storage, message queue, session store.
**Impact:** Poor performance for non-relational workloads, unnecessary load on primary DB.
**Fix:** Use purpose-built stores: Elasticsearch for search, TimescaleDB or InfluxDB for metrics, Redis for sessions/cache, S3 for documents.

### No Read Replica
**What it is:** All read queries hit the primary database. No read replicas or CQRS read model.
**Impact:** Primary DB becomes bottleneck, analytics queries compete with transactional writes, single point of failure for reads.
**Fix:** Add read replicas. Route read-heavy queries (reports, analytics, dashboards) to replicas. Consider CQRS for complex read models.

### Missing Idempotency
**What it is:** API endpoints and message consumers are not idempotent. Retrying a failed request causes duplicate processing.
**Impact:** Double charges, duplicate records, data corruption on retry.
**Fix:** Use idempotency keys on payment/critical endpoints. Design message consumers to be idempotent (check if already processed). Use database unique constraints.

---

## Integration Anti-Patterns

### Point-to-Point Integration (N×M Problem)
**What it is:** Every service calls every other service directly via REST, creating N×M connections.
**Impact:** Changing one service interface requires updating all callers. Adding a new service requires knowing all producers.
**Fix:** Introduce a message broker (Kafka, RabbitMQ). Use event-driven integration. Services publish events; interested services subscribe.

### Synchronous Everything
**What it is:** All operations — including long-running jobs, file processing, email sending, report generation — are handled synchronously in the HTTP request lifecycle.
**Impact:** Timeouts on long operations, poor user experience, thread pool exhaustion, no retry mechanism.
**Fix:** Use async task queues (Celery, SQS, BullMQ) for long-running operations. Return a job ID immediately; poll or use webhooks for completion.

---

## AI/LLM-Specific Anti-Patterns

### Unbounded LLM Calls
**What it is:** No rate limiting, token budgets, or cost controls on LLM API usage. Every user action triggers unlimited LLM calls.
**Impact:** Runaway cloud costs, accidental DoS on the LLM API, no cost predictability.
**Fix:** Set per-user and per-day token budgets. Implement rate limiting. Add cost alerting (alert at 80% of monthly budget). Cache LLM responses for repeated queries.

### No Fallback for LLM Failure
**What it is:** When the LLM API is down or rate-limited, the entire feature returns a 500 error.
**Impact:** LLM outage = product outage. Claude/OpenAI have ~99.9% uptime SLAs — not 99.99%.
**Fix:** Design graceful degradation. Return cached responses. Show a "AI assistant temporarily unavailable" message. Have a non-AI fallback path for critical workflows.

### System Prompt in Application Code
**What it is:** System prompts are hardcoded strings in application source code. No versioning, A/B testing, or non-developer editing.
**Impact:** Prompt changes require code deploys. No ability to A/B test prompts. Security review of prompts requires code review.
**Fix:** Store prompts in a prompt management system or database. Version them. Allow safe editing without deployment.

### Synchronous RAG in Critical Path
**What it is:** Every user request triggers a real-time vector DB lookup + LLM call in the synchronous request path with no caching.
**Impact:** P99 latency of 3–8 seconds on every request. Vector DB becomes bottleneck. LLM rate limits cause request failures.
**Fix:** Cache frequent RAG results (Redis with TTL). Pre-compute answers for common queries. Use async background refresh for cache.

### Over-Permissioned Agent
**What it is:** An AI agent has access to all tools — delete records, send emails to all users, modify production config — and acts without approval gates.
**Impact:** One prompt injection or hallucination can cause irreversible damage (mass email, data deletion, financial transactions).
**Fix:** Apply least-privilege to agent tools. Destructive/irreversible actions require human confirmation. Implement tool permission levels: read-only / read-write / destructive (human-in-the-loop required).
