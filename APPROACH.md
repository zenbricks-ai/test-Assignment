# Approach and trade-offs

## Approach

AI Pulse is a deterministic pipeline, not an agent. Each stage is a plain, idempotent
function, and Celery fans those functions out across workers.
The LLM does only the two things that need
judgement: summarising and categorising an item once, and assessing impact once per item ×
project profile. Everything else is ordinary code that is cheap and easy to test: fetching,
dedup, ranking, rendering and delivery.

Both LLM results are saved as JSON files. A rerun, retry or second profile therefore never
pays twice for work already done. The summary is shared across profiles, and only the
impact analysis runs per profile.

## Trade-offs

| Decision | Why | Cost / when to revisit |
|---|---|---|
| JSON files instead of a database | Nothing to install or migrate. Data is human-readable and easy to diff or demo | One host or a shared filesystem only. Lookups scan whole directories. Move to Postgres when workers span hosts or records pass ~100k |
| One file per record, with atomic replace and exclusive create | Parallel workers never write the same file, and "only once" is guaranteed without locks | Many small files. 30-day retention keeps the count bounded |
| Queue pipeline over LangChain or agents | Predictable cost, easy to debug, every step retryable | Less flexible if the analysis later needs tool use |
| Celery over RQ or arq | Chords (fan-in) and Beat (cron) built in | Heavier. Beat must run as exactly one process |
| Title dedup with difflib | No extra dependency | O(n) over 72h. Misses rephrased headlines. Move to embeddings if samples show misses |
| First-seen copy wins on duplicates | No re-enrichment, simpler | A vendor post seen after its HN thread keeps the HN link |
| Relevance judged by the LLM against a written rubric | No training data needed | Quality depends on the profile text. A 👍/👎 feedback loop is the next step |
| Server-rendered digest (one Jinja template for web and email) | Read-only content, zero JS, web and email can't drift | Not interactive |
| Per-run item cap, highest-weight sources first | Bounded cost per day | Low-weight sources (arXiv) can be cut on busy days |
| LLM providers behind one `chat()` protocol, tried in order as fallbacks | Any vendor can be swapped or reordered at runtime, and an outage or refusal on one never stops the run. Identical prompts and JSON-schema validation keep outputs comparable | Different models score relevance a little differently, and a fallback day may read slightly differently. Each call logs which provider answered |
| One OpenAI-compatible adapter for OpenAI, OpenRouter and Gemini; native SDK for Claude | Three vendors from one small adapter. Claude gets native structured output and refusal handling | Gemini's OpenAI-compatible layer can lag its native API; if it rejects strict JSON schema, Gemini needs its own adapter |
| `max_output_tokens` cap (default 8000), and a truncated reply counts as a failure | Bounded latency and cost per call. A cut-off reply never becomes a half-written record | Too low a cap on a reasoning model makes every call fail over to the next provider. 8000 leaves room for low-effort reasoning; raise it per entry if logs show truncation |
| Per-task routes (cheaper model for summaries) available but not on by default | Follows "use smaller models where they're good enough" | Not tuned: which cheaper model holds quality has to be checked on real digests. Recorded provider and prompt version make that comparison possible |
| Each result records provider and prompt version; tokens recorded per provider | Quality and cost can be traced to a model or prompt change | A few extra fields per record |
| No streaming, Batch API, compression or moderation | None pays off for a background daily job over public news | Batch API would roughly halve LLM cost but adds up to a day of latency. Worth it if on-demand runs are dropped |
| Delivery via factory with a `send()` protocol | Slack or Teams is one new file plus one registry line | – |

## Not built (yet)

- Slack channel, multi-tenant auth and a profile editor UI.
- A feedback loop for tuning the ranking.
- A globally shared LLM rate limiter, HA Beat (`celery-redbeat`), and a Postgres-backed store.
