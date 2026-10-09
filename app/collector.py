"""Time-bounded search, delayed reconciliation and explicitly bounded context."""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app.db import now
from app.metrics import CollectionProgress
from app.pipeline import Feishu, PipelineError, body_gate, parse_message


def time_filter(start, end):
    # API accepts whole seconds; expand outward and enforce the exact half-open window locally.
    return {
        "start_time": start.replace(microsecond=0).isoformat(),
        "end_time": (end.replace(microsecond=0) + timedelta(seconds=1)).isoformat(),
    }


async def search_window(client, filters, start, end):
    ids, notices = await client.search({**filters, "time_range": time_filter(start, end)})
    if not notices or end - start <= timedelta(minutes=5):
        return ids, notices
    middle = start + (end - start) / 2
    left, left_issues = await search_window(client, filters, start, middle)
    right, right_issues = await search_window(client, filters, middle, end)
    # Retain the parent's hits even when the split query returns fewer indexed messages.
    return ids | left | right, left_issues + right_issues


async def history(client, container_type, container_id, start, end, on_page=None):
    token, seen = "", set()
    while True:
        params = {
            "container_id_type": container_type,
            "container_id": container_id,
            "page_size": 50,
            "sort_type": "ByCreateTimeAsc",
            "user_id_type": "open_id",
        }
        if container_type == "chat":
            params.update(
                start_time=str(int(start.timestamp())), end_time=str(int(end.timestamp()) + 1)
            )
        # Thread containers do not support server-side time filtering.
        if token:
            params["page_token"] = token
        await body_gate.wait()
        data = await client.request("GET", "/im/v1/messages", params=params)
        if not isinstance(data.get("items"), list):
            raise PipelineError("HISTORY_SCHEMA_CHANGED")
        if on_page:
            on_page()
        for item in data["items"]:
            yield item
        if not data.get("has_more"):
            return
        token = data.get("page_token")
        if not token or token in seen:
            raise PipelineError("HISTORY_PAGINATION_LOOP")
        seen.add(token)


def stamp(item):
    return datetime.fromtimestamp(int(item["create_time"]) / 1000, timezone.utc)


def qualifies(item, open_id, p2p):
    return (
        p2p
        or item.get("sender", {}).get("id") == open_id
        or any(m.get("id") == open_id for m in item.get("mentions", []))
    )


async def collect_messages(run, identity, access):
    reasons: dict[str, set[str]] = {}
    issues: list[str] = []
    counts: dict[str, int] = {}
    progress = CollectionProgress(counts)
    progress.update(
        force=True,
        phase=1,
        search_pages=0,
        unique=0,
        body_read=0,
        body_attempted=0,
        history_pages=0,
    )
    seen_search_ids: set[str] = set()

    def search_page(ids):
        seen_search_ids.update(ids)
        progress.update(search_pages=counts["search_pages"] + 1, unique=len(seen_search_ids))

    def history_page():
        progress.update(history_pages=counts["history_pages"] + 1)

    messages: dict[str, dict] = {}
    raw_hits: dict[str, dict] = {}
    async with httpx.AsyncClient(timeout=30) as http:
        client = Feishu(http, access)
        client.on_search_page = search_page
        routes: dict[str, dict[str, Any]] = {
            "p2p": {"chat_type": "p2p"},
            "at_me": {"chat_type": "group", "is_at_me": True},
            "own": {"from_ids": [identity.open_id]},
        }
        successful = 0

        async def search_routes(start, end, tail=False):
            nonlocal successful
            for route, filters in routes.items():
                try:
                    ids, notices = await search_window(
                        client, {**filters, "from_types": ["user"]}, start, end
                    )
                    if not tail:
                        successful += 1
                    counts[route] = len(ids) if not tail else counts.get(route, 0)
                    issues.extend(f"{route}:{issue}" for issue in notices)
                    for identifier in ids:
                        reasons.setdefault(identifier, set()).add(route)
                except PipelineError as error:
                    if error.code == "AUTHORIZATION_REQUIRED":
                        raise
                    issues.append(f"{route}:{error.code}")

        await search_routes(run.window_start, run.window_end)
        if not successful:
            raise PipelineError("ALL_SEARCH_ROUTES_FAILED")
        progress.update(force=True, phase=2, unique=len(reasons))
        await asyncio.sleep(
            max(0, (run.window_end + timedelta(seconds=60) - now()).total_seconds())
        )
        progress.update(force=True, phase=3)
        before_tail = len(reasons)
        await search_routes(
            max(run.window_start, run.window_end - timedelta(minutes=10)), run.window_end, tail=True
        )
        counts["tail_new"] = len(reasons) - before_tail
        progress.update(force=True, phase=4, body_total=len(reasons), unique=len(reasons))
        for identifier, matched in reasons.items():
            try:
                item = await client.body(identifier)
                counts["body_read"] += 1
                parsed = parse_message(
                    item, identity.open_id, matched, run.window_start, run.window_end
                )
                if parsed:
                    parsed["context_only"] = False
                    messages[identifier], raw_hits[identifier] = parsed, item
            except PipelineError as error:
                if error.code == "AUTHORIZATION_REQUIRED":
                    raise
                issues.append("MESSAGE_BODY_UNAVAILABLE")
            except (ValueError, KeyError, TypeError):
                issues.append("MESSAGE_BODY_UNAVAILABLE")
            finally:
                progress.update(body_attempted=counts["body_attempted"] + 1)
        progress.update(force=True)
        if reasons and not messages and "MESSAGE_BODY_UNAVAILABLE" in issues:
            raise PipelineError("NO_READABLE_MESSAGES")
        # Query each thread once. All matching replies are retained; only ancillary context is capped.
        progress.update(force=True, phase=5)
        queried_threads = set()
        for identifier, item in raw_hits.items():
            metadata = client.search_metadata.get(identifier, {})
            thread = item.get("thread_id") or metadata.get("thread_id")
            chat = item.get("chat_id") or metadata.get("chat_id")
            p2p = "p2p" in reasons[identifier] or metadata.get("is_p2p_chat") is True
            start = stamp(item) - timedelta(minutes=10)
            end = min(stamp(item) + timedelta(minutes=10), run.window_end)
            kind, container = ("thread", thread) if thread else ("chat", chat)
            if not container or (thread and thread in queried_threads):
                continue
            if thread:
                queried_threads.add(thread)
            context = []
            try:
                async for candidate in history(client, kind, container, start, end, history_page):
                    timestamp = stamp(candidate)
                    if timestamp >= run.window_end:
                        continue
                    matched = run.window_start <= timestamp and qualifies(
                        candidate, identity.open_id, p2p
                    )
                    parsed = parse_message(
                        candidate,
                        identity.open_id,
                        {"history"},
                        run.window_start if matched else datetime.min.replace(tzinfo=timezone.utc),
                        run.window_end,
                    )
                    if not parsed:
                        continue
                    parsed["context_only"] = not matched
                    if matched:
                        messages[parsed["message_id"]] = parsed
                    elif start <= timestamp < end:
                        context.append(parsed)
                context.sort(
                    key=lambda x: abs(
                        (
                            datetime.fromisoformat(x.get("timestamp", start.isoformat()))
                            - stamp(item)
                        ).total_seconds()
                    )
                )
                if len(context) > 20:
                    issues.append("CONTEXT_LIMIT_REACHED")
                for parsed in context[:20]:
                    messages.setdefault(parsed["message_id"], parsed)
            except PipelineError as error:
                if error.code == "AUTHORIZATION_REQUIRED":
                    raise
                issues.append("CONTEXT_UNAVAILABLE")
            except (ValueError, TypeError, KeyError):
                issues.append("CONTEXT_UNAVAILABLE")
    values = list(messages.values())
    counts.update(
        unique=len(reasons),
        parsed=sum(bool(x.get("text")) and not x.get("context_only", False) for x in values),
        context=sum(x.get("context_only", False) for x in values),
        unsupported=sum(bool(x.get("unsupported")) for x in values),
    )
    if counts["unsupported"]:
        issues.append("UNSUPPORTED_MESSAGE_TYPES")
    progress.update(force=True, phase=6)
    return values, counts, sorted(set(issues))
