"""NDFC Kokoro Bridge 状态模型。

本模块定义插件维护的私聊运行状态，包括回复等待、预约思考、
主动触发记录和近期心理活动流。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class MentalEntry:
    """单条心理活动或运行事件记录。"""

    event_type: str
    content: str
    timestamp: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。

        Returns:
            可写入 JSON 的字典。
        """
        return {
            "event_type": self.event_type,
            "content": self.content,
            "timestamp": self.timestamp,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MentalEntry":
        """从字典反序列化心理活动记录。

        Args:
            data: JSON 字典。

        Returns:
            MentalEntry 实例。
        """
        metadata = data.get("metadata", {})
        if not isinstance(metadata, dict):
            metadata = {}
        return cls(
            event_type=str(data.get("event_type", "unknown")),
            content=str(data.get("content", "")),
            timestamp=float(data.get("timestamp", time.time())),
            metadata=metadata,
        )


@dataclass
class BridgeSession:
    """单个聊天流的 NDFC Kokoro Bridge 运行状态。"""

    stream_id: str
    platform: str = ""
    user_id: str = ""
    created_at: float = field(default_factory=time.time)
    last_activity_at: float = field(default_factory=time.time)
    last_user_message_at: float | None = None
    last_bot_message_at: float | None = None
    last_proactive_at: float | None = None
    waiting_until: float | None = None
    waiting_reason: str = ""
    waiting_expected_reaction: str = ""
    consecutive_timeout_count: int = 0
    scheduled_at: float | None = None
    scheduled_reason: str = ""
    trigger_reason: str = ""
    trigger_type: str = ""
    trigger_at: float | None = None
    mental_entries: list[MentalEntry] = field(default_factory=list)

    def is_waiting(self, now: float | None = None) -> bool:
        """判断当前是否处于回复等待期。

        Args:
            now: 当前 Unix 时间戳，留空则使用当前时间。

        Returns:
            等待状态是否仍然有效。
        """
        if self.waiting_until is None:
            return False
        return (now or time.time()) < self.waiting_until

    def set_waiting(
        self,
        seconds: float,
        reason: str = "",
        expected_reaction: str = "",
    ) -> None:
        """设置或清除回复等待状态。

        Args:
            seconds: 等待秒数；小于等于 0 时清除等待。
            reason: 等待理由。
            expected_reaction: 期待对方如何回应。
        """
        if seconds <= 0:
            self.clear_waiting()
            return
        now = time.time()
        self.waiting_until = now + seconds
        self.waiting_reason = reason
        self.waiting_expected_reaction = expected_reaction
        self.last_activity_at = now

    def clear_waiting(self) -> None:
        """清除回复等待状态。"""
        self.waiting_until = None
        self.waiting_reason = ""
        self.waiting_expected_reaction = ""
        self.last_activity_at = time.time()

    def set_scheduled(self, at: float | None, reason: str = "") -> None:
        """设置或清除预约思考状态。

        Args:
            at: 预约触发时间；None 表示清除预约。
            reason: 预约理由。
        """
        self.scheduled_at = at
        self.scheduled_reason = reason if at is not None else ""
        self.last_activity_at = time.time()

    def mark_trigger(self, trigger_type: str, reason: str) -> None:
        """记录最近一次系统触发原因。

        Args:
            trigger_type: 触发类型。
            reason: 触发理由。
        """
        now = time.time()
        self.trigger_type = trigger_type
        self.trigger_reason = reason
        self.trigger_at = now
        self.last_activity_at = now
        if trigger_type in {"scheduled", "silence", "wait_timeout"}:
            self.last_proactive_at = now

    def add_entry(self, entry: MentalEntry, max_entries: int) -> None:
        """追加心理活动并裁剪最大长度。

        Args:
            entry: 待追加记录。
            max_entries: 保留的最大记录数量。
        """
        self.mental_entries.append(entry)
        if max_entries > 0 and len(self.mental_entries) > max_entries:
            self.mental_entries = self.mental_entries[-max_entries:]

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。

        Returns:
            可写入 JSON 的会话状态字典。
        """
        return {
            "stream_id": self.stream_id,
            "platform": self.platform,
            "user_id": self.user_id,
            "created_at": self.created_at,
            "last_activity_at": self.last_activity_at,
            "last_user_message_at": self.last_user_message_at,
            "last_bot_message_at": self.last_bot_message_at,
            "last_proactive_at": self.last_proactive_at,
            "consecutive_timeout_count": self.consecutive_timeout_count,
            "scheduled_at": self.scheduled_at,
            "scheduled_reason": self.scheduled_reason,
            "trigger_reason": self.trigger_reason,
            "trigger_type": self.trigger_type,
            "trigger_at": self.trigger_at,
            "mental_entries": [entry.to_dict() for entry in self.mental_entries],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], max_entries: int = 50) -> "BridgeSession":
        """从 JSON 字典反序列化会话状态。

        Args:
            data: JSON 字典。
            max_entries: 心理活动最大条目数。

        Returns:
            BridgeSession 实例。
        """
        session = cls(
            stream_id=str(data.get("stream_id", "")),
            platform=str(data.get("platform", "")),
            user_id=str(data.get("user_id", "")),
        )
        session.created_at = float(data.get("created_at", time.time()))
        session.last_activity_at = float(data.get("last_activity_at", time.time()))
        session.last_user_message_at = _optional_float(data.get("last_user_message_at"))
        session.last_bot_message_at = _optional_float(data.get("last_bot_message_at"))
        session.last_proactive_at = _optional_float(data.get("last_proactive_at"))
        session.consecutive_timeout_count = int(data.get("consecutive_timeout_count", 0))
        session.scheduled_at = _optional_float(data.get("scheduled_at"))
        session.scheduled_reason = str(data.get("scheduled_reason", ""))
        session.trigger_reason = str(data.get("trigger_reason", ""))
        session.trigger_type = str(data.get("trigger_type", ""))
        session.trigger_at = _optional_float(data.get("trigger_at"))
        raw_entries = data.get("mental_entries", [])
        if isinstance(raw_entries, list):
            session.mental_entries = [
                MentalEntry.from_dict(item)
                for item in raw_entries
                if isinstance(item, dict)
            ]
        if max_entries > 0 and len(session.mental_entries) > max_entries:
            session.mental_entries = session.mental_entries[-max_entries:]
        return session


def _optional_float(value: Any) -> float | None:
    """把可选值转换为 float。

    Args:
        value: 输入值。

    Returns:
        转换后的 float，无法转换或为空时返回 None。
    """
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
