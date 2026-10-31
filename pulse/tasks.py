"""Celery app and the distributed version of the pipeline.

start_run -> chord(collect x sources) -> chord(enrich x items) -> chord(analyze x item*profile)
          -> build digests -> chord(deliver x subscriptions) -> finish
Per-item tasks swallow their error after the last retry so one bad item never sinks the chord.
"""

import functools
import random
from collections.abc import Callable
from typing import Any

import structlog
from celery import Celery, Task, chord, group
from celery.schedules import crontab
from celery.signals import worker_process_init

from pulse import pipeline
from pulse.config import configure_logging, get_settings, sources

s = get_settings()
app = Celery("pulse", broker=s.redis_url, backend=s.redis_url)
app.conf.update(
    timezone=s.timezone,
    task_acks_late=True,  # a killed worker's task is redelivered, not lost
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    result_expires=86_400,
    task_routes={
        "pulse.tasks.enrich_task": {"queue": "llm"},
        "pulse.tasks.analyze_task": {"queue": "llm"},
        "pulse.tasks.deliver_task": {"queue": "deliver"},
        "pulse.tasks.collect_task": {"queue": "collect"},
    },
    beat_schedule={
        "daily-run": {
            "task": "pulse.tasks.start_run_task",
            "schedule": crontab(hour=s.daily_run_hour, minute=0),
            "kwargs": {"trigger": "cron"},
        }
    },
)
log = structlog.get_logger()


@worker_process_init.connect
def _init_worker(**_: Any) -> None:
    configure_logging()


def _retry_or(task: Task, exc: Exception, fallback: Any) -> Any:
    """Retry with exponential backoff and jitter (50-100% of each step), then log and return
    fallback after the last attempt. Jitter spreads out tasks that hit a rate limit together."""
    if task.request.retries < task.max_retries:
        ceiling = min(15 * 2**task.request.retries, 600)
        raise task.retry(exc=exc, countdown=random.uniform(ceiling / 2, ceiling))
    log.error("task_gave_up", task=task.name, args=task.request.args, error=repr(exc))
    return fallback


def _fails_run(fn: Callable[[list[Any], str], None]) -> Callable[[list[Any], str], None]:
    """A crashing stage callback marks the run failed instead of leaving it 'running'."""

    @functools.wraps(fn)
    def wrapper(results: list[Any], run_key: str) -> None:
        try:
            fn(results, run_key)
        except Exception:
            pipeline.finish_run(run_key, "failed")
            raise

    return wrapper


def _fan_out(tasks: list[Any], then: Any) -> None:
    """chord() with an empty header never fires its callback, so call it directly."""
    if tasks:
        chord(group(tasks))(then)
    else:
        then.delay([])


@app.task
def start_run_task(trigger: str = "manual", force: bool = False) -> str | None:
    run_key = pipeline.start_run(trigger, force)
    if run_key is not None:
        log.info("run_started", run=run_key, trigger=trigger)
        _fan_out([collect_task.s(n) for n in sources()], after_collect.s(run_key))
    return run_key


@app.task(bind=True, max_retries=3)
def collect_task(self: Task, source: str) -> dict[str, Any]:
    try:
        return pipeline.collect_source(source)
    except Exception as e:
        return _retry_or(self, e, {"source": source, "error": repr(e)})


@app.task
@_fails_run
def after_collect(results: list[Any], run_key: str) -> None:
    pipeline.record_collect(run_key, results)
    _fan_out(
        [enrich_task.s(run_key, i) for i in pipeline.enrich_candidates()],
        after_enrich.s(run_key),
    )


@app.task(bind=True, max_retries=3, rate_limit=s.llm_rate_limit)
def enrich_task(self: Task, run_key: str, item_id: str) -> bool:
    try:
        return pipeline.enrich_item(run_key, item_id)
    except Exception as e:
        return _retry_or(self, e, False)


@app.task
@_fails_run
def after_enrich(results: list[Any], run_key: str) -> None:
    pipeline.update_stats(run_key, enriched=sum(map(bool, results)))
    _fan_out(
        [analyze_task.s(run_key, i, p) for i, p in pipeline.analysis_candidates()],
        after_analyze.s(run_key),
    )


@app.task(bind=True, max_retries=3, rate_limit=s.llm_rate_limit)
def analyze_task(self: Task, run_key: str, item_id: str, slug: str) -> bool:
    try:
        return pipeline.analyze_item(run_key, item_id, slug)
    except Exception as e:
        return _retry_or(self, e, False)


@app.task
@_fails_run
def after_analyze(results: list[Any], run_key: str) -> None:
    pipeline.update_stats(run_key, analysed=sum(map(bool, results)))
    slugs = pipeline.build_digests(run_key)
    _fan_out(
        [deliver_task.s(run_key, slug, sub) for slug, sub in pipeline.delivery_jobs(slugs)],
        finish_task.s(run_key),
    )


@app.task(bind=True, max_retries=5)
def deliver_task(self: Task, run_key: str, slug: str, subscription_id: str) -> str | None:
    try:
        return pipeline.deliver(run_key, slug, subscription_id)
    except Exception as e:
        return _retry_or(self, e, None)


@app.task
@_fails_run
def finish_task(results: list[Any], run_key: str) -> None:
    pipeline.update_stats(
        run_key, deliveries=sum(r is not None for r in results), pruned=pipeline.prune()
    )
    pipeline.finish_run(run_key)
