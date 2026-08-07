"""NDFC Kokoro Bridge 插件入口。

插件以旁路形式增强 neo_default_chatter 的多场景体验：提供上下文完整快照、
私人备忘录、回复等待、预约思考、运行时状态注入和主动唤醒能力，
不修改框架源码和 neo_default_chatter。
"""

from __future__ import annotations

from src.app.plugin_system.api.action_api import clear_schema_cache
from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.base import BasePlugin, register_plugin

from .actions.memo import MemoAction, MemoDeleteAction
from .actions.schedule_thought import ScheduleThoughtAction, ScheduleThoughtGroupAction
from .actions.set_reply_wait import SetReplyWaitAction
from .config import NDFCKokoroBridgeConfig
from .handlers.chatter_bound_observer import NDFCKokoroChatterBoundObserver
from .handlers.context_snapshot_handler import NDFCKokoroContextSnapshotHandler
from .handlers.message_observer import NDFCKokoroMessageObserver
from .handlers.prompt_state_injector import NDFCKokoroPromptStateInjector
from .scheduler import NDFCKokoroScheduler
from .services.state_service import NDFCKokoroStateService
from .snapshot_store import SnapshotStore
from .store import BridgeSessionStore

logger = get_logger("ndfc_kokoro_bridge.plugin")


@register_plugin
class NDFCKokoroBridgePlugin(BasePlugin):
    """NeoDefaultChatter 全场景增强扩展插件。"""

    plugin_name: str = "ndfc_kokoro_bridge"
    plugin_description: str = "为 neo_default_chatter 提供完整上下文快照、备忘录、回复等待、预约思考与主动唤醒"
    plugin_version: str = "2.2.0"

    configs: list[type] = [NDFCKokoroBridgeConfig]
    dependent_components: list[str] = ["neo_default_chatter:chatter:neo_default_chatter"]

    session_store: BridgeSessionStore
    snapshot_store: SnapshotStore
    scheduler: NDFCKokoroScheduler

    def __init__(self, config: object | None = None) -> None:
        """初始化插件实例。

        Args:
            config: 插件配置实例。
        """
        super().__init__(config)  # type: ignore[arg-type]
        effective_config = self.get_config()
        self.session_store = BridgeSessionStore(max_entries=effective_config.mental.max_log_entries)
        self.snapshot_store = SnapshotStore()
        #: 已被 NDFC 生命周期事件确认为 NDFC 驱动的聊天流集合；
        #: 消息观察器与调度器仅处理集合内流，避免误处理其他 chatter 的会话。
        self.ndfc_streams: set[str] = set()
        self.scheduler = NDFCKokoroScheduler(
            effective_config,
            self.session_store,
            is_ndfc_stream=self.is_ndfc_stream,
        )
        self._sync_action_descriptions(effective_config)

    def mark_ndfc_stream(self, stream_id: str) -> None:
        """登记一个由 NDFC 驱动的聊天流。

        由 NDFC 专属生命周期事件（before_llm_request / on_prompt_build）调用，
        这些事件只有 NDFC 流会触发，因此集合内只会出现 NDFC 流。

        Args:
            stream_id: 聊天流 ID。
        """
        if stream_id:
            self.ndfc_streams.add(stream_id)

    def is_ndfc_stream(self, stream_id: str) -> bool:
        """判断指定流是否已登记为 NDFC 驱动。

        Args:
            stream_id: 聊天流 ID。

        Returns:
            是否为已登记的 NDFC 流。
        """
        return stream_id in self.ndfc_streams

    def get_config(self) -> NDFCKokoroBridgeConfig:
        """获取有效配置对象，供本插件所有组件统一调用。"""
        if isinstance(self.config, NDFCKokoroBridgeConfig):
            return self.config
        return NDFCKokoroBridgeConfig()

    def get_components(self) -> list[type]:
        """返回插件包含的组件类。"""
        config = self.get_config()
        if not config.plugin.enabled:
            logger.info("ndfc_kokoro_bridge 已在配置中禁用")
            return []

        return [
            MemoAction,
            MemoDeleteAction,
            ScheduleThoughtAction,
            ScheduleThoughtGroupAction,
            SetReplyWaitAction,
            NDFCKokoroContextSnapshotHandler,
            NDFCKokoroPromptStateInjector,
            NDFCKokoroMessageObserver,
            NDFCKokoroChatterBoundObserver,
            NDFCKokoroStateService,
        ]

    async def on_plugin_loaded(self) -> None:
        """插件加载后刷新 Action 文案并启动调度检查。"""
        config = self.get_config()
        if not config.plugin.enabled:
            return
        self._sync_action_descriptions(config)
        # 注入状态服务引用，供调度器在触发后同步长期 system reminder
        self.scheduler._state_service = NDFCKokoroStateService(plugin=self)
        await self.scheduler.start()

    def _sync_action_descriptions(self, config: NDFCKokoroBridgeConfig) -> None:
        """把配置中的工具提示词同步到 Action 类描述并清理 schema 缓存。"""
        SetReplyWaitAction.description = config.action_prompt.set_reply_wait_description

        private_parts = [config.action_prompt.schedule_thought_description.strip()]
        private_guidance = config.action_prompt.schedule_guidance.strip()
        if private_guidance:
            private_parts.append(private_guidance)
        ScheduleThoughtAction.description = "\n\n".join(part for part in private_parts if part)

        group_parts = [config.action_prompt.schedule_thought_group_description.strip()]
        group_guidance = config.action_prompt.schedule_guidance_group.strip()
        if group_guidance:
            group_parts.append(group_guidance)
        ScheduleThoughtGroupAction.description = "\n\n".join(part for part in group_parts if part)

        MemoAction.description = config.action_prompt.memo_description
        MemoDeleteAction.description = config.action_prompt.memo_delete_description
        for signature in (
            "ndfc_kokoro_bridge:action:set_reply_wait",
            "ndfc_kokoro_bridge:action:schedule_thought",
            "ndfc_kokoro_bridge:action:schedule_thought_group",
            "ndfc_kokoro_bridge:action:memo",
            "ndfc_kokoro_bridge:action:memo_delete",
        ):
            clear_schema_cache(signature)

    async def on_plugin_unloaded(self) -> None:
        """插件卸载时移除调度任务。"""
        await self.scheduler.stop()
