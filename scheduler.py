"""NDFC Kokoro Bridge 调度封装。

本模块集中封装 src.kernel.scheduler 的使用，周期检查预约思考、回复等待超时
和沉默主动触发，并通过 wake.py 唤醒 neo_default_chatter 所在流。
"""

from __future__ import annotations

import random
import time
from collections.abc import Awaitable, Callable
from typing import Any

from src.app.plugin_system.api.log_api import get_logger
from src.kernel.scheduler import TriggerType, get_unified_scheduler

from .config import NDFCKokoroBridgeConfig
from .store import BridgeSessionStore
from .wake import wake_stream

logger = get_logger("ndfc_kokoro_bridge.scheduler")

_TASK_NAME = "ndfc_kokoro_bridge_check"


class NDFCKokoroScheduler:
    """NDFC Kokoro Bridge 周期检查器。"""

    def __init__(
        self,
        config: NDFCKokoroBridgeConfig,
        store: BridgeSessionStore,
        state_service: Any = None,
        is_ndfc_stream: Callable[[str], bool] | None = None,
    ) -> None:
        """初始化调度器封装。

        Args:
            config: 插件配置。
            store: 会话存储。
            state_service: 状态服务实例，用于触发后同步长期 system reminder。
            is_ndfc_stream: 判断指定流是否仍由 NDFC 驱动的回调；为 None 时不做过滤。
        """
        self._config = config
        self._store = store
        self._state_service = state_service
        self._is_ndfc_stream = is_ndfc_stream

    async def start(self) -> None:
        """启动周期检查任务。"""
        if not self._config.plugin.enabled:
            return
        scheduler = get_unified_scheduler()
        if not scheduler.get_statistics().get("is_running", False):
            await scheduler.start()
        await scheduler.create_schedule(
            callback=self.check_all_sessions,
            trigger_type=TriggerType.TIME,
            trigger_config={"interval_seconds": max(1, self._config.proactive.check_interval)},
            is_recurring=True,
            task_name=_TASK_NAME,
            force_overwrite=True,
        )
        logger.info("NDFC Kokoro Bridge 调度检查已启动")

    async def stop(self) -> None:
        """停止周期检查任务。"""
        scheduler = get_unified_scheduler()
        await scheduler.remove_schedule_by_name(_TASK_NAME)

    async def check_all_sessions(self) -> None:
        """检查全部已知会话并触发需要唤醒的流。"""
        if not self._config.plugin.enabled:
            return

        stream_ids: set[str] = set(self._store.get_all_cached().keys())
        stream_ids.update(await self._store.list_all_stream_ids())
        for stream_id in stream_ids:
            # 仅处理仍由 NDFC 驱动的流；已被配置为其他 chatter 的流不触发唤醒，
            # 避免向非 NDFC 流注入插件状态或唤醒错误会话。
            if self._is_ndfc_stream is not None and not self._is_ndfc_stream(stream_id):
                continue
            await self._check_one(stream_id)

    def _is_scene_enabled(self, chat_type: str) -> bool:
        """判断会话场景是否在启用列表中。

        Args:
            chat_type: 会话场景标识。

        Returns:
            场景是否启用。
        """
        return self._config.plugin.is_scene_enabled(chat_type or "private")

    def _is_proactive_allowed(self, chat_type: str) -> bool:
        """判断场景是否允许预约/主动触发。

        私聊按 proactive.private_enabled（默认开），群聊/讨论组按
        proactive.group_enabled（默认关，需手动开启）。

        Args:
            chat_type: 会话场景标识。

        Returns:
            是否允许主动触发。
        """
        proactive = self._config.proactive
        if not proactive.enabled:
            return False
        return proactive.is_scene_enabled(chat_type or "private")

    async def _check_one(self, stream_id: str) -> None:
        """检查单个会话。"""
        allow_cold_start = True
        async with self._store.lock(stream_id):
            session = await self._store.get(stream_id)
            if session is None:
                return

            chat_type = session.chat_type or "private"
            if not self._is_scene_enabled(chat_type):
                return

            now = time.time()
            trigger_type = ""
            reason = ""

            if (
                session.scheduled_at is not None
                and now >= session.scheduled_at
                and self._is_proactive_allowed(chat_type)
            ):
                trigger_type = "scheduled"
                # 预约理由原样投回，让被唤醒的你能立刻接上当初约定的意图
                reason = session.scheduled_reason or "预约的主动思考时间到了。"
                allow_cold_start = self._config.proactive.wake_cold_stream
                session.scheduled_at = None
                session.scheduled_reason = ""
            elif (
                session.waiting_until is not None
                and now >= session.waiting_until
                and self._is_proactive_allowed(chat_type)
            ):
                trigger_type = "wait_timeout"
                reason = session.waiting_reason or "回复等待超时。"
                allow_cold_start = False
                session.waiting_until = None
                session.waiting_reason = ""
                session.waiting_expected_reaction = ""
                session.consecutive_timeout_count += 1
            elif self._should_trigger_silence(now, session.last_activity_at, session.last_proactive_at):
                trigger_type = "silence"
                # 中性触发：只是突然想起这个聊天流，回来看一眼；有值得说的才说
                reason = "突然想起这个聊天流，回来看一眼。"
                allow_cold_start = False

            if not trigger_type:
                return

            session.mark_trigger(trigger_type, reason)
            await self._store.save(session)

        success = await wake_stream(
            session,
            trigger_type=trigger_type,
            reason=reason,
            config=self._config,
            allow_cold_start=allow_cold_start,
        )
        if success:
            logger.info(f"NDFC Kokoro Bridge 已唤醒 stream={stream_id[:8]} trigger={trigger_type}")

        # 触发后预约可能已被清空，重新同步长期状态到 system reminder
        sync: Callable[[str], Awaitable[object]] | None = getattr(
            self._state_service, "sync_long_term_reminder", None
        )
        if sync is not None:
            await sync(stream_id)

    def _should_trigger_silence(
        self,
        now: float,
        last_activity_at: float,
        last_proactive_at: float | None,
    ) -> bool:
        """判断是否满足沉默主动触发条件。"""
        config = self._config.proactive
        if not config.enabled:
            return False
        if self._is_quiet_hours():
            return False
        if now - last_activity_at < config.silence_threshold:
            return False
        if last_proactive_at is not None and now - last_proactive_at < config.min_interval:
            return False
        probability = max(0.0, min(1.0, float(config.trigger_probability)))
        return random.random() <= probability

    def _is_quiet_hours(self) -> bool:
        """检查当前是否处于沉默触发勿扰时段。"""
        config = self._config.proactive
        try:
            current = time.localtime()
            current_minutes = current.tm_hour * 60 + current.tm_min
            start_h, start_m = config.quiet_hours_start.split(":", 1)
            end_h, end_m = config.quiet_hours_end.split(":", 1)
            start_minutes = int(start_h) * 60 + int(start_m)
            end_minutes = int(end_h) * 60 + int(end_m)
        except (ValueError, IndexError):
            return False

        if start_minutes <= end_minutes:
            return start_minutes <= current_minutes < end_minutes
        return current_minutes >= start_minutes or current_minutes < end_minutes
