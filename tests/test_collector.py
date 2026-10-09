from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.collector import collect_messages, history, search_window
from app.pipeline import Feishu, PipelineError, body_gate

START = datetime(2026, 9, 29, tzinfo=timezone.utc)


async def test_notice_splits_and_keeps_parent_hits():
    calls = []

    class Client:
        async def search(self, filters):
            calls.append(filters)
            return (
                ({"parent"}, ["SEARCH_NOTICE"]) if len(calls) == 1 else ({f"child{len(calls)}"}, [])
            )

    ids, issues = await search_window(Client(), {}, START, START + timedelta(minutes=10))
    assert ids == {"parent", "child2", "child3"} and not issues
    assert len(calls) == 3


async def test_thread_history_pages_without_server_time_filter(monkeypatch):
    async def wait():
        pass

    monkeypatch.setattr(body_gate, "wait", wait)
    calls = []

    class Client:
        async def request(self, method, path, params):
            calls.append(params)
            return {
                "items": [{"message_id": str(len(calls))}],
                "has_more": len(calls) == 1,
                "page_token": "next",
            }

    items = [
        item
        async for item in history(Client(), "thread", "thread-a", START, START + timedelta(days=1))
    ]
    assert len(items) == 2 and "start_time" not in calls[0] and "end_time" not in calls[0]
    assert calls[1]["page_token"] == "next"


async def test_history_detects_loop(monkeypatch):
    async def wait():
        pass

    monkeypatch.setattr(body_gate, "wait", wait)

    class Client:
        async def request(self, *args, **kwargs):
            return {"items": [], "has_more": True, "page_token": "same"}

    with pytest.raises(PipelineError, match=""):
        _ = [
            item
            async for item in history(Client(), "chat", "chat-a", START, START + timedelta(days=1))
        ]


def item(identifier, timestamp, sender="a"):
    return {
        "message_id": identifier,
        "thread_id": "thread-a",
        "chat_id": "chat-a",
        "create_time": str(int(timestamp.timestamp() * 1000)),
        "sender": {"sender_type": "user", "id": sender},
        "msg_type": "text",
        "body": {"content": '{"text":"测试"}'},
        "mentions": [],
    }


async def test_collect_filters_future_and_preserves_matching_thread_replies(monkeypatch):
    import app.collector as module

    async def search(self, filters):
        self.search_metadata["root"] = {"thread_id": "thread-a"}
        return {"root"}, []

    async def body(self, identifier):
        return item("root", START + timedelta(minutes=1))

    async def mocked_history(*args):
        yield item("past-context", START - timedelta(minutes=1), sender="b")
        yield item("own-reply", START + timedelta(minutes=2))
        yield item("future", START + timedelta(days=1))

    monkeypatch.setattr(Feishu, "search", search)
    monkeypatch.setattr(Feishu, "body", body)
    monkeypatch.setattr(module, "history", mocked_history)
    run = SimpleNamespace(window_start=START, window_end=START + timedelta(hours=1))
    messages, counts, issues = await collect_messages(run, SimpleNamespace(open_id="a"), "fake")
    rows = {m["message_id"]: m for m in messages}
    assert set(rows) == {"root", "past-context", "own-reply"}
    assert rows["past-context"]["context_only"] is True
    assert not rows["own-reply"]["context_only"]
    assert counts["context"] == 1 and not issues
