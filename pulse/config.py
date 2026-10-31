import hashlib
import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

import structlog
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    # project-root .env, so the CLI works from any working directory
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    data_dir: Path = ROOT / "data"  # runtime JSON records (see store.py)
    config_dir: Path = ROOT / "config"  # sources + project profiles, committed to git
    retention_days: int = 30
    redis_url: str = "redis://localhost:6379/0"

    # LLM keys: set any subset; providers without a key are skipped (chain: config/llm.json)
    openrouter_api_key: SecretStr = SecretStr("")
    openai_api_key: SecretStr = SecretStr("")
    gemini_api_key: SecretStr = SecretStr("")
    anthropic_api_key: SecretStr = SecretStr("")
    # OpenAI only: bill a specific org / project (use separate projects for staging and prod)
    openai_org_id: str = ""
    openai_project_id: str = ""
    llm_timeout_s: float = 60
    llm_rate_limit: str = "30/m"  # per worker; see README "Scaling"

    lookback_hours: int = 36
    enrich_max_items_per_run: int = 40
    daily_token_budget: int = 400_000
    digest_top_n: int = 8

    public_base_url: str = "http://localhost:8000"
    timezone: str = "Asia/Kolkata"
    daily_run_hour: int = 7

    smtp_host: str = "localhost"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: SecretStr = SecretStr("")
    smtp_starttls: bool = True
    mail_from: str = "AI Pulse <pulse@example.com>"
    digest_recipients: str = ""  # comma-separated; emailed every profile's digest

    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # fails fast on missing env


def configure_logging() -> None:
    level = logging.getLevelNamesMapping()[get_settings().log_level.upper()]
    logging.basicConfig(level=level, format="%(message)s")
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
    )


def sources() -> dict[str, dict[str, Any]]:
    """config/sources.json: [{name, type, url, weight, enabled}] keyed by name."""
    raw = json.loads((get_settings().config_dir / "sources.json").read_text("utf-8"))
    return {s["name"]: s for s in raw if s.get("enabled", True)}


def profiles() -> dict[str, dict[str, Any]]:
    """config/profiles/<slug>.json keyed by slug."""
    return {
        p.stem: {**json.loads(p.read_text("utf-8")), "slug": p.stem}
        for p in sorted((get_settings().config_dir / "profiles").glob("*.json"))
    }


def subscriptions(profile: dict[str, Any]) -> list[dict[str, str]]:
    """Profile's own subscriptions plus DIGEST_RECIPIENTS emails, each with a stable id."""
    subs = list(profile.get("subscriptions", [{"channel": "web", "target": "public"}]))
    subs += [
        {"channel": "email", "target": r.strip()}
        for r in get_settings().digest_recipients.split(",")
        if r.strip()
    ]
    return [
        {**s, "id": hashlib.sha1(f"{s['channel']}:{s['target']}".encode()).hexdigest()[:12]}
        for s in subs
    ]
