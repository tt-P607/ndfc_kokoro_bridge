"""设置 NDFC 回复等待状态的 Action。

该 Action 记录"发完消息后是否等待对方回复"的心理等待状态。
等待的"一对一"语义在私聊外（群聊/讨论组）受 ``private_only`` 配置控制：
开启时仅在私聊暴露，关闭时全场景可用。
"""

from __future__ import annotations

from typing import Annotated

from src.app.plugin_system.base import BaseAction
from src.app.plugin_system.types import ChatType

from ..config import NDFCKokoroBridgeConfig
from ..models import SCENE_PRIVATE
from ..services.state_service import NDFCKokoroStateService


class SetReplyWaitAction(BaseAction):
    """登记发完消息后的回复等待状态。"""

    name: str = "set_reply_wait"
    description: str = (
        "登记你发完消息后是否在等对方回复。"
        "期待对方很快回应时填一个短等待，例如你问了问题、聊得正起劲、或想维持当前聊天节奏。"
        "话题告一段落、已经告别、或对方不需要特别回什么时，把 seconds 设为 0。"
        "如果是想过一段时间再主动找对方，那应该使用 schedule_thought，不是这个工具。"
        "本工具只记录心理等待理由和超时状态；如需让 NDFC 控制流等待，可在同轮另行调用 pass_and_wait。"
    )
    chatter_allow: list[str] = ["neo_default_chatter"]
    associated_types: list[str] = ["text"]
    chat_type: ChatType = ChatType.ALL

    async def go_activate(self) -> bool:
        """根据配置和聊天类型决定是否暴露 Action。"""
        scene = str(getattr(self.chat_stream, "chat_type", "") or "")
        scene = getattr(scene, "value", scene) if hasattr(scene, "value") else scene
        config = self.plugin.config
        if isinstance(config, NDFCKokoroBridgeConfig):
            if not config.plugin.enabled or not config.wait.enabled:
                return False
            if not config.plugin.is_scene_enabled(str(scene)):
                return False
            if config.wait.private_only and str(scene) != SCENE_PRIVATE:
                return False
            return True
        return True

    async def execute(
        self,
        seconds: Annotated[float, "你愿意等待对方回复的最长时间（秒）；0 表示不等待或取消当前等待。"],
        thought: Annotated[
            str,
            "内心思考：记录你此刻等待或取消等待时的真实想法、心情、判断依据和后续打算，可适当详细。",
        ],
        reason: Annotated[str, "等待理由：你为什么觉得此刻应该等对方一会儿。"] = "",
        expected_reaction: Annotated[str, "你期望对方接下来如何回应。"] = "",
    ) -> tuple[bool, str]:
        """设置回复等待状态。

        Args:
            seconds: 等待秒数，0 表示取消等待。
            thought: 内心思考。
            reason: 等待理由。
            expected_reaction: 期待对方回应。

        Returns:
            是否成功与结果说明。
        """
        service = NDFCKokoroStateService(plugin=self.plugin)
        return await service.set_reply_wait(
            stream_id=self.chat_stream.stream_id,
            raw_seconds=seconds,
            reason=reason,
            expected_reaction=expected_reaction,
            thought=thought,
        )
