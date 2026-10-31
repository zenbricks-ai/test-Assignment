from datetime import UTC, datetime, timedelta

import pytest

from pulse.config import get_settings
from pulse.dedup import canonical_url, find_similar, normalize_title, url_hash
from pulse.delivery import create_channel
from pulse.delivery.mail import EmailChannel
from pulse.delivery.web import WebChannel
from pulse.rank import build_payload
from pulse.render import render_digest

NOW = datetime(2026, 10, 30, 12, tzinfo=UTC)


def test_canonical_url_strips_tracking_and_noise():
    a = "http://www.Example.com/post/?utm_source=x&id=7&ref=hn"
    b = "https://example.com/post?id=7"
    assert canonical_url(a) == canonical_url(b) == "https://example.com/post?id=7"
    assert url_hash(a) == url_hash(b)
    assert url_hash("https://example.com/a") != url_hash("https://example.com/b")


def test_find_similar_titles():
    recent = {1: normalize_title("OpenAI releases GPT-6 Luna with 1M context")}
    assert find_similar("OpenAI Releases GPT-6 Luna, with 1M context!", recent) == 1
    assert find_similar("Google ships Gemini 4 Flash", recent) is None


def _row(i, relevance, priority="P1", effort="M", impact="opportunity", hours_old=1, rec="Do x"):
    return {
        "item_id": i,
        "title": f"t{i}",
        "url": f"https://x/{i}",
        "source": "s",
        "source_weight": 1.0,
        "published_at": NOW - timedelta(hours=hours_old),
        "summary": "sum",
        "category": "tool",
        "relevance": relevance,
        "impact_type": impact,
        "rationale": "why",
        "recommendation": rec,
        "priority": priority,
        "effort": effort,
    }


def test_build_payload_ranks_and_builds_action_list():
    rows = [
        _row(1, "Low"),
        _row(2, "Medium", priority="P2"),
        _row(3, "High", priority="P0", effort="L"),
        _row(4, "High", priority="P0", effort="S", hours_old=30),
        _row(5, "Medium", impact="neutral"),
        _row(6, "High", rec="No action needed"),
    ]
    p = build_payload({"slug": "s", "name": "S"}, rows, NOW, top_n=4, failed_sources=["X"])
    assert [r["item_id"] for r in p["top_items"]] == [3, 6, 2, 5]  # Low never in top
    assert [r["item_id"] for r in p["actions"]] == [3, 2]  # neutral / "No action" dropped
    assert {r["item_id"] for r in p["also_noted"]} == {1, 4}
    assert p["failed_sources"] == ["X"]

    msg = render_digest(p, "2026-10-30", "https://pulse.test")
    assert (
        "t3" in msg.html
        and "P0" in msg.text
        and "https://pulse.test/digests/s/2026-10-30" in msg.text
    )


def test_render_escapes_untrusted_html():
    p = build_payload({"slug": "s", "name": "S"}, [_row(1, "High")], NOW, 5, [])
    p["top_items"][0]["title"] = "<script>alert(1)</script>"
    assert "<script>alert" not in render_digest(p, "2026-10-30").html


def test_channel_factory():
    s = get_settings()
    assert isinstance(create_channel("web", s), WebChannel)
    assert isinstance(create_channel("email", s), EmailChannel)
    with pytest.raises(ValueError, match="Unknown channel"):
        create_channel("pigeon", s)
