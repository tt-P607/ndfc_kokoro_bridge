"""NDFC Kokoro Bridge 消息观察器测试。"""

from __future__ import annotations

import time
from uuid import uuid4

import pytest

from plugins.ndfc_kokoro_bridge.config import NDFCKokoroBridgeConfig
from plugins.ndfc_kokoro_bridge.handlers.message_observer import NDFCKokoroMessageObserver
from plugins.ndfc_kokoro_bridge.plugin import NDFCKokoroBridgePlugin
from plugins.ndfc_kokoro_bridge.store import BridgeSessionStore
from src.app.plugin_system.api.event_api import EventDecision
from src.app.plugin_system.types import ChatType, EventType, Message


def _unique_stream_id(prefix: str) -> str:
    """生成唯一的测试流 ID，避免磁盘持久化存储跨测试/跨运行污染。"""
    return f"stream-{prefix}-{uuid4().hex}"


def _make_message(
    stream_id: str,
    chat_type: str = "private",
    message_id: str = "msg-observer-test",
    content: str = "我回来了",
) -> Message:
    """构造最小消息对象。"""
    return Message(
        message_id=message_id,
        time=time.time(),
        content=content,
        processed_plain_text=content,
        sender_id="user-1",
        sender_name="User",
        platform="qq",
        chat_type=chat_type,
        stream_id=stream_id,
    )


@pytest.mark.asyncio
async def test_message_received_clears_stale_trigger_reason() -> None:
    """已登记为 NDFC 的流，用户新消息到达后应清理上一轮系统触发原因。"""

    plugin = NDFCKokoroBridgePlugin(config=NDFCKokoroBridgeConfig())
    store = getattr(plugin, "session_store")
    assert isinstance(store, BridgeSessionStore)
    stream_id = _unique_stream_id("observer-clear-trigger")
    plugin.mark_ndfc_stream(stream_id)  # type: ignore[attr-defined]
    async with store.lock(stream_id):
        session = await store.get_or_create(stream_id)
        session.mark_trigger("wait_timeout", "回复等待超时。")
        session.set_waiting(30, reason="等回复", expected_reaction="继续聊")
        await store.save(session)

    handler = NDFCKokoroMessageObserver(plugin=plugin)
    message = _make_message(stream_id, message_id="msg-observer-clear-trigger")

    decision, _ = await handler.execute(EventType.ON_MESSAGE_RECEIVED.value, {"message": message})

    assert decision is EventDecision.SUCCESS
    session = await store.get(stream_id)
    assert session is not None
    assert session.trigger_type == ""
    assert session.trigger_reason == ""
    assert session.trigger_at is None
    assert session.waiting_until is None


@pytest.mark.asyncio
async def test_message_received_clears_wait_with_enum_event_and_chat_type() -> None:
    """真实事件传入枚举值时，已登记 NDFC 的流应清理旧回复等待。"""

    plugin = NDFCKokoroBridgePlugin(config=NDFCKokoroBridgeConfig())
    store = getattr(plugin, "session_store")
    assert isinstance(store, BridgeSessionStore)
    stream_id = _unique_stream_id("observer-enum-clear-wait")
    plugin.mark_ndfc_stream(stream_id)  # type: ignore[attr-defined]
    async with store.lock(stream_id):
        session = await store.get_or_create(stream_id)
        session.set_waiting(120, reason="刚才在等你", expected_reaction="继续回复")
        await store.save(session)

    handler = NDFCKokoroMessageObserver(plugin=plugin)
    message = _make_message(stream_id)
    message.chat_type = ChatType.PRIVATE  # type: ignore[assignment]

    decision, _ = await handler.execute(EventType.ON_MESSAGE_RECEIVED, {"message": message})

    assert decision is EventDecision.SUCCESS
    session = await store.get(stream_id)
    assert session is not None
    assert session.waiting_until is None
    assert session.waiting_reason == ""
    assert session.waiting_expected_reaction == ""


@pytest.mark.asyncio
async def test_message_observer_skips_non_ndfc_stream() -> None:
    """未登记为 NDFC 的流（如 KFC 私聊）不应创建任何会话数据。"""

    plugin = NDFCKokoroBridgePlugin(config=NDFCKokoroBridgeConfig())
    store = getattr(plugin, "session_store")
    assert isinstance(store, BridgeSessionStore)
    stream_id = _unique_stream_id("non-ndfc")

    handler = NDFCKokoroMessageObserver(plugin=plugin)
    message = _make_message(stream_id)

    decision, _ = await handler.execute(EventType.ON_MESSAGE_RECEIVED.value, {"message": message})

    assert decision is EventDecision.SUCCESS
    session = await store.get(stream_id)
    assert session is None


@pytest.mark.asyncio
async def test_group_message_does_not_override_user_id() -> None:
    """群聊消息不应把 sender_id 写入 user_id（群聊没有唯一 user_id）。"""

    plugin = NDFCKokoroBridgePlugin(config=NDFCKokoroBridgeConfig())
    store = getattr(plugin, "session_store")
    assert isinstance(store, BridgeSessionStore)
    stream_id = _unique_stream_id("group-userid")
    plugin.mark_ndfc_stream(stream_id)  # type: ignore[attr-defined]

    # 首次群聊消息：user_id 应为空，而非 sender_id
    handler = NDFCKokoroMessageObserver(plugin=plugin)
    message = _make_message(
        stream_id,
        chat_type="group",
        message_id="msg-group-1",
    )

    decision, _ = await handler.execute(EventType.ON_MESSAGE_RECEIVED.value, {"message": message})

    assert decision is EventDecision.SUCCESS
    session = await store.get(stream_id)
    assert session is not None
    assert session.chat_type == "group"
    assert session.user_id == ""


@pytest.mark.asyncio
async def test_private_message_sets_user_id() -> None:
    """私聊消息应把 sender_id 写入 user_id。"""

    plugin = NDFCKokoroBridgePlugin(config=NDFCKokoroBridgeConfig())
    store = getattr(plugin, "session_store")
    assert isinstance(store, BridgeSessionStore)
    stream_id = _unique_stream_id("private-userid")
    plugin.mark_ndfc_stream(stream_id)  # type: ignore[attr-defined]

    handler = NDFCKokoroMessageObserver(plugin=plugin)
    message = _make_message(
        stream_id,
        chat_type="private",
        message_id="msg-private-1",
    )

    decision, _ = await handler.execute(EventType.ON_MESSAGE_RECEIVED.value, {"message": message})

    assert decision is EventDecision.SUCCESS
    session = await store.get(stream_id)
    assert session is not None
    assert session.chat_type == "private"
    assert session.user_id == "user-1"
