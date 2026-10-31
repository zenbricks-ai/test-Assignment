from datetime import datetime
from typing import Any

RELEVANCE_WEIGHT = {"High": 3.0, "Medium": 2.0, "Low": 1.0}
EFFORT_ORDER = {"S": 0, "M": 1, "L": 2}
HALF_LIFE_HOURS = 24
ALSO_NOTED_MAX = 15


def score(relevance: str, source_weight: float, published_at: datetime, now: datetime) -> float:
    age_h = max((now - published_at).total_seconds() / 3600, 0)
    return RELEVANCE_WEIGHT[relevance] * source_weight * 0.5 ** (age_h / HALF_LIFE_HOURS)


def build_payload(
    profile: dict[str, str],
    rows: list[dict[str, Any]],
    now: datetime,
    top_n: int,
    failed_sources: list[str],
) -> dict[str, Any]:
    """rows: one dict per analysed item (item fields + analysis fields + source_weight)."""
    ranked = sorted(
        rows,
        key=lambda r: score(r["relevance"], r["source_weight"], r["published_at"], now),
        reverse=True,
    )
    top = [r for r in ranked if r["relevance"] != "Low"][:top_n]
    top_ids = {r["item_id"] for r in top}
    actions = sorted(
        (
            r
            for r in top
            if r["impact_type"] != "neutral" and not r["recommendation"].startswith("No action")
        ),
        key=lambda r: (r["priority"], EFFORT_ORDER[r["effort"]]),
    )
    also = [r for r in ranked if r["item_id"] not in top_ids][:ALSO_NOTED_MAX]

    def clean(r: dict[str, Any]) -> dict[str, Any]:
        return {k: v.isoformat() if isinstance(v, datetime) else v for k, v in r.items()}

    return {
        "profile": profile,
        "generated_at": now.isoformat(),
        "top_items": [clean(r) for r in top],
        "actions": [clean(r) for r in actions],
        "also_noted": [clean(r) for r in also],
        "failed_sources": failed_sources,
        "stats": {"analysed": len(rows), "relevant": len(top)},
    }
