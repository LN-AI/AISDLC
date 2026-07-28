# AI / LLM Architecture Standards

## Production-Grade AI Architecture Requirements

Every AI/LLM feature must meet these requirements before production release.

---

## 1. LLM API Integration Checklist

- [ ] **Timeout configured** — all LLM API calls have a timeout (max 30s for real-time, 5min for batch)
- [ ] **Retry with backoff** — exponential backoff on 429/500/503 errors (max 3 retries)
- [ ] **Fallback response** — when LLM fails, return a graceful degradation message, not a 500 error
- [ ] **Rate limiting** — per-user and per-organisation token limits enforced
- [ ] **Token budget** — hard monthly/daily token limits set, alerting at 80%
- [ ] **Model version pinned** — not using `latest`, using a specific model version
- [ ] **Streaming handled** — if using streaming, partial failures are handled gracefully

---

## 2. RAG Architecture Standards

### Required Components
- **Vector store** — Pinecone, pgvector, Weaviate, or Chroma. Not in-memory for production.
- **Embedding model** — Pinned version. Changing embedding model = re-embedding all documents.
- **Chunking strategy** — Defined and documented. Typical: 512 tokens, 64-token overlap.
- **Retrieval filter** — Documents scoped to tenant/user/role. No cross-tenant retrieval.
- **Citation** — Every RAG response must cite the source document(s). No silent retrieval.
- **Freshness** — Define how often the knowledge base is re-indexed. Stale KB = wrong answers.

### RAG Security
- Retrieval must respect access controls — users must not retrieve documents they cannot access
- No PHI or confidential data in shared vector stores unless tenant-isolated
- System prompt must be isolated from retrieved context (injection risk)

---

## 3. Agent / Tool-Use Architecture

### Tool Permission Levels

| Level | Description | Requires Human Approval |
|---|---|---|
| **Read-only** | Query data, retrieve documents, calculate | No |
| **Stateful read** | Access user session, personalised data | No |
| **Write** | Create records, send notifications, update data | Recommended for production data |
| **Destructive** | Delete records, send mass emails, financial transactions | Yes — mandatory |
| **Privileged** | Modify system configuration, access admin functions | Yes — mandatory |

### Agent Safety Requirements
- System prompt must be immutable from user input (no prompt injection via tool results)
- Tool inputs must be validated before execution (prevent command injection)
- Agent must not have write access to its own system prompt or configuration
- All tool calls must be logged with: timestamp, user ID, tool name, inputs, outputs
- Maximum steps per workflow must be bounded (prevent infinite loops)
- Confidence threshold: if agent is uncertain, escalate to human rather than guess

---

## 4. Multi-Agent Architecture

When using multiple coordinating agents:
- Define explicit agent roles and responsibilities (no overlap)
- Agents communicate via defined message schemas (no free-form text between agents)
- Supervisor agent must validate sub-agent outputs before passing to next agent
- Human escalation point must exist in every workflow — at minimum before irreversible actions
- Each agent's tool access must be separately restricted to minimum required

---

## 5. Prompt Management Standards

- Prompts must be stored in a managed system (database or prompt registry), not hardcoded
- Prompt changes must be versioned and reviewed like code changes
- A/B testing infrastructure should exist for prompt optimisation
- System prompts must be reviewed by security before deployment (OWASP LLM01)
- Prompts handling PHI/PII must be reviewed for data leakage risk (OWASP LLM06)

---

## 6. AI Monitoring Requirements

Post-deployment monitoring must track:
- **Token usage** per user, feature, and model — daily/weekly trend
- **Latency** P50/P95/P99 for LLM calls
- **Error rate** by error type (timeout, rate limit, invalid response)
- **Hallucination rate** — sample 1% of outputs for human review or use LLM-as-judge
- **RAG retrieval quality** — measure retrieval precision on golden test set monthly
- **Cost per feature** — track LLM cost attributed per product feature
- **Prompt drift detection** — alert if model behaviour changes after model version updates

---

## 7. AI Risk Classification

| Risk Class | Criteria | Human Oversight |
|---|---|---|
| Class 1 — Low | Suggestion-only, no data written, no external action | Optional |
| Class 2 — Medium | Writes to non-critical data, recommends actions | Recommended |
| Class 3 — High | Writes to critical data, external communications, financial impact | Mandatory before action |
| Class 4 — Critical | Clinical decisions, legal/regulatory filings, large financial transactions | Mandatory — senior reviewer |

NIST AI RMF alignment: Class 3+ systems require GOVERN, MAP, MEASURE, MANAGE documentation.
