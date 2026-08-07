"""设置 NDFC 主动思考预约的 Action（私聊版与群聊版）。

该模块提供两套预约思考工具，按聊天场景分流：

- :class:`ScheduleThoughtAction`：私聊版，默认开启。针对一对一关系，提示词
  强调「与 Ta 约定某事 / 某时机重新想起对方」。
- :class:`ScheduleThoughtGroupAction`：群聊版，默认关闭，需在配置中手动开启。
  针对群聊/讨论组，提示词强调「与群里的人约定某事 / 某个话题值得回来看」。

两个 Action 都在「约定了一件事」或「认定某个时机需要重新唤醒自己」时才设置，
到期时 reason 会原样投回给模型。新的预约会覆盖旧预约，delay_minutes=0 可取消。
"""

from __future__ import annotations

from typing import Annotated

from src.app.plugin_system.base import BaseAction
from src.app.plugin_system.types import ChatType

from ..config import NDFCKokoroBridgeConfig
from ..models import SCENE_GROUP, SCENE_PRIVATE
from ..services.state_service import NDFCKokoroStateService

#: 私聊版预约思考的工具描述（配置可覆盖）。
PRIVATE_SCHEDULE_DESCRIPTION: str = (
    "维护你和 Ta 之间的下一次主动联系预约，这是一段需要时常照看的长期状态，而不只是一次性的临时提醒。"
    "在私聊里，它是保持这段关系自然延续的主要方式：对话告一段落后，安排一个合适的时机重新想起 Ta、续上话题或主动联系。"
    "你可以根据关系进展随时检查现有预约是否仍然合适：合适就保持，不合适就覆盖成新的想法。"
    "设置时写清 reason：你在等什么、想续什么话题、或为什么那个时间值得再联系 Ta。"
    "这个 reason 会在预约到期唤醒你时原样投回给你，让未来的你能立刻接上。"
    "新的预约会覆盖旧的预约；传 delay_minutes=0 可取消当前预约（取消时才可留空 reason）。"
    "预约不受勿扰时段限制，即使是深夜或清晨设定的预约也会如期触发。"
    "delay_minutes=0 取消预约；其他值会被限制到插件配置的允许范围内，默认 30~1440 分钟。"
    "预约是稳定、可控的联系节律；但稳定不等于频繁更新，优先维护一个清晰、合适的预约。"
)

#: 群聊版预约思考的工具描述（配置可覆盖）。
GROUP_SCHEDULE_DESCRIPTION: str = (
    "预约是在「约定了一件事」或「认定某个时机需要重新回来看一眼这个群」时才设置的主动提醒。"
    "设置时必须写清 reason：你约定了什么、在等什么、或为什么那个时间值得再回来看一眼；"
    "这个 reason 会在预约到期唤醒你时原样投回给你。"
    "新的预约会覆盖旧的预约；传 delay_minutes=0 可取消当前预约（取消时才可留空 reason）。"
    "预约不受勿扰时段限制，即使是深夜或清晨设定的预约也会如期触发。"
    "delay_minutes=0 取消预约；其他值会被限制到插件配置的允许范围内，默认 30~1440 分钟。"
)


def _scene_enabled(
    config: NDFCKokoroBridgeConfig,
    scene: str,
    is_group: bool,
) -> bool:
    """判断预约思考在当前场景是否可用。

    Args:
        config: 插件配置。
        scene: 当前聊天场景标识。
        is_group: 是否为群聊/讨论组场景。

    Returns:
        场景是否启用预约思考。
    """
    if not config.plugin.enabled or not config.proactive.enabled:
        return False
    if not config.plugin.is_scene_enabled(scene):
        return False
    if is_group:
        # 群聊版：仅在群聊/讨论组场景启用，且需 group_enabled
        if scene not in (SCENE_GROUP, "discuss"):
            return False
        return config.proactive.group_enabled
    # 私聊版：仅私聊场景启用，且需 private_enabled
    if scene != SCENE_PRIVATE:
        return False
    return config.proactive.private_enabled


def _current_scene(chat_stream: object) -> str:
    """从聊天流提取场景标识。

    Args:
        chat_stream: 聊天流对象。

    Returns:
        场景标识字符串，缺省视为私聊。
    """
    scene = str(getattr(chat_stream, "chat_type", "") or "")
    scene = getattr(scene, "value", scene) if hasattr(scene, "value") else scene
    return scene


class ScheduleThoughtAction(BaseAction):
    """预约下一次主动思考或联系（私聊版）。"""

    name: str = "schedule_thought"
    description: str = PRIVATE_SCHEDULE_DESCRIPTION
    chatter_allow: list[str] = ["neo_default_chatter"]
    associated_types: list[str] = ["text"]
    chat_type: ChatType = ChatType.PRIVATE

    async def go_activate(self) -> bool:
        """根据配置和聊天类型决定是否暴露私聊版 Action。"""
        scene = _current_scene(self.chat_stream)
        config = self.plugin.config
        if isinstance(config, NDFCKokoroBridgeConfig):
            return _scene_enabled(config, scene, is_group=False)
        return True

    async def execute(
        self,
        delay_minutes: Annotated[
            int,
            "多少分钟后发起主动思考。传 0 表示取消当前预约；其他值会被限制到配置允许范围内，默认 30~1440。",
        ] = 30,
        reason: Annotated[
            str,
            "预约理由（必填）：你和 Ta 约定了什么、在等什么、或为什么那个时间值得再回来看一眼。"
            "到期唤醒时这个理由会原样投回给你。取消预约时可留空。",
        ] = "",
    ) -> tuple[bool, str]:
        """设置或取消主动思考预约。

        Args:
            delay_minutes: 延迟分钟数，0 表示取消预约。
            reason: 预约理由，到期时投回。

        Returns:
            是否成功与结果说明。
        """
        service = NDFCKokoroStateService(plugin=self.plugin)
        return await service.schedule_thought(
            stream_id=self.chat_stream.stream_id,
            delay_minutes=delay_minutes,
            reason=reason,
        )


class ScheduleThoughtGroupAction(BaseAction):
    """预约下一次主动思考或回看群聊（群聊版）。"""

    name: str = "schedule_thought_group"
    description: str = GROUP_SCHEDULE_DESCRIPTION
    chatter_allow: list[str] = ["neo_default_chatter"]
    associated_types: list[str] = ["text"]
    chat_type: ChatType = ChatType.GROUP

    async def go_activate(self) -> bool:
        """根据配置和聊天类型决定是否暴露群聊版 Action。"""
        scene = _current_scene(self.chat_stream)
        config = self.plugin.config
        if isinstance(config, NDFCKokoroBridgeConfig):
            # 群聊版在 group 与 discuss 场景都可暴露
            if scene not in (SCENE_GROUP, "discuss"):
                return False
            return _scene_enabled(config, scene, is_group=True)
        return True

    async def execute(
        self,
        delay_minutes: Annotated[
            int,
            "多少分钟后发起主动思考。传 0 表示取消当前预约；其他值会被限制到配置允许范围内，默认 30~1440。",
        ] = 30,
        reason: Annotated[
            str,
            "预约理由（必填）：你和群里约定了什么、在等什么、或为什么那个时间值得再回来看一眼。"
            "到期唤醒时这个理由会原样投回给你。取消预约时可留空。",
        ] = "",
    ) -> tuple[bool, str]:
        """设置或取消群聊主动思考预约。

        Args:
            delay_minutes: 延迟分钟数，0 表示取消预约。
            reason: 预约理由，到期时投回。

        Returns:
            是否成功与结果说明。
        """
        service = NDFCKokoroStateService(plugin=self.plugin)
        return await service.schedule_thought(
            stream_id=self.chat_stream.stream_id,
            delay_minutes=delay_minutes,
            reason=reason,
        )


__all__ = [
    "ScheduleThoughtAction",
    "ScheduleThoughtGroupAction",
    "PRIVATE_SCHEDULE_DESCRIPTION",
    "GROUP_SCHEDULE_DESCRIPTION",
]
