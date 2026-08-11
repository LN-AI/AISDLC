# Bring Your Own LLM (BYOL) — How It Works & How To Use It

## 1. What BYOL is

Every agent (Code Review, Security, Requirement Review, etc.) ultimately makes one call
through a single function: `call_tool()` in `backend/services/llm_gateway.py:287`. Agents
never talk to a provider directly — they build a prompt + a JSON tool schema and hand it to
the gateway. The gateway decides, per call, whether to use the platform's own Anthropic key
or a caller-supplied override. This is what makes BYOL possible without touching any of the
~25 agent classes.

Two provider "shapes" are supported:

| provider | wire format | works with |
|---|---|---|
| `anthropic` | Anthropic Messages API | Anthropic direct, AWS Bedrock Claude, GCP Vertex Claude, any Anthropic-compatible gateway (`base_url` override) |
| `openai` | OpenAI chat-completions + tool-calling | OpenAI, Azure OpenAI, **Ollama**, **OmniRoute**, LiteLLM proxy, any OpenAI-compatible server |

Ollama and OmniRoute aren't special-cased — they just need to speak the OpenAI
chat-completions + tool-calling wire format. You point `provider: "openai"` and `base_url`
at them.

## 2. Is it project-based or API-key-based?

**Two different things, at two different layers.** Since the LLM Providers screen landed
there are now *two* ways to point this platform at a non-Anthropic model, and they answer
different questions:

| | **Platform connection** (admin) | **BYOL** (per API key) |
|---|---|---|
| Question it answers | "What does *this deployment* run on?" | "What does *this customer* run on?" |
| Configured at | Settings → **LLM Providers** (super admin) | API Keys → Configure LLM, or `PUT /api/v1/llm-config` |
| Applies to | **Everything** — the browser UI's review forms, Agent Hub, Swarm, workflows, background jobs, and the v1 API | Only calls made with that API key |
| Whose bill | The platform's — so it still draws down project budgets | The caller's — recorded but not charged to a budget |
| Stored in | `llm_connections`, active id in `platform_settings` | `api_keys.llm_*` / `api_key_agent_llm_config` |

The rest of this section is about BYOL. For the platform connection see §8.

BYOL is **API-key-based**, and keys belong to a project — so it's project-scoped one layer up.

- BYOL config (provider, encrypted API key, base_url, model) is stored on the `api_keys`
  table (`backend/database.py:186-188`), encrypted at rest via `crypto_service`.
- Every API key is created against one specific project
  (`POST /api/v1/keys`, super-admin only — `backend/routes/api_keys.py:150`).
- A project can have multiple keys, each pointed at a different LLM if desired.
- There is **no** separate project-wide LLM setting independent of keys.

### Resolution order (most specific wins)
Source: `backend/services/api_key_service.py:295-338`

1. **Per-request fields** on that one call (`llm_provider` / `llm_api_key` / `llm_base_url` / `llm_model` in the request body) — a one-off override, nothing saved.
2. **Per-agent override** for this key (e.g. "Code Review always uses my local Ollama, everything else uses the platform default").
3. **This key's global default** (applies to every agent).
4. **Platform default** — the shared `ANTHROPIC_API_KEY` from `.env`.

## 3. Which surface actually supports BYOL

This is the detail most likely to trip you up:

| Surface | Route prefix | Auth | BYOL support |
|---|---|---|---|
| Browser dashboard's **review forms** (Code Review, Security Review, etc. — the buttons you click to submit a review) | `/api/review/*` (legacy, `backend/main.py`) | session login | **No per-key BYOL** — a browser session has no API-key record to hang a saved config on. It does follow the **active platform connection** (§8), which is how you run the UI on a local model. |
| Browser dashboard's **API Keys admin screen** (the "Configure LLM" modal) | `/api/v1/keys/{id}/llm-config*` (`backend/routes/api_keys.py`) | JWT (admin) | **Yes** — but this only *configures* a key's BYOL settings, it doesn't run reviews |
| **v1 Agent API** (`/api/v1/agents/*`) | `X-API-Key` or JWT bearer | **Yes** — this is where BYOL actually executes |

**Practical implication:** if you want *one customer* on their own LLM, that is BYOL and it
only takes effect on the v1 API. If you want *the whole deployment* — including the
dashboard's own review buttons — on Ollama or OmniRoute, that is a platform connection
(§8), and it works everywhere.

Also: a JWT/browser-login caller has no stored API-key record to hang a saved config on.
For JWT sessions, only the **per-request override fields** work (option 1 above) — the
persisted key-level config (options 2/3) only applies to `X-API-Key` callers.

## 4. Practical guide — Frontend (configuring BYOL, admin only)

1. Log in to the dashboard as an admin.
2. Go to the **API Keys** admin screen.
3. Find (or create) the key for the project you want to route to your local LLM.
4. Click **"Configure LLM"** on that key → opens the BYOL modal
   (`frontend/app.js:3605`, `openLlmConfigModal`).
5. Two tabs in that modal:
   - **Global default** — applies to every agent this key calls.
   - **Per-agent override** — pick an agent (Code Review, VAPT, etc.) from the dropdown;
     only that agent uses this config, everything else falls back to the global default.
6. Fill in:
   - Provider: `openai` (for Ollama/OmniRoute)
   - API Key: any non-empty placeholder string (Ollama/OmniRoute don't check it) — e.g. `local`
   - Base URL: your local server's OpenAI-compatible endpoint (see §6)
   - Model: the model name your local server expects
7. Save. This calls `PUT /api/v1/keys/{key_id}/llm-config` (or the `/agents/{agent_name}` variant) under the hood.

This step only **stores** the config — actually running a review against it requires the
v1 API (§5).

## 5. Practical guide — Backend / API keys (actually running against your local LLM)

### Step A — Get an API key
Ask a super-admin to create one for your project (`POST /api/v1/keys`), or use one you
already have. It looks like `lmsdlc_<64 hex chars>`.

### Step B — Save the BYOL config once (optional, persists across calls)
```bash
curl -X PUT http://localhost:8000/api/v1/llm-config \
  -H "X-API-Key: lmsdlc_yourkey" \
  -H "Content-Type: application/json" \
  -d '{
    "provider": "openai",
    "api_key": "local",
    "base_url": "http://localhost:11434/v1",
    "model": "gemma3:1b"
  }'
```
Per-agent instead of global: `PUT /api/v1/llm-config/agents/code` with the same body.

### Step C — Run a review
Once saved, every call with that key automatically uses your local LLM — no extra fields
needed:
```bash
curl -X POST http://localhost:8000/api/v1/agents/code \
  -H "X-API-Key: lmsdlc_yourkey" -H "Content-Type: application/json" \
  -d '{"code": "def add(a,b): return a+b"}'
```

### Step D — Or skip saving, override per-request
```bash
curl -X POST http://localhost:8000/api/v1/agents/code \
  -H "X-API-Key: lmsdlc_yourkey" -H "Content-Type: application/json" \
  -d '{
    "code": "def add(a,b): return a+b",
    "llm_provider": "openai",
    "llm_api_key": "local",
    "llm_base_url": "http://localhost:11434/v1",
    "llm_model": "gemma3:1b"
  }'
```

### Self-service endpoints (X-API-Key only, `backend/routes/llm_config.py`)
| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/llm-config` | View your key's current global config (secret masked) |
| PUT | `/api/v1/llm-config` | Set your key's global BYOL default |
| DELETE | `/api/v1/llm-config` | Revert to platform default |
| GET | `/api/v1/llm-config/agents` | View global + all per-agent overrides |
| PUT/DELETE | `/api/v1/llm-config/agents/{agent_name}` | Set/clear an override for one agent |

## 6. Integrating your two local LLMs

### Ollama (your `local_llm_testing.py` uses its *native* `/api/chat` — different from what BYOL needs)
Ollama also exposes an **OpenAI-compatible** endpoint, which is what this app's `openai`
provider path talks to:
- `base_url`: `http://localhost:11434/v1`
- `model`: `gemma3:1b` (or whatever `ollama list` shows)
- `api_key`: any non-empty placeholder — Ollama doesn't validate it

**Caveat:** every agent forces the model to call a specific JSON tool
(`tool_choice: {"type": "function", ...}`) to get structured output. `gemma3:1b` is very
small and Ollama's tool-calling support on tiny models is inconsistent — you may see
`ValueError: ... did not return structured output for tool '...'`. If that happens, try a
larger tool-calling-capable model (`qwen2.5`, `llama3.1`, etc.) before assuming BYOL is
broken.

### OmniRoute (port 20128)
Confirmed from `docs/omnirouter-apis.json` **and** by hitting the live instance directly:
OmniRoute is a local-first AI API proxy router with an OpenAI-compatible endpoint at
**`POST /api/v1/chat/completions`** (note the extra `/api` — not the plain `/v1/...` path
OpenAI/Ollama use). It also has a per-provider variant
`/api/v1/providers/{provider}/chat/completions` and an Ollama-compatible shim at
`/api/v1/api/chat`.

Two things the spec didn't make obvious, found by testing:
- **`model: "gpt-4o"` 404s** with `"No active credentials for provider: openai"` — OmniRoute
  infers the upstream provider from the model name, and this instance has no OpenAI
  connection registered. `GET /api/v1/models` (no auth needed) lists what's actually
  routable — on this instance it's a set of aggregator backends (`owned_by`: `auggie`,
  `chipotle`, `mimocode`, `opencode`, `theoldllm`, and web-search-backed ones) plus
  `combo`-owned routing aliases like `auto/best-fast`, `auto/best-coding`,
  `auto/best-reasoning`, which pick a backend automatically and are the easiest models to
  test with. **Neither Ollama nor a plain OpenAI connection is registered on this instance**
  — to route through your local Ollama via OmniRoute (rather than calling it directly),
  register it as a provider first: `POST /api/providers` with
  `{"provider": "ollama", "url": "http://localhost:11434", "name": "local-ollama", "isActive": true}`.
- **It defaults to streaming** (SSE `data: {...}` chunks) even without `"stream": true` set
  — pass `"stream": false"` explicitly to get one plain JSON object back with `choices[0].message.content`.
- Despite the spec marking chat completions as requiring a Bearer token, this instance
  answered with no `Authorization` header at all. If yours 401s (`AUTH_001`), generate a key
  in OmniRoute's dashboard and add `Authorization: Bearer <key>`.

For this app's BYOL config (the OpenAI Python SDK appends `/chat/completions` to `base_url`
itself):
- `base_url`: `http://localhost:20128/api/v1`
- `model`: e.g. `auto/best-fast`, or a model tied to a provider you've registered
- `api_key`: your OmniRoute-issued key if it enforces auth, otherwise leave it blank — the
  gateway substitutes a placeholder for the OpenAI-compatible path, because the OpenAI SDK
  refuses to build a client without one but local routers don't check it

A standalone test script mirroring `local_llm_testing.py` is at `local_omniroute_testing.py`
— confirmed working against the live instance, returns a joke via `auto/best-fast`.

## 7. Quick answers
- **Configurable for all agents?** Yes — as a key's global default, or narrowed to one agent via a per-agent override.
- **Project basis?** Indirectly, via the API key (which is scoped to one project).
- **API key basis?** Yes — that's the actual unit of configuration for BYOL.
- **Usable from the frontend?** For BYOL, you can *configure* it from the API Keys screen but it only *runs* on the v1 API. To put the dashboard itself on a different model, use a platform connection instead — §8.

## 8. Platform connections — running the whole deployment on another model

**Settings → LLM Providers** (super admin). Define named endpoints, test them, mark one
active. The active one is resolved inside `llm_gateway.call_tool()` itself
(`platform_default()`), which is why it reaches every surface rather than one route:
the UI's review forms, Agent Hub, Swarm, workflows, background jobs and the v1 API.

### Full precedence, most specific first
1. Per-request fields on a v1 call (`llm_provider` / `llm_api_key` / …)
2. Per-agent override on an API key (`api_key_agent_llm_config`)
3. That API key's global BYOL default (`api_keys.llm_*`)
4. **The active platform connection**
5. `ANTHROPIC_API_KEY` from `.env`

### What to enter for OmniRoute
Provider type **OmniRoute**, which prefills wire format `openai`, base URL
`http://localhost:20128/api/v1` and model `auto/best-fast`. Leave the API key blank unless
your instance enforces auth. Then press **Test** before making it active.

### Test is not a ping
It sends the same kind of forced JSON tool call every agent makes. An endpoint that
answers HTTP but can't produce a tool call cannot run a single review, and without this
you'd only find out in the middle of one.

### Consequences of switching, all real and all measured
- **`ANTHROPIC_API_KEY` becomes optional.** The routes now refuse a call only when
  *nothing* is configured (`main.py:_require_llm`), so you can empty it.
- **Costs become estimates.** Spend is priced from a per-model table; an unknown model
  falls back to a default rate and is flagged `cost_estimated` on the Usage screen. A free
  local endpoint still shows a non-zero figure, and it still draws down project budgets —
  a platform connection is the platform's own endpoint, so it is billable by design
  (`agent_guard.py`, the `billable` line).
- **Prompt caching is Anthropic-only.** Switching away silently loses it; switching back
  restores it.
- **Latency is the thing that will surprise you.** Measured on the local OmniRoute
  instance: the trivial connection-test prompt took **65s** on one run and **194s** on
  another. Real reviews send far larger prompts. Set a per-connection timeout so a slow
  route fails fast instead of burning `LLM_TIMEOUT_SECONDS` × `LLM_MAX_RETRIES`.
- **Rotating `SECRET_KEY` invalidates every stored credential.** `crypto_service.decrypt`
  returns `""` rather than raising, so the symptom is "no API key" at call time. The
  connection list shows `⚠ unreadable (SECRET_KEY changed?)` when this has happened.

### API equivalents
| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/llm-providers/presets` | Field prefills for the known provider types |
| GET | `/api/v1/llm-providers/active` | Which LLM is serving calls (any signed-in user) |
| PUT | `/api/v1/llm-providers/active` | Choose the active connection; `null` reverts to `.env` |
| GET/POST | `/api/v1/llm-providers/connections` | List / create |
| PUT/DELETE | `/api/v1/llm-providers/connections/{id}` | Edit / remove |
| POST | `/api/v1/llm-providers/connections/{id}/test` | Forced-tool-call probe |

Everything except `GET /active` is super-admin only. No endpoint ever returns a plaintext
key. On edit, an empty `api_key` means "keep the stored one".
