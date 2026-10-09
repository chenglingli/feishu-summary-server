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
