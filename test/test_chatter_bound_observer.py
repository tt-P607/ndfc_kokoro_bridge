"""NDFC Kokoro Bridge Chatter 归属观察处理器测试。"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from plugins.ndfc_kokoro_bridge.config import NDFCKokoroBridgeConfig
from plugins.ndfc_kokoro_bridge.handlers.chatter_bound_observer import (
    NDFCKokoroChatterBoundObserver,
)
from plugins.ndfc_kokoro_bridge.plugin import NDFCKokoroBridgePlugin
from src.app.plugin_system.api.event_api import EventDecision
from src.app.plugin_system.types import EventType


def _unique_stream_id() -> str:
    """生成唯一的测试流 ID。"""
    return f"stream-chatter-{uuid4().hex}"


def _fake_chatter(name: str) -> SimpleNamespace:
    """构造带指定 name 的 chatter 模拟对象。"""
    return SimpleNamespace(name=name)


@pytest.mark.asyncio
async def test_chatter_bound_removes_non_ndfc_from_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """流被配置为其他 chatter 驱动时，应从 NDFC 注册表移除。"""

    plugin = NDFCKokoroBridgePlugin(config=NDFCKokoroBridgeConfig())
    stream_id = _unique_stream_id()
    plugin.mark_ndfc_stream(stream_id)
    assert plugin.is_ndfc_stream(stream_id)

    monkeypatch.setattr(
        "plugins.ndfc_kokoro_bridge.handlers.chatter_bound_observer.get_chatter_by_stream",
        lambda sid: _fake_chatter("kokoro_flow_chatter"),
    )

    handler = NDFCKokoroChatterBoundObserver(plugin=plugin)
    decision, _ = await handler.execute(
        EventType.AFTER_CHATTER_STEP.value,
        {"stream_id": stream_id},
    )

    assert decision is EventDecision.SUCCESS
    assert not plugin.is_ndfc_stream(stream_id)


@pytest.mark.asyncio
async def test_chatter_bound_keeps_ndfc_in_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """流仍由 NDFC 驱动时，应保留在注册表中。"""

    plugin = NDFCKokoroBridgePlugin(config=NDFCKokoroBridgeConfig())
    stream_id = _unique_stream_id()
    plugin.mark_ndfc_stream(stream_id)

    monkeypatch.setattr(
        "plugins.ndfc_kokoro_bridge.handlers.chatter_bound_observer.get_chatter_by_stream",
        lambda sid: _fake_chatter("neo_default_chatter"),
    )

    handler = NDFCKokoroChatterBoundObserver(plugin=plugin)
    decision, _ = await handler.execute(
        EventType.AFTER_CHATTER_STEP.value,
        {"stream_id": stream_id},
    )

    assert decision is EventDecision.SUCCESS
    assert plugin.is_ndfc_stream(stream_id)


@pytest.mark.asyncio
async def test_chatter_bound_noop_without_stream_id() -> None:
    """缺少 stream_id 时不应报错，直接放行。"""

    plugin = NDFCKokoroBridgePlugin(config=NDFCKokoroBridgeConfig())
    handler = NDFCKokoroChatterBoundObserver(plugin=plugin)

    decision, _ = await handler.execute(
        EventType.AFTER_CHATTER_STEP.value,
        {},
    )

    assert decision is EventDecision.SUCCESS
