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


def test_report_groups_categories_without_exposing_evidence():
    from app.summarizer import render_summary

    result = Summary(
        topics=[
            Finding(summary="人员安排", sources=["S1"], category="组织类"),
            Finding(summary="接口方案", sources=["S2"], category="技术类"),
        ],
        decisions=[],
        actions=[
            Finding(
                summary="验收项目", sources=["S3"], category="项目类", owner="张三", deadline="周五"
            )
        ],
    )
    text = render_summary([result])
    assert text.index("【组织类】") < text.index("【项目类】") < text.index("【技术类】")
    assert "待办：验收项目；负责人：张三；截止：周五" in text
    assert "来源定位" not in text and "S1" not in text and "S3" not in text
    assert "日常协作类" not in text and "其他" not in text
    # Hiding display references must not discard evidence used by validation.
    assert result.topics[0].sources == ["S1"]


def test_report_statistics_use_window_and_collector_counts():
    from datetime import datetime, timedelta, timezone

    from app.cards import report_content

    end = datetime(2026, 1, 2, 11, tzinfo=timezone.utc)
    text = report_content(
        end - timedelta(hours=24),
        end,
        {"unique": 120, "parsed": 97, "context": 14, "unsupported": 11},
        ["UNSUPPORTED_MESSAGE_TYPES"],
        "【组织类】\n• 要点：虚构测试",
    )
    assert "2026-01-01 19:00 至 2026-01-02 19:00（北京时间）" in text
    assert "过去 24 小时内 97 条" in text and "14 条上下文" in text
    assert "11 条非文本" in text and "覆盖提示" in text
    assert "120 条" not in text and "partial" not in text
    empty = report_content(end - timedelta(hours=12), end, {"parsed": 0}, [], "没有可总结的消息。")
    assert "过去 12 小时内 0 条" in empty and "覆盖提示" not in empty


async def test_model_logs_attempts_without_message_or_response_content(monkeypatch, caplog):
    import httpx

    from app.config import get_settings
    from app.leases import current_lease

    monkeypatch.setattr(Settings, "secret", lambda self, field: "secret-do-not-log")
    monkeypatch.setattr(get_settings(), "model_base_url", "https://example.test/v1")
    caplog.set_level("INFO", logger="summary.runner")
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "private-invalid-output"
                            if len(calls) == 1
                            else '{"topics":[],"decisions":[],"actions":[]}'
                        }
                    }
                ]
            },
        )

    token = current_lease.set(None)
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            await request_summary(client, [{"text": "private-chat-body"}], {}, set())
    finally:
        current_lease.reset(token)
    records = [json.loads(r.message) for r in caplog.records if r.name == "summary.runner"]
    starts = [r for r in records if r["event"] == "model_call_started"]
    assert len(starts) == 2 and starts[0]["call_id"] != starts[1]["call_id"]
    assert any(r.get("code") == "MODEL_JSON_SCHEMA_INVALID" for r in records)
    assert records[-1]["event"] == "model_call_succeeded"
    assert all(
        value not in caplog.text
        for value in ["private-chat-body", "private-invalid-output", "secret-do-not-log"]
    )


@pytest.mark.parametrize("texts", [["甲乙", "丙丁"], ["甲乙丙丁"]])
async def test_truncated_extraction_splits_without_losing_text(monkeypatch, texts):
    from app import summarizer as module

    calls = []
    budget = object()

    async def request(client, data, sources, primary, **kwargs):
        assert kwargs["budget"] is budget and kwargs["split_on_truncation"]
        calls.append(data)
        if len(calls) == 1:
            raise PipelineError("MODEL_OUTPUT_TRUNCATED")
        return Summary(topics=[], decisions=[], actions=[])

    monkeypatch.setattr(module, "request_summary", request)
    monkeypatch.setattr(module, "retry", lambda code: None)
    pieces = [{"source": f"S{i}", "text": text} for i, text in enumerate(texts)]
    sources = {p["source"]: p for p in pieces}
    results = await module.extract_batches(None, pieces, sources, set(sources), budget)
    assert len(results) == 2
    assert "".join(p["text"] for call in calls[1:] for p in call) == "".join(texts)
    assert {p["source"] for call in calls[1:] for p in call} == set(sources)


@pytest.mark.parametrize(
    "code, expected_calls", [("MODEL_OUTPUT_TRUNCATED", 4), ("MODEL_AUTH_REJECTED", 1)]
)
async def test_extraction_split_is_bounded_and_does_not_hide_other_errors(
    monkeypatch, code, expected_calls
):
    from app import summarizer as module

    calls = []

    async def request(*args, **kwargs):
        calls.append(1)
        raise PipelineError(code)

    monkeypatch.setattr(module, "request_summary", request)
    monkeypatch.setattr(module, "retry", lambda code: None)
    piece = {"source": "S1", "text": "字" * 100}
    with pytest.raises(PipelineError, match=code):
        await module.extract_batches(None, [piece], {"S1": piece}, {"S1"}, object())
    assert len(calls) == expected_calls


@pytest.mark.parametrize(
    "code",
    [
        "MODEL_OUTPUT_TRUNCATED",
        "MODEL_MERGE_INCOMPLETE",
        "MODEL_SOURCE_INVALID",
        "MODEL_JSON_SCHEMA_INVALID",
        "MODEL_RESPONSE_SCHEMA_CHANGED",
        "MODEL_EMPTY_OUTPUT",
        "MODEL_OUTPUT_INVALID",
        "MODEL_TIMEOUT",
        "MODEL_NETWORK_ERROR",
        "MODEL_SERVICE_UNAVAILABLE",
        "MODEL_RATE_LIMITED",
        "MODEL_TOKEN_BUDGET_EXCEEDED",
    ],
)
async def test_failed_merge_keeps_all_validated_findings(monkeypatch, code):
    from app import summarizer as module

    async def request(*args, **kwargs):
        assert kwargs["split_on_truncation"]
        raise PipelineError(code)

    monkeypatch.setattr(module, "request_summary", request)
    inputs = [
        Summary(topics=[Finding(summary=f"要点{i}", sources=[f"S{i}"])], decisions=[], actions=[])
        for i in range(2)
    ]
    results = await module.merge_batch(None, [s.model_dump() for s in inputs], {}, set(), object())
    assert results == inputs


def test_budget_settles_actual_usage_and_keeps_reasoning_in_total():
    from app.summarizer import ModelBudget

    budget = ModelBudget(10000)
    payload = {"messages": [{"content": "测试"}], "max_tokens": 6000}
    reserved = budget.reserve(payload)
    counts = budget.settle(
        reserved,
        {
            "prompt_tokens": 100,
            "completion_tokens": 200,
            "total_tokens": 300,
            "completion_tokens_details": {"reasoning_tokens": 150},
        },
    )
    assert budget.used == 300
    assert counts["reasoning_tokens"] == 150
    budget.reserve(payload)  # Reclaimed unused output reservation makes the next call fit.


@pytest.mark.parametrize(
    "usage",
    [
        None,
        [],
        {},
        {"total_tokens": 1},
        {"prompt_tokens": -1, "completion_tokens": 2, "total_tokens": 1},
        {"prompt_tokens": True, "completion_tokens": 2, "total_tokens": 3},
        {"prompt_tokens": "1", "completion_tokens": 2, "total_tokens": 3},
        {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 4},
        {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    ],
)
def test_unreliable_usage_keeps_conservative_reservation(usage):
    from app.summarizer import ModelBudget

    budget = ModelBudget(10000)
    reserved = budget.reserve({"messages": [], "max_tokens": 6000})
    assert budget.settle(reserved, usage) == {}
    assert budget.used == reserved


async def test_truncation_is_charged_actual_usage_and_logs_only_metadata(monkeypatch, caplog):
    import httpx

    from app.summarizer import ModelBudget

    monkeypatch.setattr(Settings, "secret", lambda self, field: "secret-key")
    caplog.set_level("INFO", logger="summary.runner")
    response = {
        "choices": [
            {
                "finish_reason": "length",
                "message": {"content": "private-output", "reasoning_content": "private-reasoning"},
            }
        ],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 200,
            "total_tokens": 300,
            "completion_tokens_details": {"reasoning_tokens": 150},
        },
    }
    budget = ModelBudget(50000)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response))
    ) as client:
        with pytest.raises(PipelineError, match="MODEL_OUTPUT_TRUNCATED"):
            await request_summary(
                client,
                [{"text": "private-input"}],
                {},
                set(),
                budget=budget,
                split_on_truncation=True,
            )
    assert budget.used == 300
    rows = [json.loads(r.message) for r in caplog.records if r.name == "summary.runner"]
    usage = next(r for r in rows if r["event"] == "model_usage")
    assert usage["total_tokens"] == 300 and usage["reasoning_tokens"] == 150
    assert next(r for r in rows if r["event"] == "model_output")["code"] == "length"
    assert not any(
        s in caplog.text
        for s in ["private-input", "private-output", "private-reasoning", "secret-key"]
    )


async def test_actual_usage_over_budget_stops_even_if_response_is_valid(monkeypatch):
    import httpx

    from app.summarizer import ModelBudget

    monkeypatch.setattr(Settings, "secret", lambda self, field: "fake")
    budget = ModelBudget(20000)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "usage": {
                        "prompt_tokens": 20000,
                        "completion_tokens": 1000,
                        "total_tokens": 21000,
                    },
                    "choices": [
                        {"message": {"content": '{"topics":[],"decisions":[],"actions":[]}'}}
                    ],
                },
            )
        )
    ) as client:
        with pytest.raises(PipelineError, match="MODEL_TOKEN_BUDGET_EXCEEDED"):
            await request_summary(client, [], {}, set(), budget=budget)
    assert budget.used == 21000


@pytest.mark.parametrize(
    "model", ["openai/qwen3.8-flash", "qwen3.8-flash", "openai/deepseek-v4-flash"]
)
@pytest.mark.parametrize("merge", [False, True])
async def test_qwen_disables_thinking_for_extraction_merge_and_retry(monkeypatch, model, merge):
    import httpx

    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "model_id", model)
    monkeypatch.setattr(Settings, "secret", lambda self, field: "fake")
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        content = (
            "invalid-json" if len(requests) == 1 else '{"topics":[],"decisions":[],"actions":[]}'
        )
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        await request_summary(client, [], {}, set(), merge=merge)
    assert len(requests) == 2
    for payload in requests:
        assert payload["model"] == model
        if "qwen" in model:
            assert payload["enable_thinking"] is False
        else:
            assert "enable_thinking" not in payload


@pytest.mark.parametrize(
    "bad_sources, reason",
    [([], "missing_sources"), (["S99"], "unknown_sources"), (["S2"], "context_only_sources")],
)
async def test_source_repair_has_specific_reason_and_allowed_ids(
    monkeypatch, caplog, bad_sources, reason
):
    import httpx

    monkeypatch.setattr(Settings, "secret", lambda self, field: "fake")
    caplog.set_level("INFO", logger="summary.runner")
    payloads = []

    def respond(request):
        payloads.append(json.loads(request.content))
        result = Summary(
            topics=[
                Finding(summary="测试结论", sources=bad_sources if len(payloads) == 1 else ["S1"])
            ],
            decisions=[],
            actions=[],
        )
        return httpx.Response(
            200, json={"choices": [{"message": {"content": result.model_dump_json()}}]}
        )

    sources = {"S1": {"text": "测试结论"}, "S2": {"text": "背景", "context_only": True}}
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await request_summary(client, [], sources, {"S1", "S_OTHER_BATCH"})
    assert result.topics[0].sources == ["S1"]
    repair = payloads[1]["messages"][-1]["content"]
    assert reason in repair and '"S1"' in repair and '"S2"' in repair
    assert "S_OTHER_BATCH" not in repair
    assert any(
        json.loads(r.message).get("code") == reason
        for r in caplog.records
        if r.name == "summary.runner"
    )


async def test_context_only_batch_never_calls_model(monkeypatch):
    from app import summarizer as module

    async def request(*args, **kwargs):
        pytest.fail("Background-only batches must not call the model")

    monkeypatch.setattr(module, "request_summary", request)
    item = {"source": "S2", "text": "只有背景", "context_only": True}
    assert await module.extract_batches(None, [item], {"S2": item}, {"S1"}, object()) == []


async def test_merge_rejects_source_outside_its_input_summaries(monkeypatch):
    import httpx

    monkeypatch.setattr(Settings, "secret", lambda self, field: "fake")
    original = Summary(
        topics=[Finding(summary="本批结论", sources=["S1"])], decisions=[], actions=[]
    )
    bad = Summary(topics=[Finding(summary="别批结论", sources=["S2"])], decisions=[], actions=[])
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"choices": [{"message": {"content": bad.model_dump_json()}}]}
            )
        )
    ) as client:
        with pytest.raises(PipelineError, match="MODEL_SOURCE_INVALID"):
            await request_summary(
                client,
                [original.model_dump()],
                {"S1": {"text": "本批结论"}, "S2": {"text": "别批结论"}},
                {"S1", "S2"},
                merge=True,
            )


@pytest.mark.parametrize(
    "code",
    [
        "CANCELLED",
        "LEASE_LOST",
        "MODEL_AUTH_REJECTED",
        "MODEL_CONTENT_FILTERED",
        "MODEL_REQUEST_REJECTED",
        "UNEXPECTED_ERROR",
    ],
)
async def test_merge_fallback_does_not_hide_control_or_policy_errors(monkeypatch, code):
    from app import summarizer as module

    async def request(*args, **kwargs):
        raise PipelineError(code)

    monkeypatch.setattr(module, "request_summary", request)
    batch = [Summary(topics=[], decisions=[], actions=[]).model_dump()] * 2
    with pytest.raises(PipelineError, match=code):
        await module.merge_batch(None, batch, {}, set(), object())


async def test_incomplete_merge_retries_then_renders_original_decisions(monkeypatch):
    import httpx

    from app.summarizer import ModelBudget, merge_batch, render_summary

    monkeypatch.setattr(Settings, "secret", lambda self, field: "fake")
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"topics":[],"decisions":[],"actions":[]}'}}]
            },
        )

    summaries = [
        Summary(
            topics=[], decisions=[Finding(summary=f"保留决定{i}", sources=[f"S{i}"])], actions=[]
        )
        for i in (1, 2)
    ]
    sources = {f"S{i}": {"text": f"保留决定{i}"} for i in (1, 2)}
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await merge_batch(
            client, [s.model_dump() for s in summaries], sources, set(sources), ModelBudget(200000)
        )
    assert len(requests) == 2
    assert '"decisions": ["S1", "S2"]' in requests[1]["messages"][-1]["content"]
    assert result == summaries
    report = render_summary(result)
    assert "保留决定1" in report and "保留决定2" in report


@pytest.mark.parametrize(
    "error, expected_calls", [("MODEL_SOURCE_INVALID", 3), ("MODEL_TOKEN_BUDGET_EXCEEDED", 2)]
)
async def test_partial_report_retains_successful_batches(monkeypatch, error, expected_calls):
    from app import summarizer as module

    calls = []
    monkeypatch.setattr(module, "batches", lambda values: ([v] for v in values))

    async def request(client, data, sources, primary, **kwargs):
        calls.append(data)
        if len(calls) == 2:
            raise PipelineError(error)
        return Summary(
            topics=[Finding(summary=data[0]["text"], sources=[data[0]["source"]])],
            decisions=[],
            actions=[],
        )

    monkeypatch.setattr(module, "request_summary", request)
    report = await module.summarize_messages(
        [{"text": text} for text in ["成功甲", "失败乙", "成功丙"]]
    )
    assert report.incomplete and "部分总结" in report and "成功甲" in report
    assert "失败乙" not in report
    assert ("成功丙" in report) == (expected_calls == 3)
    assert len(calls) == expected_calls


async def test_no_valid_result_reports_failure_not_no_activity(monkeypatch):
    from app import summarizer as module

    async def request(*args, **kwargs):
        raise PipelineError("MODEL_SERVICE_UNAVAILABLE")

    monkeypatch.setattr(module, "request_summary", request)
    report = await module.summarize_messages([{"text": "有待办"}])
    assert report.incomplete
    assert "未能生成" in report and "这不代表没有事项" in report


async def test_split_keeps_validated_child_when_sibling_fails(monkeypatch):
    from app import summarizer as module

    calls = []

    async def request(client, data, sources, primary, **kwargs):
        calls.append(data)
        if len(calls) == 1:
            raise PipelineError("MODEL_OUTPUT_TRUNCATED")
        if len(calls) == 3:
            raise PipelineError("MODEL_SOURCE_INVALID")
        return Summary(topics=[Finding(summary="保留", sources=["S1"])], decisions=[], actions=[])

    monkeypatch.setattr(module, "request_summary", request)
    monkeypatch.setattr(module, "retry", lambda code: None)
    data = [{"source": "S1", "text": "甲"}, {"source": "S2", "text": "乙"}]
    collected = []
    with pytest.raises(PipelineError, match="MODEL_SOURCE_INVALID"):
        await module.extract_batches(
            None, data, {v["source"]: v for v in data}, {"S1", "S2"}, object(), collected=collected
        )
    assert len(collected) == 1 and collected[0].topics[0].summary == "保留"
