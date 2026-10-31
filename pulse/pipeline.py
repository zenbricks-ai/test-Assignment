"""Pipeline stages. tasks.py fans them out across Celery workers.
Every stage is idempotent, so any retry or rerun is safe."""

import json
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import structlog

from pulse import llm, store
from pulse.collectors import COLLECTORS
from pulse.config import get_settings, profiles, sources, subscriptions
from pulse.dedup import find_similar, normalize_title, url_hash
from pulse.delivery import create_channel
from pulse.rank import build_payload
from pulse.render import render_digest

log = structlog.get_logger()
DEDUP_WINDOW = timedelta(hours=72)


def utcnow() -> datetime:
    return datetime.now(UTC)


def _ts(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


def _window_start() -> datetime:
    return utcnow() - timedelta(hours=get_settings().lookback_hours)


def run_day(run_key: str) -> str:
    return run_key[:10]  # "2026-10-30" or "2026-10-30T1761800000"


# ---------- run bookkeeping ----------


def start_run(trigger: str, force: bool = False) -> str | None:
    """Create today's run. Returns None if it already exists (double trigger = no-op)."""
    day = datetime.now(ZoneInfo(get_settings().timezone)).date().isoformat()
    key = f"{day}T{int(time.time())}" if force else day
    run: dict[str, Any] = {
        "key": key,
        "date": day,
        "trigger": trigger,
        "status": "running",
        "stats": {},
        "started_at": utcnow().isoformat(),
        "finished_at": None,
    }
    if not store.create(f"runs/{key}", run):
        log.info("run_already_exists", key=key)
        return None
    return key


def update_stats(run_key: str, **kv: Any) -> None:
    # stages run one after another, so there is a single writer per run file
    run = store.load(f"runs/{run_key}")
    run["stats"].update(kv)
    store.save(f"runs/{run_key}", run)


def finish_run(run_key: str, status: str = "done") -> None:
    run = store.load(f"runs/{run_key}")
    run.update(status=status, finished_at=utcnow().isoformat())
    store.save(f"runs/{run_key}", run)
    log.info("run_finished", run=run_key, status=status)


def _budget_left(run_key: str) -> bool:
    return store.tokens_used(run_day(run_key)) < get_settings().daily_token_budget


# ---------- collect ----------


def collect_source(name: str) -> dict[str, Any]:
    src = sources()[name]
    raw = COLLECTORS[src["type"]](src["url"], _window_start())

    cutoff = utcnow() - DEDUP_WINDOW
    recent = {
        i["id"]: normalize_title(i["title"])
        for i in store.scan("items")
        if i["dup_of"] is None and _ts(i["fetched_at"]) >= cutoff
    }
    new = 0
    for r in raw:
        dup = find_similar(r.title, recent)
        item_id = url_hash(r.url)
        created = store.create(
            f"items/{item_id}",
            {
                "id": item_id,
                "source": name,
                "url": r.url,
                "title": r.title,
                "content": r.content,
                "published_at": r.published_at.isoformat(),
                "fetched_at": utcnow().isoformat(),
                "summary": None,
                "category": None,
                "enriched_at": None,
                "dup_of": dup,
            },
        )
        if not created:  # exact URL already stored
            continue
        new += 1
        if dup is None:
            recent[item_id] = normalize_title(r.title)
    log.info("collected", source=name, fetched=len(raw), new=new)
    return {"source": name, "new": new}


def record_collect(run_key: str, results: Sequence[dict[str, Any] | None]) -> None:
    ok = [r for r in results if r]
    update_stats(
        run_key,
        new_items=sum(r.get("new", 0) for r in ok),
        failed_sources=[r["source"] for r in ok if r.get("error")],
    )


def _live_items() -> list[dict[str, Any]]:
    """Non-duplicate items published inside the lookback window."""
    start = _window_start()
    return [
        i for i in store.scan("items") if i["dup_of"] is None and _ts(i["published_at"]) >= start
    ]


# ---------- enrich ----------


def enrich_candidates() -> list[str]:
    weights = {n: s["weight"] for n, s in sources().items()}
    todo = [i for i in _live_items() if i["summary"] is None]
    # under the cap, vendor news beats the arXiv firehose
    todo.sort(key=lambda i: (weights.get(i["source"], 1.0), i["published_at"]), reverse=True)
    return [i["id"] for i in todo[: get_settings().enrich_max_items_per_run]]


def enrich_item(run_key: str, item_id: str) -> bool:
    item = store.load(f"items/{item_id}")
    if item["summary"]:
        return True
    if not _budget_left(run_key):
        log.warning("token_budget_exhausted", stage="enrich", item=item_id)
        return False

    out, tokens, provider = llm.complete(
        llm.ENRICH_SYSTEM,
        llm.item_block(item["title"], item["source"], item["content"]),
        llm.Enrichment,
    )
    store.add_tokens(run_day(run_key), run_key, tokens, provider)
    item.update(
        summary=out.summary,
        category=out.category,
        enriched_at=utcnow().isoformat(),
        llm={"provider": provider, "prompt_version": llm.PROMPT_VERSION},
    )
    store.save(f"items/{item_id}", item)  # one task per item: single writer
    return True


# ---------- analyze ----------


def analysis_candidates() -> list[tuple[str, str]]:
    enriched = [i["id"] for i in _live_items() if i["summary"]]
    pairs = []
    for slug in profiles():
        done = set(store.names(f"analyses/{slug}"))
        pairs += [(i, slug) for i in enriched if i not in done]
    return pairs


def _profile_block(p: dict[str, Any]) -> str:
    return (
        f"<project>\nName: {p['name']}\n{p['description']}\n"
        f"Details: {json.dumps(p['context'], indent=1)}\n</project>"
    )


def analyze_item(run_key: str, item_id: str, slug: str) -> bool:
    if not _budget_left(run_key):
        log.warning("token_budget_exhausted", stage="analyze", item=item_id)
        return False
    item = store.load(f"items/{item_id}")
    prompt = (
        _profile_block(profiles()[slug])
        + "\n\n"
        + llm.item_block(
            item["title"],
            item["source"],
            f"Category: {item['category']}\nSummary: {item['summary']}",
        )
    )
    out, tokens, provider = llm.complete(llm.ANALYZE_SYSTEM, prompt, llm.Impact)
    store.add_tokens(run_day(run_key), run_key, tokens, provider)
    store.create(
        f"analyses/{slug}/{item_id}",
        {
            "item_id": item_id,
            "created_at": utcnow().isoformat(),
            "llm": {"provider": provider, "prompt_version": llm.PROMPT_VERSION},
            **out.model_dump(),
        },
    )
    return True


# ---------- digest ----------


def build_digests(run_key: str) -> list[str]:
    """Write one digest per profile for the run's day. Returns the profile slugs."""
    s = get_settings()
    run = store.load(f"runs/{run_key}")
    weights = {n: src["weight"] for n, src in sources().items()}
    items = {i["id"]: i for i in _live_items()}
    slugs = []
    for slug, profile in profiles().items():
        rows = []
        for a in store.scan(f"analyses/{slug}"):
            i = items.get(a["item_id"])
            if i is None:  # outside the window
                continue
            rows.append(
                {
                    "item_id": i["id"],
                    "title": i["title"],
                    "url": i["url"],
                    "source": i["source"],
                    "source_weight": weights.get(i["source"], 1.0),
                    "published_at": _ts(i["published_at"]),
                    "summary": i["summary"],
                    "category": i["category"],
                    **{k: a[k] for k in llm.Impact.model_fields},
                }
            )
        payload = build_payload(
            profile={"slug": slug, "name": profile["name"]},
            rows=rows,
            now=utcnow(),
            top_n=s.digest_top_n,
            failed_sources=run["stats"].get("failed_sources", []),
        )
        payload |= {"date": run["date"], "run": run_key}
        store.save(f"digests/{slug}/{run['date']}", payload)  # forced rerun replaces the day's
        slugs.append(slug)
    return slugs


# ---------- deliver ----------


def delivery_jobs(slugs: list[str]) -> list[tuple[str, str]]:
    all_profiles = profiles()
    return [(slug, sub["id"]) for slug in slugs for sub in subscriptions(all_profiles[slug])]


def deliver(run_key: str, slug: str, subscription_id: str) -> str:
    """One delivery per subscription per run: retries never double-send, a forced rerun does."""
    rel = f"deliveries/{run_key}/{slug}-{subscription_id}"
    if done := store.load(rel):
        return str(done["detail"])
    s = get_settings()
    sub = next(x for x in subscriptions(profiles()[slug]) if x["id"] == subscription_id)
    payload = store.load(f"digests/{slug}/{run_day(run_key)}")
    msg = render_digest(payload, payload["date"], s.public_base_url)

    result = create_channel(sub["channel"], s).send(msg, sub["target"])
    store.create(
        rel,
        {
            "channel": sub["channel"],
            "target": sub["target"],
            "status": result.status,
            "detail": result.detail,
            "created_at": utcnow().isoformat(),
        },
    )
    log.info("delivered", channel=sub["channel"], status=result.status, detail=result.detail)
    return result.detail


# ---------- retention ----------


def prune() -> int:
    """Drop items (and their analyses) older than RETENTION_DAYS. Digests keep their snapshot."""
    cutoff = utcnow() - timedelta(days=get_settings().retention_days)
    old = [i["id"] for i in store.scan("items") if _ts(i["published_at"]) < cutoff]
    for item_id in old:
        for slug in profiles():
            store.delete(f"analyses/{slug}/{item_id}")
        store.delete(f"items/{item_id}")
    return len(old)
