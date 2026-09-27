"""SSE 解析器（规格 §3.1，验收点 2b）。

三条实测特征 + 一条防御用例。这四条用例存在的理由都很具体：照文档写的解析器
（读 ``event:`` 行）在这条流上会**一个事件都收不到**，而混进一条 OpenAI 兼容
分支的帧会让「先 json.loads 再取 event」的实现直接抛异常。
"""

from __future__ import annotations

import json
from pathlib import Path

from drivers.hermes.http_client import SseParser, parse_sse_text

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "hermes_run_sse.txt"


def test_fixture_is_the_probe_capture() -> None:
    """先证明 fixture 没被人「顺手整理」过。"""
    raw = FIXTURE.read_text(encoding="utf-8")
    assert "event:" not in raw, "实测流里没有 event: 行；fixture 出现它说明被改过"
    assert "\nid:" not in raw, "实测流里没有 id: 行"
    assert raw.count("data: ") == 15


def test_parses_data_only_frames_with_event_name_in_payload() -> None:
    events = parse_sse_text(FIXTURE.read_text(encoding="utf-8"))
    assert len(events) == 15
    # 特征 1：没有 event: 行 → declared_event 恒为 None，名字来自载荷。
    assert all(event.declared_event is None for event in events)
    # 特征 2：没有 id: 行 → 没有 Last-Event-ID 可用。
    assert all(event.declared_id is None for event in events)
    names = [event.event_name for event in events]
    assert names[0] == "tool.started"
    assert names[1] == "tool.completed"
    assert names[-1] == "reasoning.available"
    assert names.count("message.delta") == 12
    # 特征 3：每条载荷都带 run_id 与 timestamp。
    for event in events:
        payload = event.payload()
        assert payload["run_id"] == "run_36414bb8ee694016b045c8e2121c41f6"
        assert isinstance(payload["timestamp"], float)


def test_incremental_feed_matches_one_shot_parse() -> None:
    """按任意字节边界喂进去，结果必须与一次性解析完全一致。"""
    raw = FIXTURE.read_text(encoding="utf-8")
    parser = SseParser()
    events = []
    for index in range(0, len(raw), 7):
        events.extend(parser.feed(raw[index : index + 7]))
    events.extend(parser.close())
    assert [e.data for e in events] == [e.data for e in parse_sse_text(raw)]


def test_openai_shaped_frames_do_not_raise() -> None:
    """防御用例：混入 ``chat.completion.chunk``（有 choices、无 event）与 ``[DONE]``。"""
    stream = (
        "data: "
        + json.dumps({"id": "chatcmpl-1", "object": "chat.completion.chunk",
                      "choices": [{"delta": {"content": "x"}}]})
        + "\n\n"
        "data: [DONE]\n\n"
        ": ping\n\n"
        'data: {"event": "message.delta", "run_id": "run_1", "delta": "ok"}\n\n'
    )
    events = parse_sse_text(stream)
    assert len(events) == 3
    assert events[0].event_name is None, "OpenAI 帧没有 event 键 → 不是我们的事件"
    assert events[1].payload() is None, "非 JSON 的 data 行必须安静地返回 None"
    assert events[2].event_name == "message.delta"


def test_comment_lines_are_kept_for_keepalive_observation() -> None:
    """§8-⑤ 未定案：先把注释行留下来，将来判断有没有心跳时不用重跑探针。"""
    parser = SseParser()
    parser.feed(": ping\n\n")
    assert parser.comments == ["ping"]


def test_multiline_data_is_joined_with_newline() -> None:
    """Hermes 现在不发多行 data，但 SSE 规范要求这么拼——多认没有成本。"""
    events = parse_sse_text("data: a\ndata: b\n\n")
    assert events[0].data == "a\nb"


def test_crlf_split_across_chunks() -> None:
    parser = SseParser()
    assert parser.feed("data: x\r") == []
    events = parser.feed("\n\r\n")
    assert len(events) == 1 and events[0].data == "x"
