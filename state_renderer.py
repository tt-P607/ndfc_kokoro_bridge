"""NDFC Kokoro Bridge 运行时状态渲染。

本模块把 BridgeSession 转换为可追加到 neo_default_chatter user prompt extra
的事实状态块：当前场景、等待、预约、备忘录、触发原因与近期心理活动。
"""

from __future__ import annotations

import time
from datetime import datetime

from .config import NDFCKokoroBridgeConfig
from .models import BridgeSession, MentalEntry
from .models import SCENE_PRIVATE

_SCENE_LABELS = {
    SCENE_PRIVATE: "私聊",
    "group": "群聊",
    "discuss": "讨论组",
}


def render_user_extra(session: BridgeSession, config: NDFCKokoroBridgeConfig) -> str:
    """渲染可注入 NDFC user prompt extra 的即时运行状态。

    仅包含会随轮次动态变化的即时事实：场景、最近消息间隔、回复等待、
    触发原因与近期心理活动。稳定长期状态（预约、备忘）走 system reminder，
    不在此注入，避免在 user prompt 前缀引入动态内容破坏缓存。

    Args:
        session: 当前流状态。
        config: 插件配置。

    Returns:
        即时状态文本；没有可注入内容时返回空字符串。
    """
    now = time.time()
    prompt_config = config.state_prompt
    lines: list[str] = [
        prompt_config.title,
        prompt_config.intro,
        "",
        prompt_config.current_state_title,
    ]

    scene = session.chat_type or SCENE_PRIVATE
    scene_label = _SCENE_LABELS.get(scene, scene)
    lines.append(prompt_config.scene_line.format(scene=scene_label))

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

    # 动态时间信息放最后，避免频繁变化的内容破坏 user prompt 前缀缓存
    if session.last_user_message_at is not None:
        lines.append(f"- 最近一次用户消息距今 {_format_delta(now - session.last_user_message_at)}。")
    if session.last_bot_message_at is not None:
        lines.append(f"- 最近一次你发送消息距今 {_format_delta(now - session.last_bot_message_at)}。")

    return "\n".join(lines).strip()


def render_long_term_reminder(session: BridgeSession, config: NDFCKokoroBridgeConfig) -> str:
    """渲染应注入 system reminder 的稳定长期状态。

    仅包含低频变化的长期事实：主动思考预约与有效备忘。该文本应通过
    ``prompt_api.add_stream_reminder`` 以 DYNAMIC 类型写入 actor 流私有
    bucket，由框架注入到最后一条 user 消息，并在内容变化时剥离旧文本，
    保证始终只存在最新一份、不会在 user prompt 前缀累积。

    Args:
        session: 当前流状态。
        config: 插件配置。

    Returns:
        长期状态文本；没有可注入内容时返回空字符串。
    """
    now = time.time()
    prompt_config = config.state_prompt
    lines: list[str] = []

    if session.scheduled_at is not None:
        if session.scheduled_at > now:
            lines.append(f"- 已预约下一次主动思考：{_format_time(session.scheduled_at)}。")
        else:
            lines.append("- 已存在到期的主动思考预约。")
        if session.scheduled_reason:
            lines.append(f"- 预约理由：{session.scheduled_reason}")

    valid_memos = _valid_memos(session)
    if valid_memos and config.memo.enabled:
        lines.append("")
        lines.append(prompt_config.memo_title)
        for memo in valid_memos:
            remaining = _format_delta(memo.remaining_seconds(now))
            hint = f"（{memo.intent}）" if memo.intent else ""
            lines.append(f"- [{memo.memo_id}] {memo.content}{hint}，剩余 {remaining}")

    return "\n".join(lines).strip()


def _valid_memos(session: BridgeSession) -> list:
    """获取当前有效的备忘录列表。

    Args:
        session: 当前流状态。

    Returns:
        未过期的备忘列表。
    """
    now = time.time()
    return [memo for memo in session.memos if not memo.is_expired(now)]


def _recent_entries(session: BridgeSession, config: NDFCKokoroBridgeConfig) -> list[MentalEntry]:
    """获取需要注入的近期心理活动。

    Args:
        session: 当前流状态。
        config: 插件配置。

    Returns:
        需要注入的心理活动列表。
    """
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
