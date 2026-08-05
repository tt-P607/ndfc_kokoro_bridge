"""NDFC Kokoro Bridge 消息观察器测试。"""

from __future__ import annotations

import time

import pytest

from plugins.ndfc_kokoro_bridge.config import NDFCKokoroBridgeConfig
from plugins.ndfc_kokoro_bridge.handlers.message_observer import NDFCKokoroMessageObserver
from plugins.ndfc_kokoro_bridge.plugin import NDFCKokoroBridgePlugin
from plugins.ndfc_kokoro_bridge.store import BridgeSessionStore
from src.app.plugin_system.api.event_api import EventDecision
from src.app.plugin_system.types import ChatType, EventType, Message


@pytest.mark.asyncio
async def test_message_received_clears_stale_trigger_reason() -> None:
    """用户新消息到达后应清理上一轮系统触发原因。"""

    plugin = NDFCKokoroBridgePlugin(config=NDFCKokoroBridgeConfig())
    store = getattr(plugin, "session_store")
    assert isinstance(store, BridgeSessionStore)
    async with store.lock("stream-observer-clear-trigger"):
        session = await store.get_or_create("stream-observer-clear-trigger")
        session.mark_trigger("wait_timeout", "回复等待超时。")
        session.set_waiting(30, reason="等回复", expected_reaction="继续聊")
        await store.save(session)

    handler = NDFCKokoroMessageObserver(plugin=plugin)
    message = Message(
        message_id="msg-observer-clear-trigger",
        time=time.time(),
        content="我回来了",
        processed_plain_text="我回来了",
        sender_id="user-1",
        sender_name="User",
        platform="qq",
        chat_type="private",
        stream_id="stream-observer-clear-trigger",
    )

    decision, _ = await handler.execute(EventType.ON_MESSAGE_RECEIVED.value, {"message": message})

    assert decision is EventDecision.SUCCESS
    session = await store.get("stream-observer-clear-trigger")
    assert session is not None
    assert session.trigger_type == ""
    assert session.trigger_reason == ""
    assert session.trigger_at is None
    assert session.waiting_until is None


@pytest.mark.asyncio
async def test_message_received_clears_wait_with_enum_event_and_chat_type() -> None:
    """真实事件传入枚举值时，新用户消息也应清理旧回复等待。"""

    plugin = NDFCKokoroBridgePlugin(config=NDFCKokoroBridgeConfig())
    store = getattr(plugin, "session_store")
    assert isinstance(store, BridgeSessionStore)
    stream_id = "stream-observer-enum-clear-wait"
    async with store.lock(stream_id):
        session = await store.get_or_create(stream_id)
        session.set_waiting(120, reason="刚才在等你", expected_reaction="继续回复")
        await store.save(session)

    handler = NDFCKokoroMessageObserver(plugin=plugin)
    message = Message(
        message_id="msg-observer-enum-clear-wait",
        time=time.time(),
        content="我来了",
        processed_plain_text="我来了",
        sender_id="user-1",
        sender_name="User",
        platform="qq",
        chat_type="private",
        stream_id=stream_id,
    )
    message.chat_type = ChatType.PRIVATE  # type: ignore[assignment]

    decision, _ = await handler.execute(EventType.ON_MESSAGE_RECEIVED, {"message": message})

    assert decision is EventDecision.SUCCESS
    session = await store.get(stream_id)
    assert session is not None
    assert session.waiting_until is None
    assert session.waiting_reason == ""
    assert session.waiting_expected_reaction == ""
