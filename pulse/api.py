import re
from datetime import date
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from pulse import providers, store
from pulse.config import ROOT, configure_logging, get_settings, profiles
from pulse.render import render_digest
from pulse.tasks import start_run_task

configure_logging()
app = FastAPI(title="AI Pulse", docs_url="/docs")
RUN_KEY = re.compile(r"^\d{4}-\d{2}-\d{2}(T\d+)?$")


def _profile(slug: str) -> dict[str, Any]:
    profile = profiles().get(slug)  # allow-list: slug is used in a file path
    if profile is None:
        raise HTTPException(404, "unknown profile")
    return profile


@app.get("/", include_in_schema=False)
def ui() -> FileResponse:
    return FileResponse(ROOT / "pulse" / "static" / "app.html")


@app.get("/api/digests")
def list_digests() -> list[dict[str, str]]:
    digests = [
        {"date": day, "slug": slug, "name": p["name"]}
        for slug, p in profiles().items()
        for day in store.names(f"digests/{slug}")
    ]
    return sorted(digests, key=lambda d: d["date"], reverse=True)[:60]


@app.get("/digests/{slug}/latest")
def latest(slug: str) -> RedirectResponse:
    _profile(slug)
    days = store.names(f"digests/{slug}")
    if not days:
        raise HTTPException(404, "no digest yet")
    return RedirectResponse(f"/digests/{slug}/{days[-1]}")


def _digest(slug: str, day: date) -> dict[str, Any]:
    _profile(slug)
    payload = store.load(f"digests/{slug}/{day.isoformat()}")
    if payload is None:
        raise HTTPException(404, "digest not found")
    return dict(payload)


@app.get("/digests/{slug}/{day}", response_class=HTMLResponse)
def digest_html(slug: str, day: date) -> str:
    return render_digest(_digest(slug, day), day.isoformat()).html


@app.get("/api/digests/{slug}/{day}")
def digest_json(slug: str, day: date) -> dict[str, Any]:
    return _digest(slug, day)


@app.post("/runs", status_code=202)
def trigger_run(force: bool = False) -> dict[str, str]:
    task = start_run_task.delay(trigger="manual", force=force)
    return {"task_id": task.id}


def _run_view(run: dict[str, Any]) -> dict[str, Any]:
    return {
        **run,
        "tokens_used": store.tokens_used(run["date"], run["key"]),
        "tokens_by_provider": store.tokens_by_provider(run["date"], run["key"]),
    }


@app.get("/runs")
def list_runs() -> list[dict[str, Any]]:
    """Newest 20 runs."""
    return [_run_view(store.load(f"runs/{k}")) for k in reversed(store.names("runs")[-20:])]


@app.get("/runs/{run_key}")
def get_run(run_key: str) -> dict[str, Any]:
    run = store.load(f"runs/{run_key}") if RUN_KEY.match(run_key) else None
    if run is None:
        raise HTTPException(404, "run not found")
    return _run_view(run)


def _llm_view() -> dict[str, Any]:
    config, source = providers.active_config()
    return {
        "source": source,  # "override" (set at runtime) or "default" (config/llm.json)
        "config": config.model_dump(exclude_defaults=True),  # what PUT /llm accepts
        "has_key": {
            name: bool(providers.api_key(providers.ProviderSpec(provider=name, model="-")))
            for name in providers.PROVIDERS
        },
    }


@app.get("/llm")
def get_llm() -> dict[str, Any]:
    return _llm_view()


@app.put("/llm")
def put_llm(config: providers.LLMConfig) -> dict[str, Any]:
    """Replace the provider chain. Workers pick it up on their next LLM call."""
    providers.save_override(config)
    return _llm_view()


@app.delete("/llm")
def reset_llm() -> dict[str, Any]:
    providers.clear_override()
    return _llm_view()


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz")
def readyz() -> dict[str, str]:
    import redis

    try:
        store.save("readyz", {"ok": True})  # data dir writable
        redis.Redis.from_url(get_settings().redis_url, socket_timeout=2).ping()
    except Exception as e:
        raise HTTPException(503, f"not ready: {e.__class__.__name__}") from e
    return {"status": "ready"}
