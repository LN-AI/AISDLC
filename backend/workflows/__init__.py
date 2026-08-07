"""Composite workflows — multi-agent / multi-stage pipelines, as first-class units.

Each module here owns one workflow and exposes a `run(orch, ...)` function. They used to
be methods on `SDLCOrchestrator`, which made them second-class next to agents: they had no
identity of their own, couldn't be entitled or metered separately, and couldn't be lifted
out of the app without dragging the whole 1200-line god object along.

`SDLCOrchestrator` keeps a same-named method for every one of these, delegating here. That
is deliberate — the four modules that instantiate the orchestrator (`main.py`,
`routes/v1_agents.py`, `routes/agents.py`, `routes/swarm.py`) and the swarm service all
keep working unchanged, and each workflow could be moved independently.

Workflows receive the orchestrator as their agent-access handle rather than importing
agents directly. That keeps `_kb()` (knowledge-base injection, which 14 of the 20 agents
depend on), `_compress()` (Headroom cost optimization) and `reviews` persistence in one
place, and avoids an import cycle.

Logging: workflows deliberately do NOT re-apply the `@_log_call` decorator. The
orchestrator method that delegates here is already wrapped by it, so adding another layer
would double-log every workflow run.
"""
