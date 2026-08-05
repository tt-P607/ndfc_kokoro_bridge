"""NDFC Kokoro Bridge Prompt 注入处理器测试。"""

from __future__ import annotations

import pytest

from plugins.ndfc_kokoro_bridge.config import NDFCKokoroBridgeConfig
from plugins.ndfc_kokoro_bridge.handlers.prompt_state_injector import NDFCKokoroPromptStateInjector
from plugins.ndfc_kokoro_bridge.models import MentalEntry
from plugins.ndfc_kokoro_bridge.plugin import NDFCKokoroBridgePlugin
from plugins.ndfc_kokoro_bridge.store import BridgeSessionStore
from src.app.plugin_system.api.event_api import EventDecision


@pytest.mark.asyncio
async def test_prompt_state_injector_appends_extra_for_private_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prompt 注入器应向 private NDFC prompt 的 values.extra 追加状态。"""

    async def fake_get_stream_info(_stream_id: str) -> dict[str, str]:
        return {"chat_type": "private", "platform": "qq"}

    monkeypatch.setattr(
        "plugins.ndfc_kokoro_bridge.handlers.prompt_state_injector.get_stream_info",
        fake_get_stream_info,
    )

    config = NDFCKokoroBridgeConfig()
    plugin = NDFCKokoroBridgePlugin(config=config)
    store = getattr(plugin, "session_store")
    assert isinstance(store, BridgeSessionStore)
    async with store.lock("stream-prompt"):
        session = await store.get_or_create("stream-prompt")
        session.add_entry(MentalEntry(event_type="inner_thought", content="刚才有点犹豫"), max_entries=10)
        await store.save(session)

    handler = NDFCKokoroPromptStateInjector(plugin=plugin)
    params = {
        "name": "neo_default_chatter_user_prompt",
        "values": {"stream_id": "stream-prompt", "extra": "已有额外信息"},
    }

    decision, next_params = await handler.execute("on_prompt_build", params)

    assert decision is EventDecision.SUCCESS
    assert next_params is params
    extra = next_params["values"]["extra"]
    assert "已有额外信息" in extra
    assert "# 私聊运行状态" in extra
    assert "刚才有点犹豫" in extra


@pytest.mark.asyncio
async def test_prompt_state_injector_skips_group_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """private_only 开启时群聊流不应注入状态。"""

    async def fake_get_stream_info(_stream_id: str) -> dict[str, str]:
        return {"chat_type": "group", "platform": "qq"}

    monkeypatch.setattr(
        "plugins.ndfc_kokoro_bridge.handlers.prompt_state_injector.get_stream_info",
        fake_get_stream_info,
    )

    plugin = NDFCKokoroBridgePlugin(config=NDFCKokoroBridgeConfig())
    handler = NDFCKokoroPromptStateInjector(plugin=plugin)
    params = {
        "name": "neo_default_chatter_user_prompt",
        "values": {"stream_id": "stream-group", "extra": "原始"},
    }

    decision, next_params = await handler.execute("on_prompt_build", params)

    assert decision is EventDecision.SUCCESS
    assert next_params["values"]["extra"] == "原始"
