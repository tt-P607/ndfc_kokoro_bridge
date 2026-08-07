"""NDFC Kokoro Bridge 备忘录 Action 测试。"""

from __future__ import annotations

from typing import TypeVar
from uuid import uuid4

import pytest

from plugins.ndfc_kokoro_bridge.actions.memo import MemoAction, MemoDeleteAction
from plugins.ndfc_kokoro_bridge.config import NDFCKokoroBridgeConfig
from plugins.ndfc_kokoro_bridge.plugin import NDFCKokoroBridgePlugin
from plugins.ndfc_kokoro_bridge.store import BridgeSessionStore
from src.core.models.stream import ChatStream

_T = TypeVar("_T", MemoAction, MemoDeleteAction)


def _unique_stream_id() -> str:
    """生成唯一的测试流 ID，避免磁盘持久化存储跨测试/跨运行污染。"""
    return f"stream-memo-{uuid4().hex}"


def _make_plugin() -> NDFCKokoroBridgePlugin:
    """构造测试插件实例。"""
    return NDFCKokoroBridgePlugin(config=NDFCKokoroBridgeConfig())


def _make_action(
    plugin: NDFCKokoroBridgePlugin,
    action_cls: type[_T],
    stream_id: str,
) -> _T:
    """构造绑定私有流的 Action 实例。

    Args:
        plugin: 插件实例。
        action_cls: Action 类。
        stream_id: 目标流 ID。

    Returns:
        绑定私有流的 Action 实例。
    """
    stream = ChatStream(stream_id=stream_id, platform="qq", chat_type="private")
    return action_cls(chat_stream=stream, plugin=plugin)


@pytest.mark.asyncio
async def test_memo_action_writes_memo() -> None:
    """memo Action 应写入一条带过期时间的备忘。"""
    plugin = _make_plugin()
    stream_id = _unique_stream_id()
    action = _make_action(plugin, MemoAction, stream_id)

    success, result = await action.execute(
        content="记得跟进那件事",
        intent="不能忘",
        expire_hours=2,
    )

    assert success is True
    assert "已记下备忘" in result

    store = getattr(plugin, "session_store")
    assert isinstance(store, BridgeSessionStore)
    session = await store.get(stream_id)
    assert session is not None
    assert len(session.memos) == 1
    assert session.memos[0].content == "记得跟进那件事"
    assert session.memos[0].intent == "不能忘"


@pytest.mark.asyncio
async def test_memo_action_refreshes_duplicate_content() -> None:
    """相同内容的备忘应刷新过期时间而不是重复创建。"""
    plugin = _make_plugin()
    stream_id = _unique_stream_id()
    action = _make_action(plugin, MemoAction, stream_id)

    _, first = await action.execute(content="同一件事", expire_hours=1)
    assert "已记下备忘" in first

    _, second = await action.execute(content="同一件事", expire_hours=3)
    assert "已刷新备忘" in second

    store = getattr(plugin, "session_store")
    assert isinstance(store, BridgeSessionStore)
    session = await store.get(stream_id)
    assert session is not None
    assert len(session.memos) == 1


@pytest.mark.asyncio
async def test_memo_delete_action_removes_by_id() -> None:
    """memo_delete Action 应按 id 删除备忘。"""
    plugin = _make_plugin()
    stream_id = _unique_stream_id()
    write_action = _make_action(plugin, MemoAction, stream_id)
    delete_action = _make_action(plugin, MemoDeleteAction, stream_id)

    _, _ = await write_action.execute(content="待删除的备忘")
    store = getattr(plugin, "session_store")
    assert isinstance(store, BridgeSessionStore)
    session = await store.get(stream_id)
    assert session is not None
    memo_id = session.memos[0].memo_id

    success, result = await delete_action.execute(memo_ids=[memo_id])

    assert success is True
    assert "已删除 1 条备忘" in result
    session = await store.get(stream_id)
    assert session is not None
    assert len(session.memos) == 0


@pytest.mark.asyncio
async def test_memo_action_rejects_empty_content() -> None:
    """content 为空时备忘录 Action 应返回失败。"""
    plugin = _make_plugin()
    stream_id = _unique_stream_id()
    action = _make_action(plugin, MemoAction, stream_id)

    success, result = await action.execute(content="   ")

    assert success is False
    assert "不能为空" in result
