import json
import os
import shutil
import smtplib
from pathlib import Path

import pytest

os.environ.update(
    OPENROUTER_API_KEY="test-key",
    DIGEST_RECIPIENTS="team@example.com",
    LOG_LEVEL="WARNING",
)

from pulse.config import get_settings  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TEST_SOURCES = [
    {"name": "Vendor", "type": "rss", "url": "https://vendor.test/rss", "weight": 1.5},
    {"name": "HN", "type": "hn", "url": "https://hn.test/search?query=LLM", "weight": 1.0},
    {"name": "Broken", "type": "rss", "url": "https://broken.test/rss", "weight": 1.0},
    {"name": "Off", "type": "rss", "url": "https://off.test/rss", "enabled": False},
]


@pytest.fixture
def data(tmp_path, monkeypatch):
    """Empty data dir + config dir with test sources and the real sample profile."""
    config = tmp_path / "config"
    shutil.copytree(ROOT / "config" / "profiles", config / "profiles")
    shutil.copy(ROOT / "config" / "llm.json", config / "llm.json")
    (config / "sources.json").write_text(json.dumps(TEST_SOURCES))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CONFIG_DIR", str(config))
    get_settings.cache_clear()
    yield tmp_path / "data"
    get_settings.cache_clear()


class FakeSMTP:
    sent: list = []

    def __init__(self, *a, **kw): ...
    def __enter__(self):
        return self

    def __exit__(self, *a): ...
    def starttls(self): ...
    def login(self, *a): ...
    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


@pytest.fixture
def smtp(monkeypatch):
    FakeSMTP.sent = []
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    return FakeSMTP
