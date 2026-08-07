"""NDFC Kokoro Bridge 上下文快照序列化与恢复。

本模块把 NDFC 每轮发送给模型的完整 payload 链（系统提示、对话历史、
工具调用、工具结果、媒体）序列化为可持久化的 :class:`ContextSnapshot`，
并在 NDFC 重启后首次发起 LLM 请求时，把快照反序列化为合法的多角色
payload 链（user / assistant / tool_result）合并回请求，实现上下文恢复。

媒体按快照捕获时刻的真实内容完整保存（base64 数据、类型与 MIME）；
框架上下文压缩后媒体已从 payload 丢弃时，快照条目中自然不携带媒体，
与框架保持同步。

恢复上下文时输出彩色面板日志，便于人工确认恢复是否发生。
"""

from __future__ import annotations

import json
import time
from typing import Any

from src.app.plugin_system.api.log_api import get_logger, COLOR
from src.app.plugin_system.types import LLMPayload, ROLE, Text, ToolCall, ToolResult

from .models import (
    SNAPSHOT_ROLE_ASSISTANT,
    SNAPSHOT_ROLE_SYSTEM,
    SNAPSHOT_ROLE_TOOL_RESULT,
    SNAPSHOT_ROLE_USER,
    ContextSnapshot,
    SnapshotEntry,
)

#: LLM 请求中表示"工具 schema 声明"的角色，不属于对话历史，不进快照。
_SNAPSHOT_ROLE_TOOL_DECL = "tool"

logger = get_logger("ndfc_kokoro_bridge.snapshot", display="NDFC快照", color=COLOR.CYAN)

#: 媒体类型的类名集合；这些类型的 value 为 base64 字符串，应完整入快照。
_MEDIA_TYPES = {"Image", "Audio", "Video", "File"}

#: 单条内容的最大文本长度，防止异常巨型内容撑爆快照。
_MAX_ENTRY_CHARS = 4000

#: 从 meta_data 中读取 stream_id 的候选键。
_STREAM_ID_KEYS = ("stream_id", "streamId")

#: 快照条目角色到框架 ROLE 枚举的映射。
_ROLE_MAP = {
    SNAPSHOT_ROLE_SYSTEM: ROLE.SYSTEM,
    SNAPSHOT_ROLE_USER: ROLE.USER,
    SNAPSHOT_ROLE_ASSISTANT: ROLE.ASSISTANT,
    SNAPSHOT_ROLE_TOOL_RESULT: ROLE.TOOL_RESULT,
}


def extract_stream_id(meta_data: dict[str, Any] | None) -> str:
    """从请求 meta_data 中提取聊天流 ID。

    Args:
        meta_data: LLM 请求的 meta_data 字典。

    Returns:
        stream_id；无法确定时返回空串。
    """
    if not isinstance(meta_data, dict):
        return ""
    for key in _STREAM_ID_KEYS:
        value = meta_data.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def serialize_payloads(payloads: list[Any]) -> list[SnapshotEntry]:
    """把 LLM payload 列表序列化为快照条目。

    Args:
        payloads: LLM payload 列表（来自 before_llm_request 事件）。

    Returns:
        list[SnapshotEntry]: 序列化后的快照条目。
    """
    entries: list[SnapshotEntry] = []
    for payload in payloads or []:
        role = _extract_role(payload)
        if not role or role == _SNAPSHOT_ROLE_TOOL_DECL:
            # 工具 schema 声明属于运行时声明而非对话历史，跳过
            continue

        content_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        media: list[dict[str, Any]] = []

        def flush_buffered() -> None:
            """把当前累积的文本、tool_call 与媒体落为一条快照条目。"""
            if not content_parts and not tool_calls and not media:
                return
            entry = SnapshotEntry(role=role)
            if content_parts:
                entry.content = _truncate("\n".join(content_parts))
            entry.tool_calls = tool_calls
            entry.media = media
            entries.append(entry)

        for part in getattr(payload, "content", None) or []:
            part_type = type(part).__name__
            if part_type in ("Text", "ReasoningText"):
                text = _safe_text(getattr(part, "text", ""))
                if text:
                    content_parts.append(text)
            elif part_type == "ToolCall":
                tool_calls.append(_serialize_tool_call(part))
            elif part_type == "ToolResult":
                # 每个 ToolResult 独立成条：NDFC 会把同轮多个 ToolResult 合并进
                # 同一个 payload，若只保留最后一个 call_id，恢复时 assistant 的
                # tool_call 将无法全部配对，触发上下文校验错误。
                flush_buffered()
                call_id = getattr(part, "call_id", None) or ""
                value = _serialize_tool_result_value(getattr(part, "value", ""))
                entries.append(
                    SnapshotEntry(
                        role=role,
                        content=_truncate(value),
                        call_id=str(call_id),
                    )
                )
                content_parts = []
                tool_calls = []
                media = []
            elif part_type in _MEDIA_TYPES:
                # 完整保存快照捕获时刻的媒体内容（base64 数据 + 类型 + MIME），
                # 便于多模态上下文完整还原；框架压缩丢弃后此处为空。
                media.append(_serialize_media(part))
            else:
                rendered = _render_unknown_part(part)
                if rendered:
                    content_parts.append(rendered)

        flush_buffered()

    return _merge_consecutive(entries)


def capture_snapshot_from_payloads(
    stream_id: str,
    payloads: list[Any],
) -> ContextSnapshot | None:
    """从 LLM 请求 payloads 构造快照。

    Args:
        stream_id: 聊天流 ID。
        payloads: LLM 请求的 payload 列表。

    Returns:
        ContextSnapshot：序列化成功且非空时返回，否则返回 None。
    """
    if not stream_id or not payloads:
        return None

    entries = serialize_payloads(payloads)

    if not entries:
        return None

    return ContextSnapshot(
        stream_id=stream_id,
        updated_at=time.time(),
        entries=entries,
    )


def deserialize_snapshot(snapshot: ContextSnapshot) -> list[Any]:
    """把快照反序列化为可注入的多角色 LLMPayload 列表。

    恢复只保留 user / assistant / tool_result 的历史链，跳过 system
    （NDFC 会自行提供 system prompt）。反序列化前先清洗快照条目，
    保证恢复出的 payload 序列对 LLM 上下文校验合法：

    - 丢弃头部孤立的 assistant / tool_result（无 user 起始）；
    - 丢弃尾部悬挂的 tool_calls（未配对的 assistant tool_calls）；
    - 合并连续同角色文本条目。

    Args:
        snapshot: 待反序列化的快照。

    Returns:
        list[Any]: LLMPayload 列表。
    """
    entries = _sanitize_entries(list(snapshot.entries or []))
    payloads: list[Any] = []
    for entry in entries:
        role = _ROLE_MAP.get(entry.role)
        if role is None:
            continue
        if entry.tool_calls:
            calls: list[Any] = [
                ToolCall(
                    id=str(call.get("id", "") or ""),
                    name=str(call.get("name", "") or ""),
                    args=call.get("args", {}) or {},
                )
                for call in entry.tool_calls
            ]
            payloads.append(LLMPayload(role, calls))
        elif entry.role == SNAPSHOT_ROLE_TOOL_RESULT:
            payloads.append(
                LLMPayload(
                    role,
                    ToolResult(entry.content, call_id=entry.call_id or None),
                )
            )
        else:
            media_parts = _deserialize_media(entry.media)
            content: list[Any] = []
            if entry.content:
                content.append(Text(entry.content))
            content.extend(media_parts)
            if not content:
                continue
            payloads.append(LLMPayload(role, content))
    return payloads


def _sanitize_entries(entries: list[SnapshotEntry]) -> list[SnapshotEntry]:
    """清洗快照条目，保证反序列化后角色序列对 LLM 上下文校验合法。

    步骤：
    1. 移除 system 条目（NDFC 自行提供 system prompt）。
    2. 丢弃起始处孤立的 assistant / tool_result（无 user 起始）。
    3. 丢弃未闭合的 assistant tool_calls 段：维护一个"安全点"（safe point），
       每当所有 tool_call 都被对应 tool_result 闭合时记录当前处理位置；若后续
       出现新的 user 或到达结尾时仍存在未配对 tool_call，则回退到安全点，
       丢弃整个未闭合段（含其中部分配对的 tool_result 与普通 assistant）。
    4. 合并连续同角色文本条目。

    Args:
        entries: 原始快照条目。

    Returns:
        清洗后的条目列表。
    """
    # 1) 移除 system
    entries = [e for e in entries if e.role != SNAPSHOT_ROLE_SYSTEM]
    if not entries:
        return []

    # 2) 丢弃头部孤立条目，直到遇到 user
    while entries and entries[0].role != SNAPSHOT_ROLE_USER:
        entries.pop(0)

    # 3) 构建闭合序列：丢弃未配对的 tool_calls 段
    cleaned: list[SnapshotEntry] = []
    pending_ids: set[str] = set()
    # 所有 tool_call 已闭合的最近合法处理位置；未闭合中断时回退到这里
    safe_len = 0

    def record_safe_point() -> None:
        """当没有未配对 tool_call 时，更新安全点。"""
        nonlocal safe_len
        if not pending_ids:
            safe_len = len(cleaned)

    for entry in entries:
        if entry.role == SNAPSHOT_ROLE_USER:
            # 到达新的 user：若仍存在未配对的 tool_call，回退到最近安全点，
            # 丢弃整个未闭合段（含其中的 SUSPEND 与部分配对的 tool_result）。
            if pending_ids:
                del cleaned[safe_len:]
                pending_ids.clear()
            cleaned.append(entry)
            record_safe_point()
        elif entry.role == SNAPSHOT_ROLE_ASSISTANT and entry.tool_calls:
            for call in entry.tool_calls:
                call_id = str(call.get("id", "") or "")
                if call_id:
                    pending_ids.add(call_id)
            cleaned.append(entry)
        elif entry.role == SNAPSHOT_ROLE_TOOL_RESULT:
            if entry.call_id and entry.call_id in pending_ids:
                pending_ids.discard(entry.call_id)
            cleaned.append(entry)
            record_safe_point()
        else:
            cleaned.append(entry)
            record_safe_point()

    # 结尾：若仍存在未配对的 tool_calls，丢弃尾部未闭合段
    if pending_ids:
        del cleaned[safe_len:]
        pending_ids.clear()

    # 4) 合并连续同角色文本条目
    if not cleaned:
        return []
    merged: list[SnapshotEntry] = [cleaned[0]]
    for entry in cleaned[1:]:
        prev = merged[-1]
        if (
            prev.role == entry.role
            and not prev.tool_calls
            and not entry.tool_calls
            and prev.call_id == entry.call_id
        ):
            prev.content = _truncate(
                "\n".join(part for part in [prev.content, entry.content] if part)
            )
            continue
        merged.append(entry)
    return merged


def log_snapshot_restored(snapshot: ContextSnapshot, payload_count: int) -> None:
    """输出快照恢复面板日志。

    Args:
        snapshot: 已恢复的快照。
        payload_count: 恢复的 payload 数量。
    """
    entry_count = len(snapshot.entries)
    role_summary = _summarize_roles(snapshot.entries)
    updated_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(snapshot.updated_at))
    logger.print_panel(
        "上下文快照已恢复\n"
        f"- 聊天流：{snapshot.stream_id}\n"
        f"- 恢复条目：{entry_count}（payload {payload_count}）\n"
        f"- 角色构成：{role_summary}\n"
        f"- 快照时间：{updated_at}\n"
        "（快照源自上次运行，多角色上下文链已还原并合并回 LLM 请求。）",
        title="NDFC 上下文快照恢复",
        border_style="bright_cyan",
    )
    logger.info(
        f"快照已恢复 stream={snapshot.stream_id[:8]} entries={entry_count} payloads={payload_count}"
    )


def _extract_role(payload: Any) -> str:
    """提取 payload 角色字符串。"""
    role = getattr(payload, "role", None)
    if role is None:
        return ""
    value = getattr(role, "value", role)
    return str(value or "")


def _safe_text(value: Any) -> str:
    """把任意值安全转换为字符串。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return str(value)
    except Exception:
        return ""


def _serialize_tool_call(part: Any) -> dict[str, Any]:
    """序列化单个 ToolCall。"""
    call_id = getattr(part, "id", None) or ""
    name = getattr(part, "name", "") or ""
    args = getattr(part, "args", {}) or {}
    return {
        "id": str(call_id),
        "name": str(name),
        "args": args,
    }


def _serialize_tool_result_value(value: Any) -> str:
    """序列化 ToolResult 的值。"""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return _safe_text(value)


def _serialize_media(part: Any) -> dict[str, Any]:
    """序列化媒体内容，完整保留 base64 数据、类型与 MIME。"""
    media: dict[str, Any] = {
        "type": type(part).__name__,
        "data": _safe_text(getattr(part, "value", "") or ""),
    }
    mime_type = getattr(part, "mime_type", None)
    if mime_type:
        media["mime_type"] = str(mime_type)
    return media


def _deserialize_media(media: list[dict[str, Any]]) -> list[Any]:
    """把快照中的媒体条目还原为框架媒体对象。"""
    if not media:
        return []
    from src.app.plugin_system.types import Audio, Image

    restored: list[Any] = []
    for item in media:
        if not isinstance(item, dict):
            continue
        media_type = str(item.get("type", "") or "")
        data = str(item.get("data", "") or "")
        if not data:
            continue
        mime_type = item.get("mime_type")
        try:
            if media_type == "Image":
                restored.append(Image(data))
            elif media_type == "Audio":
                restored.append(Audio(data))
            elif media_type == "Video":
                from src.kernel.llm import Video

                restored.append(Video(data, mime_type=str(mime_type or "video/mp4")))
            elif media_type == "File":
                from src.kernel.llm.payload.content import File

                restored.append(File(data))
        except Exception:
            continue
    return restored


def _render_unknown_part(part: Any) -> str:
    """渲染无法识别的 content 类型。"""
    if isinstance(part, str):
        return part
    if isinstance(part, dict):
        return json.dumps(part, ensure_ascii=False)
    return _safe_text(part)


def _truncate(text: str) -> str:
    """裁剪过长文本。"""
    if len(text) <= _MAX_ENTRY_CHARS:
        return text
    return text[:_MAX_ENTRY_CHARS].rstrip() + "..."


def _merge_consecutive(entries: list[SnapshotEntry]) -> list[SnapshotEntry]:
    """合并相邻同角色条目，减少快照冗余。

    仅合并不含 tool_call 与媒体、且 call_id 相同的纯文本相邻条目，
    避免破坏媒体与 tool_call 的独立条目语义。
    """
    if not entries:
        return entries
    merged: list[SnapshotEntry] = [entries[0]]
    for entry in entries[1:]:
        prev = merged[-1]
        if (
            prev.role == entry.role
            and not prev.tool_calls
            and not entry.tool_calls
            and not prev.media
            and not entry.media
            and prev.call_id == entry.call_id
        ):
            prev.content = _truncate("\n".join(part for part in [prev.content, entry.content] if part))
            continue
        merged.append(entry)
    return merged


def _summarize_roles(entries: list[SnapshotEntry]) -> str:
    """统计快照条目的角色构成。"""
    counts: dict[str, int] = {}
    for entry in entries:
        counts[entry.role] = counts.get(entry.role, 0) + 1
    return ", ".join(f"{role}×{count}" for role, count in counts.items())


__all__ = [
    "capture_snapshot_from_payloads",
    "deserialize_snapshot",
    "extract_stream_id",
    "log_snapshot_restored",
    "serialize_payloads",
]
