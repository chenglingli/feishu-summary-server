import json
from datetime import datetime, timedelta, timezone

from app.pipeline import parse_message
from app.runner import split_report


def test_at_all_excluded_but_own_union_retained():
    start = datetime(2026, 9, 29, tzinfo=timezone.utc)
    item = {
        "message_id": "m1",
        "create_time": str(int(start.timestamp() * 1000)),
        "sender": {"sender_type": "user", "id": "a"},
        "mentions": [{"id": "all", "id_type": "open_id"}],
        "msg_type": "text",
        "body": {"content": json.dumps({"text": "项目进展"})},
    }
    assert parse_message(item, "a", {"at_me"}, start, start + timedelta(days=1)) is None
    assert (
        parse_message(item, "a", {"at_me", "own"}, start, start + timedelta(days=1))["text"]
        == "项目进展"
    )
    assert parse_message(item, "a", {"own"}, start - timedelta(days=1), start) is None


def test_utf8_splitting_preserves_all_text():
    content = "中文🧪" * 20000
    pieces = split_report(content)
    assert all(len(x.encode()) < 20000 for x in pieces)
    assert "".join(x.split("\n", 1)[1] for x in pieces) == content


async def test_search_uses_official_nested_metadata_and_paginates(monkeypatch):
    import httpx

    from app.pipeline import Feishu, search_gate

    async def no_wait():
        return None

    monkeypatch.setattr(search_gate, "wait", no_wait)
    requests = []

    def respond(request):
        requests.append(request)
        page = request.url.params.get("page_token")
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "items": [
                        {
                            "id": "m2" if page else "m1",
                            "meta_data": {"message_id": "m2" if page else "m1"},
                        }
                    ],
                    "has_more": not bool(page),
                    "page_token": "next" if not page else "",
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        ids, issues = await Feishu(client, "fake").search({"chat_type": "p2p"})
    assert ids == {"m1", "m2"}
    assert issues == []
    assert len(requests) == 2


def test_direct_mention_uses_string_open_id():
    start = datetime(2026, 9, 29, tzinfo=timezone.utc)
    item = {
        "message_id": "m1",
        "create_time": str(int(start.timestamp() * 1000)),
        "sender": {"sender_type": "user", "id": "b"},
        "mentions": [{"id": "a", "id_type": "open_id"}],
        "msg_type": "text",
        "body": {"content": '{"text":"@a 请看一下"}'},
    }
    assert (
        parse_message(item, "a", {"at_me"}, start, start + timedelta(days=1))["text"]
        == "@a 请看一下"
    )
