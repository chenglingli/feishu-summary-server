import json

import pytest

from app.cards import report_parts
from app.config import Settings
from app.pipeline import Finding, PipelineError, Summary
from app.summarizer import deduplicate, request_summary, validate_findings


def test_only_context_cannot_create_action():
    result = Summary(topics=[], decisions=[], actions=[Finding(summary="new task", sources=["S1"])])
    with pytest.raises(ValueError):
        validate_findings(result, {"S1": {"text": "old action"}}, set())


def test_owner_and_deadline_require_explicit_source_words():
    result = Summary(
        topics=[],
        decisions=[],
        actions=[Finding(summary="检查", sources=["S1"], owner="李四", deadline="明天")],
    )
    validate_findings(result, {"S1": {"text": "张三周五检查"}}, {"S1"})
    assert result.actions[0].owner is None and result.actions[0].deadline is None


def test_dedup_preserves_sources_and_distinct_deadlines():
    records = [
        Finding(summary="检查", sources=["S1"], deadline="周五"),
        Finding(summary="检查", sources=["S2"], deadline="周五"),
        Finding(summary="检查", sources=["S3"], deadline="周六"),
    ]
    result = deduplicate(records)
    assert len(result) == 2 and result[0].sources == ["S1", "S2"]


def test_cards_preserve_unicode_and_bound_serialized_request():
    content = '中文😀"\\\n' * 6000
    cards = report_parts(content)
    recovered = ""
    for encoded in cards:
        card = json.loads(encoded)
        assert card["elements"][0]["text"]["tag"] == "plain_text"
        recovered += card["elements"][0]["text"]["content"].split("\n", 1)[1]
        assert (
            len(
                json.dumps(
                    {
                        "receive_id": "x" * 128,
                        "content": encoded,
                        "msg_type": "interactive",
                        "uuid": "x" * 50,
                    },
                    ensure_ascii=False,
                ).encode()
            )
            <= 24000
        )
    assert recovered == content


async def test_merge_cannot_silently_drop_decision(monkeypatch):
    import httpx

    monkeypatch.setattr(Settings, "secret", lambda self, name: "fake")
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "model_base_url", "https://model.example/v1")
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"topics":[],"decisions":[],"actions":[]}'}}]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(PipelineError):
            await request_summary(
                client,
                [
                    {
                        "topics": [],
                        "decisions": [{"summary": "决定", "sources": ["S1"]}],
                        "actions": [],
                    }
                ],
                {"S1": {"text": "决定"}},
                {"S1"},
                merge=True,
            )
    assert len(calls) == 2


def test_model_budget_covers_repair_and_merge_requests():
    from app.pipeline import PipelineError
    from app.summarizer import ModelBudget

    budget = ModelBudget(10000)
    payload = {"messages": [{"content": "fake"}], "max_tokens": 6000}
    budget.reserve(payload)
    with pytest.raises(PipelineError) as error:
        budget.reserve(payload)
    assert error.value.code == "MODEL_TOKEN_BUDGET_EXCEEDED"


@pytest.mark.asyncio
async def test_kimi_k3_uses_supported_parameters(monkeypatch):
    import httpx

    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "model_id", "kimi-k3")
    monkeypatch.setattr(get_settings(), "model_base_url", "https://example.test/v1")
    monkeypatch.setattr(Settings, "secret", lambda self, field: "fake-key")
    calls = []

    def respond(request):
        payload = json.loads(request.content)
        calls.append(payload)
        assert "temperature" not in payload
        assert payload["reasoning_effort"] == "low"
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"topics":[],"decisions":[],"actions":[]}'}}]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        await request_summary(client, [], {}, set())
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("status", "choice", "expected", "attempts"),
    [
        (401, None, "MODEL_AUTH_REJECTED", 1),
        (400, None, "MODEL_REQUEST_REJECTED", 1),
        (429, None, "MODEL_RATE_LIMITED", 2),
        (503, None, "MODEL_SERVICE_UNAVAILABLE", 2),
        (
            200,
            {"finish_reason": "length", "message": {"content": "{"}},
            "MODEL_OUTPUT_TRUNCATED",
            2,
        ),
        (200, {"message": {"content": ""}}, "MODEL_EMPTY_OUTPUT", 2),
        (200, {"message": {"content": "{"}}, "MODEL_JSON_SCHEMA_INVALID", 2),
        (
            200,
            {
                "message": {
                    "content": '{"topics":[{"summary":"test","sources":["unknown"]}],"decisions":[],"actions":[]}'
                }
            },
            "MODEL_SOURCE_INVALID",
            2,
        ),
    ],
)
async def test_model_failures_are_classified(monkeypatch, status, choice, expected, attempts):
    import httpx

    import app.summarizer as summarizer
    from app.config import get_settings

    monkeypatch.setattr(Settings, "secret", lambda self, field: "fake-key")
    monkeypatch.setattr(get_settings(), "model_base_url", "https://example.test/v1")
    calls, retries = [], []
    monkeypatch.setattr(summarizer, "retry", retries.append)

    async def no_wait(seconds):
        pass

    monkeypatch.setattr(summarizer.asyncio, "sleep", no_wait)

    def respond(request):
        calls.append(request)
        return httpx.Response(status, json={"choices": [choice]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(PipelineError) as error:
            await request_summary(client, [], {}, set())
    assert error.value.code == expected
    assert len(calls) == attempts
    assert retries == ([expected.lower()] if attempts == 2 else [])


async def test_truncated_kimi_output_increases_budgeted_limit(monkeypatch):
    import httpx

    from app.config import get_settings
    from app.summarizer import ModelBudget

    monkeypatch.setattr(Settings, "secret", lambda self, field: "fake-key")
    monkeypatch.setattr(get_settings(), "model_base_url", "https://example.test/v1")
    monkeypatch.setattr(get_settings(), "model_id", "kimi-k3")
    payloads = []
    budget = ModelBudget(100000)

    def respond(request):
        payloads.append(json.loads(request.content))
        first = len(payloads) == 1
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "length" if first else "stop",
                        "message": {
                            "content": "{" if first else '{"topics":[],"decisions":[],"actions":[]}'
                        },
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        await request_summary(client, [], {}, set(), budget=budget)
    assert [p["max_completion_tokens"] for p in payloads] == [6000, 12000]
    assert all(p["response_format"] == {"type": "json_object"} for p in payloads)
    assert all("max_tokens" not in p for p in payloads)
    assert budget.used == sum(
        len(json.dumps(p["messages"], ensure_ascii=False).encode())
        + p["max_completion_tokens"]
        + 1024
        for p in payloads
    )


async def test_timeout_retry_keeps_prompt_and_classifies_failure(monkeypatch):
    import httpx

    import app.summarizer as summarizer
    from app.config import get_settings

    monkeypatch.setattr(Settings, "secret", lambda self, field: "fake-key")
    monkeypatch.setattr(get_settings(), "model_base_url", "https://example.test/v1")
    calls, retries = [], []
    monkeypatch.setattr(summarizer, "retry", retries.append)

    async def no_wait(seconds):
        pass

    monkeypatch.setattr(summarizer.asyncio, "sleep", no_wait)

    def respond(request):
        calls.append(json.loads(request.content))
        raise httpx.ReadTimeout("synthetic timeout", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(PipelineError) as error:
            await request_summary(client, [], {}, set())
    assert error.value.code == "MODEL_TIMEOUT"
    assert len(calls) == 2 and calls[0] == calls[1]
    assert retries == ["model_timeout"]
