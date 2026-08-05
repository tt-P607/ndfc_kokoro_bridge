"""NDFC Kokoro Bridge 插件入口。

插件以旁路形式增强 neo_default_chatter 的私聊体验：提供内心记录、回复等待、
预约思考、运行时状态注入和主动唤醒能力，不修改框架源码和 neo_default_chatter。
"""

from __future__ import annotations

from src.app.plugin_system.api.action_api import clear_schema_cache
from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.base import BasePlugin, register_plugin

from .actions.schedule_thought import ScheduleThoughtAction
from .actions.set_reply_wait import SetReplyWaitAction
from .config import NDFCKokoroBridgeConfig
from .handlers.message_observer import NDFCKokoroMessageObserver
from .handlers.prompt_state_injector import NDFCKokoroPromptStateInjector
from .scheduler import NDFCKokoroScheduler
from .services.state_service import NDFCKokoroStateService
from .store import BridgeSessionStore

logger = get_logger("ndfc_kokoro_bridge.plugin")


@register_plugin
class NDFCKokoroBridgePlugin(BasePlugin):
    """NeoDefaultChatter 私聊心理增强桥接插件。"""

    plugin_name: str = "ndfc_kokoro_bridge"
    plugin_description: str = "为 neo_default_chatter 提供 KFC 风格的私聊心理状态、回复等待、预约思考与主动唤醒"
    plugin_version: str = "1.0.0"

    configs: list[type] = [NDFCKokoroBridgeConfig]
    dependent_components: list[str] = ["neo_default_chatter:chatter:neo_default_chatter"]

    session_store: BridgeSessionStore
    scheduler: NDFCKokoroScheduler

    def __init__(self, config: object | None = None) -> None:
        """初始化插件实例。

        Args:
            config: 插件配置实例。
        """
        super().__init__(config)  # type: ignore[arg-type]
        effective_config = self.get_config()
        self.session_store = BridgeSessionStore(max_entries=effective_config.mental.max_log_entries)
        self.scheduler = NDFCKokoroScheduler(effective_config, self.session_store)
        self._sync_action_descriptions(effective_config)

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
            ScheduleThoughtAction,
            SetReplyWaitAction,
            NDFCKokoroPromptStateInjector,
            NDFCKokoroMessageObserver,
            NDFCKokoroStateService,
        ]

    async def on_plugin_loaded(self) -> None:
        """插件加载后刷新 Action 文案并启动调度检查。"""
        config = self.get_config()
        if not config.plugin.enabled:
            return
        self._sync_action_descriptions(config)
        await self.scheduler.start()

    def _sync_action_descriptions(self, config: NDFCKokoroBridgeConfig) -> None:
        """把配置中的工具提示词同步到 Action 类描述并清理 schema 缓存。"""
        SetReplyWaitAction.description = config.action_prompt.set_reply_wait_description
        schedule_parts = [config.action_prompt.schedule_thought_description.strip()]
        guidance = config.action_prompt.schedule_guidance.strip()
        if guidance:
            schedule_parts.append(guidance)
        ScheduleThoughtAction.description = "\n\n".join(part for part in schedule_parts if part)
        clear_schema_cache("ndfc_kokoro_bridge:action:set_reply_wait")
        clear_schema_cache("ndfc_kokoro_bridge:action:schedule_thought")

    async def on_plugin_unloaded(self) -> None:
        """插件卸载时移除调度任务。"""
        await self.scheduler.stop()
