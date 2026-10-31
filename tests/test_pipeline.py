"""End-to-end: collect (mocked HTTP) -> enrich/analyze (mocked LLM) -> digest -> deliver."""

import json
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from pulse import llm, pipeline, store, tasks

NOW = datetime.now(UTC)


def _rss(*entries):
    items = "".join(
        f"<item><title>{t}</title><link>{u}</link><description>{d}</description>"
        f"<pubDate>{format_datetime(p)}</pubDate></item>"
        for t, u, d, p in entries
    )
    return (
        f'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>{items}</channel></rss>'
    )


FEED = _rss(
    ("OpenAI releases GPT-6 Luna", "https://vendor.test/gpt6?utm_source=rss", "<p>New</p>", NOW),
    ("Cheaper embeddings API", "https://vendor.test/emb", "Price cut", NOW - timedelta(hours=2)),
    ("Ancient news", "https://vendor.test/old", "old", NOW - timedelta(days=10)),
)
HN = {
    "hits": [
        {  # same story as the feed, different URL -> title dedup
            "objectID": "1",
            "title": "OpenAI Releases GPT-6 Luna!",
            "url": "https://news.test/gpt6",
            "created_at_i": int(NOW.timestamp()),
        },
        {
            "objectID": "2",
            "title": "A cute LLM poem generator",
            "url": None,
            "created_at_i": int(NOW.timestamp()),
        },
    ]
}


def fake_complete(system, user, schema):
    if schema is llm.Enrichment:
        out = llm.Enrichment(summary="Short summary.", category="new_model")
        return llm.LLMResult(out, 100, "fake:small")
    item = user.split("<item>")[1]
    high = "GPT-6" in item or "embeddings" in item
    out = llm.Impact(
        relevance="High" if high else "Low",
        impact_type="opportunity" if high else "neutral",
        rationale="Affects the answer model.",
        recommendation="Benchmark it on the eval set" if high else "No action",
        priority="P0" if high else "P2",
        effort="S",
    )
    return llm.LLMResult(out, 200, "fake:big")


def mock_feeds(router):
    router.get("https://vendor.test/rss").respond(200, text=FEED)
    hn = router.get(url__startswith="https://hn.test/search").respond(200, json=HN)
    router.get("https://broken.test/rss").mock(side_effect=httpx.ConnectError("down"))
    return hn


def run(force=False):
    """The real Celery canvas, executed in-process (eager mode, no broker)."""
    return tasks.start_run_task.delay(trigger="manual", force=force).get()


@respx.mock
def test_full_run(data, smtp, monkeypatch):
    hn = mock_feeds(respx)
    calls = []
    monkeypatch.setattr(llm, "complete", lambda *a: calls.append(a[2]) or fake_complete(*a))
    monkeypatch.setitem(tasks.app.conf, "task_always_eager", True)

    key = run()

    assert "created_at_i" in hn.calls[0].request.url.params["numericFilters"]
    items = store.scan("items")
    rec = store.load(f"runs/{key}")
    digest = store.load(f"digests/support-bot/{rec['date']}")

    assert len(items) == 4  # old item filtered; disabled source never fetched
    assert sum(i["dup_of"] is not None for i in items) == 1
    assert rec["status"] == "done"
    assert rec["stats"]["failed_sources"] == ["Broken"]
    assert rec["stats"]["enriched"] == 3 and rec["stats"]["analysed"] == 3
    assert rec["stats"]["deliveries"] == 2
    assert store.tokens_used(rec["date"]) == 3 * 100 + 3 * 200
    assert store.tokens_by_provider(rec["date"]) == {"fake:small": 300, "fake:big": 600}
    # provenance: which model + prompt version produced each result
    enriched = [i for i in items if i["summary"]]
    assert {i["llm"]["provider"] for i in enriched} == {"fake:small"}
    analyses = store.scan("analyses/support-bot")
    assert {a["llm"]["provider"] for a in analyses} == {"fake:big"}
    assert {a["llm"]["prompt_version"] for a in analyses} == {llm.PROMPT_VERSION}

    top = [i["title"] for i in digest["top_items"]]
    assert set(top) == {"OpenAI releases GPT-6 Luna", "Cheaper embeddings API"}
    assert len(digest["actions"]) == 2
    assert [i["title"] for i in digest["also_noted"]] == ["A cute LLM poem generator"]

    deliveries = store.scan(f"deliveries/{key}")
    assert {d["status"] for d in deliveries} == {"published", "sent"}
    assert len(smtp.sent) == 1 and smtp.sent[0]["To"] == "team@example.com"
    assert "GPT-6 Luna" in smtp.sent[0].get_body(("html",)).get_content()

    # double trigger is a no-op; forced rerun re-sends but reuses all LLM work
    assert run() is None
    n_calls = len(calls)
    assert run(force=True) is not None
    assert len(calls) == n_calls
    assert len(smtp.sent) == 2
    assert store.names("digests/support-bot") == [rec["date"]]

    # web channel: the API serves it
    from pulse.api import app

    client = TestClient(app)
    day = rec["date"]
    page = client.get(f"/digests/support-bot/{day}")
    assert page.status_code == 200 and "Benchmark it on the eval set" in page.text
    assert "AI Pulse" in client.get("/").text  # the frontend app
    assert client.get("/api/digests").json() == [
        {"date": day, "slug": "support-bot", "name": "HelpDesk AI (LLM support chatbot)"}
    ]
    assert client.get("/digests/support-bot/latest", follow_redirects=False).status_code == 307
    assert client.get("/api/digests/support-bot/1999-01-01").status_code == 404
    assert client.get(f"/digests/..%2F..%2Fruns/{day}").status_code == 404
    assert client.post("/runs").status_code == 202
    listed = client.get("/runs").json()
    assert [r["key"] for r in listed][-1] == key and listed[0]["trigger"] == "manual"
    got = client.get(f"/runs/{key}").json()
    assert got["status"] == "done" and got["tokens_used"] == 900
    assert got["tokens_by_provider"] == {"fake:small": 300, "fake:big": 600}
    assert client.get("/runs/..%2F..%2Fx").status_code == 404


def test_token_budget_stops_llm_calls(data, monkeypatch):
    monkeypatch.setattr(llm, "complete", fake_complete)
    key = pipeline.start_run("manual")
    store.create(
        "items/h1",
        {
            "id": "h1",
            "source": "Vendor",
            "url": "https://x/1",
            "title": "t",
            "content": "",
            "published_at": NOW.isoformat(),
            "fetched_at": NOW.isoformat(),
            "summary": None,
            "category": None,
            "enriched_at": None,
            "dup_of": None,
        },
    )
    store.add_tokens(key, key, 10**9)
    assert pipeline.enrich_candidates() == ["h1"]
    assert pipeline.enrich_item(key, "h1") is False


def test_store_create_is_exclusive_and_paths_are_contained(data):
    assert store.create("items/a", {"v": 1}) is True
    assert store.create("items/a", {"v": 2}) is False
    assert store.load("items/a") == {"v": 1}
    store.save("items/a", {"v": 3})
    assert store.scan("items") == [{"v": 3}]
    assert not list((data / "items").glob("*.tmp"))
    with pytest.raises(ValueError):
        store.load("../../etc/passwd")


def test_prune_drops_old_items(data):
    old = (NOW - timedelta(days=40)).isoformat()
    store.create("items/old", {"id": "old", "published_at": old})
    store.create("analyses/support-bot/old", {"item_id": "old"})
    store.create("items/new", {"id": "new", "published_at": NOW.isoformat()})
    assert pipeline.prune() == 1
    assert store.names("items") == ["new"] and store.names("analyses/support-bot") == []
    json.dumps(store.scan("items"))
