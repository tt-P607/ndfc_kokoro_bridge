"""NDFC Kokoro Bridge 调度封装。

本模块集中封装 src.kernel.scheduler 的使用，周期检查预约思考、回复等待超时
和沉默主动触发，并通过 wake.py 唤醒 neo_default_chatter 所在流。
"""

from __future__ import annotations

import random
import time

from src.app.plugin_system.api.log_api import get_logger
from src.kernel.scheduler import TriggerType, get_unified_scheduler

from .config import NDFCKokoroBridgeConfig
from .models import MentalEntry
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
    ) -> None:
        """初始化调度器封装。

        Args:
            config: 插件配置。
            store: 会话存储。
        """
        self._config = config
        self._store = store

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
            await self._check_one(stream_id)

    async def _check_one(self, stream_id: str) -> None:
        """检查单个会话。"""
        allow_cold_start = True
        async with self._store.lock(stream_id):
            session = await self._store.get(stream_id)
            if session is None:
                return

            now = time.time()
            trigger_type = ""
            reason = ""

            if session.scheduled_at is not None and now >= session.scheduled_at:
                trigger_type = "scheduled"
                reason = session.scheduled_reason or "预约主动思考到期。"
                allow_cold_start = self._config.proactive.wake_cold_stream
                session.scheduled_at = None
                session.scheduled_reason = ""
            elif session.waiting_until is not None and now >= session.waiting_until:
                trigger_type = "wait_timeout"
                reason = session.waiting_reason or "回复等待超时。"
                allow_cold_start = False
                session.waiting_until = None
                session.waiting_reason = ""
                session.waiting_expected_reaction = ""
                session.consecutive_timeout_count += 1
            elif self._should_trigger_silence(now, session.last_activity_at, session.last_proactive_at):
                trigger_type = "silence"
                reason = "私聊已经沉默较久，触发一次主动思考。"
                allow_cold_start = False

            if not trigger_type:
                return

            session.mark_trigger(trigger_type, reason)
            session.add_entry(
                MentalEntry(
                    event_type=f"{trigger_type}_trigger",
                    content=reason,
                    metadata={"trigger_type": trigger_type},
                ),
                max_entries=self._config.mental.max_log_entries,
            )
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
