# Cloud Architecture Patterns & Standards

## AWS Reference Patterns

### Standard Web Application
```
Route 53 → CloudFront (CDN) → ALB → ECS/EKS (API) → RDS (Multi-AZ) + ElastiCache
                                                    ↓
                                              S3 (file storage)
                                              SQS → Lambda (async tasks)
                                              CloudWatch (logs/metrics)
```

### Serverless API
```
Route 53 → CloudFront → API Gateway → Lambda → DynamoDB / Aurora Serverless
                                             → SQS (async)
                                             → S3
```

### Event-Driven Microservices
```
Services → MSK (Kafka) / EventBridge → Consumer Services
         → SQS (per service queue)
         → SNS (fan-out notifications)
```

---

## Cloud Security Baselines

### AWS Mandatory Controls
- [ ] S3 buckets: Block Public Access enabled by default
- [ ] RDS: Multi-AZ enabled, encryption at rest, no public accessibility
- [ ] EC2/ECS: IAM roles (not access keys), IMDSv2 enforced
- [ ] API Gateway: WAF attached for public-facing APIs
- [ ] Secrets: AWS Secrets Manager (not environment variables or Parameter Store plain text)
- [ ] Logging: CloudTrail enabled in all regions, logs to S3 with integrity validation
- [ ] VPC: Private subnets for databases and internal services, NAT Gateway for outbound
- [ ] GuardDuty enabled
- [ ] Security Hub enabled with AWS Foundational Security Best Practices standard

---

## Container Architecture Standards

### Kubernetes Minimum Production Requirements
- Resource requests and limits on every container
- Liveness and readiness probes on every service
- Pod Disruption Budgets for critical services
- Network Policies to restrict inter-pod communication
- Secrets from Kubernetes Secrets or external secrets operator (not ConfigMaps)
- Non-root containers (runAsNonRoot: true)
- Read-only root filesystem where possible
- RBAC configured — no cluster-admin for application service accounts
- Image scanning in CI pipeline (Trivy, Snyk)
- Namespaces per environment (dev/staging/prod)

---

## Cost Architecture Standards

### Required Cost Controls
- Auto-scaling configured with appropriate min/max bounds
- Savings Plans or Reserved Instances for predictable baseline compute (>30% discount)
- S3 Intelligent-Tiering for data >30 days without predictable access pattern
- CloudWatch billing alerts: 50% of budget → warning, 80% → action required, 100% → page
- Cost allocation tags on all resources (team, environment, service)
- Regular right-sizing review (monthly for compute, quarterly for databases)

### AI/LLM Cost Controls
- Per-user daily token budget enforced at API level
- Caching for repeated queries (target: >30% cache hit rate for common queries)
- Model selection: use smaller models for simple classification tasks
- Batch processing for non-real-time AI workloads (60–80% cost reduction)
- Monthly LLM cost reviewed and attributed per feature

---

## Multi-Region / DR Standards

| Tier | RTO | RPO | Pattern |
|---|---|---|---|
| Tier 1 (Critical) | <15 min | <1 min | Active-Active multi-region |
| Tier 2 (Important) | <1 hour | <15 min | Active-Passive with warm standby |
| Tier 3 (Standard) | <4 hours | <1 hour | Backup and restore with pilot light |
| Tier 4 (Internal) | <24 hours | <24 hours | Backup and restore |

Healthcare/HIPAA systems must be Tier 1 or Tier 2 minimum.
Financial transaction systems must be Tier 1.
