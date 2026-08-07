"""NDFC Kokoro Bridge Prompt 状态注入处理器。

监听 on_prompt_build 事件，在 neo_default_chatter 构建 user prompt 时把插件
维护的运行状态（场景、等待、预约、备忘）追加到 values["extra"]。

上下文快照的恢复不在本处理器完成——真正的多角色恢复由
``context_snapshot_handler`` 在 ``before_llm_request`` 事件中完成，
本处理器只负责运行时状态注入。
"""

from __future__ import annotations

from typing import Any

from src.app.plugin_system.api.event_api import EventDecision
from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.api.stream_api import get_stream_info
from src.app.plugin_system.base import BaseEventHandler

from ..config import NDFCKokoroBridgeConfig
from ..services.state_service import NDFCKokoroStateService

logger = get_logger("ndfc_kokoro_bridge.prompt")


class NDFCKokoroPromptStateInjector(BaseEventHandler):
    """向 NDFC user prompt extra 注入运行状态。"""

    name: str = "ndfc_kokoro_prompt_state_injector"
    description: str = "在 neo_default_chatter user prompt 中注入 NDFC Kokoro Bridge 运行状态"
    weight: int = 12
    intercept_message: bool = False
    init_subscribe: list[str] = ["on_prompt_build"]

    def get_config(self) -> NDFCKokoroBridgeConfig:
        """获取插件配置，委托给插件实例。"""
        return self.plugin.get_config()  # type: ignore[attr-defined]

    async def execute(
        self,
        event_name: str,
        params: dict[str, Any],
    ) -> tuple[EventDecision, dict[str, Any]]:
        """处理 prompt 构建事件。

        Args:
            event_name: 事件名称。
            params: on_prompt_build 事件参数。

        Returns:
            事件决策与原参数对象。
        """
        _ = event_name
        config = self.get_config()
        if not config.plugin.enabled or not config.plugin.enable_prompt_state_injection:
            return EventDecision.SUCCESS, params

        if str(params.get("name", "")) != config.plugin.target_prompt:
            return EventDecision.SUCCESS, params

        values = params.get("values")
        if not isinstance(values, dict):
            return EventDecision.SUCCESS, params

        stream_id = str(values.get("stream_id", ""))
        if not stream_id:
            return EventDecision.SUCCESS, params

        # 已确认是 NDFC 的 user prompt 构建事件，登记该流，供消息观察器识别 NDFC 流
        self.plugin.mark_ndfc_stream(stream_id)  # type: ignore[attr-defined]

        # 场景过滤：未启用的场景不注入
        try:
            stream_info = await get_stream_info(stream_id)
        except Exception as exc:
            logger.debug(f"获取 stream_info 失败 stream={stream_id[:8]}: {exc}")
            stream_info = None
        if stream_info:
            scene = str(stream_info.get("chat_type", "") or "")
            if scene and not config.plugin.is_scene_enabled(scene):
                return EventDecision.SUCCESS, params

        # 运行状态注入：每轮先同步长期状态到 system reminder（进程重启后自动重建），
        # 再把即时状态追加到 user prompt extra
        service = NDFCKokoroStateService(plugin=self.plugin)
        await service.sync_long_term_reminder(stream_id)
        state_text = await service.render_prompt_state(stream_id)
        if not state_text:
            return EventDecision.SUCCESS, params

        existing = str(values.get("extra", "") or "")
        values["extra"] = existing + "\n\n" + state_text if existing else state_text
        params["values"] = values

        if config.plugin.debug_log:
            logger.info(f"已向 NDFC prompt 注入状态 stream={stream_id[:8]} len={len(state_text)}")

        return EventDecision.SUCCESS, params
