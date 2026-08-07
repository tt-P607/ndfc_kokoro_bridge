"""NDFC Kokoro Bridge 主动唤醒封装。

本模块集中封装公开 stream_api 与必要的 src.core 流循环内部调用，
避免内部依赖扩散到 Action、EventHandler 和 Service。
"""

from __future__ import annotations

import time
import uuid

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.api.stream_api import get_or_create_stream, get_stream
from src.app.plugin_system.types import Message, MessageType

from .config import NDFCKokoroBridgeConfig
from .models import BridgeSession

logger = get_logger("ndfc_kokoro_bridge.wake")


def build_trigger_content(trigger_type: str, reason: str, config: NDFCKokoroBridgeConfig) -> str:
    """构造系统触发消息正文。

    Args:
        trigger_type: 触发类型。
        reason: 触发理由。
        config: 插件配置。

    Returns:
        系统触发消息正文。
    """
    template = config.wake_prompt.trigger_template
    return template.format(trigger_type=trigger_type, reason=reason or "无")


async def wake_stream(
    session: BridgeSession,
    trigger_type: str,
    reason: str,
    config: NDFCKokoroBridgeConfig | None = None,
    allow_cold_start: bool = True,
) -> bool:
    """向目标聊天流注入系统触发消息并尝试启动流循环。

    私聊场景按 user_id 定位目标，群聊/讨论组按 chat_type 还原场景；
    冷启动时按场景创建流。

    Args:
        session: 目标会话状态。
        trigger_type: 触发类型。
        reason: 触发理由。
        config: 插件配置。
        allow_cold_start: 目标流不在内存中时是否允许冷启动。

    Returns:
        是否成功注入并唤醒。
    """
    if not session.stream_id:
        return False

    chat_type = session.chat_type or "private"
    is_group = chat_type in ("group", "discuss")

    chat_stream = await get_stream(session.stream_id)
    is_cold_start = chat_stream is None

    if chat_stream is None:
        if not allow_cold_start:
            logger.debug(f"跳过冷启动唤醒 stream={session.stream_id[:8]} trigger={trigger_type}")
            return False
        if not session.platform:
            logger.debug(f"缺少冷启动元信息 stream={session.stream_id[:8]}")
            return False
        if not is_group and not session.user_id:
            logger.debug(f"缺少私聊冷启动目标 stream={session.stream_id[:8]}")
            return False
        try:
            chat_stream = await get_or_create_stream(
                stream_id=session.stream_id,
                platform=session.platform,
                user_id=session.user_id,
                chat_type=chat_type,
            )
        except Exception as exc:
            logger.warning(f"冷启动流失败 stream={session.stream_id[:8]}: {exc}")
            return False

    if config is None:
        config = NDFCKokoroBridgeConfig()

    content = build_trigger_content(trigger_type, reason, config)
    trigger_message = Message(
        message_id=f"ndfc_kokoro_trigger_{uuid.uuid4().hex[:12]}",
        time=time.time(),
        content=content,
        processed_plain_text=content,
        message_type=MessageType.TEXT,
        sender_id="system",
        sender_name="系统",
        platform=chat_stream.platform or session.platform,
        chat_type=chat_type,
        stream_id=session.stream_id,
        target_user_id=session.user_id,
    )
    chat_stream.context.add_unread_message(trigger_message)
    logger.debug(f"已注入 NDFC Kokoro 触发消息 stream={session.stream_id[:8]}")

    if is_cold_start:
        return await _start_stream_loop(session.stream_id)
    return True


async def _start_stream_loop(stream_id: str) -> bool:
    """启动目标流循环。

    Args:
        stream_id: 聊天流 ID。

    Returns:
        是否成功启动。
    """
    try:
        from src.core.transport.distribution.stream_loop_manager import (
            get_stream_loop_manager,
        )

        loop_manager = get_stream_loop_manager()
        if not loop_manager.is_running:
            await loop_manager.start()
        return await loop_manager.start_stream_loop(stream_id)
    except Exception as exc:
        logger.warning(f"启动流循环失败 stream={stream_id[:8]}: {exc}")
        return False
