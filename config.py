"""NDFC Kokoro Bridge 插件配置。

配置文件默认路径：config/plugins/ndfc_kokoro_bridge/config.toml。
本配置只包含运行参数，不包含自定义人设、表达风格或长期提示词。
"""

from __future__ import annotations

from typing import ClassVar

from src.app.plugin_system.base import BaseConfig, Field, SectionBase, config_section


class NDFCKokoroBridgeConfig(BaseConfig):
    """NDFC Kokoro Bridge 插件配置模型。"""

    name: ClassVar[str] = "config"
    description: ClassVar[str] = "NDFC Kokoro Bridge 配置"

    @config_section("plugin")
    class PluginSection(SectionBase):
        """插件基础配置。"""

        enabled: bool = Field(default=True, description="是否启用插件")
        debug_log: bool = Field(default=False, description="是否输出调试日志")
        private_only: bool = Field(default=True, description="是否仅在私聊中生效")
        target_prompt: str = Field(
            default="neo_default_chatter_user_prompt",
            description="目标 prompt 名称，仅用于定位 neo_default_chatter 的 user prompt 构建事件",
        )
        enable_prompt_state_injection: bool = Field(
            default=True,
            description="是否把本插件维护的运行时状态注入 neo_default_chatter user prompt extra",
        )

    @config_section("mental")
    class MentalSection(SectionBase):
        """心理活动记录配置。"""

        max_log_entries: int = Field(default=50, description="每个私聊保留的最大心理活动条数")
        inject_recent_entries: int = Field(default=6, description="每次注入最近多少条心理活动")
        max_entry_chars: int = Field(default=240, description="单条心理活动注入时的最大字符数")

    @config_section("wait")
    class WaitSection(SectionBase):
        """回复等待配置。"""

        enabled: bool = Field(default=True, description="是否启用回复等待")
        min_seconds: float = Field(default=10.0, description="最小等待秒数")
        max_seconds: float = Field(default=600.0, description="最大等待秒数")
        max_consecutive_timeouts: int = Field(default=3, description="连续超时上限")
        timeout_stage_1_prompt: str = Field(
            default=(
                "你发出消息已经过去一段时间了，对方还没有回应。\n"
                "你想想：有没有什么没说完的话，或者忽然想到什么想跟对方说的？\n"
                "如果有，发出去就好；如果脑子里没什么，继续等一等也无妨。\n"
                "你可以发送消息，或调用 `set_reply_wait(seconds>0)` 继续等待，或调用 `set_reply_wait(seconds=0)` 结束等待。"
            ),
            description="第 1 阶段（首次超时）注入的引导提示",
        )
        timeout_stage_2_prompt: str = Field(
            default=(
                "对方一直没有回复。\n"
                "你已经主动说了几次，对方始终没有回应。请判断你是真的有内容要说，还是只是想打破沉默？\n"
                "如果确实有话说，可以发送消息；或调用 `set_reply_wait(seconds=0)` 结束等待。"
            ),
            description="第 2 阶段（中间阶段）注入的引导提示",
        )
        timeout_stage_3_prompt: str = Field(
            default=(
                "你已经等了很久，对方始终没有出现。\n"
                "本次等待到此为止，**不得**再设置新的等待（`seconds` 必须为 0）。\n"
                "如果要说话就直接说完并结束等待。"
            ),
            description="第 3 阶段（达到上限）注入的引导提示词",
        )

        def apply_rules(self, raw_seconds: float, consecutive_timeouts: int) -> float:
            """应用等待时长规则。

            Args:
                raw_seconds: 模型请求的等待秒数。
                consecutive_timeouts: 当前连续等待超时次数。

            Returns:
                归一化后的等待秒数；0 表示不等待。
            """
            if not self.enabled or raw_seconds <= 0:
                return 0.0
            if self.is_limit_reached(consecutive_timeouts):
                return 0.0
            return max(self.min_seconds, min(raw_seconds, self.max_seconds))

        def is_limit_reached(self, consecutive_timeouts: int) -> bool:
            """判断连续等待超时是否已达到上限。"""
            return consecutive_timeouts >= self.max_consecutive_timeouts

        def get_timeout_stage(self, consecutive_timeouts: int) -> int:
            """获取当前连续超时次数对应的三阶段编号。

            阶段规则固定为三段：
            - 第 1 次超时：第一阶段（引导思考）。
            - 达到上限（max_consecutive_timeouts）：第三阶段（强制结束）。
            - 中间次数：第二阶段（警示沉默）。
            """
            if consecutive_timeouts <= 1:
                return 1
            if consecutive_timeouts >= self.max_consecutive_timeouts:
                return 3
            return 2

        def get_timeout_stage_prompt(self, consecutive_timeouts: int) -> str:
            """获取当前连续超时阶段对应的提示词。"""
            stage = self.get_timeout_stage(consecutive_timeouts)
            if stage == 1:
                return self.timeout_stage_1_prompt
            if stage == 2:
                return self.timeout_stage_2_prompt
            return self.timeout_stage_3_prompt

    @config_section("proactive")
    class ProactiveSection(SectionBase):
        """主动思考与预约配置。"""

        enabled: bool = Field(default=True, description="是否启用主动思考")
        silence_threshold: int = Field(default=7200, description="沉默多久后可触发主动思考，单位秒")
        trigger_probability: float = Field(default=0.3, description="沉默主动触发概率，范围 0~1")
        min_interval: int = Field(default=1800, description="两次主动触发最小间隔，单位秒")
        quiet_hours_start: str = Field(default="23:00", description="勿扰开始时间，格式 HH:MM")
        quiet_hours_end: str = Field(default="07:00", description="勿扰结束时间，格式 HH:MM")
        check_interval: int = Field(default=60, description="调度检查间隔，单位秒")
        schedule_min_minutes: int = Field(default=30, description="模型预约最小分钟数")
        schedule_max_minutes: int = Field(default=1440, description="模型预约最大分钟数")
        wake_cold_stream: bool = Field(default=True, description="冷流预约到期时是否尝试启动流循环")

    @config_section("action_prompt")
    class ActionPromptSection(SectionBase):
        """Bridge Actions 展示给 NDFC 的工具提示词配置。"""

        set_reply_wait_description: str = Field(
            default=(
                "登记你发完消息后是否在等对方回复，并记录这一刻的内心思考。"
                "thought 是必填项，用来保存你此刻等待或取消等待时的真实想法、心情、判断依据和后续打算；"
                "可以写得相对完整，因为它会进入本插件维护的私聊状态记录，供之后恢复上下文时参考。"
                "期待对方很快回应时填一个短等待，例如你问了问题、聊得正起劲、或想维持当前聊天节奏。"
                "短等待通常可在 120 到 300 秒之间选择，不要死板使用固定数值；应根据实际聊天情境灵活调整时长。"
                "话题告一段落、已经告别、或对方不需要特别回什么时，把 seconds 设为 0，并在 thought 中说明原因。"
                "如果是想过一段时间再主动找对方，那应该使用 schedule_thought，不是这个工具。"
                "本工具只记录等待理由、期待回应和内心思考；如需让 NDFC 控制流等待，可在同轮另行调用 pass_and_wait。"
            ),
            description="set_reply_wait Action 的工具描述，可复用/改写 KFC wait_instruction 语义",
        )
        schedule_thought_description: str = Field(
            default=(
                "维护一个长期存在的下一次主动思考预约。"
                "它不是每轮都要刷新的临时提醒，而是你和这个私聊对象保持连续联系的长期状态。"
                "当当前对话已经告一段落、出现明确的下次联系理由、旧预约明显不合适，或你确实想在某个时间重新想起对方时再设置或更新。"
                "如果现有预约仍然合适，不要因为本轮聊了几句就重复覆盖；普通沉默主动思考会作为兜底。"
                "新的预约会覆盖旧的预约；传 delay_minutes=0 可取消当前预约。"
                "预约不受勿扰时段限制，即使是深夜或清晨设定的预约也会如期触发。"
                "delay_minutes=0 取消预约；其他值会被限制到插件配置的允许范围内，默认 30~1440 分钟。"
                "reason 必填：记录此刻的真实想法。可以是一件具体的事，也可以只是「那个时间想找 Ta 说说话」"
                "——怎么自然怎么写，重要的是让未来的你看到时能自然接上。取消预约时可留空。"
                "预约是稳定、可控的长期联系节律；但稳定不等于频繁更新。"
            ),
            description="schedule_thought Action 的工具描述，可复用/改写 KFC schedule_proactive 语义",
        )
        schedule_guidance: str = Field(
            default=(
                "预约是你和 Ta 保持长线联系的主要方式，但不需要每一轮都更新。\n\n"
                "对话结束、话题转折、关系状态变化、或你产生明确的下次联系念头时，可以检查现有预约是否仍合适；"
                "合适就保持，不合适再覆盖。\n\n"
                "目标不是频繁刷新预约，而是让未来的你有一个清晰、自然、可延续的重新想起点。"
                "系统的沉默触发只是兜底，预约负责更稳定、更有意图的长期联系。"
            ),
            description="追加到 schedule_thought 描述后的预约使用指导，留空则不追加",
        )

    @config_section("state_prompt")
    class StatePromptSection(SectionBase):
        """注入 NDFC user prompt extra 的运行时状态文案配置。"""

        title: str = Field(default="# 私聊运行状态", description="状态注入块标题")
        intro: str = Field(
            default="以下内容是系统记录的当前私聊状态，不是用户发来的消息。你可以参考它保持上下文连续性，但不要原样复述。",
            description="状态注入块开头说明",
        )
        current_state_title: str = Field(default="## 当前状态", description="当前状态小标题")
        mental_log_title: str = Field(default="## 近期心理活动记录", description="心理活动记录小标题")
        trigger_title: str = Field(default="## 当前触发原因", description="触发原因小标题")
        private_chat_line: str = Field(default="- 当前是私聊。", description="私聊事实提示行")

    @config_section("wake_prompt")
    class WakePromptSection(SectionBase):
        """主动唤醒注入未读消息的系统触发文案配置。"""

        trigger_template: str = Field(
            default="[心理状态触发]\n触发类型：{trigger_type}\n触发原因：{reason}",
            description="主动唤醒触发消息模板，可使用 {trigger_type} 与 {reason}",
        )

    plugin: PluginSection = Field(default_factory=PluginSection)
    mental: MentalSection = Field(default_factory=MentalSection)
    wait: WaitSection = Field(default_factory=WaitSection)
    proactive: ProactiveSection = Field(default_factory=ProactiveSection)
    action_prompt: ActionPromptSection = Field(default_factory=ActionPromptSection)
    state_prompt: StatePromptSection = Field(default_factory=StatePromptSection)
    wake_prompt: WakePromptSection = Field(default_factory=WakePromptSection)
