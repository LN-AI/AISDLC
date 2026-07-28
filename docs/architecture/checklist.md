# Architecture Review Checklist

Use this checklist for every architecture review. Mark each item Pass / Fail / N/A.

---

## 1. Foundation

- [ ] Architecture type is explicitly identified and documented
- [ ] Bounded contexts are defined and respected
- [ ] Data ownership is clear — each dataset has one authoritative owner
- [ ] Technology choices are justified (not just familiar)
- [ ] Architecture Decision Records (ADRs) exist for key decisions

---

## 2. Scalability

- [ ] Services/components can scale horizontally
- [ ] No shared mutable in-process state between instances
- [ ] Database has a scaling strategy (read replicas, sharding, or connection pool)
- [ ] Caching strategy defined for expensive operations
- [ ] Load testing performed or planned at 3× expected peak

---

## 3. Resilience

- [ ] No single point of failure in the critical path
- [ ] Circuit breakers on all synchronous external calls
- [ ] Retry logic with exponential backoff on all external calls
- [ ] Graceful degradation defined for each external dependency
- [ ] Multi-AZ deployment or equivalent redundancy
- [ ] DR plan: RTO and RPO targets defined and tested

---

## 4. Security

- [ ] Authentication required on all endpoints (no anonymous access to data)
- [ ] Authorisation enforced at service boundary (RBAC/ABAC)
- [ ] All external inputs validated and sanitised
- [ ] Secrets in secrets manager (not in config files or environment variables in plain text)
- [ ] TLS 1.2+ on all inter-service and external communication
- [ ] Security headers present on all HTTP responses
- [ ] Dependency vulnerability scan in CI pipeline
- [ ] PHI/PII not logged anywhere

---

## 5. Observability

- [ ] Structured JSON logging with correlation ID on all services
- [ ] Distributed tracing instrumented (OpenTelemetry recommended)
- [ ] RED metrics (request Rate, Error rate, Duration) exposed per service
- [ ] Alerting configured on SLO breach (error rate, p99 latency)
- [ ] On-call runbook exists for each alert
- [ ] Dashboards available for: service health, error rates, latency, DB performance

---

## 6. Data

- [ ] Data classification completed (public / internal / confidential / PHI/PII)
- [ ] Data at rest encrypted (AES-256 or equivalent)
- [ ] Data in transit encrypted (TLS)
- [ ] Backup and restore tested
- [ ] Data retention policy defined
- [ ] GDPR/HIPAA data deletion capability if required

---

## 7. Deployment

- [ ] CI/CD pipeline exists with automated tests
- [ ] Rollback procedure documented and tested
- [ ] Blue/green or canary deployment capability
- [ ] Feature flags available for gradual rollout
- [ ] Deployment does not require downtime (or downtime is explicitly accepted and communicated)
- [ ] Infrastructure as Code (Terraform, Pulumi, CDK)

---

## 8. AI/LLM Specific (if applicable)

- [ ] LLM API calls have timeout and retry
- [ ] Fallback exists for LLM failure
- [ ] Token budget set with alerting
- [ ] System prompt isolated from user input
- [ ] Agent tool permissions follow least privilege
- [ ] Human approval gate for destructive agent actions
- [ ] RAG retrieval respects data access controls
- [ ] AI outputs validated before use in downstream systems
- [ ] Model version pinned
- [ ] Red-team testing completed

---

## 9. Governance

- [ ] Business owner assigned
- [ ] Technical owner assigned
- [ ] Risk assessment completed
- [ ] Compliance requirements identified (HIPAA, GDPR, GxP, SOC2)
- [ ] Audit trail implemented for regulated data
- [ ] Incident response plan defined
- [ ] Monitoring plan post-deployment defined

---

## Scoring

Count Pass items / Total applicable items × 100 = Architecture Readiness Score

| Score | Status |
|---|---|
| 90–100% | Green — ready to proceed |
| 70–89%  | Amber — proceed with documented exceptions |
| <70%    | Red — significant gaps must be addressed first |
