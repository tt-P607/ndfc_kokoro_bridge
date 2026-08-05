"""NDFC Kokoro Bridge 运行时状态渲染。

本模块把 BridgeSession 转换为可追加到 neo_default_chatter user prompt extra 的事实状态块。
"""

from __future__ import annotations

import time
from datetime import datetime

from .config import NDFCKokoroBridgeConfig
from .models import BridgeSession, MentalEntry


def render_session_state(session: BridgeSession, config: NDFCKokoroBridgeConfig) -> str:
    """渲染可注入 NDFC Prompt 的私聊运行状态。

    Args:
        session: 当前流状态。
        config: 插件配置。

    Returns:
        状态文本；没有可注入内容时返回空字符串。
    """
    now = time.time()
    prompt_config = config.state_prompt
    lines: list[str] = [
        prompt_config.title,
        prompt_config.intro,
        "",
        prompt_config.current_state_title,
        prompt_config.private_chat_line,
    ]

    if session.last_user_message_at is not None:
        lines.append(f"- 最近一次用户消息距今 {_format_delta(now - session.last_user_message_at)}。")
    if session.last_bot_message_at is not None:
        lines.append(f"- 最近一次你发送消息距今 {_format_delta(now - session.last_bot_message_at)}。")

    if session.waiting_until is not None:
        if session.is_waiting(now):
            lines.append(f"- 当前正在等待对方回复，剩余 {_format_delta(session.waiting_until - now)}。")
        else:
            lines.append("- 上一次回复等待已经超时。")
        if session.waiting_reason:
            lines.append(f"- 等待理由：{session.waiting_reason}")
        if session.waiting_expected_reaction:
            lines.append(f"- 期待对方反应：{session.waiting_expected_reaction}")
    else:
        lines.append("- 当前没有登记中的回复等待。")

    if session.consecutive_timeout_count > 0:
        max_count = config.wait.max_consecutive_timeouts
        stage_num = config.wait.get_timeout_stage(session.consecutive_timeout_count)
        lines.append(f"- 连续回复等待超时次数：{session.consecutive_timeout_count}/{max_count}。")
        lines.append(f"- 当前处于等待第 {stage_num} 阶段。")
        stage_prompt = config.wait.get_timeout_stage_prompt(session.consecutive_timeout_count).strip()
        if stage_prompt:
            lines.append(f"- 阶段引导提示：{stage_prompt}")

    if session.scheduled_at is not None:
        if session.scheduled_at > now:
            lines.append(f"- 已预约下一次主动思考：{_format_time(session.scheduled_at)}。")
        else:
            lines.append("- 已存在到期的主动思考预约。")
        if session.scheduled_reason:
            lines.append(f"- 预约理由：{session.scheduled_reason}")
    else:
        lines.append("- 当前没有预约中的主动思考。")

    if session.trigger_type or session.trigger_reason:
        lines.extend(["", prompt_config.trigger_title])
        if session.trigger_type:
            lines.append(f"- 触发类型：{session.trigger_type}")
        if session.trigger_reason:
            lines.append(f"- 触发原因：{session.trigger_reason}")

    recent_entries = _recent_entries(session, config)
    if recent_entries:
        lines.extend(["", prompt_config.mental_log_title])
        for entry in recent_entries:
            lines.append(f"- [{_format_time(entry.timestamp)}] {entry.event_type}: {_truncate(entry.content, config.mental.max_entry_chars)}")

    return "\n".join(lines).strip()


def _recent_entries(session: BridgeSession, config: NDFCKokoroBridgeConfig) -> list[MentalEntry]:
    """获取需要注入的近期心理活动。"""
    count = max(0, int(config.mental.inject_recent_entries))
    if count <= 0:
        return []
    return session.mental_entries[-count:]


def _format_delta(seconds: float) -> str:
    """把秒数格式化为简短中文时长。"""
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{seconds:.0f} 秒"
    minutes = seconds / 60
    if minutes < 60:
        return f"{minutes:.0f} 分钟"
    hours = minutes / 60
    if hours < 48:
        return f"{hours:.1f} 小时"
    return f"{hours / 24:.1f} 天"


def _format_time(timestamp: float) -> str:
    """格式化 Unix 时间戳。"""
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")


def _truncate(text: str, limit: int) -> str:
    """裁剪过长文本。"""
    normalized = " ".join(str(text).split())
    if limit <= 0 or len(normalized) <= limit:
        return normalized
    return normalized[:limit].rstrip() + "..."
