# Agent Productization Options — Decision Document

## Context

**Goal:** Today this repo is one FastAPI app that bundles ~25 AI agents (Code Review, Security/VAPT, Compliance, Governance Report, etc.) across 9 "Intelligence" modules, all wired through a single `SDLCOrchestrator`, one shared SQLite database, and one monolithic frontend. The user wants each agent (and the multi-agent workflows built on top of them, like `full_review` and `autofix`) to eventually be usable as its **own separate product** — not just as a feature inside this one app.

The audience for these separate products is **not yet decided** — could be external paying customers, could be internal teams embedding an agent in their own systems, could be both. The multi-agent **composite workflows** (`full_review`, `autofix`, `migration execute`, `swarm`) matter too, not just single agents — so any plan has to account for splitting those, not only the individual agent classes.

This document is a **reference for comparing options and deciding direction later** — it does not commit to any implementation path.

### Current architecture (from codebase exploration)

- **Agents are already loosely coupled.** Every agent (`backend/agents/<module>/*.py`) is a thin subclass of `BaseAgent` (`backend/agents/base_agent.py`) — essentially a system prompt + a tool schema + a call to `llm_gateway.call_tool()`. A grep across all agent modules found **zero direct imports of one agent by another**. The only extra dependencies most agents have are `backend/controls/control_library.py` (static reference text, trivial to copy) and sometimes `backend/services/knowledge_base.py` (optional org-context lookup).
- **Composite workflows are NOT decoupled.** `backend/services/orchestrator.py` (`SDLCOrchestrator`, ~1200 lines) instantiates all ~20 agents itself and hardcodes multi-agent chains as its own methods: `full_review` (runs requirement/compliance/architecture/code/prompt agents, merges findings, feeds them to the gate + report agents), `autofix` (via GitHub PR review), `migration execute`, and `generate_scaffold`. These are real, non-trivial workflows, not just "call one agent."
- **Routing:** `backend/routes/v1_agents.py` already exposes a typed, per-agent REST endpoint for each agent, with its own auth (API key/JWT), rate limiting, and budget guard (`backend/services/agent_guard.py`). This is the most "product-ready" surface that already exists. The legacy `/api/review/*` endpoints in `backend/main.py` are what the current frontend actually uses, and are more tangled.
- **Data layer:** `backend/database.py` is one shared SQLite file/schema — one `reviews` table for every agent type (discriminated by a `review_type` column), plus shared `users`, `api_keys`, `audit_trail`, `projects` tables. Nothing is namespaced per agent today.
- **Guardrails:** `agent_guard.py` (budget/rate-limit) and `guardrail_service.py` (input limits) are applied per-call, not globally — they could travel with an extracted agent, but `agent_guard.py` depends on the shared `database.py` for key/budget bookkeeping.
- **Frontend:** `frontend/app.js` (~3766 lines) is one SPA with a dedicated render function per agent, all sharing one `apiFetch` helper and hitting the legacy `/api/review/*` endpoints — not modular per agent.

---

## The Options

### Option A — Extract each agent as a standalone code library

**What it means:** Turn each agent into an independently importable package (like a pip package), with no dependency on this repo's app, routes, database, or orchestrator. Someone else's application imports the agent class directly and calls it.

**What would need to change:**
- Copy/move each agent file out of `backend/agents/<module>/` into its own package, along with `base_agent.py` and the small pieces of `control_library.py` it needs (vendored, not imported live).
- Replace the dependency on `backend/services/llm_gateway.py` with either (a) vendoring a slimmed-down version of it (LLM call + retry logic), or (b) requiring the consumer to supply their own LLM-calling function.
- Drop all dependencies on `backend/database.py`, `agent_guard.py`, and auth — the library has no concept of users, billing, or persistence.
- Composite workflows (`full_review`, etc.) would need to be rewritten as example/reference code showing how to chain the extracted agents — the actual chaining logic isn't reusable as-is because it currently lives inside `SDLCOrchestrator`.

**Effect:**
- ✅ Fastest option — the agents are already close to this shape.
- ✅ Good for internal teams or open-sourcing individual agents.
- ❌ Not a sellable product by itself — no auth, billing, persistence, or UI. Whoever adopts it builds all of that.
- ❌ Composite workflows lose their "just works" quality — the consumer has to reimplement orchestration.

---

### Option B — One shared deployment, access/billing scoped per agent

**What it means:** Keep everything running as a single app (one deployment, one database), but change *access control* so that a given customer/API key/plan only unlocks specific agents (or specific workflows), rather than the whole platform. From the customer's point of view, it feels like they bought "the Code Review product," even though it's the same backend as everyone else's.

**What would need to change:**
- Extend `v1_agents.py` and `agent_guard.py` so API keys/plans carry a list of **entitled agents/workflows**, and requests to non-entitled agents are rejected.
- Add per-agent (and per-workflow) usage metering/billing hooks — likely extending the existing budget-guard bookkeeping in `agent_guard.py`/`database.py` rather than building new infrastructure.
- Restructure `SDLCOrchestrator`'s composite methods (`full_review`, `autofix`, `migration execute`, `swarm`) so each one can be entitlement-gated as its own "product" the same way single agents are — right now they're plain methods on the god object, not modeled as first-class things with their own identity.
- Frontend: either build lightweight per-product landing pages/dashboards that only show the entitled agent(s), or keep the one SPA but hide non-entitled agents based on the caller's plan.

**Effect:**
- ✅ Reuses existing infrastructure — `v1_agents.py` and `agent_guard.py` already do most of the per-agent request/auth/budget plumbing.
- ✅ Fast relative to Option C — mostly configuration/entitlement work, not a rewrite.
- ✅ Handles composites naturally once they're modeled as first-class products alongside single agents.
- ❌ No real isolation — all customers share one database and one running app. A problem with one customer's usage (cost spike, bug, data issue) can in theory affect others.
- ❌ Can't offer a customer their own private/dedicated instance.

---

### Option C — Fully separate services per agent (true microservices)

**What it means:** Each agent (or each product grouping of agents) becomes its own independently deployable service: its own database, its own auth, its own scaling, potentially its own repo. Genuinely separate products at the infrastructure level.

**What would need to change:**
- Split `backend/database.py`'s shared tables. `users`/`api_keys`/`audit_trail`/`projects` currently serve everything — these would need to become either (a) a separate shared identity/billing service that all the split-out agent-services call over the network, or (b) duplicated per service (more isolation, more maintenance).
- Rebuild composite workflows (`full_review`, `autofix`, `migration execute`, `swarm`) as their own orchestration service that calls the now-separate agent services over HTTP instead of in-process Python calls — this also means handling network failures, latency, and partial-failure cases that don't exist today.
- Each service needs its own deployment pipeline, monitoring, and versioning.
- Frontend would need either a separate small UI per product or an API gateway that fans out to the right service(s) while still presenting one UI.

**Effect:**
- ✅ True independence: each product can be deployed, scaled, priced, or discontinued on its own. Can offer a customer a fully isolated/private instance if needed.
- ✅ Failure in one agent/product can't take down others.
- ❌ By far the most work — most of the effort is rebuilding the shared identity/billing layer and the composite-workflow orchestration, not the agents themselves (which are already close to separable).
- ❌ Ongoing cost: more services to deploy, monitor, and maintain.
- ❌ Only worth it if you actually need the isolation (e.g., a customer requires a dedicated/private deployment, or one agent's traffic needs to scale independently of the others).

---

### Option D — Recommended middle path: make composites first-class, defer the infrastructure split

**What it means:** Don't choose between "cheap but shared" (B) and "expensive but isolated" (C) right now. Instead, restructure the *code* so that both single agents and composite workflows are equally self-contained and swappable — while still running everything as one shared deployment (like B) today. This keeps the option open to peel any single agent or workflow into its own service (like C) later, for a specific customer or scaling need, without re-architecting again.

**What would need to change:**
1. **Agent layer** — minor cleanup only; agents are already close to self-contained (per exploration above).
2. **New workflow layer** — pull `full_review`, `autofix`, `migration execute`, and `swarm` out of `SDLCOrchestrator` into their own explicit workflow definitions (e.g., one file/class per workflow, declaring "call agent X, feed its output into agent Y"). This is the key structural change: right now these workflows only exist as tangled methods inside the orchestrator god object; afterward, each workflow is a standalone unit just like an agent is.
3. **Product/API layer** — apply the same per-agent entitlement/metering pattern from Option B to both agents *and* workflows, via `v1_agents.py` + `agent_guard.py`.
4. **Shared infra** (database, users, API keys) — stays centralized for now; this is deliberately not split yet, since it's the most expensive part (per Option C above) and nothing in steps 1–3 requires it.

**Effect:**
- ✅ Gets you a real, sellable/meterable "per-agent and per-workflow product" quickly (same speed as Option B).
- ✅ Doesn't foreclose Option C later — because the agent and workflow code no longer depends on the orchestrator god object, any one of them could be lifted into its own service later without rewriting its logic again.
- ✅ Directly addresses "composites matter too" — workflows become as portable as individual agents instead of being a second-class afterthought.
- ❌ Requires touching `orchestrator.py` now (extracting workflow logic), which is more work than Option B alone would need if you never intend to split anything out later.
- ❌ Still shares infrastructure/database/blast-radius across customers today, same as Option B, until/unless a specific agent or workflow is later split out under Option C.

---

## Comparison at a glance

| | A: Library/SDK | B: Shared app, scoped access | C: Full microservices | D: Middle path (recommended) |
|---|---|---|---|---|
| Effort now | Low | Low–Medium | High | Medium |
| Gives you a sellable product? | No | Yes | Yes | Yes |
| Handles composite workflows? | Consumer rebuilds them | Needs workflow restructuring | Needs full rebuild as separate service | Restructured as first-class, reusable |
| Customer isolation | N/A (embedded) | None (shared DB/app) | Full | None yet, but easy to add later per agent/workflow |
| Good fit for | Internal reuse, OSS | Fast path to a real product | Only if isolation is a hard requirement | Uncertain audience, want future flexibility |
| Forecloses future options? | Yes (no infra built) | No, but infra work is still needed later | N/A (already there) | No — deliberately keeps the door open |

## Open questions to revisit before committing

1. **Audience** — external paying customers, internal teams, or both? (Affects how urgent Option C's isolation becomes.)
2. **Which agents/workflows first?** Not all 25 agents need to move at the same time — a pilot on 1–2 agents (e.g., Code Review) plus 1 workflow (e.g., `full_review`) would validate the approach cheaply.
3. **Billing model** — per-call, per-seat, per-plan? This shapes what the entitlement/metering work in Option B/D actually needs to track.
4. **How soon might a customer demand a dedicated/isolated deployment?** If that's a near-term likely requirement, it may be worth prioritizing more of Option C's groundwork (e.g., the identity/billing split) sooner rather than later.
