# AI Pulse

AI Pulse tracks AI news every day and tells a project team how each update affects their
project and what to do about it.

**▶ [Watch the demo video](https://drive.google.com/file/d/18mIsiWPRi0IywsFwV2ngMLYyxpOLdmZ0/view?usp=sharing)**: a walkthrough of the app, the architecture and the trade-offs.

```
collect (13 sources) → dedup → summarise + categorise → impact analysis per project
→ rank → digest (web page + email) → action list
```

It runs every day at 07:00 (in `TIMEZONE`) and on demand. Any mix of OpenRouter, OpenAI,
Gemini and Claude can do the LLM work, tried in order as fallbacks and switchable at
runtime. Storage is plain JSON files on disk, so there is no database to set up.

## Using it

Open **http://localhost:8081**. Everything is a button,
so no terminal or Python is needed:

| Section | Buttons |
|---|---|
| Runs | **Run now**, **Force rerun** (asks first, re-sends the digest), **Refresh**. The table shows status, items, tokens and failed sources, and updates itself while a run is in progress |
| Digests | **Open** the digest page, **Download HTML**, **Download JSON** (these are the sample digests for the deliverable) |

There is no login: it's an internal tool. Keep it on the internal network (or behind your
VPN / SSO proxy), because anyone who can reach it can start runs and change settings.

## Running locally

You need Python 3.12+, [uv](https://docs.astral.sh/uv/), Redis on `REDIS_URL`, and an API
key for at least one LLM provider. Run each process in its own terminal:

```bash
cp .env.example .env    # set any of the four LLM keys
uv sync
uv run python -m uvicorn pulse.api:app --reload --port 8081
uv run python -m celery -A pulse.tasks worker -Q celery,collect,llm,deliver --pool solo --loglevel INFO
uv run python -m celery -A pulse.tasks beat --loglevel INFO
```

`--pool solo` is for Windows; drop it on Linux and macOS. The app is at
http://localhost:8081, Swagger UI at http://localhost:8081/docs. Results land in `./data/`.

## Production (Docker Compose)

```bash
cp .env.example .env
docker compose up -d --build                # redis, api, worker, beat (+ shared data volume)
docker compose --profile ops up -d flower   # optional queue dashboard on http://localhost:5555
```

Then open http://localhost:8081. The same actions are available over HTTP (below).

## Storage

There are two directories, and both hold plain JSON.

**`config/`** is committed to git and edited by hand:

```
config/sources.json                [{name, type: rss|hn, url, weight, enabled}]
config/profiles/<slug>.json        a project profile, plus its subscriptions
```

**`data/`** is written at runtime (`DATA_DIR`). It holds one file per record:

```
data/items/<url_hash>.json               news item; summary + category added after enrichment
data/analyses/<profile>/<url_hash>.json  impact analysis for one item × profile
data/runs/<run_key>.json                 run status + per-stage stats
data/runs/<date>.tokens                  append-only token ledger (daily budget)
data/digests/<profile>/<date>.json       the digest snapshot the web page and email render from
data/deliveries/<run_key>/<profile>-<subscription>.json
```

**Why one file per record, rather than one big JSON file?**
- **Parallel workers can't clobber each other.** Only one task ever writes any given file.
- **Each write is all-or-nothing.** Updates go to a temp file and are then swapped in with `os.replace`, so a reader never sees half a file.
- **"Already exists" checks are safe.** New records are created with a hard link, which fails if the file already exists. That gives the same "only once" guarantee a database unique constraint would: one item per URL, one analysis per item and profile, one run per day, one delivery per subscription per run.
- **Old data is cleaned up.** Items older than `RETENTION_DAYS` (default 30) and their analyses are deleted at the end of each run. Digests keep their own snapshot, so they aren't affected.

## HTTP API

| Route | Purpose |
|---|---|
| `GET /` | The app (single page, `pulse/static/app.html`) |
| `GET /api/digests` | Newest 60 digests: date, profile, name |
| `GET /digests/{profile}/latest` | Redirects to the newest digest |
| `GET /digests/{profile}/{date}` | Digest web page |
| `GET /api/digests/{profile}/{date}` | Digest JSON |
| `POST /runs[?force=true]` | Queues a run |
| `GET /runs` | Newest 20 runs with stats and tokens |
| `GET /runs/{run_key}` | One run's status, stats, tokens used |
| `GET` / `PUT` / `DELETE /llm` | Read, replace or reset the LLM provider chain (operators, via `/docs`) |
| `GET /healthz`, `GET /readyz` | Liveness; readiness (data dir writable, Redis reachable) |

## LLM providers

All LLM work goes through one function, `llm.complete(system, user, Schema)`. It returns a
validated pydantic object whichever vendor answered. Every provider gets the same prompts,
and every answer is checked against the same JSON schema.

### The provider chain

`config/llm.json` lists providers in order, and each is tried until one succeeds:

```json
{"chain": [
  {"provider": "openrouter", "model": "openai/gpt-6-luna", "params": {"reasoning": {"effort": "low"}}},
  {"provider": "openai",     "model": "gpt-6-luna",        "params": {"reasoning_effort": "low"}},
  {"provider": "gemini",     "model": "gemini-3.8-flash"},
  {"provider": "anthropic",  "model": "claude-opus-5-5",   "params": {"effort": "low"}}
]}
```

| Provider | Adapter | Key | Structured output |
|---|---|---|---|
| `openrouter` | OpenAI-compatible | `OPENROUTER_API_KEY` | `response_format` JSON schema |
| `openai` | OpenAI-compatible | `OPENAI_API_KEY` | `response_format` JSON schema |
| `gemini` | OpenAI-compatible (Gemini's `/v1beta/openai/` endpoint) | `GEMINI_API_KEY` | `response_format` JSON schema |
| `anthropic` | Native `anthropic` SDK | `ANTHROPIC_API_KEY` | `output_config.format` JSON schema, plus server-side refusal fallback |

Optional fields on each entry:
- **`params`:** vendor-specific settings, passed through unchanged (for example reasoning effort).
- **`max_output_tokens`:** cap on generated tokens, reasoning included (default 8000). A reply cut off by the cap counts as a failure, so the next provider is tried.

To use a cheaper model for one task, add `routes`. Tasks without a route use `chain`:

```json
"routes": {"Enrichment": [{"provider": "gemini", "model": "gemini-3.8-flash"},
                          {"provider": "openrouter", "model": "openai/gpt-6-luna"}]}
```

### Fallback behaviour

- **Missing key:** a provider with no API key is skipped.
- **Failures move on:** any error makes `complete()` try the next provider. That covers a network error, rate limit, outage, refusal, or output that is still invalid after one repair attempt.
- **Cooldown:** a provider that just failed moves to the back of the chain for 120 seconds, so a dead primary doesn't cost a timeout on every item. It is still tried as a last resort.
- **Logging:** every call logs `llm_call` with provider, task, input/output tokens, latency, request IDs, `repaired` and `fallback`. Results record which model and prompt version produced them.
- **All fail:** if every provider fails, `LLMUnavailable` is raised. The Celery task retries with backoff, then skips the item.

### Switching at runtime (no restart)

```bash
curl localhost:8081/llm                       # chain, source, has_key per entry
curl -X PUT -H "Content-Type: application/json" localhost:8081/llm \
  -d '{"chain":[{"provider":"anthropic","model":"claude-opus-5-5"},{"provider":"gemini","model":"gemini-3.8-flash"}]}'
curl -X DELETE localhost:8081/llm             # back to config/llm.json
```

`PUT /llm` validates the chain and saves it to `data/llm.json`. That override wins over
`config/llm.json`, and the chain is re-read on every LLM call, so every worker switches on
its next call.

### Adding a vendor

Write `pulse/providers/<vendor>.py` with a `chat(system, messages, schema_name, schema)`
method that returns a `Completion`. Then add one line to `_REGISTRY` in
`pulse/providers/__init__.py`, and a `<vendor>_api_key` setting in `config.py`. An
OpenAI-compatible vendor can reuse `OpenAICompatProvider` with its `base_url` in `_OPENAI_LIKE`.

## Production practices (OpenAI guide)

This is how the code follows OpenAI's
[production best practices](https://developers.openai.com/api/docs/guides/production-best-practices).
The same changes apply to every provider, not only OpenAI.

| Practice | How it's done here |
|---|---|
| Keys out of code; env vars or a secret manager | Keys come only from `.env` or the process environment, are held as `SecretStr` (never logged), and `.env` is git-ignored |
| Separate projects for staging and production | `OPENAI_ORG_ID` / `OPENAI_PROJECT_ID`, sent only to the `openai` provider. Pair them with one key per environment |
| Plan for rate limits | Each vendor SDK retries 429 and 5xx twice. Then the next provider is tried. Then the Celery task retries with exponential backoff and jitter. There's also a per-worker `LLM_RATE_LIMIT` |
| Limit generated tokens (latency and cost) | `max_output_tokens` on each chain entry, default 8000. It's sent as `max_completion_tokens` to OpenAI and OpenRouter, `max_tokens` elsewhere. A reply cut off by the cap counts as a failure (`finish_reason: length` / `stop_reason: max_tokens`), so it falls back instead of storing broken JSON |
| Reasoning effort, `n`, `best_of` | Effort is `low` in `params`. One completion per request |
| Smaller models where they're good enough | `routes` in `config/llm.json` gives a task its own chain, e.g. a cheaper model for summaries than for impact analysis |
| Cache common queries | Results are stored per item and per (item, profile) and never requested twice. Retries and reruns reuse them |
| Track token usage | Token ledger per run and per provider (`GET /runs/{key}` → `tokens_by_provider`), a daily `DAILY_TOKEN_BUDGET`, and input/output tokens logged on every call |
| Monitor model quality over time (MLOps) | Every summary and analysis records `llm.provider` and `llm.prompt_version`, so results can be compared across models and prompt changes |
| Error handling and debuggability | Each call logs provider, latency, retry and fallback flags, and the vendor `request_id`. Failures log the request ID as well |
| Input sanitisation | Article text is HTML-stripped, capped at 4,000 characters, wrapped as untrusted data, and the reply is schema-validated |

### Account-side checklist (dashboard, not code)

- **Spend alerts:** set a notification threshold, plus a hard monthly spend limit for the production project.
- **Separate projects:** create staging and production projects, give each its own rate and spend limits, and restrict who can access production.
- **Key hygiene:** use project keys with an expiry date. Rotate them by creating the new key, updating `.env`, restarting `api`/`worker`/`beat`, checking `GET /llm` and the next run's `llm_call` logs, then revoking the old key.
- **Usage tracking:** enable per-key usage tracking on the API keys page.
- **Other providers:** apply the same spend caps on OpenRouter, Google AI Studio and the Anthropic Console.

### Deliberately not done

- **Streaming:** only shortens the time to the first token, and nobody watches these calls live.
- **Batch API:** about half the cost, but results can take up to 24 hours, which breaks on-demand runs.
- **zstd request compression:** prompts are a few kilobytes, so there's nothing to gain.
- **Moderation API:** inputs are public news and output goes to an internal team, with no end-user content either way.
- **Stop sequences:** structured JSON output already ends cleanly.

## How it works

| Stage | What happens | Where |
|---|---|---|
| Collect | One task per source. RSS/Atom (vendor blogs, arXiv, GitHub `releases.atom`) and Hacker News (Algolia). Only items inside `LOOKBACK_HOURS` are kept. | `collectors.py` |
| Dedup | Canonical URL hash, which is also the item's file name. Near-identical titles within 72h are kept but marked `dup_of`. | `dedup.py` |
| Enrich | One LLM call per item: a 2–3 sentence summary and a category (new model, pricing/API, tool, research, policy). Capped per run, highest-weight sources first. | `pipeline.py`, `llm.py` |
| Analyze | One LLM call per item × profile: relevance (H/M/L), opportunity/risk, why, recommendation, priority (P0–P2), effort (S/M/L). | `pipeline.py`, `llm.py` |
| Digest | Score = relevance × source weight × recency decay. Top N, an action list sorted by priority then effort, and "also noted". | `rank.py` |
| Deliver | One task per subscription, through the channel factory. | `delivery/` |

The sample profile is `config/profiles/support-bot.json`, an LLM support chatbot (RAG,
OpenAI models, GDPR, cost and latency goals). To add a team, drop another JSON file in
`config/profiles/`. Every profile gets its own analysis and its own digest. Emails in
`DIGEST_RECIPIENTS` receive every profile's digest.

### Delivery channels (factory)

```python
create_channel("email", settings).send(digest, "team@example.com")
```

To add Slack, write `pulse/delivery/slack.py` with a `send(digest, target)` method, add one
line to `_REGISTRY` in `pulse/delivery/__init__.py`, and add
`{"channel": "slack", "target": "<webhook>"}` to a profile's `subscriptions`. The pipeline
needs no changes.

### Adding a source

Add an entry to `config/sources.json` with `type: "rss"` (any RSS or Atom URL) or
`type: "hn"` (an Algolia search URL). For a new kind of source, add a function to
`COLLECTORS` in `collectors.py`.

## Reliability

- **Idempotent everywhere.**
  - Creating a record that already exists is a no-op.
  - A double trigger does nothing, and a retried task never double-sends.
  - A forced rerun reuses all LLM work already done.
- **Failure isolation.**
  - Each source, item and delivery is its own task, with exponential-backoff retries.
  - When a task still fails after its last retry, it's logged and skipped, and the digest is built from whatever succeeded. Sources that failed are listed on the digest.
  - If a stage crashes, the run is marked `failed`.
- **At-least-once workers.** `acks_late` plus `reject_on_worker_lost` mean a task on a killed worker is redelivered.
- **Cost control.**
  - A per-run item cap and a daily token budget (when it's exhausted, analysis stops and the digest uses what is done).
  - Reasoning effort is set to `low`.
  - Per-worker rate limits on the LLM queue.
  - Each vendor SDK retries 429 and 5xx responses twice before the next provider is tried.
- **LLM output.** Strict JSON-schema structured output generated from pydantic models, with one repair retry. Article text is wrapped as untrusted data, and the HTML output is autoescaped.
- **Path safety.** Profile slugs in URLs are checked against `config/profiles`, and the store refuses any path outside `DATA_DIR`.

## Scaling

The API and workers are stateless apart from `DATA_DIR`. Scale them up as replicas on one
host that share the `pulse-data` volume. Workers listen on four queues
(`celery`, `collect`, `llm`, `deliver`), and each queue can get its own pool:

```bash
celery -A pulse.tasks worker -Q llm --concurrency 8
celery -A pulse.tasks worker -Q collect,deliver,celery --concurrency 16
```

Known limits:
- **JSON store:** works on one host, or across hosts with a shared filesystem. Lookups scan whole directories, which is fine for a few thousand items, the size the 30-day retention keeps it at. Switch `store.py` to Postgres when workers need to span hosts without a shared disk, or the data grows past roughly 100k records. Only `store.py` and its callers in `pipeline.py` would change.
- **Beat:** must run as exactly one process. Use `celery-redbeat` for a highly available scheduler.
- **LLM rate limit:** `LLM_RATE_LIMIT` applies to each worker, not across all of them. Add a shared Redis token bucket when you run many LLM workers.

## Development

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy pulse
```

## Layout

```
config/            sources.json, profiles/*.json, llm.json (provider chain)
pulse/
  config.py        settings (validated at boot), JSON logging, config loaders
  store.py         JSON-file store: atomic save, exclusive create, scan, token ledger
  collectors.py    rss + hn collectors
  dedup.py         URL canonicalisation, title similarity
  llm.py           prompts, output schemas, complete() over the fallback chain
  providers/       provider factory + adapters: openai_compat (OpenAI/OpenRouter/Gemini), claude
  pipeline.py      stage functions + retention
  tasks.py         Celery app, canvas, beat schedule
  rank.py          scoring + digest payload
  render.py        one payload → HTML + text
  delivery/        channel factory: web, email
  api.py           FastAPI: JSON API + serves the app
  static/app.html  the frontend app (one file, no build step)
```
