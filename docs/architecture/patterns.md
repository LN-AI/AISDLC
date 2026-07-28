# Architecture Patterns Reference

## 1. Traditional Monolith
**When to use:** Small teams (<5 devs), early-stage product, simple domain, low traffic.
**Key traits:** Single deployable unit, shared database, all logic in one process.
**Strengths:** Simple to develop, test, deploy, debug. Low operational overhead.
**Weaknesses:** Scales as a whole unit only, single tech stack, long build times at scale, deployment risk is high.
**Anti-pattern risk:** Becomes a Big Ball of Mud without discipline.

## 2. Modular Monolith
**When to use:** Team growing (5–15 devs), domain complexity increasing, not yet at microservices scale.
**Key traits:** Single deployment, clearly bounded internal modules, each module owns its data (no cross-module DB queries), communicates via defined interfaces (not direct function calls).
**Strengths:** Monolith simplicity + module isolation. Easy path to microservices later.
**Weaknesses:** Requires discipline to maintain boundaries. Still single deployment unit.
**Recommended for:** Most products under 50k users that don't yet need independent scaling.

## 3. Microservices
**When to use:** Large teams (multiple squads), independent scaling required, different tech stacks needed, high availability requirements.
**Key traits:** Each service independently deployable, owns its data store, communicates via REST/gRPC/events.
**Strengths:** Independent scaling, independent deployments, technology diversity, team autonomy.
**Weaknesses:** Distributed system complexity, network latency, eventual consistency, harder debugging, high operational overhead.
**Minimum viable:** API gateway, service discovery, distributed tracing, circuit breakers, centralized logging.
**Anti-pattern risk:** Distributed Monolith if services are too tightly coupled.

## 4. Event-Driven Architecture
**When to use:** High throughput, loose coupling required, audit trail needed, multiple consumers of the same event.
**Key traits:** Message broker (Kafka, RabbitMQ, AWS SQS/SNS), async producers and consumers, events as source of truth.
**Strengths:** Loose coupling, high throughput, natural audit log, easy to add new consumers.
**Weaknesses:** Eventual consistency, harder debugging (async flows), message ordering complexity, schema evolution.
**Best paired with:** Microservices or CQRS.

## 5. Serverless / FaaS
**When to use:** Bursty or unpredictable workloads, event-driven processing, simple stateless functions, cost-sensitive.
**Key traits:** AWS Lambda / Azure Functions / GCP Cloud Functions, triggered by events, no persistent server, pay-per-invocation.
**Strengths:** Zero server management, auto-scaling to zero, cost-efficient for low/bursty traffic.
**Weaknesses:** Cold start latency, 15-min execution limit (AWS Lambda), harder local development, vendor lock-in, stateless only.
**Avoid for:** Long-running processes, stateful workflows, latency-sensitive APIs under consistent load.

## 6. Hexagonal / Clean Architecture
**When to use:** Complex domain logic, long-lived system requiring many integrations, high testability requirement.
**Key traits:** Domain at the centre. Ports define what the domain needs. Adapters implement ports for specific tech (DB, API, UI). Framework-agnostic core.
**Strengths:** Testable domain without infrastructure, easy to swap adapters (change DB, add new API), protects business logic from framework changes.
**Weaknesses:** More boilerplate, steeper learning curve, overkill for simple CRUD apps.

## 7. CQRS + Event Sourcing
**When to use:** Audit trail mandatory (finance, healthcare, GxP), complex read/write scaling imbalance, temporal queries needed.
**Key traits:** Command model writes events. Query model built from events. State is derived from event log.
**Strengths:** Complete audit trail, temporal queries (replay history), independent read/write scaling, natural event bus.
**Weaknesses:** High complexity, eventual consistency on reads, event schema evolution is hard, significant operational overhead.
**Regulatory fit:** GxP, 21 CFR Part 11, HIPAA audit requirements — event sourcing provides immutable audit trail.

## 8. N-Tier / Layered
**When to use:** Traditional enterprise apps, team familiar with MVC/MVP, simple CRUD-heavy applications.
**Key traits:** Presentation → Business Logic → Data Access layers. Each layer only calls the layer below.
**Strengths:** Familiar, well-understood, easy to reason about.
**Weaknesses:** Tends toward tight vertical coupling, hard to scale individual layers, often results in anemic domain model.

## 9. Data Mesh
**When to use:** Large organisation (100+ data producers), domain-owned analytics, federated governance needed.
**Key traits:** Domains own their data as products. Self-serve data platform. Federated computational governance. Domain-oriented ownership.
**Strengths:** Scales data ownership, domain expertise in data quality, decentralised.
**Weaknesses:** High organisational maturity required, complex governance, not suitable for small teams.

## 10. AI Agent Architecture
**When to use:** LLM-powered features, autonomous workflows, RAG-based knowledge retrieval, tool-calling automation.
**Key traits:** LLM as orchestrator, tools as capabilities, RAG for grounding, human-in-the-loop gates for high-risk decisions.
**Strengths:** Flexible automation, natural language interface, adaptable workflows.
**Critical requirements:**
- **Prompt isolation:** System prompts must not be overridable by user input
- **Tool least-privilege:** Agents must only have access to tools they need
- **Human approval gates:** High-risk actions (send email, delete data, approve payment) require human confirmation
- **Fallback handling:** LLM API down must not break the whole product
- **Token budget:** Unbounded LLM calls create runaway costs
- **Output validation:** LLM outputs must be validated before use in downstream systems
- **RAG citation:** Retrieval-augmented outputs must cite sources

## 11. Hybrid Architecture
**When to use:** Large enterprise systems, legacy modernisation, different domains with different scaling needs.
**Key traits:** Different subsystems use different patterns. Core domain may be microservices while analytics is event-driven, ML pipeline is serverless, etc.
**Governance requirement:** Define clear boundaries between pattern zones. Document which pattern applies where and why.
