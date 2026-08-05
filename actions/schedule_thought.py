"""设置 NDFC 主动思考预约的 Action。

该 Action 允许 neo_default_chatter 预约未来某个时间点重新思考或主动联系，
新的预约会覆盖旧预约，delay_minutes=0 可取消当前预约。
"""

from __future__ import annotations

from typing import Annotated

from src.app.plugin_system.base import BaseAction
from src.app.plugin_system.types import ChatType

from ..config import NDFCKokoroBridgeConfig
from ..services.state_service import NDFCKokoroStateService


class ScheduleThoughtAction(BaseAction):
    """预约下一次主动思考或联系。"""

    name: str = "schedule_thought"
    description: str = (
        "预约一个时间点，届时系统会主动唤醒你去思考是否发起新一轮私聊对话。"
        "新的预约会覆盖旧的预约；传 delay_minutes=0 可取消当前预约。"
        "预约不受勿扰时段限制，即使是深夜或清晨设定的预约也会如期触发。"
        "delay_minutes=0 取消预约；其他值会被限制到插件配置的允许范围内，默认 30~1440 分钟。"
        "reason 必填：记录此刻的真实想法。可以是一件具体的事，也可以只是「那个时间想找 Ta 说说话」"
        "——怎么自然怎么写，重要的是让未来的你看到时能自然接上。取消预约时可留空。"
    )
    chatter_allow: list[str] = ["neo_default_chatter"]
    associated_types: list[str] = ["text"]
    chat_type: ChatType = ChatType.PRIVATE

    async def go_activate(self) -> bool:
        """根据配置和聊天类型决定是否暴露 Action。"""
        if str(self.chat_stream.chat_type) != ChatType.PRIVATE.value:
            return False
        config = self.plugin.config
        if isinstance(config, NDFCKokoroBridgeConfig):
            return bool(config.plugin.enabled and config.proactive.enabled)
        return True

    async def execute(
        self,
        delay_minutes: Annotated[
            int,
            "多少分钟后发起主动思考。传 0 表示取消当前预约；其他值会被限制到配置允许范围内，默认 30~1440。",
        ] = 30,
        reason: Annotated[
            str,
            "此刻的真实想法：可以是一件具体的事，也可以只是「那个时间想找 Ta 说说话」。取消预约时可留空。",
        ] = "",
    ) -> tuple[bool, str]:
        """设置或取消主动思考预约。

        Args:
            delay_minutes: 延迟分钟数，0 表示取消当前预约。
            reason: 预约理由。

        Returns:
            是否成功与结果说明。
        """
        service = NDFCKokoroStateService(plugin=self.plugin)
        return await service.schedule_thought(
            stream_id=self.chat_stream.stream_id,
            delay_minutes=delay_minutes,
            reason=reason,
        )
