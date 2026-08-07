"""NDFC Kokoro Bridge 上下文快照序列化与恢复测试。"""

from __future__ import annotations

from unittest.mock import patch

from plugins.ndfc_kokoro_bridge.models import ContextSnapshot, SnapshotEntry
from plugins.ndfc_kokoro_bridge.snapshot import (
    capture_snapshot_from_payloads,
    deserialize_snapshot,
    extract_stream_id,
    log_snapshot_restored,
    serialize_payloads,
)
from src.app.plugin_system.types import (
    Image,
    LLMPayload,
    ROLE,
    Text,
    ToolCall,
    ToolResult,
)


def test_extract_stream_id_from_meta_data() -> None:
    """应从 meta_data 中提取 stream_id。"""
    assert extract_stream_id({"stream_id": "stream-abc"}) == "stream-abc"
    assert extract_stream_id({"streamId": "stream-xyz"}) == "stream-xyz"
    assert extract_stream_id({}) == ""
    assert extract_stream_id(None) == ""


def test_serialize_payloads_round_trip() -> None:
    """payload 链应序列化为快照条目，Text/ToolCall/ToolResult 被正确归类。"""
    payloads = [
        LLMPayload(ROLE.SYSTEM, Text("你是助手")),
        LLMPayload(ROLE.USER, Text("你好")),
        LLMPayload(ROLE.ASSISTANT, ToolCall("call-1", "send_text", {"content": "hi"})),
        LLMPayload(ROLE.TOOL_RESULT, ToolResult("已发送", call_id="call-1")),
    ]

    entries = serialize_payloads(payloads)

    roles = [e.role for e in entries]
    assert "system" in roles
    assert "user" in roles
    assert "assistant" in roles
    assert "tool_result" in roles

    assistant = next(e for e in entries if e.role == "assistant")
    assert assistant.tool_calls[0]["name"] == "send_text"
    assert assistant.tool_calls[0]["args"]["content"] == "hi"

    tool_result = next(e for e in entries if e.role == "tool_result")
    assert tool_result.call_id == "call-1"
    assert tool_result.content == "已发送"


def test_capture_snapshot_and_round_trip() -> None:
    """快照应能序列化、反序列化并保留结构。"""
    payloads = [
        LLMPayload(ROLE.SYSTEM, Text("你是助手")),
        LLMPayload(ROLE.USER, Text("你好")),
    ]

    snapshot = capture_snapshot_from_payloads("stream-1", payloads)
    assert snapshot is not None
    assert snapshot.stream_id == "stream-1"
    assert len(snapshot.entries) == 2

    data = snapshot.to_dict()
    loaded = ContextSnapshot.from_dict(data)

    assert loaded.stream_id == "stream-1"
    assert loaded.version == snapshot.version
    assert [e.role for e in loaded.entries] == ["system", "user"]
    assert loaded.entries[0].content == "你是助手"


def test_capture_snapshot_none_on_empty() -> None:
    """无 stream_id 或空 payload 时不应生成快照。"""
    assert capture_snapshot_from_payloads("", []) is None
    assert capture_snapshot_from_payloads("stream-1", []) is None


def test_serialize_payloads_keeps_media_data() -> None:
    """快照捕获时刻媒体仍在 payload 中时，应完整保存 base64 数据与类型。"""
    image = Image("aGVsbG8=")  # 合法 base64
    payloads = [LLMPayload(ROLE.USER, image)]

    entries = serialize_payloads(payloads)

    user_entry = next(e for e in entries if e.role == "user")
    assert user_entry.media == [{"type": "Image", "data": "aGVsbG8="}]
    assert "aGVsbG8=" in user_entry.media[0]["data"]
    assert "[媒体]" not in user_entry.content


def test_deserialize_snapshot_restores_media() -> None:
    """带媒体的快照应还原为包含 Image 的 payload，且保留 base64 数据。"""
    snapshot = ContextSnapshot(
        stream_id="stream-1",
        entries=[
            SnapshotEntry(
                role="user",
                content="看图",
                media=[{"type": "Image", "data": "aGVsbG8="}],
            ),
        ],
    )

    payloads = deserialize_snapshot(snapshot)

    assert len(payloads) == 1
    parts = payloads[0].content
    types = {type(p).__name__ for p in parts}
    assert "Text" in types
    assert "Image" in types
    image = next(p for p in parts if type(p).__name__ == "Image")
    assert image.value == "aGVsbG8="


def test_serialize_payloads_skips_tool_declaration() -> None:
    """ROLE.TOOL（工具 schema 声明）不应进入快照。"""
    tool_payload = LLMPayload(ROLE.TOOL, [])
    payloads = [
        LLMPayload(ROLE.SYSTEM, Text("你是助手")),
        tool_payload,
        LLMPayload(ROLE.USER, Text("你好")),
    ]

    entries = serialize_payloads(payloads)

    roles = [e.role for e in entries]
    assert "tool" not in roles
    assert roles == ["system", "user"]


def test_snapshot_restored_log_uses_panel() -> None:
    """恢复日志应调用 print_panel 输出醒目面板。"""
    snapshot = ContextSnapshot(
        stream_id="stream-1",
        entries=[SnapshotEntry(role="user", content="你好")],
    )

    with patch("plugins.ndfc_kokoro_bridge.snapshot.logger.print_panel") as mock_panel:
        log_snapshot_restored(snapshot, payload_count=2)
        mock_panel.assert_called_once()
        args, kwargs = mock_panel.call_args
        assert "上下文快照已恢复" in args[0]
        assert kwargs["border_style"] == "bright_cyan"


def test_deserialize_snapshot_skips_system_and_restores_history() -> None:
    """反序列化应跳过 system，恢复 user/assistant/tool_result 历史链。"""
    snapshot = ContextSnapshot(
        stream_id="stream-1",
        entries=[
            SnapshotEntry(role="system", content="你是助手"),
            SnapshotEntry(role="user", content="你好"),
            SnapshotEntry(
                role="assistant",
                tool_calls=[{"id": "call-1", "name": "send_text", "args": {"content": "hi"}}],
            ),
            SnapshotEntry(role="tool_result", content="已发送", call_id="call-1"),
        ],
    )

    payloads = deserialize_snapshot(snapshot)

    roles = [p.role for p in payloads]
    assert roles == [ROLE.USER, ROLE.ASSISTANT, ROLE.TOOL_RESULT]

    assert payloads[0].content[0].text == "你好"
    assert payloads[1].content[0].name == "send_text"
    assert payloads[2].content[0].call_id == "call-1"
    assert payloads[2].content[0].value == "已发送"


def test_deserialize_snapshot_drops_unpaired_tool_calls_tail() -> None:
    """反序列化应丢弃尾部悬挂的未配对 tool_calls。"""
    snapshot = ContextSnapshot(
        stream_id="stream-1",
        entries=[
            SnapshotEntry(role="user", content="你好"),
            SnapshotEntry(
                role="assistant",
                tool_calls=[{"id": "call-1", "name": "send_text", "args": {}}],
            ),
            # 悬挂：call-1 后无 tool_result
        ],
    )

    payloads = deserialize_snapshot(snapshot)

    roles = [p.role for p in payloads]
    assert roles == [ROLE.USER]
    assert payloads[0].content[0].text == "你好"


def test_deserialize_snapshot_drops_unpaired_tool_calls_in_middle() -> None:
    """反序列化应丢弃中间位置的未配对 tool_calls（其后是 user 而非 tool_result）。"""
    snapshot = ContextSnapshot(
        stream_id="stream-1",
        entries=[
            SnapshotEntry(role="user", content="第一轮"),
            SnapshotEntry(
                role="assistant",
                tool_calls=[{"id": "call-ok", "name": "send_text", "args": {}}],
            ),
            SnapshotEntry(role="tool_result", content="已执行", call_id="call-ok"),
            SnapshotEntry(role="assistant", content="好的"),
            # 中断：此 assistant 发出未配对的 tool_call，后面直接是 user
            SnapshotEntry(
                role="assistant",
                tool_calls=[{"id": "call-orphan", "name": "mute_group_member", "args": {}}],
            ),
            SnapshotEntry(role="user", content="第二轮"),
        ],
    )

    payloads = deserialize_snapshot(snapshot)

    roles = [p.role for p in payloads]
    # call-orphan 段被丢弃，保留 call-ok 完成的一轮
    assert roles == [ROLE.USER, ROLE.ASSISTANT, ROLE.TOOL_RESULT, ROLE.ASSISTANT, ROLE.USER]
    assert payloads[0].content[0].text == "第一轮"
    assert payloads[4].content[0].text == "第二轮"
    # 不包含 orphan tool_call
    orphan_names = [
        c.name
        for p in payloads
        if p.role == ROLE.ASSISTANT
        for c in getattr(p, "content", [])
        if hasattr(c, "name")
    ]
    assert "mute_group_member" not in orphan_names


def test_deserialize_snapshot_drops_leading_orphan_assistant() -> None:
    """反序列化应丢弃起始处孤立的 assistant（无 user 起始）。"""
    snapshot = ContextSnapshot(
        stream_id="stream-1",
        entries=[
            SnapshotEntry(role="assistant", content="孤立的助手发言"),
            SnapshotEntry(role="user", content="你好"),
        ],
    )

    payloads = deserialize_snapshot(snapshot)

    roles = [p.role for p in payloads]
    assert roles == [ROLE.USER]


def test_deserialize_snapshot_round_trip_after_capture() -> None:
    """捕获后反序列化应还原为合法的 user/assistant/tool_result 序列。"""
    payloads_src = [
        LLMPayload(ROLE.SYSTEM, Text("你是助手")),
        LLMPayload(ROLE.USER, Text("你好")),
        LLMPayload(ROLE.ASSISTANT, ToolCall("call-1", "send_text", {"content": "hi"})),
        LLMPayload(ROLE.TOOL_RESULT, ToolResult("已发送", call_id="call-1")),
    ]

    snapshot = capture_snapshot_from_payloads("stream-1", payloads_src)
    assert snapshot is not None

    restored = deserialize_snapshot(snapshot)

    roles = [p.role for p in restored]
    assert roles == [ROLE.USER, ROLE.ASSISTANT, ROLE.TOOL_RESULT]
    assert restored[0].content[0].text == "你好"
    assert restored[1].content[0].name == "send_text"
    assert restored[2].content[0].call_id == "call-1"
    assert restored[2].content[0].value == "已发送"


def test_serialize_payloads_keeps_each_tool_result_call_id() -> None:
    """同一 payload 内多个 ToolResult（NDFC 合并写入）应各自独立成条并保留 call_id。"""
    payloads = [
        LLMPayload(ROLE.USER, Text("禁言测试")),
        # NDFC 的 run_tool_call 会把同轮多个 ToolResult 追加进同一个 payload
        LLMPayload(
            ROLE.ASSISTANT,
            [
                ToolCall("action-a-1", "action-mute_group_member", {"user_id": 1}),
                ToolCall("action-b-2", "action-memo", {"content": "x"}),
                ToolCall("action-c-3", "action-send_text", {"content": "ok"}),
            ],
        ),
        LLMPayload(
            ROLE.TOOL_RESULT,
            [
                ToolResult("禁言成功", call_id="action-a-1"),
                ToolResult("备忘已写", call_id="action-b-2"),
                ToolResult("消息已发送", call_id="action-c-3"),
            ],
        ),
    ]

    entries = serialize_payloads(payloads)

    tool_results = [e for e in entries if e.role == "tool_result"]
    # 每个 ToolResult 独立成条，且都保留自己的 call_id
    assert [e.call_id for e in tool_results] == [
        "action-a-1",
        "action-b-2",
        "action-c-3",
    ]
    assert [e.content for e in tool_results] == ["禁言成功", "备忘已写", "消息已发送"]


def test_serialize_and_deserialize_preserves_all_tool_results() -> None:
    """真实场景：多个 ToolResult 合并的 payload 序列化后，恢复出的每个 call_id 都配对。"""
    payloads_src = [
        LLMPayload(ROLE.USER, Text("禁言测试")),
        LLMPayload(
            ROLE.ASSISTANT,
            [
                ToolCall("action-a-1", "action-mute_group_member", {"user_id": 1}),
                ToolCall("action-b-2", "action-memo", {"content": "x"}),
                ToolCall("action-c-3", "action-send_text", {"content": "ok"}),
            ],
        ),
        LLMPayload(
            ROLE.TOOL_RESULT,
            [
                ToolResult("禁言成功", call_id="action-a-1"),
                ToolResult("备忘已写", call_id="action-b-2"),
                ToolResult("消息已发送", call_id="action-c-3"),
            ],
        ),
    ]

    snapshot = capture_snapshot_from_payloads("stream-1", payloads_src)
    assert snapshot is not None

    restored = deserialize_snapshot(snapshot)

    roles = [p.role for p in restored]
    assert roles == [ROLE.USER, ROLE.ASSISTANT, ROLE.TOOL_RESULT, ROLE.TOOL_RESULT, ROLE.TOOL_RESULT]

    # 恢复出的每个 tool_result 都必须匹配 assistant 发出的 tool_call id
    assistant_calls = {
        c.id for p in restored if p.role == ROLE.ASSISTANT for c in p.content if hasattr(c, "id")
    }
    result_call_ids = {
        r.call_id
        for p in restored
        if p.role == ROLE.TOOL_RESULT
        for r in p.content
        if hasattr(r, "call_id")
    }
    assert result_call_ids == assistant_calls


def test_deserialize_snapshot_rolls_back_incomplete_segment() -> None:
    """未闭合段应回退到最近安全点，保留已闭合轮次与 SUSPEND，丢弃悬挂 tool_call。"""
    snapshot = ContextSnapshot(
        stream_id="stream-1",
        entries=[
            # 第一轮：完整闭合
            SnapshotEntry(role="user", content="第一轮"),
            SnapshotEntry(
                role="assistant",
                tool_calls=[{"id": "call-1", "name": "action-send_text", "args": {}}],
            ),
            SnapshotEntry(role="tool_result", content="已发送", call_id="call-1"),
            # SUSPEND 占位（普通 assistant，属于安全点）
            SnapshotEntry(role="assistant", content="__SUSPEND__"),
            # 未闭合的 tool_call 段，其后直接是 user
            SnapshotEntry(
                role="assistant",
                tool_calls=[{"id": "call-orphan", "name": "action-memo_delete", "args": {}}],
            ),
            SnapshotEntry(role="user", content="第二轮"),
        ],
    )

    payloads = deserialize_snapshot(snapshot)

    roles = [p.role for p in payloads]
    # 保留第一轮闭合链 + SUSPEND，丢弃 call-orphan 未闭合段，保留第二轮 user
    assert roles == [
        ROLE.USER,
        ROLE.ASSISTANT,
        ROLE.TOOL_RESULT,
        ROLE.ASSISTANT,
        ROLE.USER,
    ]
    assert payloads[0].content[0].text == "第一轮"
    assert payloads[-1].content[0].text == "第二轮"
    # 不含孤立的 tool_call
    orphan_names = [
        c.name
        for p in payloads
        if p.role == ROLE.ASSISTANT
        for c in getattr(p, "content", [])
        if hasattr(c, "name")
    ]
    assert "action-memo_delete" not in orphan_names
