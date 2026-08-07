"""NDFC Kokoro Bridge Chatter 归属观察处理器。

订阅 ``AFTER_CHATTER_STEP`` 事件，维护 NDFC 流注册表的负向清理：当某个
已登记的流被配置为其他 chatter 驱动时，将其从注册表移除，避免消息观察器
与调度器继续误处理非 NDFC 会话。

正向登记由 NDFC 专属事件（``before_llm_request`` / ``on_prompt_build``）
完成；本处理器只负责"流不再归 NDFC 时"的移除，两者互补。
"""

from __future__ import annotations

from typing import Any

from src.app.plugin_system.api.chat_api import get_chatter_by_stream
from src.app.plugin_system.api.event_api import EventDecision
from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.base import BaseEventHandler
from src.app.plugin_system.types import EventType

#: 本插件跟随的 NDFC chatter 名称（与 context_snapshot_handler 的请求名一致）。
_NDFC_CHATTER_NAME = "neo_default_chatter"

logger = get_logger("ndfc_kokoro_bridge.chatter_bound")


class NDFCKokoroChatterBoundObserver(BaseEventHandler):
    """在 chatter 步进后把不再归 NDFC 的流从注册表移除。"""

    name: str = "ndfc_kokoro_chatter_bound_observer"
    description: str = "在 chatter 步进后维护 NDFC 流注册表的负向清理"
    weight: int = 30
    intercept_message: bool = False
    init_subscribe: list[str] = [EventType.AFTER_CHATTER_STEP.value]

    def _unmark_if_non_ndfc(self, stream_id: str) -> None:
        """若指定流当前由非 NDFC chatter 驱动，则从注册表移除。"""
        chatter = get_chatter_by_stream(stream_id)
        if chatter is None:
            return
        chatter_name = str(getattr(chatter, "name", "") or "")
        if chatter_name and chatter_name != _NDFC_CHATTER_NAME:
            self.plugin.ndfc_streams.discard(stream_id)  # type: ignore[attr-defined]
            logger.debug(f"流已改由其他 chatter 驱动，移出 NDFC 注册表 stream={stream_id[:8]}")

    async def execute(
        self,
        event_name: str,
        params: dict[str, Any],
    ) -> tuple[EventDecision, dict[str, Any]]:
        """处理 chatter 步进完成事件，维护 NDFC 流注册表。"""
        _ = event_name
        stream_id = str(params.get("stream_id", "") or "")
        if not stream_id:
            return EventDecision.SUCCESS, params
        if self.plugin.is_ndfc_stream(stream_id):  # type: ignore[attr-defined]
            self._unmark_if_non_ndfc(stream_id)
        return EventDecision.SUCCESS, params
