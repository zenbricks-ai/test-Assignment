"""Provider abstraction: fallback chain, runtime switching, and each adapter's wire format."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from pulse import llm, providers
from pulse.providers.base import Completion, ProviderError
from pulse.providers.claude import ClaudeProvider
from pulse.providers.openai_compat import OpenAICompatProvider

GOOD = '{"summary": "Fine.", "category": "tool"}'
SCHEMA = llm.Enrichment.model_json_schema()
USER = [{"role": "user", "content": "U"}]


class FakeProvider:
    """Replies from a script; an Exception in the script is raised instead."""

    calls: dict[str, int] = {}

    def __init__(self, label, script):
        self.label, self.script = label, list(script)

    def chat(self, system, messages, schema_name, schema):
        FakeProvider.calls[self.label] = FakeProvider.calls.get(self.label, 0) + 1
        reply = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(reply, Exception):
            raise reply
        return Completion(reply, input_tokens=7, output_tokens=3, request_id="req_1")


@pytest.fixture
def fakes(data, monkeypatch):
    """Provider kind "fake": model name picks a script. Model "nokey" has no API key."""
    scripts = {
        "down": [ConnectionError("503")],
        "refuses": [ProviderError("refused")],
        "truncated": [ProviderError("hit max_output_tokens")],
        "garbage": ["not json"],
        "repairs": ['{"summary": "x", "category": "rumour"}', f"```json\n{GOOD}\n```"],
        "ok": [GOOD],
        "cheap": [GOOD],
        "nokey": [GOOD],
    }
    monkeypatch.setitem(
        providers._REGISTRY,
        "fake",
        lambda spec, key, s: FakeProvider(spec.label, scripts[spec.model]),
    )

    def key(spec):
        if spec.provider != "fake":
            return real_key(spec)
        return "" if spec.model == "nokey" else "k"

    real_key = providers.api_key
    monkeypatch.setattr(providers, "api_key", key)
    monkeypatch.setattr(llm, "api_key", key)
    monkeypatch.setattr(llm, "_failed_at", {})
    FakeProvider.calls = {}


def fake(model):
    return {"provider": "fake", "model": model}


def use_chain(*models, routes=None):
    providers.save_override(
        providers.LLMConfig(chain=[fake(m) for m in models], routes=routes or {})
    )


def test_falls_back_through_the_chain(fakes):
    use_chain("down", "refuses", "truncated", "garbage", "ok")
    out, tokens, provider = llm.complete("sys", "user", llm.Enrichment)
    assert out.summary == "Fine." and provider == "fake:ok"
    assert FakeProvider.calls == {
        "fake:down": 1,
        "fake:refuses": 1,
        "fake:truncated": 1,
        "fake:garbage": 2,
        "fake:ok": 1,
    }
    assert tokens == 10  # tokens of the provider that answered


def test_invalid_json_gets_one_repair_turn_on_the_same_provider(fakes):
    use_chain("repairs", "ok")
    result = llm.complete("sys", "user", llm.Enrichment)
    assert result.output.category == "tool" and result.tokens == 20
    assert FakeProvider.calls == {"fake:repairs": 2}


def test_failed_provider_moves_to_the_back_during_cooldown(fakes):
    use_chain("down", "ok")
    llm.complete("sys", "user", llm.Enrichment)
    llm.complete("sys", "user", llm.Enrichment)
    assert FakeProvider.calls == {"fake:down": 1, "fake:ok": 2}  # not retried on 2nd call


def test_providers_without_keys_are_skipped_and_all_failing_raises(fakes):
    use_chain("nokey")
    with pytest.raises(llm.LLMUnavailable, match="no provider"):
        llm.complete("sys", "user", llm.Enrichment)
    use_chain("down", "refuses")
    with pytest.raises(llm.LLMUnavailable, match="fake:down.*fake:refuses"):
        llm.complete("sys", "user", llm.Enrichment)


def test_per_task_routes_send_cheap_tasks_to_a_cheaper_model(fakes):
    use_chain("ok", routes={"Enrichment": [fake("cheap"), fake("ok")]})
    assert llm.complete("sys", "user", llm.Enrichment).provider == "fake:cheap"
    good_impact = (
        '{"relevance":"Low","impact_type":"neutral","rationale":"r",'
        '"recommendation":"No action","priority":"P2","effort":"S"}'
    )
    providers._REGISTRY["fake"] = lambda spec, key, s: FakeProvider(spec.label, [good_impact])
    assert llm.complete("sys", "user", llm.Impact).provider == "fake:ok"  # default chain

    with pytest.raises(ValueError, match="unknown task"):
        providers.LLMConfig(chain=[fake("ok")], routes={"Summaries": [fake("ok")]})


def test_chain_switches_at_runtime_via_api(fakes):
    from pulse.api import app

    client = TestClient(app)
    view = client.get("/llm").json()
    assert view["source"] == "default"
    chain = view["config"]["chain"]
    assert [c["provider"] for c in chain] == ["openrouter", "openai", "gemini", "anthropic"]
    assert view["has_key"] == {
        "openrouter": True,
        "openai": False,
        "gemini": False,
        "anthropic": False,
    }
    # what GET returns is exactly what PUT accepts
    assert client.put("/llm", json=view["config"]).status_code == 200

    body = {"chain": [fake("ok")]}
    assert client.put("/llm", json=body).json()["source"] == "override"
    assert llm.complete("sys", "user", llm.Enrichment).output.summary == "Fine."  # no restart

    bad = {"chain": [{"provider": "skynet", "model": "x"}]}
    assert client.put("/llm", json=bad).status_code == 422
    assert client.put("/llm", json={"chain": []}).status_code == 422
    too_big = {"chain": [{**fake("ok"), "max_output_tokens": 10**7}]}
    assert client.put("/llm", json=too_big).status_code == 422
    assert client.delete("/llm").json()["source"] == "default"


def test_default_chain_uses_correct_endpoints_keys_and_org(data, monkeypatch):
    config, _ = providers.active_config()
    assert providers.api_key(config.chain[2]) == ""
    for env in ("OPENAI_API_KEY", "GEMINI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.setenv(env, f"{env}-value")
    monkeypatch.setenv("OPENAI_PROJECT_ID", "proj_prod")
    monkeypatch.delenv("OPENAI_ORG_ID", raising=False)
    providers.get_settings.cache_clear()
    assert providers.api_key(config.chain[2]) == "GEMINI_API_KEY-value"

    built = {s.provider: providers.create_provider(s) for s in config.chain}
    assert str(built["openrouter"].client.base_url) == "https://openrouter.ai/api/v1/"
    assert str(built["gemini"].client.base_url).startswith("https://generativelanguage.googleapis")
    assert built["openai"].client.api_key == "OPENAI_API_KEY-value"
    assert built["openai"].client.project == "proj_prod"  # org/project only sent to OpenAI
    assert built["openrouter"].client.project is None
    assert built["openai"].max_tokens_param == "max_completion_tokens"
    assert built["gemini"].max_tokens_param == "max_tokens"
    assert isinstance(built["anthropic"], ClaudeProvider)
    assert built["anthropic"].max_output_tokens == 8000


def _openai_response(content=GOOD, finish_reason="stop"):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(message=SimpleNamespace(content=content), finish_reason=finish_reason)
        ],
        usage=SimpleNamespace(prompt_tokens=5, completion_tokens=2),
        _request_id="req_openai",
    )


def test_openai_compat_wire_format_and_truncation():
    sent = {}
    replies = [_openai_response(), _openai_response(), _openai_response('{"summ', "length")]

    def create(**kw):
        sent.clear()
        sent.update(kw)
        return replies.pop(0)

    p = OpenAICompatProvider(
        label="openai:m",
        model="m",
        api_key="k",
        base_url=None,
        params={"reasoning_effort": "low"},
        timeout=5,
        max_output_tokens=1234,
    )
    p.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    assert p.chat("SYS", USER, "Enrichment", SCHEMA) == Completion(GOOD, 5, 2, "req_openai")
    assert sent["messages"][0] == {"role": "system", "content": "SYS"}
    assert sent["response_format"]["json_schema"]["strict"] is True
    assert sent["extra_body"] == {"reasoning_effort": "low"}
    assert sent["max_completion_tokens"] == 1234 and "max_tokens" not in sent

    p.chat("SYS", USER, "Enrichment", SCHEMA)
    with pytest.raises(ProviderError, match="max_output_tokens"):
        p.chat("SYS", USER, "Enrichment", SCHEMA)


def _claude_response(stop_reason="end_turn", text=GOOD):
    return SimpleNamespace(
        stop_reason=stop_reason,
        stop_details=None,
        content=[SimpleNamespace(type="thinking"), SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=5, output_tokens=3),
        _request_id="req_claude",
    )


def test_claude_wire_format_refusal_and_truncation():
    sent = {}
    replies = [
        _claude_response(),
        _claude_response("refusal", ""),
        _claude_response("max_tokens", '{"summ'),
    ]

    def create(**kw):
        sent.clear()
        sent.update(kw)
        return replies.pop(0)

    p = ClaudeProvider(
        label="anthropic:claude-opus-5-5",
        model="claude-opus-5-5",
        api_key="k",
        params={"effort": "low"},
        timeout=5,
        max_output_tokens=4321,
    )
    p.client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create)))
    assert p.chat("SYS", USER, "Enrichment", SCHEMA) == Completion(GOOD, 5, 3, "req_claude")
    assert sent["system"] == "SYS" and sent["messages"] == USER
    assert sent["max_tokens"] == 4321
    assert sent["output_config"] == {
        "format": {"type": "json_schema", "schema": SCHEMA},
        "effort": "low",
    }
    assert sent["fallbacks"] == "default" and sent["betas"] == ["server-side-fallback-2026-07-01"]
    with pytest.raises(ProviderError, match="refused"):
        p.chat("SYS", USER, "Enrichment", SCHEMA)
    with pytest.raises(ProviderError, match="max_output_tokens"):
        p.chat("SYS", USER, "Enrichment", SCHEMA)


def test_token_ledger_reads_old_two_column_lines(data):
    from pulse import store

    path = data / "runs" / "2026-10-30.tokens"
    path.parent.mkdir(parents=True)
    path.write_text("2026-10-30\t50\n")  # written before the provider column existed
    store.add_tokens("2026-10-30", "2026-10-30", 25, "openai:m")
    assert store.tokens_used("2026-10-30") == 75
    assert store.tokens_by_provider("2026-10-30") == {"unknown": 50, "openai:m": 25}
