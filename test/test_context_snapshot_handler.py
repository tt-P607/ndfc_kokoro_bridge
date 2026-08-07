"""NDFC Kokoro Bridge 上下文快照事件处理器测试。"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from plugins.ndfc_kokoro_bridge.config import NDFCKokoroBridgeConfig
from plugins.ndfc_kokoro_bridge.handlers.context_snapshot_handler import (
    NDFCKokoroContextSnapshotHandler,
)
from plugins.ndfc_kokoro_bridge.models import ContextSnapshot, SnapshotEntry
from plugins.ndfc_kokoro_bridge.plugin import NDFCKokoroBridgePlugin
from src.app.plugin_system.api.event_api import EventDecision
from src.app.plugin_system.types import EventType, ROLE

def _unique_stream_id() -> str:
    """生成唯一的测试流 ID，避免 SnapshotStore 内存缓存跨测试串扰。"""
    return f"stream-snap-{uuid4().hex}"


def _text_payload(role: str, text: str) -> SimpleNamespace:
    """构造最小 LLM payload 模拟对象。"""
    return SimpleNamespace(role=role, content=[SimpleNamespace(text=text)])


def _ndfc_params(stream_id: str, payloads: list | None = None) -> dict:
    """构造 NDFC 的 before_llm_request 事件参数。"""
    return {
        "request_name": "neo_default_chatter",
        "meta_data": {"stream_id": stream_id},
        "payloads": payloads if payloads is not None else [_text_payload("system", "你是助手")],
    }


async def _prepare_snapshot(plugin: NDFCKokoroBridgePlugin, stream_id: str) -> None:
    """预置一条历史快照。"""
    store = getattr(plugin, "snapshot_store")
    snapshot = ContextSnapshot(
        stream_id=stream_id,
        entries=[
            SnapshotEntry(role="system", content="你是助手"),
            SnapshotEntry(role="user", content="上次说到哪了"),
            SnapshotEntry(role="assistant", content="我们在聊计划"),
        ],
    )
    async with store.lock(stream_id):
        await store.save(snapshot)


@pytest.mark.asyncio
async def test_before_request_restores_history_after_system() -> None:
    """重启后首次 before_llm_request 应在 system 之后注入恢复的历史链。"""
    plugin = NDFCKokoroBridgePlugin(config=NDFCKokoroBridgeConfig())
    handler = NDFCKokoroContextSnapshotHandler(plugin=plugin)
    NDFCKokoroContextSnapshotHandler._restored_streams.clear()
    stream_id = _unique_stream_id()
    await _prepare_snapshot(plugin, stream_id)

    params = _ndfc_params(stream_id)
    decision, next_params = await handler.execute(
        EventType.BEFORE_LLM_REQUEST.value, params
    )

    assert decision is EventDecision.SUCCESS
    restored = next_params["payloads"]
    # system(本轮) + 恢复的 user/assistant = 3 条，system 保持最前
    assert len(restored) == 3
    roles = [p.role for p in restored]
    assert roles == [ROLE.SYSTEM, ROLE.USER, ROLE.ASSISTANT]
    assert restored[0].content[0].text == "你是助手"
    assert restored[1].content[0].text == "上次说到哪了"
    assert restored[2].content[0].text == "我们在聊计划"


@pytest.mark.asyncio
async def test_before_request_does_not_restore_twice_in_same_process() -> None:
    """同进程内第二次请求不应重复恢复快照。"""
    plugin = NDFCKokoroBridgePlugin(config=NDFCKokoroBridgeConfig())
    handler = NDFCKokoroContextSnapshotHandler(plugin=plugin)
    NDFCKokoroContextSnapshotHandler._restored_streams.clear()
    stream_id = _unique_stream_id()
    await _prepare_snapshot(plugin, stream_id)

    params = _ndfc_params(stream_id)
    _, first = await handler.execute(EventType.BEFORE_LLM_REQUEST.value, params)
    assert len(first["payloads"]) == 3

    # 第二次请求：不再重复注入历史快照
    params2 = _ndfc_params(stream_id, [_text_payload("system", "你是助手")])
    _, second = await handler.execute(EventType.BEFORE_LLM_REQUEST.value, params2)
    assert len(second["payloads"]) == 1


@pytest.mark.asyncio
async def test_before_request_injects_after_system_and_tool() -> None:
    """恢复链应插到 NDFC 的 system 与 TOOL 声明之后、user 之前。"""
    plugin = NDFCKokoroBridgePlugin(config=NDFCKokoroBridgeConfig())
    handler = NDFCKokoroContextSnapshotHandler(plugin=plugin)
    NDFCKokoroContextSnapshotHandler._restored_streams.clear()
    stream_id = _unique_stream_id()
    await _prepare_snapshot(plugin, stream_id)

    # 模拟 NDFC 本轮 payloads: system + TOOL 声明 + user
    payloads = [
        _text_payload("system", "你是助手"),
        SimpleNamespace(role="tool", content=[]),
        _text_payload("user", "新的问题"),
    ]
    params = _ndfc_params(stream_id, payloads)
    _, next_params = await handler.execute(EventType.BEFORE_LLM_REQUEST.value, params)

    restored = next_params["payloads"]
    roles = [
        str(getattr(getattr(p, "role", ""), "value", getattr(p, "role", "")))
        for p in restored
    ]
    # system, tool, 恢复的 user/assistant, 本轮 user
    assert roles == ["system", "tool", "user", "assistant", "user"]
    assert restored[2].content[0].text == "上次说到哪了"
    assert restored[3].content[0].text == "我们在聊计划"
    assert restored[4].content[0].text == "新的问题"


@pytest.mark.asyncio
async def test_before_request_filters_non_ndfc() -> None:
    """非 NDFC 请求应被过滤，不捕获快照。"""
    plugin = NDFCKokoroBridgePlugin(config=NDFCKokoroBridgeConfig())
    handler = NDFCKokoroContextSnapshotHandler(plugin=plugin)
    stream_id = _unique_stream_id()

    params = {
        "request_name": "kokoro_flow_chatter",
        "meta_data": {"stream_id": stream_id},
        "payloads": [_text_payload("system", "KFC 上下文")],
    }
    decision, next_params = await handler.execute(
        EventType.BEFORE_LLM_REQUEST.value, params
    )

    assert decision is EventDecision.SUCCESS
    # 未被修改
    assert next_params is params
    snapshot = await getattr(plugin, "snapshot_store").get(stream_id)
    assert snapshot is None


@pytest.mark.asyncio
async def test_after_request_ignored() -> None:
    """after_llm_request 事件应被忽略（不再合并响应，避免重复追加）。"""
    plugin = NDFCKokoroBridgePlugin(config=NDFCKokoroBridgeConfig())
    handler = NDFCKokoroContextSnapshotHandler(plugin=plugin)
    stream_id = _unique_stream_id()
    await _prepare_snapshot(plugin, stream_id)

    params = {
        "request_name": "neo_default_chatter",
        "meta_data": {"stream_id": stream_id},
        "message": "我继续讲",
        "reasoning_content": "我决定继续说",
        "tool_calls": [],
    }
    decision, next_params = await handler.execute(EventType.AFTER_LLM_REQUEST.value, params)

    assert decision is EventDecision.SUCCESS
    # 事件未订阅，参数未被修改
    assert next_params is params
    snapshot = await getattr(plugin, "snapshot_store").get(stream_id)
    assert snapshot is not None
    contents = [e.content for e in snapshot.entries]
    assert "我继续讲" not in contents
