"""NDFC Kokoro Bridge 主动唤醒测试。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plugins.ndfc_kokoro_bridge import wake
from plugins.ndfc_kokoro_bridge.config import NDFCKokoroBridgeConfig
from plugins.ndfc_kokoro_bridge.models import BridgeSession
from plugins.ndfc_kokoro_bridge.wake import build_trigger_content


def test_build_trigger_content_uses_configured_template() -> None:
    """主动唤醒内容应使用配置模板渲染。"""

    config = NDFCKokoroBridgeConfig()
    config.wake_prompt.trigger_template = "类型={trigger_type}; 原因={reason}"

    content = build_trigger_content("scheduled", "预约到期", config)

    assert content == "类型=scheduled; 原因=预约到期"


def test_build_trigger_content_fills_empty_reason() -> None:
    """主动唤醒内容应为空理由填入默认文本。"""

    config = NDFCKokoroBridgeConfig()
    config.wake_prompt.trigger_template = "类型={trigger_type}; 原因={reason}"

    content = build_trigger_content("silence", "", config)

    assert content == "类型=silence; 原因=无"


@pytest.mark.asyncio
async def test_wake_existing_stream_still_confirms_loop_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """热流也必须经过循环启动确认，不能只因对象存在就报告唤醒成功。"""
    stream = SimpleNamespace(
        stream_id="stream-1",
        platform="qq",
        context=SimpleNamespace(add_unread_message=lambda _message: None),
    )
    monkeypatch.setattr(wake, "get_stream", lambda _stream_id: _async_value(stream))

    start = AsyncMock(return_value=False)
    monkeypatch.setattr(wake, "_start_stream_loop", start)
    result = await wake.wake_stream(BridgeSession(stream_id="stream-1"), "scheduled", "test")
    assert result is False
    start.assert_awaited_once_with("stream-1")


async def _async_value(value: object) -> object:
    """返回异步测试替身值。"""
    return value
