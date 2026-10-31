from datetime import datetime
from typing import Any

from jinja2 import Environment, PackageLoader, select_autoescape

from pulse.delivery.base import DigestMessage

env = Environment(
    loader=PackageLoader("pulse", "templates"),
    autoescape=select_autoescape(["html", "html.j2"]),
    trim_blocks=True,
    lstrip_blocks=True,
)

CATEGORY_LABELS = {
    "new_model": "New model",
    "pricing_api": "Pricing / API",
    "tool": "Tool",
    "research": "Research",
    "policy": "Policy",
}
env.filters["category"] = lambda c: CATEGORY_LABELS.get(c, c)
env.filters["day"] = lambda iso: datetime.fromisoformat(iso).strftime("%d %b %H:%M")


def render_digest(payload: dict[str, Any], date: str, base_url: str = "") -> DigestMessage:
    """One payload -> HTML (web page and email body) + plain text. Same source, no drift."""
    slug = payload["profile"]["slug"]
    ctx = {"d": payload, "date": date, "base_url": base_url.rstrip("/")}
    n = len(payload["top_items"])
    subject = f"AI Pulse {date}: {n} update{'s' * (n != 1)} for {payload['profile']['name']}"
    return DigestMessage(
        date=date,
        profile_slug=slug,
        subject=subject,
        html=env.get_template("digest.html.j2").render(ctx),
        text=env.get_template("digest.txt.j2").render(ctx),
    )
