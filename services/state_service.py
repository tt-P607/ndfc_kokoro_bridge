"""NDFC Kokoro Bridge 状态服务。

服务层统一封装 BridgeSession 的读写、心理活动追加、回复等待、预约思考
以及 prompt 状态渲染，供 Action、EventHandler 和 Scheduler 复用。
"""

from __future__ import annotations

import time
from typing import Any

from src.app.plugin_system.base import BaseService

from ..config import NDFCKokoroBridgeConfig
from ..models import BridgeSession, MentalEntry
from ..state_renderer import render_session_state
from ..store import BridgeSessionStore


class NDFCKokoroStateService(BaseService):
    """NDFC Kokoro Bridge 状态服务。"""

    name: str = "ndfc_kokoro_state"
    description: str = "维护 NDFC 私聊心理状态、等待状态和预约思考状态"
    version: str = "1.0.0"

    def get_config(self) -> NDFCKokoroBridgeConfig:
        """获取插件配置，委托给插件实例。"""
        return self.plugin.get_config()  # type: ignore[attr-defined]

    def get_store(self) -> BridgeSessionStore:
        """获取插件级会话存储。"""
        return self.plugin.session_store  # type: ignore[attr-defined]

    async def get_or_create_session(
        self,
        stream_id: str,
        platform: str = "",
        user_id: str = "",
    ) -> BridgeSession:
        """获取或创建会话状态并补充元信息。

        Args:
            stream_id: 聊天流 ID。
            platform: 平台名称。
            user_id: 私聊用户 ID。

        Returns:
            BridgeSession 实例。
        """
        store = self.get_store()
        config = self.get_config()
        async with store.lock(stream_id):
            session = await store.get_or_create(stream_id)
            changed = False
            if platform and session.platform != platform:
                session.platform = platform
                changed = True
            if user_id and session.user_id != user_id:
                session.user_id = user_id
                changed = True
            if len(session.mental_entries) > config.mental.max_log_entries:
                session.mental_entries = session.mental_entries[-config.mental.max_log_entries:]
                changed = True
            if changed:
                await store.save(session)
            return session

    async def save_session(self, session: BridgeSession) -> None:
        """保存会话状态。"""
        await self.get_store().save(session)

    async def add_entry(
        self,
        stream_id: str,
        event_type: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> BridgeSession:
        """追加心理活动记录。

        Args:
            stream_id: 聊天流 ID。
            event_type: 事件类型。
            content: 可读内容。
            metadata: 附加结构化元数据。

        Returns:
            更新后的会话状态。
        """
        store = self.get_store()
        config = self.get_config()
        async with store.lock(stream_id):
            session = await store.get_or_create(stream_id)
            entry = MentalEntry(
                event_type=event_type,
                content=content,
                metadata=metadata or {},
            )
            session.add_entry(entry, max_entries=config.mental.max_log_entries)
            session.last_activity_at = time.time()
            await store.save(session)
            return session

    async def set_reply_wait(
        self,
        stream_id: str,
        raw_seconds: float,
        reason: str = "",
        expected_reaction: str = "",
        thought: str = "",
    ) -> tuple[bool, str]:
        """设置回复等待状态。

        Args:
            stream_id: 聊天流 ID。
            raw_seconds: 模型请求等待秒数。
            reason: 等待理由。
            expected_reaction: 期待对方回应。
            thought: 内心思考。

        Returns:
            执行结果与说明。
        """
        store = self.get_store()
        config = self.get_config()
        async with store.lock(stream_id):
            session = await store.get_or_create(stream_id)
            if raw_seconds > 0 and config.wait.is_limit_reached(session.consecutive_timeout_count):
                session.add_entry(
                    MentalEntry(
                        event_type="reply_wait_rejected",
                        content="已达到连续回复等待上限，拒绝继续设置等待。",
                        metadata={
                            "raw_seconds": raw_seconds,
                            "consecutive_timeout_count": session.consecutive_timeout_count,
                            "max_consecutive_timeouts": config.wait.max_consecutive_timeouts,
                        },
                    ),
                    max_entries=config.mental.max_log_entries,
                )
                await store.save(session)
                return False, "已达到连续回复等待上限，不能继续设置回复等待"

            seconds = config.wait.apply_rules(raw_seconds, session.consecutive_timeout_count)
            if seconds <= 0:
                session.clear_waiting()
                session.add_entry(
                    MentalEntry(
                        event_type="reply_wait_cancelled",
                        content="已取消回复等待。",
                        metadata={"raw_seconds": raw_seconds, "reason": reason},
                    ),
                    max_entries=config.mental.max_log_entries,
                )
                await store.save(session)
                return True, "已取消回复等待"
            session.set_waiting(seconds, reason=reason, expected_reaction=expected_reaction)
            metadata = {
                "seconds": seconds,
                "raw_seconds": raw_seconds,
                "expected_reaction": expected_reaction,
                "thought": thought.strip(),
            }
            content_parts = [f"设置回复等待 {seconds:.0f} 秒。"]
            if reason:
                content_parts.append(reason)
            if thought.strip():
                content_parts.append(f"内心思考：{thought.strip()}")
            session.add_entry(
                MentalEntry(
                    event_type="reply_wait_set",
                    content=" ".join(content_parts).strip(),
                    metadata=metadata,
                ),
                max_entries=config.mental.max_log_entries,
            )
            await store.save(session)
            return True, f"已设置回复等待 {seconds:.0f} 秒"

    async def schedule_thought(
        self,
        stream_id: str,
        delay_minutes: int,
        reason: str = "",
    ) -> tuple[bool, str]:
        """设置或取消主动思考预约。

        Args:
            stream_id: 聊天流 ID。
            delay_minutes: 延迟分钟数，0 表示取消。
            reason: 预约理由。

        Returns:
            执行结果与说明。
        """
        store = self.get_store()
        config = self.get_config()
        async with store.lock(stream_id):
            session = await store.get_or_create(stream_id)
            if delay_minutes == 0:
                session.set_scheduled(None)
                session.add_entry(
                    MentalEntry(
                        event_type="schedule_cancelled",
                        content="已取消主动思考预约。",
                    ),
                    max_entries=config.mental.max_log_entries,
                )
                await store.save(session)
                return True, "已取消当前主动思考预约"

            normalized_minutes = max(
                config.proactive.schedule_min_minutes,
                min(delay_minutes, config.proactive.schedule_max_minutes),
            )
            scheduled_at = time.time() + normalized_minutes * 60
            session.set_scheduled(scheduled_at, reason=reason)
            session.add_entry(
                MentalEntry(
                    event_type="schedule_set",
                    content=f"预约 {normalized_minutes} 分钟后主动思考。{reason}".strip(),
                    metadata={
                        "delay_minutes": normalized_minutes,
                        "raw_delay_minutes": delay_minutes,
                        "scheduled_at": scheduled_at,
                    },
                ),
                max_entries=config.mental.max_log_entries,
            )
            await store.save(session)
            return True, f"已预约在 {normalized_minutes} 分钟后主动思考"

    async def render_prompt_state(self, stream_id: str) -> str:
        """渲染指定流的 prompt 注入状态。"""
        session = await self.get_store().get(stream_id)
        if session is None:
            return ""
        return render_session_state(session, self.get_config())
