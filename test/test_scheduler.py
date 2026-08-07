"""NDFC Kokoro Bridge 调度器测试。"""

from __future__ import annotations

import time
from uuid import uuid4

import pytest

from plugins.ndfc_kokoro_bridge.config import NDFCKokoroBridgeConfig
from plugins.ndfc_kokoro_bridge.scheduler import NDFCKokoroScheduler
from plugins.ndfc_kokoro_bridge.store import BridgeSessionStore


def _unique_stream_id(prefix: str) -> str:
    """生成唯一的测试流 ID，避免磁盘持久化存储跨测试/跨运行污染。"""
    return f"stream-{prefix}-{uuid4().hex}"


@pytest.mark.asyncio
async def test_wait_timeout_does_not_allow_cold_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """回复等待是临时状态；运行期超时也不应冷启动私聊流。"""

    config = NDFCKokoroBridgeConfig()
    store = BridgeSessionStore()
    scheduler = NDFCKokoroScheduler(config=config, store=store)
    stream_id = _unique_stream_id("wait-timeout")

    async with store.lock(stream_id):
        session = await store.get_or_create(stream_id)
        session.waiting_until = time.time() - 1.0
        session.waiting_reason = "等回复"
        session.platform = "qq"
        session.user_id = "user-1"
        await store.save(session)

    wake_kwargs: list[dict[str, object]] = []

    async def fake_wake_stream(*args: object, **kwargs: object) -> bool:
        _ = args
        wake_kwargs.append(kwargs)
        return True

    monkeypatch.setattr("plugins.ndfc_kokoro_bridge.scheduler.wake_stream", fake_wake_stream)

    await scheduler._check_one(stream_id)

    session = await store.get(stream_id)
    assert session is not None
    assert session.waiting_until is None
    assert session.trigger_type == "wait_timeout"
    assert wake_kwargs == [
        {
            "trigger_type": "wait_timeout",
            "reason": "等回复",
            "config": config,
            "allow_cold_start": False,
        }
    ]


@pytest.mark.asyncio
async def test_scheduled_trigger_allows_configured_cold_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """预约思考是持久化状态，到期时应按配置允许冷启动。"""

    config = NDFCKokoroBridgeConfig()
    config.proactive.wake_cold_stream = True
    store = BridgeSessionStore()
    scheduler = NDFCKokoroScheduler(config=config, store=store)
    stream_id = _unique_stream_id("scheduled-trigger")

    async with store.lock(stream_id):
        session = await store.get_or_create(stream_id)
        session.scheduled_at = time.time() - 1.0
        session.scheduled_reason = "预约到了"
        session.platform = "qq"
        session.user_id = "user-1"
        await store.save(session)

    wake_kwargs: list[dict[str, object]] = []

    async def fake_wake_stream(*args: object, **kwargs: object) -> bool:
        _ = args
        wake_kwargs.append(kwargs)
        return True

    monkeypatch.setattr("plugins.ndfc_kokoro_bridge.scheduler.wake_stream", fake_wake_stream)

    await scheduler._check_one(stream_id)

    session = await store.get(stream_id)
    assert session is not None
    assert session.scheduled_at is None
    assert session.trigger_type == "scheduled"
    assert wake_kwargs == [
        {
            "trigger_type": "scheduled",
            "reason": "预约到了",
            "config": config,
            "allow_cold_start": True,
        }
    ]


@pytest.mark.asyncio
async def test_check_all_sessions_skips_non_ndfc_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    """已配置为其他 chatter 的流不应被调度器触发唤醒。"""

    config = NDFCKokoroBridgeConfig()
    store = BridgeSessionStore()

    ndfc_stream = _unique_stream_id("ndfc")
    non_ndfc_stream = _unique_stream_id("non-ndfc")
    scheduler = NDFCKokoroScheduler(
        config=config,
        store=store,
        is_ndfc_stream=lambda sid: sid == ndfc_stream,
    )

    # 两个流都设置了到期的预约
    for stream_id in (ndfc_stream, non_ndfc_stream):
        async with store.lock(stream_id):
            session = await store.get_or_create(stream_id)
            session.scheduled_at = time.time() - 1.0
            session.scheduled_reason = "预约到了"
            session.platform = "qq"
            session.user_id = "user-1"
            await store.save(session)

    wake_kwargs: list[dict[str, object]] = []

    async def fake_wake_stream(*args: object, **kwargs: object) -> bool:
        _ = args
        wake_kwargs.append(kwargs)
        return True

    monkeypatch.setattr("plugins.ndfc_kokoro_bridge.scheduler.wake_stream", fake_wake_stream)

    await scheduler.check_all_sessions()

    # 只有 NDFC 流被唤醒；非 NDFC 流即使有到期预约也不触发
    assert len(wake_kwargs) == 1
    assert wake_kwargs[0]["trigger_type"] == "scheduled"

    ndfc_session = await store.get(ndfc_stream)
    assert ndfc_session is not None
    assert ndfc_session.scheduled_at is None
    assert ndfc_session.trigger_type == "scheduled"

    non_ndfc_session = await store.get(non_ndfc_stream)
    assert non_ndfc_session is not None
    assert non_ndfc_session.scheduled_at is not None
    assert non_ndfc_session.trigger_type == ""
