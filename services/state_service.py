"""NDFC Kokoro Bridge 状态服务。

服务层统一封装 BridgeSession 的读写、心理活动追加、回复等待、预约思考、
私人备忘录以及 prompt 状态渲染，供 Action、EventHandler 和 Scheduler 复用。
"""

from __future__ import annotations

import time
from typing import Any

from src.app.plugin_system.api import prompt_api
from src.app.plugin_system.base import BaseService
from src.core.prompt import SystemReminderBucket, SystemReminderInsertType

from ..config import NDFCKokoroBridgeConfig
from ..models import BridgeSession, Memo, MentalEntry
from ..state_renderer import render_long_term_reminder, render_user_extra
from ..store import BridgeSessionStore

#: 长期状态 system reminder 的写入名（写入 actor 流私有 bucket）。
_LONG_TERM_REMINDER_NAME = "ndfc_kokoro_state"


class NDFCKokoroStateService(BaseService):
    """NDFC Kokoro Bridge 状态服务。"""

    name: str = "ndfc_kokoro_state"
    description: str = "维护 NDFC 聊天流状态、等待状态、预约思考状态和备忘录"
    version: str = "2.0.0"

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
            else:
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

        # 锁外同步长期状态到 system reminder（内部会重新读取最新 session）
        await self.sync_long_term_reminder(stream_id)

        if delay_minutes == 0:
            return True, "已取消当前主动思考预约"
        return True, f"已预约在 {normalized_minutes} 分钟后主动思考"

    async def upsert_memo(
        self,
        stream_id: str,
        memo: Memo,
    ) -> tuple[Memo | None, bool, str]:
        """写入或刷新一条备忘录。

        Args:
            stream_id: 聊天流 ID。
            memo: 待写入的备忘。

        Returns:
            tuple: ``(最终落盘的备忘, 是否为新建, 回执描述)``。
        """
        store = self.get_store()
        config = self.get_config()
        async with store.lock(stream_id):
            session = await store.get_or_create(stream_id)
            saved_memo, is_new = session.upsert_memo(memo)
            session.add_entry(
                MentalEntry(
                    event_type="memo_written" if is_new else "memo_refreshed",
                    content=saved_memo.content,
                    metadata={
                        "memo_id": saved_memo.memo_id,
                        "expires_at": saved_memo.expires_at,
                    },
                ),
                max_entries=config.mental.max_log_entries,
            )
            await store.save(session)
        await self.sync_long_term_reminder(stream_id)
        return saved_memo, is_new, "已写入备忘录"

    async def delete_memos(
        self,
        stream_id: str,
        memo_ids: list[str],
    ) -> list[Memo]:
        """按 id 删除备忘录。

        Args:
            stream_id: 聊天流 ID。
            memo_ids: 待删除的备忘 id 列表。

        Returns:
            list[Memo]: 实际被删除的备忘。
        """
        store = self.get_store()
        config = self.get_config()
        async with store.lock(stream_id):
            session = await store.get_or_create(stream_id)
            deleted = session.delete_memos(memo_ids)
            if deleted:
                for memo in deleted:
                    session.add_entry(
                        MentalEntry(
                            event_type="memo_deleted",
                            content=memo.content,
                            metadata={"memo_id": memo.memo_id},
                        ),
                        max_entries=config.mental.max_log_entries,
                    )
                await store.save(session)
        await self.sync_long_term_reminder(stream_id)
        return deleted

    async def prune_expired_memos(self, stream_id: str) -> list[Memo]:
        """清理已过期的备忘录（懒清理入口）。

        Args:
            stream_id: 聊天流 ID。

        Returns:
            list[Memo]: 被清理的备忘。
        """
        store = self.get_store()
        config = self.get_config()
        async with store.lock(stream_id):
            session = await store.get_or_create(stream_id)
            expired = session.prune_expired_memos()
            if expired:
                for memo in expired:
                    session.add_entry(
                        MentalEntry(
                            event_type="memo_expired",
                            content=memo.content,
                            metadata={"memo_id": memo.memo_id},
                        ),
                        max_entries=config.mental.max_log_entries,
                    )
                await store.save(session)
        await self.sync_long_term_reminder(stream_id)
        return expired

    async def render_prompt_state(self, stream_id: str) -> str:
        """渲染指定流注入 user prompt extra 的即时状态。

        Args:
            stream_id: 聊天流 ID。

        Returns:
            即时状态文本；无会话或无内容时返回空字符串。
        """
        session = await self.get_store().get(stream_id)
        if session is None:
            return ""
        return render_user_extra(session, self.get_config())

    async def sync_long_term_reminder(self, stream_id: str) -> str:
        """把长期状态（预约/备忘）同步为 actor 流私有 system reminder。

        内容为空时删除旧 reminder；否则以 DYNAMIC 类型写入，由框架注入到
        最后一条 user 消息并在内容变化时剥离旧文本，保证始终只存在最新一份。

        Args:
            stream_id: 聊天流 ID。

        Returns:
            实际写入的长期状态文本。
        """
        session = await self.get_store().get(stream_id)
        content = render_long_term_reminder(session, self.get_config()) if session else ""
        if not content:
            prompt_api.delete_stream_reminder(
                stream_id,
                SystemReminderBucket.ACTOR.value,
                _LONG_TERM_REMINDER_NAME,
            )
            return ""
        prompt_api.add_stream_reminder(
            stream_id,
            SystemReminderBucket.ACTOR.value,
            _LONG_TERM_REMINDER_NAME,
            content,
            insert_type=SystemReminderInsertType.DYNAMIC,
        )
        return content
