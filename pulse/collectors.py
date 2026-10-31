"""Source collectors. Each takes a URL and a cutoff and returns RawItems newer than the cutoff."""

import calendar
import html
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser
import httpx

USER_AGENT = "ai-pulse/0.1 (+https://github.com/)"
MAX_CONTENT_CHARS = 4000
_TAG_RE = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class RawItem:
    url: str
    title: str
    content: str
    published_at: datetime


def strip_html(text: str) -> str:
    return " ".join(html.unescape(_TAG_RE.sub(" ", text)).split())[:MAX_CONTENT_CHARS]


def _get(url: str) -> httpx.Response:
    resp = httpx.get(url, headers={"User-Agent": USER_AGENT}, timeout=20, follow_redirects=True)
    resp.raise_for_status()
    return resp


def collect_feed(url: str, since: datetime) -> list[RawItem]:
    """RSS/Atom: vendor blogs, arXiv API, GitHub releases.atom."""
    feed = feedparser.parse(_get(url).content)
    items = []
    for e in feed.entries:
        ts = e.get("published_parsed") or e.get("updated_parsed")
        if not ts or not e.get("link") or not e.get("title"):
            continue
        published = datetime.fromtimestamp(calendar.timegm(ts), UTC)  # feedparser gives UTC
        if published < since:
            continue
        body = e.get("summary") or (e.get("content") or [{}])[0].get("value", "")
        items.append(RawItem(e.link, strip_html(e.title), strip_html(body), published))
    return items


def collect_hn(url: str, since: datetime) -> list[RawItem]:
    """Hacker News via Algolia. `url` is the search endpoint incl. query/points filters."""
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query))
    query["numericFilters"] = ",".join(
        f for f in (query.get("numericFilters"), f"created_at_i>{int(since.timestamp())}") if f
    )
    data = _get(urlunsplit(parts._replace(query=urlencode(query)))).json()
    return [
        RawItem(
            url=h.get("url") or f"https://news.ycombinator.com/item?id={h['objectID']}",
            title=h["title"],
            content=strip_html(h.get("story_text") or ""),
            published_at=datetime.fromtimestamp(h["created_at_i"], UTC),
        )
        for h in data.get("hits", [])
        if h.get("title")
    ]


COLLECTORS: dict[str, Callable[[str, datetime], list[RawItem]]] = {
    "rss": collect_feed,
    "hn": collect_hn,
}
