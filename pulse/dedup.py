import hashlib
import re
from difflib import SequenceMatcher
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TRACKING = ("utm_", "ref", "source", "fbclid", "gclid")
_NON_WORD = re.compile(r"[^a-z0-9 ]+")
TITLE_SIMILARITY = 0.85


def canonical_url(url: str) -> str:
    parts = urlsplit(url.strip())
    host = parts.netloc.lower().removeprefix("www.")
    query = urlencode(
        sorted((k, v) for k, v in parse_qsl(parts.query) if not k.lower().startswith(_TRACKING))
    )
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(("https", host, path, query, ""))


def url_hash(url: str) -> str:
    return hashlib.sha256(canonical_url(url).encode()).hexdigest()


def normalize_title(title: str) -> str:
    return " ".join(_NON_WORD.sub(" ", title.lower()).split())


def find_similar(title: str, recent: dict[int, str]) -> int | None:
    """Return id of a recent item whose normalized title is near-identical, else None."""
    norm = normalize_title(title)
    for item_id, other in recent.items():
        if SequenceMatcher(None, norm, other).ratio() >= TITLE_SIMILARITY:
            return item_id
    return None
