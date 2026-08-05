"""NDFC Kokoro Bridge Action 与插件组件测试。"""

from __future__ import annotations

from typing import cast
from uuid import uuid4

import pytest

from plugins.ndfc_kokoro_bridge.actions.schedule_thought import ScheduleThoughtAction
from plugins.ndfc_kokoro_bridge.actions.set_reply_wait import SetReplyWaitAction
from plugins.ndfc_kokoro_bridge.config import NDFCKokoroBridgeConfig
from plugins.ndfc_kokoro_bridge.handlers.message_observer import NDFCKokoroMessageObserver
from plugins.ndfc_kokoro_bridge.handlers.prompt_state_injector import NDFCKokoroPromptStateInjector
from plugins.ndfc_kokoro_bridge.plugin import NDFCKokoroBridgePlugin
from plugins.ndfc_kokoro_bridge.services.state_service import NDFCKokoroStateService
from plugins.ndfc_kokoro_bridge.store import BridgeSessionStore
from src.app.plugin_system.types import ChatType
from src.core.models.stream import ChatStream


def test_plugin_components_match_manifest_enabled_components() -> None:
    """插件启用时应暴露 manifest 中声明的组件。"""

    plugin = NDFCKokoroBridgePlugin(config=NDFCKokoroBridgeConfig())

    assert plugin.get_components() == [
        ScheduleThoughtAction,
        SetReplyWaitAction,
        NDFCKokoroPromptStateInjector,
        NDFCKokoroMessageObserver,
        NDFCKokoroStateService,
    ]


def test_plugin_components_empty_when_disabled() -> None:
    """插件禁用时不应暴露组件。"""

    config = NDFCKokoroBridgeConfig()
    config.plugin.enabled = False
    plugin = NDFCKokoroBridgePlugin(config=config)

    assert plugin.get_components() == []


def test_plugin_syncs_action_descriptions_from_config() -> None:
    """插件应把配置中的 Action 文案同步到组件类。"""

    config = NDFCKokoroBridgeConfig()
    config.action_prompt.set_reply_wait_description = "自定义等待工具说明"
    config.action_prompt.schedule_thought_description = "自定义预约工具说明"
    config.action_prompt.schedule_guidance = "自定义预约指导"

    NDFCKokoroBridgePlugin(config=config)

    assert SetReplyWaitAction.description == "自定义等待工具说明"
    assert ScheduleThoughtAction.description == "自定义预约工具说明\n\n自定义预约指导"


def test_bridge_actions_are_private_only() -> None:
    """Bridge Actions 应只在私聊中暴露。"""

    assert SetReplyWaitAction.chat_type is ChatType.PRIVATE
    assert ScheduleThoughtAction.chat_type is ChatType.PRIVATE


@pytest.mark.asyncio
async def test_bridge_actions_go_activate_rejects_group_chat() -> None:
    """即使 NDFC 注入路径绕过 ActionManager，go_activate 也应拒绝群聊。"""

    plugin = NDFCKokoroBridgePlugin(config=NDFCKokoroBridgeConfig())
    group_stream = ChatStream(stream_id="stream-group-action", platform="qq", chat_type="group")

    wait_action = SetReplyWaitAction(chat_stream=group_stream, plugin=plugin)
    schedule_action = ScheduleThoughtAction(chat_stream=group_stream, plugin=plugin)

    assert await wait_action.go_activate() is False
    assert await schedule_action.go_activate() is False


@pytest.mark.asyncio
async def test_bridge_actions_go_activate_accepts_private_chat() -> None:
    """Bridge Actions 在私聊中应可按配置激活。"""

    plugin = NDFCKokoroBridgePlugin(config=NDFCKokoroBridgeConfig())
    private_stream = ChatStream(stream_id="stream-private-action", platform="qq", chat_type="private")

    wait_action = SetReplyWaitAction(chat_stream=private_stream, plugin=plugin)
    schedule_action = ScheduleThoughtAction(chat_stream=private_stream, plugin=plugin)

    assert await wait_action.go_activate() is True
    assert await schedule_action.go_activate() is True


@pytest.mark.asyncio
async def test_actions_write_session_state() -> None:
    """等待与预约 Action 应写入同一 stream 的 BridgeSession 状态。"""

    config = NDFCKokoroBridgeConfig()
    config.proactive.schedule_min_minutes = 1
    plugin = cast(NDFCKokoroBridgePlugin, NDFCKokoroBridgePlugin(config=config))
    stream_id = f"stream-action-{uuid4().hex}"
    chat_stream = ChatStream(stream_id=stream_id, platform="qq", chat_type="private")

    wait_action = SetReplyWaitAction(chat_stream=chat_stream, plugin=plugin)
    schedule_action = ScheduleThoughtAction(chat_stream=chat_stream, plugin=plugin)

    wait_success, wait_result = await wait_action.execute(
        20,
        thought="我有点在意，想看看对方会不会继续解释。",
        reason="等回答",
        expected_reaction="继续聊",
    )
    schedule_success, schedule_result = await schedule_action.execute(1, reason="一分钟后想起")

    assert wait_success is True
    assert "已设置回复等待" in wait_result
    assert schedule_success is True
    assert "已预约" in schedule_result

    store = cast(BridgeSessionStore, getattr(plugin, "session_store"))
    session = await store.get(stream_id)
    assert session is not None
    assert session.waiting_reason == "等回答"
    assert session.scheduled_reason == "一分钟后想起"
    assert session.mental_entries[0].metadata["thought"] == "我有点在意，想看看对方会不会继续解释。"
    assert [entry.event_type for entry in session.mental_entries] == [
        "reply_wait_set",
        "schedule_set",
    ]


@pytest.mark.asyncio
async def test_set_reply_wait_rejected_at_limit() -> None:
    """验证达到连续超时上限后拒绝设置新的回复等待。"""
    config = NDFCKokoroBridgeConfig()
    config.wait.max_consecutive_timeouts = 3
    plugin = cast(NDFCKokoroBridgePlugin, NDFCKokoroBridgePlugin(config=config))
    stream_id = f"stream-limit-{uuid4().hex}"
    chat_stream = ChatStream(stream_id=stream_id, platform="qq", chat_type="private")

    wait_action = SetReplyWaitAction(chat_stream=chat_stream, plugin=plugin)

    # 模拟已连续超时 3 次
    store = cast(BridgeSessionStore, getattr(plugin, "session_store"))
    async with store.lock(stream_id):
        session = await store.get_or_create(stream_id)
        session.consecutive_timeout_count = 3
        await store.save(session)

    # 尝试设置等待（seconds > 0）
    success, result = await wait_action.execute(
        seconds=30,
        thought="我想再等等",
        reason="不甘心",
    )

    assert success is False
    assert "已达到连续回复等待上限" in result

    # 验证记录了拒绝事件
    session = await store.get(stream_id)
    assert session is not None
    assert session.mental_entries[-1].event_type == "reply_wait_rejected"

    # 尝试取消等待（seconds = 0）应允许
    success_cancel, result_cancel = await wait_action.execute(
        seconds=0,
        thought="算了不等了",
    )
    assert success_cancel is True
    assert "已取消回复等待" in result_cancel
