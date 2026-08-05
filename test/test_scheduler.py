"""NDFC Kokoro Bridge 调度器测试。"""

from __future__ import annotations

import time

import pytest

from plugins.ndfc_kokoro_bridge.config import NDFCKokoroBridgeConfig
from plugins.ndfc_kokoro_bridge.scheduler import NDFCKokoroScheduler
from plugins.ndfc_kokoro_bridge.store import BridgeSessionStore


@pytest.mark.asyncio
async def test_wait_timeout_does_not_allow_cold_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """回复等待是临时状态；运行期超时也不应冷启动私聊流。"""

    config = NDFCKokoroBridgeConfig()
    store = BridgeSessionStore(max_entries=config.mental.max_log_entries)
    scheduler = NDFCKokoroScheduler(config=config, store=store)
    stream_id = "stream-wait-timeout"

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
    store = BridgeSessionStore(max_entries=config.mental.max_log_entries)
    scheduler = NDFCKokoroScheduler(config=config, store=store)
    stream_id = "stream-scheduled-trigger"

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
