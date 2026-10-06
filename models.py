"""NDFC Kokoro Bridge 状态模型。

本模块定义插件维护的聊天流运行状态：回复等待、预约思考、主动触发记录、
私人备忘录与完整上下文快照。

对话消息由框架持久化；本插件额外维护 NDFC 每轮真正发送给模型的完整
payload 链（系统提示、历史、工具调用、工具结果、推理），作为可跨重启
恢复的上下文快照。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any


#: 快照条目类型常量，与 LLMPayload 角色对应。
SNAPSHOT_ROLE_SYSTEM = "system"
SNAPSHOT_ROLE_USER = "user"
SNAPSHOT_ROLE_ASSISTANT = "assistant"
SNAPSHOT_ROLE_TOOL_RESULT = "tool_result"

#: 场景常量（与框架 ChatType 取值一致）。
SCENE_PRIVATE = "private"
SCENE_GROUP = "group"
SCENE_DISCUSS = "discuss"
DEFAULT_SCENES: list[str] = [SCENE_PRIVATE, SCENE_GROUP, SCENE_DISCUSS]


#: 单流最大有效备忘条目数；超出按 ``created_at`` 升序淘汰。
MEMO_MAX_ENTRIES: int = 10

#: 未指定 ``expire_hours`` 时的默认过期时长。
MEMO_DEFAULT_EXPIRE_HOURS: float = 24.0

#: 允许的最小过期时长。
MEMO_MIN_EXPIRE_HOURS: float = 1.0

#: 允许的最大过期时长（14 天）。
MEMO_MAX_EXPIRE_HOURS: float = 336.0

#: 备忘 id 的短 hex 长度，便于 LLM 在对话中引用。
MEMO_ID_LENGTH: int = 6


@dataclass
class Memo:
    """一条带过期时间的私人备忘录。

    定位为 LLM 显式标记的中短期关键事项，覆盖"接下来一段时间需要明确
    意识到的事"这一语义层。
    """

    content: str = ""
    intent: str = ""
    created_at: float = 0.0
    expires_at: float = 0.0
    memo_id: str = ""

    def __post_init__(self) -> None:
        """补齐缺省的 id 与创建时间。"""
        if not self.memo_id:
            self.memo_id = uuid.uuid4().hex[:MEMO_ID_LENGTH]
        if self.created_at <= 0:
            self.created_at = time.time()

    def is_expired(self, now: float | None = None) -> bool:
        """判断该备忘是否已过期。

        Args:
            now: 当前 Unix 时间戳，留空使用当前时间。

        Returns:
            是否已过期。
        """
        current = now if now is not None else time.time()
        return self.expires_at > 0 and current >= self.expires_at

    def remaining_seconds(self, now: float | None = None) -> float:
        """返回剩余秒数；已过期返回 0。

        Args:
            now: 当前 Unix 时间戳，留空使用当前时间。

        Returns:
            剩余秒数。
        """
        current = now if now is not None else time.time()
        return max(0.0, self.expires_at - current)

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。

        Returns:
            可写入 JSON 的字典。
        """
        return {
            "memo_id": self.memo_id,
            "content": self.content,
            "intent": self.intent,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Memo":
        """从字典反序列化。

        Args:
            data: JSON 字典。

        Returns:
            Memo 实例。
        """
        return cls(
            memo_id=str(data.get("memo_id", "") or ""),
            content=str(data.get("content", "") or ""),
            intent=str(data.get("intent", "") or ""),
            created_at=float(data.get("created_at", 0.0) or 0.0),
            expires_at=float(data.get("expires_at", 0.0) or 0.0),
        )


def clamp_expire_hours(raw: float) -> float:
    """把 LLM 给出的 ``expire_hours`` 夹到合法区间。

    Args:
        raw: LLM 传入的原始小时数。

    Returns:
        位于 ``[MEMO_MIN_EXPIRE_HOURS, MEMO_MAX_EXPIRE_HOURS]``
        的时长；非数值或非正数时回退到默认值。
    """
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return MEMO_DEFAULT_EXPIRE_HOURS
    if value <= 0:
        return MEMO_DEFAULT_EXPIRE_HOURS
    return max(MEMO_MIN_EXPIRE_HOURS, min(value, MEMO_MAX_EXPIRE_HOURS))


@dataclass
class SnapshotEntry:
    """单条上下文快照条目，对应一个 LLM payload。

    ``media`` 保存快照捕获时刻 payload 中真实存在的媒体（base64 数据、
    类型与 MIME），用于完整还原多模态上下文；框架压缩后媒体已被丢弃时
    该项为空。
    """

    role: str
    content: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    call_id: str = ""
    media: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。

        Returns:
            可写入 JSON 的字典。
        """
        return {
            "role": self.role,
            "content": self.content,
            "tool_calls": self.tool_calls,
            "call_id": self.call_id,
            "media": self.media,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SnapshotEntry":
        """从字典反序列化。

        Args:
            data: JSON 字典。

        Returns:
            SnapshotEntry 实例。
        """
        return cls(
            role=str(data.get("role", "")),
            content=str(data.get("content", "") or ""),
            tool_calls=data.get("tool_calls", []) or [],
            call_id=str(data.get("call_id", "") or ""),
            media=data.get("media", []) or [],
        )


@dataclass
class ContextSnapshot:
    """单个聊天流的完整上下文快照。

    镜像 NDFC 最近一次发送给模型的完整 payload 链（系统提示、对话历史、
    工具调用、工具结果、推理）。每次 LLM 请求前覆盖式更新，随框架上下文
    压缩同步变化；持久化于磁盘，供重启后恢复。

    ``context_cleared_at`` 保存捕获时的官方清空边界；恢复时必须与数据库
    当前边界一致，避免清空前的请求在清空后落盘并重新恢复。
    """

    stream_id: str
    updated_at: float = field(default_factory=time.time)
    version: int = 1
    entries: list[SnapshotEntry] = field(default_factory=list)
    context_cleared_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。

        Returns:
            可写入 JSON 的字典。
        """
        return {
            "stream_id": self.stream_id,
            "updated_at": self.updated_at,
            "version": self.version,
            "entries": [entry.to_dict() for entry in self.entries],
            "context_cleared_at": self.context_cleared_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ContextSnapshot":
        """从字典反序列化。

        Args:
            data: JSON 字典。

        Returns:
            ContextSnapshot 实例。
        """
        raw_entries = data.get("entries", []) or []
        entries = [
            SnapshotEntry.from_dict(item)
            for item in raw_entries
            if isinstance(item, dict)
        ]
        return cls(
            stream_id=str(data.get("stream_id", "")),
            updated_at=float(data.get("updated_at", time.time())),
            version=int(data.get("version", 1)),
            entries=entries,
            context_cleared_at=float(data.get("context_cleared_at", 0.0) or 0.0),
        )


@dataclass
class BridgeSession:
    """单个聊天流的 NDFC Kokoro Bridge 运行状态。"""

    stream_id: str
    platform: str = ""
    user_id: str = ""
    chat_type: str = ""
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
    memos: list[Memo] = field(default_factory=list)

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

    # ── 备忘录管理 ──────────────────────────────────────────

    def prune_expired_memos(self) -> list[Memo]:
        """删除已过期备忘并返回被删除的列表。

        Returns:
            被删除的 Memo 列表。
        """
        if not self.memos:
            return []
        now = time.time()
        expired = [memo for memo in self.memos if memo.is_expired(now)]
        if expired:
            self.memos = [memo for memo in self.memos if not memo.is_expired(now)]
        return expired

    def upsert_memo(self, memo: Memo) -> tuple[Memo, bool]:
        """写入或刷新一条备忘。

        若已存在 ``content`` 完全相同的有效备忘，只刷新过期时间和 intent
        （保留原 ``created_at``）；否则在必要时淘汰最早的一条后追加。

        Args:
            memo: 待写入的备忘。

        Returns:
            tuple: ``(最终落盘的备忘, 是否为新建)``。
        """
        self.prune_expired_memos()

        normalized_content = memo.content.strip()
        if normalized_content:
            for existing in self.memos:
                if existing.content.strip() == normalized_content:
                    existing.expires_at = memo.expires_at
                    if memo.intent.strip():
                        existing.intent = memo.intent
                    return existing, False

        while len(self.memos) >= MEMO_MAX_ENTRIES:
            oldest_index = min(
                range(len(self.memos)),
                key=lambda index: self.memos[index].created_at,
            )
            self.memos.pop(oldest_index)

        self.memos.append(memo)
        return memo, True

    def delete_memos(self, memo_ids: list[str]) -> list[Memo]:
        """按 id 删除备忘。

        Args:
            memo_ids: 待删除的备忘 id 列表。

        Returns:
            list[Memo]: 实际被删除的备忘。
        """
        if not memo_ids or not self.memos:
            return []
        target_ids = set(memo_ids)
        deleted = [memo for memo in self.memos if memo.memo_id in target_ids]
        if deleted:
            self.memos = [
                memo for memo in self.memos if memo.memo_id not in target_ids
            ]
        return deleted

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。

        Returns:
            可写入 JSON 的会话状态字典。
        """
        return {
            "stream_id": self.stream_id,
            "platform": self.platform,
            "user_id": self.user_id,
            "chat_type": self.chat_type,
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
            "memos": [memo.to_dict() for memo in self.memos],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BridgeSession":
        """从 JSON 字典反序列化会话状态。

        Args:
            data: JSON 字典。

        Returns:
            BridgeSession 实例。
        """
        session = cls(
            stream_id=str(data.get("stream_id", "")),
            platform=str(data.get("platform", "")),
            user_id=str(data.get("user_id", "")),
            chat_type=str(data.get("chat_type", "")),
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
        raw_memos = data.get("memos", []) or []
        if isinstance(raw_memos, list):
            session.memos = [
                Memo.from_dict(item)
                for item in raw_memos
                if isinstance(item, dict)
            ]
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
