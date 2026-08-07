"""NDFC Kokoro Bridge 状态模型与渲染测试。"""

from __future__ import annotations

import time

from plugins.ndfc_kokoro_bridge.config import NDFCKokoroBridgeConfig
from plugins.ndfc_kokoro_bridge.models import BridgeSession, Memo, MentalEntry
from plugins.ndfc_kokoro_bridge.state_renderer import (
    render_long_term_reminder,
    render_user_extra,
)


def test_bridge_session_round_trip_keeps_schedule_but_drops_wait() -> None:
    """BridgeSession 序列化后应保留预约，但回复等待作为临时状态不持久化。"""

    session = BridgeSession(stream_id="stream-1", platform="qq", user_id="user-1")
    session.set_waiting(30, reason="等对方回答", expected_reaction="对方继续聊")
    session.set_scheduled(time.time() + 600, reason="稍后想起这件事")
    session.add_entry(MentalEntry(event_type="inner_thought", content="有点在意这句话"), max_entries=10)

    serialized = session.to_dict()
    loaded = BridgeSession.from_dict(serialized, max_entries=10)

    assert "waiting_until" not in serialized
    assert "waiting_reason" not in serialized
    assert "waiting_expected_reaction" not in serialized
    assert loaded.stream_id == "stream-1"
    assert loaded.platform == "qq"
    assert loaded.user_id == "user-1"
    assert loaded.waiting_until is None
    assert loaded.waiting_reason == ""
    assert loaded.waiting_expected_reaction == ""
    assert loaded.scheduled_reason == "稍后想起这件事"
    assert loaded.mental_entries[0].content == "有点在意这句话"


def test_bridge_session_round_trip_keeps_chat_type_and_memos() -> None:
    """BridgeSession 序列化应保留场景类型与备忘录。"""

    session = BridgeSession(stream_id="stream-1", chat_type="group")
    session.memos.append(Memo(content="记得跟进", memo_id="abc123"))

    loaded = BridgeSession.from_dict(session.to_dict(), max_entries=10)

    assert loaded.chat_type == "group"
    assert len(loaded.memos) == 1
    assert loaded.memos[0].content == "记得跟进"
    assert loaded.memos[0].memo_id == "abc123"


def test_render_user_extra_contains_immediate_state_only() -> None:
    """即时状态渲染应包含等待、心理活动与场景，不含预约/备忘长期状态。"""

    config = NDFCKokoroBridgeConfig()
    session = BridgeSession(stream_id="stream-1")
    session.set_waiting(30, reason="想等对方回答", expected_reaction="对方补充说明")
    session.set_scheduled(time.time() + 600, reason="稍后想起这件事")
    session.memos.append(Memo(content="备忘内容", memo_id="memo1"))
    session.add_entry(MentalEntry(event_type="inner_thought", content="我觉得刚才的话题还没结束"), max_entries=10)

    rendered = render_user_extra(session, config)

    assert "# 运行状态" in rendered
    assert "当前正在等待对方回复" in rendered
    assert "想等对方回答" in rendered
    assert "近期心理活动记录" in rendered
    assert "我觉得刚才的话题还没结束" in rendered
    # 长期状态（预约/备忘）不应出现在即时状态里
    assert "稍后想起这件事" not in rendered
    assert "备忘内容" not in rendered


def test_render_user_extra_renders_scene_line() -> None:
    """即时状态渲染应输出场景标签行。"""

    config = NDFCKokoroBridgeConfig()
    private_session = BridgeSession(stream_id="stream-p", chat_type="private")
    group_session = BridgeSession(stream_id="stream-g", chat_type="group")

    private_rendered = render_user_extra(private_session, config)
    group_rendered = render_user_extra(group_session, config)

    assert "- 当前场景：私聊" in private_rendered
    assert "- 当前场景：群聊" in group_rendered


def test_render_user_extra_uses_configurable_prompt_labels() -> None:
    """即时状态渲染应使用配置中的标题与说明文案。"""

    config = NDFCKokoroBridgeConfig()
    config.state_prompt.title = "# 自定义状态标题"
    config.state_prompt.intro = "自定义状态说明"
    config.state_prompt.current_state_title = "## 自定义当前状态"
    config.state_prompt.mental_log_title = "## 自定义心理记录"
    config.state_prompt.trigger_title = "## 自定义触发原因"
    config.state_prompt.scene_line = "- 自定义场景：{scene}"
    session = BridgeSession(stream_id="stream-1", chat_type="group")
    session.mark_trigger("scheduled", "自定义预约到期")
    session.add_entry(MentalEntry(event_type="inner_thought", content="自定义记录内容"), max_entries=10)

    rendered = render_user_extra(session, config)

    assert "# 自定义状态标题" in rendered
    assert "自定义状态说明" in rendered
    assert "## 自定义当前状态" in rendered
    assert "## 自定义心理记录" in rendered
    assert "## 自定义触发原因" in rendered
    assert "- 自定义场景：群聊" in rendered


def test_render_long_term_reminder_includes_schedule_and_memos() -> None:
    """长期状态渲染应包含预约与未过期备忘，不含即时状态。"""

    config = NDFCKokoroBridgeConfig()
    session = BridgeSession(stream_id="stream-memo")
    session.set_scheduled(time.time() + 600, reason="稍后想起这件事")
    memo = Memo(
        content="明天记得问那件事",
        intent="不能忘",
        created_at=time.time(),
        expires_at=time.time() + 3600,
        memo_id="memo1",
    )
    session.memos.append(memo)
    session.set_waiting(30, reason="想等对方回答", expected_reaction="对方补充说明")

    rendered = render_long_term_reminder(session, config)

    assert "稍后想起这件事" in rendered
    assert "## 备忘录" in rendered
    assert "[memo1]" in rendered
    assert "明天记得问那件事" in rendered
    assert "不能忘" in rendered
    assert "剩余" in rendered
    # 即时状态（等待）不应出现在长期状态里
    assert "当前正在等待对方回复" not in rendered


def test_render_long_term_reminder_empty_without_state() -> None:
    """无预约且无备忘时，长期状态渲染应返回空字符串。"""

    config = NDFCKokoroBridgeConfig()
    session = BridgeSession(stream_id="stream-empty")

    assert render_long_term_reminder(session, config) == ""


def test_wait_timeout_stages_mapping() -> None:
    """验证连续超时次数到三阶段的映射逻辑。"""
    config = NDFCKokoroBridgeConfig()
    wait_cfg = config.wait

    # 默认上限为 3
    wait_cfg.max_consecutive_timeouts = 3
    assert wait_cfg.get_timeout_stage(1) == 1
    assert wait_cfg.get_timeout_stage(2) == 2
    assert wait_cfg.get_timeout_stage(3) == 3
    assert wait_cfg.get_timeout_stage(4) == 3  # 超出也算第三阶段

    # 自定义上限为 5
    wait_cfg.max_consecutive_timeouts = 5
    assert wait_cfg.get_timeout_stage(1) == 1
    assert wait_cfg.get_timeout_stage(2) == 2
    assert wait_cfg.get_timeout_stage(3) == 2
    assert wait_cfg.get_timeout_stage(4) == 2
    assert wait_cfg.get_timeout_stage(5) == 3
    assert wait_cfg.get_timeout_stage(6) == 3


def test_render_user_extra_contains_timeout_stage_prompts() -> None:
    """验证即时状态渲染中包含正确的阶段引导提示。"""
    config = NDFCKokoroBridgeConfig()
    session = BridgeSession(stream_id="stream-stage")

    # 第一阶段
    session.consecutive_timeout_count = 1
    rendered = render_user_extra(session, config)
    assert "当前处于等待第 1 阶段" in rendered
    assert config.wait.timeout_stage_1_prompt in rendered

    # 第二阶段
    session.consecutive_timeout_count = 2
    rendered = render_user_extra(session, config)
    assert "当前处于等待第 2 阶段" in rendered
    assert config.wait.timeout_stage_2_prompt in rendered

    # 第三阶段
    session.consecutive_timeout_count = 3
    rendered = render_user_extra(session, config)
    assert "当前处于等待第 3 阶段" in rendered
    assert config.wait.timeout_stage_3_prompt in rendered
