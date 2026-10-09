"""Static plain-text cards: the model cannot inject links, buttons or card markup."""

import json

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
