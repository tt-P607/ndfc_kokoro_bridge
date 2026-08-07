"""NDFC Kokoro Bridge Action 与插件组件测试。"""

from __future__ import annotations

from typing import cast
from uuid import uuid4

import pytest

from plugins.ndfc_kokoro_bridge.actions.memo import MemoAction, MemoDeleteAction
from plugins.ndfc_kokoro_bridge.actions.schedule_thought import (
    ScheduleThoughtAction,
    ScheduleThoughtGroupAction,
)
from plugins.ndfc_kokoro_bridge.actions.set_reply_wait import SetReplyWaitAction
from plugins.ndfc_kokoro_bridge.config import NDFCKokoroBridgeConfig
from plugins.ndfc_kokoro_bridge.handlers.chatter_bound_observer import NDFCKokoroChatterBoundObserver
from plugins.ndfc_kokoro_bridge.handlers.context_snapshot_handler import NDFCKokoroContextSnapshotHandler
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
        MemoAction,
        MemoDeleteAction,
        ScheduleThoughtAction,
        ScheduleThoughtGroupAction,
        SetReplyWaitAction,
        NDFCKokoroContextSnapshotHandler,
        NDFCKokoroPromptStateInjector,
        NDFCKokoroMessageObserver,
        NDFCKokoroChatterBoundObserver,
        NDFCKokoroStateService,
    ]


def test_plugin_components_empty_when_disabled() -> None:
    """插件禁用时不应暴露组件。"""

    config = NDFCKokoroBridgeConfig()
    config.plugin.enabled = False
    plugin = NDFCKokoroBridgePlugin(config=config)

    assert plugin.get_components() == []


def test_plugin_syncs_action_descriptions_from_config() -> None:
    """插件应把配置中的 Action 文案同步到组件类（私聊版与群聊版）。"""

    config = NDFCKokoroBridgeConfig()
    config.action_prompt.set_reply_wait_description = "自定义等待工具说明"
    config.action_prompt.schedule_thought_description = "自定义预约工具说明"
    config.action_prompt.schedule_guidance = "自定义预约指导"
    config.action_prompt.schedule_thought_group_description = "自定义群聊预约工具说明"
    config.action_prompt.schedule_guidance_group = "自定义群聊预约指导"

    NDFCKokoroBridgePlugin(config=config)

    assert SetReplyWaitAction.description == "自定义等待工具说明"
    assert ScheduleThoughtAction.description == "自定义预约工具说明\n\n自定义预约指导"
    assert ScheduleThoughtGroupAction.description == "自定义群聊预约工具说明\n\n自定义群聊预约指导"


def test_bridge_actions_are_all_chat_type() -> None:
    """Bridge Actions 的 chat_type 应按场景分流：私聊版 PRIVATE、群聊版 GROUP、其余 ALL。"""

    assert SetReplyWaitAction.chat_type is ChatType.ALL
    assert ScheduleThoughtAction.chat_type is ChatType.PRIVATE
    assert ScheduleThoughtGroupAction.chat_type is ChatType.GROUP
    assert MemoAction.chat_type is ChatType.ALL
    assert MemoDeleteAction.chat_type is ChatType.ALL


@pytest.mark.asyncio
async def test_bridge_actions_go_activate_private_only_blocks_group() -> None:
    """群聊/讨论组默认不启用主动思考：私聊版在群聊拒绝，群聊版默认也拒绝。"""

    plugin = NDFCKokoroBridgePlugin(config=NDFCKokoroBridgeConfig())
    group_stream = ChatStream(stream_id="stream-group-action", platform="qq", chat_type="group")

    wait_action = SetReplyWaitAction(chat_stream=group_stream, plugin=plugin)
    schedule_action = ScheduleThoughtAction(chat_stream=group_stream, plugin=plugin)
    schedule_group_action = ScheduleThoughtGroupAction(chat_stream=group_stream, plugin=plugin)

    assert await wait_action.go_activate() is False
    assert await schedule_action.go_activate() is False
    assert await schedule_group_action.go_activate() is False


@pytest.mark.asyncio
async def test_bridge_actions_go_activate_allows_group_when_group_enabled() -> None:
    """group_enabled 开启时，群聊版预约 Action 应允许群聊；私聊版仍拒绝。"""

    config = NDFCKokoroBridgeConfig()
    config.wait.private_only = False
    config.proactive.group_enabled = True
    plugin = NDFCKokoroBridgePlugin(config=config)
    group_stream = ChatStream(stream_id="stream-group-action-open", platform="qq", chat_type="group")

    wait_action = SetReplyWaitAction(chat_stream=group_stream, plugin=plugin)
    schedule_action = ScheduleThoughtAction(chat_stream=group_stream, plugin=plugin)
    schedule_group_action = ScheduleThoughtGroupAction(chat_stream=group_stream, plugin=plugin)

    assert await wait_action.go_activate() is True
    assert await schedule_action.go_activate() is False
    assert await schedule_group_action.go_activate() is True


@pytest.mark.asyncio
async def test_bridge_actions_go_activate_accepts_private_chat() -> None:
    """Bridge Actions 在私聊中应可按配置激活；群聊版在私聊拒绝。"""

    plugin = NDFCKokoroBridgePlugin(config=NDFCKokoroBridgeConfig())
    private_stream = ChatStream(stream_id="stream-private-action", platform="qq", chat_type="private")

    wait_action = SetReplyWaitAction(chat_stream=private_stream, plugin=plugin)
    schedule_action = ScheduleThoughtAction(chat_stream=private_stream, plugin=plugin)
    schedule_group_action = ScheduleThoughtGroupAction(chat_stream=private_stream, plugin=plugin)

    assert await wait_action.go_activate() is True
    assert await schedule_action.go_activate() is True
    assert await schedule_group_action.go_activate() is False


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

    session = await store.get(stream_id)
    assert session is not None
    assert session.consecutive_timeout_count == 3

    # 尝试取消等待（seconds = 0）应允许
    success_cancel, result_cancel = await wait_action.execute(
        seconds=0,
        thought="算了不等了",
    )
    assert success_cancel is True
    assert "已取消回复等待" in result_cancel
