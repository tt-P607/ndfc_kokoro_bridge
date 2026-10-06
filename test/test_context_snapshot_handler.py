"""NDFC Kokoro Bridge 上下文快照事件处理器测试。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest

from plugins.ndfc_kokoro_bridge.config import NDFCKokoroBridgeConfig
from plugins.ndfc_kokoro_bridge.handlers.context_snapshot_handler import (
    NDFCKokoroContextSnapshotHandler,
)
from plugins.ndfc_kokoro_bridge.models import (
    BridgeSession,
    ContextSnapshot,
    Memo,
    SnapshotEntry,
)
from plugins.ndfc_kokoro_bridge.plugin import NDFCKokoroBridgePlugin
from plugins.ndfc_kokoro_bridge.snapshot_store import SnapshotStore
from plugins.ndfc_kokoro_bridge.store import BridgeSessionStore
from src.app.plugin_system.api import database_api, storage_api
from src.app.plugin_system.api.event_api import EventDecision
from src.app.plugin_system.types import ROLE, EventType, Text
from src.core.models.sql_alchemy import ChatStreams


@pytest.fixture(autouse=True)
def clear_times(monkeypatch: pytest.MonkeyPatch) -> dict[str, float | None]:
    """提供隔离数据库清空标记，避免访问运行中的数据库。"""
    timestamps: dict[str, float | None] = {}

    async def get_by(model: type[Any], **filters: Any) -> ChatStreams:
        """读取指定流的清空时间。"""
        assert model is ChatStreams
        stream_id = filters["stream_id"]
        return ChatStreams(
            stream_id=stream_id,
            context_cleared_at=timestamps.get(stream_id),
        )

    monkeypatch.setattr(database_api, "get_by", get_by)
    monkeypatch.setattr(NDFCKokoroContextSnapshotHandler, "_restored_streams", set())
    return timestamps


def _unique_stream_id() -> str:
    """生成唯一的测试流 ID，避免 SnapshotStore 内存缓存跨测试串扰。"""
    return f"stream-snap-{uuid4().hex}"


def _text_payload(role: str, text: str) -> SimpleNamespace:
    """构造最小 LLM payload 模拟对象。"""
    return SimpleNamespace(role=role, content=[Text(text)])


def _ndfc_params(stream_id: str, payloads: list | None = None) -> dict:
    """构造 NDFC 的 before_llm_request 事件参数。"""
    return {
        "request_name": "neo_default_chatter",
        "meta_data": {"stream_id": stream_id},
        "payloads": payloads if payloads is not None else [_text_payload("system", "你是助手")],
    }


async def _prepare_snapshot(
    plugin: NDFCKokoroBridgePlugin,
    stream_id: str,
    updated_at: float | None = None,
) -> None:
    """预置一条历史快照。"""
    store = cast(SnapshotStore, plugin.snapshot_store)
    snapshot = ContextSnapshot(
        stream_id=stream_id,
        entries=[
            SnapshotEntry(role="system", content="你是助手"),
            SnapshotEntry(role="user", content="上次说到哪了"),
            SnapshotEntry(role="assistant", content="我们在聊计划"),
        ],
    )
    if updated_at is not None:
        snapshot.updated_at = updated_at
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
@pytest.mark.parametrize("snapshot_time", [99.0, 100.0])
async def test_restart_does_not_restore_cleared_snapshot(
    clear_times: dict[str, float | None],
    snapshot_time: float,
) -> None:
    """清空时间之前或同一时刻的快照不得在重启后恢复。"""
    plugin = NDFCKokoroBridgePlugin(config=NDFCKokoroBridgeConfig())
    stream_id = _unique_stream_id()
    await _prepare_snapshot(plugin, stream_id, updated_at=snapshot_time)
    clear_times[stream_id] = 100.0
    plugin = NDFCKokoroBridgePlugin(config=NDFCKokoroBridgeConfig())
    handler = NDFCKokoroContextSnapshotHandler(plugin=plugin)
    payloads = [
        _text_payload("system", "你是助手"),
        _text_payload("user", "新的问题"),
    ]

    decision, next_params = await handler.execute(
        EventType.BEFORE_LLM_REQUEST.value,
        _ndfc_params(stream_id, payloads),
    )

    assert decision is EventDecision.SUCCESS
    assert next_params["payloads"] == payloads
    persisted = await SnapshotStore().get(stream_id)
    assert persisted is not None
    assert [entry.content for entry in persisted.entries] == ["你是助手", "新的问题"]


@pytest.mark.asyncio
async def test_existing_request_does_not_resave_after_clear(
    clear_times: dict[str, float | None],
) -> None:
    """清空前已使用的请求链不得在清空后生成新的有效快照。"""
    plugin = NDFCKokoroBridgePlugin(config=NDFCKokoroBridgeConfig())
    handler = NDFCKokoroContextSnapshotHandler(plugin=plugin)
    stream_id = _unique_stream_id()
    params = _ndfc_params(
        stream_id,
        [_text_payload("system", "你是助手"), _text_payload("user", "清空前的消息")],
    )
    await handler.execute(EventType.BEFORE_LLM_REQUEST.value, params)
    clear_times[stream_id] = 100.0

    await handler.execute(EventType.BEFORE_LLM_REQUEST.value, params)
    await handler.execute(EventType.BEFORE_LLM_REQUEST.value, params)

    store = cast(SnapshotStore, plugin.snapshot_store)
    assert await store.get(stream_id) is None
    assert await SnapshotStore().get(stream_id) is None


@pytest.mark.asyncio
async def test_restart_restores_snapshot_from_current_clear_boundary(
    clear_times: dict[str, float | None],
) -> None:
    """清空后保存的新快照应保留清空标记并在重启后正常恢复。"""
    stream_id = _unique_stream_id()
    clear_times[stream_id] = 100.0
    plugin = NDFCKokoroBridgePlugin(config=NDFCKokoroBridgeConfig())
    handler = NDFCKokoroContextSnapshotHandler(plugin=plugin)
    await handler.execute(
        EventType.BEFORE_LLM_REQUEST.value,
        _ndfc_params(stream_id, [_text_payload("user", "清空后的消息")]),
    )
    persisted = await SnapshotStore().get(stream_id)
    assert persisted is not None
    assert persisted.context_cleared_at == 100.0
    handler._restored_streams.clear()
    restarted = NDFCKokoroBridgePlugin(config=NDFCKokoroBridgeConfig())
    restarted_handler = NDFCKokoroContextSnapshotHandler(plugin=restarted)

    _, next_params = await restarted_handler.execute(
        EventType.BEFORE_LLM_REQUEST.value,
        _ndfc_params(stream_id),
    )

    assert next_params["payloads"][1].content[0].text == "清空后的消息"


@pytest.mark.asyncio
async def test_old_request_does_not_delete_new_snapshot(
    clear_times: dict[str, float | None],
) -> None:
    """旧请求继续执行时不能删除同一流清空后生成的新快照。"""
    stream_id = _unique_stream_id()
    plugin = NDFCKokoroBridgePlugin(config=NDFCKokoroBridgeConfig())
    handler = NDFCKokoroContextSnapshotHandler(plugin=plugin)
    old_params = _ndfc_params(stream_id, [_text_payload("user", "清空前的消息")])
    await handler.execute(EventType.BEFORE_LLM_REQUEST.value, old_params)
    clear_times[stream_id] = 100.0
    await handler.execute(
        EventType.BEFORE_LLM_REQUEST.value,
        _ndfc_params(stream_id, [_text_payload("user", "清空后的消息")]),
    )

    continued_params = dict(old_params)
    continued_params["meta_data"] = dict(old_params["meta_data"])
    await handler.execute(EventType.BEFORE_LLM_REQUEST.value, continued_params)

    persisted = await SnapshotStore().get(stream_id)
    assert persisted is not None
    assert persisted.context_cleared_at == 100.0
    assert [entry.content for entry in persisted.entries] == ["清空后的消息"]


@pytest.mark.asyncio
async def test_clear_removes_only_target_snapshot_and_index(
    clear_times: dict[str, float | None],
) -> None:
    """清空只删除目标流快照及索引，保留其他快照、备忘录和预约。"""
    plugin = NDFCKokoroBridgePlugin(config=NDFCKokoroBridgeConfig())
    handler = NDFCKokoroContextSnapshotHandler(plugin=plugin)
    stream_id = _unique_stream_id()
    other_stream_id = _unique_stream_id()
    await _prepare_snapshot(plugin, stream_id, updated_at=99.0)
    await _prepare_snapshot(plugin, other_stream_id, updated_at=99.0)
    session = BridgeSession(
        stream_id=stream_id,
        scheduled_at=500.0,
        scheduled_reason="预约原因",
        memos=[Memo(content="保留的备忘录")],
    )
    await BridgeSessionStore().save(session)
    clear_times[stream_id] = 100.0

    await handler.execute(
        EventType.BEFORE_LLM_REQUEST.value,
        _ndfc_params(stream_id, []),
    )

    store = cast(SnapshotStore, plugin.snapshot_store)
    assert await store.get(stream_id) is None
    assert stream_id not in store.get_all_cached()
    assert await SnapshotStore().list_all_stream_ids() == [other_stream_id]
    index = await storage_api.load_json("ndfc_kokoro_bridge_snapshots", "_index")
    assert index is not None
    assert stream_id not in index
    assert other_stream_id in index
    persisted_session = await BridgeSessionStore().get(stream_id)
    assert persisted_session is not None
    assert persisted_session.to_dict() == session.to_dict()


@pytest.mark.asyncio
async def test_clear_during_save_invalidates_snapshot_on_restart(
    clear_times: dict[str, float | None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """保存期间发生清空时，新写入时间不能使旧边界快照重新有效。"""
    stream_id = _unique_stream_id()
    plugin = NDFCKokoroBridgePlugin(config=NDFCKokoroBridgeConfig())
    handler = NDFCKokoroContextSnapshotHandler(plugin=plugin)
    store = cast(SnapshotStore, plugin.snapshot_store)
    save = store.save

    async def save_after_clear(snapshot: ContextSnapshot) -> None:
        """在快照写入之前提交清空标记。"""
        clear_times[stream_id] = 100.0
        snapshot.updated_at = 101.0
        await save(snapshot)

    monkeypatch.setattr(store, "save", save_after_clear)
    await handler.execute(
        EventType.BEFORE_LLM_REQUEST.value,
        _ndfc_params(stream_id, [_text_payload("user", "清空前的消息")]),
    )
    handler._restored_streams.clear()
    restarted = NDFCKokoroBridgePlugin(config=NDFCKokoroBridgeConfig())
    restarted_handler = NDFCKokoroContextSnapshotHandler(plugin=restarted)

    _, next_params = await restarted_handler.execute(
        EventType.BEFORE_LLM_REQUEST.value,
        _ndfc_params(stream_id, []),
    )

    assert next_params["payloads"] == []
    assert await SnapshotStore().get(stream_id) is None


@pytest.mark.asyncio
async def test_repeated_clears_invalidate_previous_boundary(
    clear_times: dict[str, float | None],
) -> None:
    """连续清空时，上一清空边界的请求和快照始终失效。"""
    stream_id = _unique_stream_id()
    plugin = NDFCKokoroBridgePlugin(config=NDFCKokoroBridgeConfig())
    handler = NDFCKokoroContextSnapshotHandler(plugin=plugin)
    clear_times[stream_id] = 100.0
    params = _ndfc_params(stream_id, [_text_payload("user", "第一轮新消息")])
    await handler.execute(EventType.BEFORE_LLM_REQUEST.value, params)
    clear_times[stream_id] = 200.0

    await handler.execute(EventType.BEFORE_LLM_REQUEST.value, params)
    assert await SnapshotStore().get(stream_id) is None
    await handler.execute(
        EventType.BEFORE_LLM_REQUEST.value,
        _ndfc_params(stream_id, [_text_payload("user", "第二轮新消息")]),
    )

    persisted = await SnapshotStore().get(stream_id)
    assert persisted is not None
    assert persisted.context_cleared_at == 200.0
    assert [entry.content for entry in persisted.entries] == ["第二轮新消息"]


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
