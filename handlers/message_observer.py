"""NDFC Kokoro Bridge 消息观察处理器。

监听消息接收与发送事件，维护聊天流的最近活动时间、等待状态与场景类型。
"""

from __future__ import annotations

import time
from typing import Any

from src.app.plugin_system.api.event_api import EventDecision
from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.base import BaseEventHandler
from src.app.plugin_system.types import EventType, Message

from ..config import NDFCKokoroBridgeConfig
from ..store import BridgeSessionStore

logger = get_logger("ndfc_kokoro_bridge.message")


class NDFCKokoroMessageObserver(BaseEventHandler):
    """观察消息收发并更新 BridgeSession。"""

    name: str = "ndfc_kokoro_message_observer"
    description: str = "记录消息收发状态并维护回复等待"
    weight: int = 5
    intercept_message: bool = False
    init_subscribe: list[EventType] = [EventType.ON_MESSAGE_RECEIVED, EventType.ON_MESSAGE_SENT]

    def get_config(self) -> NDFCKokoroBridgeConfig:
        """获取插件配置，委托给插件实例。"""
        return self.plugin.get_config()  # type: ignore[attr-defined]

    def get_store(self) -> BridgeSessionStore:
        """获取插件级会话存储。"""
        return self.plugin.session_store  # type: ignore[attr-defined]

    async def execute(
        self,
        event_name: str,
        params: dict[str, Any],
    ) -> tuple[EventDecision, dict[str, Any]]:
        """处理消息收发事件。"""
        config = self.get_config()
        if not config.plugin.enabled:
            return EventDecision.SUCCESS, params

        message = params.get("message")
        if not isinstance(message, Message):
            return EventDecision.PASS, params

        # 仅处理已由 NDFC 生命周期事件登记的流，避免为其他 chatter（如 KFC）
        # 的会话误建状态；未登记即视为非 NDFC 流，不创建任何数据。
        if not self.plugin.is_ndfc_stream(message.stream_id):  # type: ignore[attr-defined]
            return EventDecision.SUCCESS, params

        chat_type = str(getattr(message.chat_type, "value", message.chat_type))
        if not config.plugin.is_scene_enabled(chat_type):
            return EventDecision.SUCCESS, params

        normalized_event_name = str(getattr(event_name, "value", event_name))
        if normalized_event_name == EventType.ON_MESSAGE_RECEIVED.value:
            await self._on_message_received(message, chat_type)
        elif normalized_event_name == EventType.ON_MESSAGE_SENT.value:
            await self._on_message_sent(message, chat_type)

        return EventDecision.SUCCESS, params

    async def _on_message_received(self, message: Message, chat_type: str) -> None:
        """记录用户消息到达。

        Args:
            message: 收到的消息。
            chat_type: 聊天场景。
        """
        store = self.get_store()
        stream_id = message.stream_id
        if not stream_id:
            return

        async with store.lock(stream_id):
            session = await store.get_or_create(stream_id)
            session.platform = message.platform or session.platform
            # user_id 仅在私聊流有意义（群聊没有唯一 user_id）；群聊保持原值不覆盖
            if chat_type == "private":
                session.user_id = message.sender_id or session.user_id
            session.chat_type = chat_type or session.chat_type
            msg_time = float(message.time) if isinstance(message.time, (int, float)) else time.time()
            session.last_user_message_at = msg_time
            session.last_activity_at = msg_time
            session.trigger_type = ""
            session.trigger_reason = ""
            session.trigger_at = None
            session.consecutive_timeout_count = 0
            if session.waiting_until is not None:
                session.clear_waiting()
            await store.save(session)

    async def _on_message_sent(self, message: Message, chat_type: str) -> None:
        """记录 Bot 消息发送。

        Args:
            message: 发送的消息。
            chat_type: 聊天场景。
        """
        store = self.get_store()
        stream_id = message.stream_id
        if not stream_id:
            return

        async with store.lock(stream_id):
            session = await store.get_or_create(stream_id)
            session.platform = message.platform or session.platform
            session.chat_type = chat_type or session.chat_type
            # user_id 仅在私聊流有意义；群聊保持原值不覆盖
            target_user_id = str(message.extra.get("target_user_id") or "")
            if chat_type == "private" and target_user_id:
                session.user_id = target_user_id
            msg_time = float(message.time) if isinstance(message.time, (int, float)) else time.time()
            session.last_bot_message_at = msg_time
            session.last_activity_at = msg_time
            await store.save(session)
