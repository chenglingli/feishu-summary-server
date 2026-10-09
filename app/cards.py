"""Static plain-text cards: the model cannot inject links, buttons or card markup."""

import json
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.pipeline import PipelineError


def render_card(text):
    return json.dumps(
        {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "个人会话总结"},
                "template": "blue",
            },
            "elements": [
                {"tag": "div", "text": {"tag": "plain_text", "content": text}},
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "助理设置"},
                            "url": get_settings().public_base_url + "/",
                            "type": "default",
                        }
                    ],
                },
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def report_parts(text):
    # A 5,000-character cap bounds even double JSON escaping, emoji and source identifiers.
    fragments = [text[start : start + 5000] for start in range(0, len(text), 5000)] or ["暂无内容"]
    cards = [
        render_card(f"({index + 1}/{len(fragments)})\n{fragment}")
        for index, fragment in enumerate(fragments)
    ]
    for card in cards:
        wire = json.dumps(
            {"receive_id": "x" * 128, "msg_type": "interactive", "content": card, "uuid": "x" * 50},
            ensure_ascii=False,
        )
        if len(wire.encode()) > 24000:
            raise PipelineError("CARD_TOO_LARGE")
    return cards


def report_content(start, end, counts, issues, report):
    # Use collector metadata, never model-generated counts. Context is counted separately.
    local = ZoneInfo("Asia/Shanghai")
    hours = (end - start).total_seconds() / 3600
    lines = [
        "个人会话总结",
        f"统计范围：{start.astimezone(local):%Y-%m-%d %H:%M} 至 {end.astimezone(local):%Y-%m-%d %H:%M}（北京时间）",
        f"基于过去 {hours:g} 小时内 {counts.get('parsed', 0)} 条可总结文本消息（去重后）；另获取 {counts.get('context', 0)} 条上下文消息。",
    ]
    if counts.get("unsupported", 0):
        lines.append(f"另有 {counts['unsupported']} 条非文本等消息未解析。")
    if issues:
        lines.append("覆盖提示：部分内容未纳入总结，统计仅代表实际获取并解析的消息。")
    return "\n".join(lines) + "\n\n" + report
