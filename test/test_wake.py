"""NDFC Kokoro Bridge 主动唤醒测试。"""

from __future__ import annotations

from plugins.ndfc_kokoro_bridge.config import NDFCKokoroBridgeConfig
from plugins.ndfc_kokoro_bridge.wake import build_trigger_content


def test_build_trigger_content_uses_configured_template() -> None:
    """主动唤醒内容应使用配置模板渲染。"""

    config = NDFCKokoroBridgeConfig()
    config.wake_prompt.trigger_template = "类型={trigger_type}; 原因={reason}"

    content = build_trigger_content("scheduled", "预约到期", config)

    assert content == "类型=scheduled; 原因=预约到期"


def test_build_trigger_content_fills_empty_reason() -> None:
    """主动唤醒内容应为空理由填入默认文本。"""

    config = NDFCKokoroBridgeConfig()
    config.wake_prompt.trigger_template = "类型={trigger_type}; 原因={reason}"

    content = build_trigger_content("silence", "", config)

    assert content == "类型=silence; 原因=无"
