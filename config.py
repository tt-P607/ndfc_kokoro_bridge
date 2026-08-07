"""NDFC Kokoro Bridge 插件配置。

配置文件默认路径：config/plugins/ndfc_kokoro_bridge/config.toml。
本配置只包含运行参数，不包含自定义人设、表达风格或长期提示词。
"""

from __future__ import annotations

from typing import ClassVar

from src.app.plugin_system.base import BaseConfig, Field, SectionBase, config_section

from .models import DEFAULT_SCENES, SCENE_DISCUSS, SCENE_GROUP


class NDFCKokoroBridgeConfig(BaseConfig):
    """NDFC Kokoro Bridge 插件配置模型。"""

    name: ClassVar[str] = "config"
    description: ClassVar[str] = "NDFC Kokoro Bridge 配置"

    @config_section("plugin")
    class PluginSection(SectionBase):
        """插件基础配置。"""

        enabled: bool = Field(default=True, description="是否启用插件")
        debug_log: bool = Field(default=False, description="是否输出调试日志")
        enabled_scenes: list[str] = Field(
            default_factory=lambda: list(DEFAULT_SCENES),
            description="生效的场景列表，可选值 private/group/discuss",
        )
        target_prompt: str = Field(
            default="neo_default_chatter_user_prompt",
            description="目标 prompt 名称，仅用于定位 neo_default_chatter 的 user prompt 构建事件",
        )
        enable_prompt_state_injection: bool = Field(
            default=True,
            description="是否把本插件维护的运行时状态注入 neo_default_chatter user prompt extra",
        )

        def is_scene_enabled(self, scene: str) -> bool:
            """判断指定场景是否在启用列表中。

            Args:
                scene: 场景标识（private/group/discuss）。

            Returns:
                场景是否启用。
            """
            return scene in (self.enabled_scenes or [])

    @config_section("snapshot")
    class SnapshotSection(SectionBase):
        """上下文快照配置。"""

        enabled: bool = Field(default=True, description="是否启用完整上下文快照")
        capture_llm_lifecycle: bool = Field(
            default=True,
            description="是否监听 LLM 生命周期事件实时更新快照",
        )

    @config_section("memo")
    class MemoSection(SectionBase):
        """备忘录配置。"""

        enabled: bool = Field(default=True, description="是否启用备忘录")
        max_entries: int = Field(default=10, description="单流最大有效备忘条数")
        min_expire_hours: float = Field(default=1.0, description="最小过期时长（小时）")
        max_expire_hours: float = Field(default=336.0, description="最大过期时长（小时）")
        default_expire_hours: float = Field(default=24.0, description="默认过期时长（小时）")

    @config_section("mental")
    class MentalSection(SectionBase):
        """心理活动记录配置。"""

        max_log_entries: int = Field(default=50, description="每个聊天流保留的最大心理活动条数")
        inject_recent_entries: int = Field(default=6, description="每次注入最近多少条心理活动")
        max_entry_chars: int = Field(default=240, description="单条心理活动注入时的最大字符数")

    @config_section("wait")
    class WaitSection(SectionBase):
        """回复等待配置。"""

        enabled: bool = Field(default=True, description="是否启用回复等待")
        private_only: bool = Field(
            default=True,
            description="等待的'一对一'语义是否仅限私聊；私聊外只注入状态不触发等待",
        )
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

        enabled: bool = Field(default=True, description="是否启用主动思考总开关")
        private_enabled: bool = Field(
            default=True,
            description="私聊是否启用预约/沉默主动触发；默认开启",
        )
        group_enabled: bool = Field(
            default=False,
            description="群聊/讨论组是否启用预约/沉默主动触发；默认关闭，需手动开启",
        )
        silence_threshold: int = Field(default=7200, description="沉默多久后可触发主动思考，单位秒")
        trigger_probability: float = Field(default=0.3, description="沉默主动触发概率，范围 0~1")
        min_interval: int = Field(default=1800, description="两次主动触发最小间隔，单位秒")
        quiet_hours_start: str = Field(default="23:00", description="勿扰开始时间，格式 HH:MM")
        quiet_hours_end: str = Field(default="07:00", description="勿扰结束时间，格式 HH:MM")
        check_interval: int = Field(default=60, description="调度检查间隔，单位秒")
        schedule_min_minutes: int = Field(default=30, description="模型预约最小分钟数")
        schedule_max_minutes: int = Field(default=1440, description="模型预约最大分钟数")
        wake_cold_stream: bool = Field(default=True, description="冷流预约到期时是否尝试启动流循环")

        def is_scene_enabled(self, scene: str) -> bool:
            """判断指定场景是否启用了预约/主动触发。

            Args:
                scene: 场景标识（private/group/discuss）。

            Returns:
                场景是否允许预约与主动触发。
            """
            if scene in (SCENE_GROUP, SCENE_DISCUSS):
                return self.group_enabled
            return self.private_enabled

    @config_section("action_prompt")
    class ActionPromptSection(SectionBase):
        """Bridge Actions 展示给 NDFC 的工具提示词配置。"""

        set_reply_wait_description: str = Field(
            default=(
                "登记你发完消息后是否在等对方回复，并记录这一刻的内心思考。"
                "thought 是必填项，用来保存你此刻等待或取消等待时的真实想法、心情、判断依据和后续打算；"
                "可以写得相对完整，因为它会进入本插件维护的聊天流状态记录，供之后恢复上下文时参考。"
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
                "维护你和 Ta 之间的下一次主动联系预约，这是一段需要时常照看的长期状态，而不只是一次性的临时提醒。"
                "在私聊里，它是保持这段关系自然延续的主要方式：对话告一段落后，安排一个合适的时机重新想起 Ta、续上话题或主动联系。"
                "你可以根据关系进展随时检查现有预约是否仍然合适：合适就保持，不合适就覆盖成新的想法。"
                "设置时写清 reason：你在等什么、想续什么话题、或为什么那个时间值得再联系 Ta。"
                "这个 reason 会在预约到期唤醒你时原样投回给你，让未来的你能立刻接上。"
                "新的预约会覆盖旧的预约；传 delay_minutes=0 可取消当前预约（取消时才可留空 reason）。"
                "预约不受勿扰时段限制，即使是深夜或清晨设定的预约也会如期触发。"
                "delay_minutes=0 取消预约；其他值会被限制到插件配置的允许范围内，默认 30~1440 分钟。"
                "预约是稳定、可控的联系节律；但稳定不等于频繁更新，优先维护一个清晰、合适的预约。"
            ),
            description="schedule_thought（私聊版）Action 的工具描述",
        )
        schedule_thought_group_description: str = Field(
            default=(
                "预约是在「约定了一件事」或「认定某个时机需要重新回来看一眼这个群」时才设置的主动提醒。"
                "设置时必须写清 reason：你约定了什么、在等什么、或为什么那个时间值得再回来看一眼这个群。"
                "这个 reason 会在预约到期唤醒你时原样投回给你，让未来的你能立刻接上。"
                "如果现有预约仍然合适，不要因为本轮聊了几句就重复覆盖；只是想回来看一眼，交给系统的沉默触发即可。"
                "新的预约会覆盖旧的预约；传 delay_minutes=0 可取消当前预约（取消时才可留空 reason）。"
                "预约不受勿扰时段限制，即使是深夜或清晨设定的预约也会如期触发。"
                "delay_minutes=0 取消预约；其他值会被限制到插件配置的允许范围内，默认 30~1440 分钟。"
                "预约是稳定、可控的唤醒节律；但稳定不等于频繁更新。"
            ),
            description="schedule_thought_group（群聊版）Action 的工具描述",
        )
        memo_description: str = Field(
            default=(
                "记录一条带过期时间的私人备忘录。"
                "备忘条目会自动渲染到提示词末尾，让你保持对它的意识，"
                "但不需要时刻提起或反复念叨——只在恰当的时机自然地用上。"
                "`expire_hours` 默认 24 小时，范围 1~336 小时，超出自动夹到边界。"
                "最多保留 10 条；已有内容相同的备忘会自动刷新过期时间。"
                "当备忘对应的事已经做了/不再相关时，请主动用 `action-memo_delete` 删除它。"
            ),
            description="memo Action 的工具描述",
        )
        memo_delete_description: str = Field(
            default=(
                "删除一条或多条已不再需要的备忘录。"
                "`memo_ids`：从备忘录显示中读取的 id 列表（每条备忘渲染时都会显示其 id）。"
            ),
            description="memo_delete Action 的工具描述",
        )
        schedule_guidance: str = Field(
            default=(
                "私聊里的预约是你和 Ta 保持联系节奏的主要方式，需要时常照看而不是一次设定就丢着不管。\n\n"
                "每次对话时都可以顺带检查：现有预约是否还合适、有没有更想安排的时机或话题；合适就保持，不合适就覆盖成新的。\n\n"
                "设置时一定写清 reason——那是预约到期唤醒你时投回给你的唯一线索，没有它，未来的你就不知道为什么要回来。\n\n"
                "沉默触发只是兜底，预约负责让这段关系有更稳定、更有意图的延续。目标不是频繁刷新，而是始终维护一个清晰、合适的预约。"
            ),
            description="追加到 schedule_thought（私聊版）描述后的预约使用指导，留空则不追加",
        )
        schedule_guidance_group: str = Field(
            default=(
                "群聊里的预约只在两种情况设置：一是和群里的人约定了某件事、某个时间；"
                "二是你判断某个更合适的时机值得回来看一眼这个群。\n\n"
                "设置时一定写清 reason——那是预约到期唤醒你时投回给你的唯一线索，"
                "没有它，未来的你就不知道为什么要回来。\n\n"
                "只想单纯回来看一眼、有没有值得说的话，交给系统的沉默触发就好，不必用预约。"
                "目标不是频繁刷新预约，而是让未来的你有一个清晰、有理由、可延续的唤醒点。"
            ),
            description="追加到 schedule_thought_group（群聊版）描述后的预约使用指导，留空则不追加",
        )

    @config_section("state_prompt")
    class StatePromptSection(SectionBase):
        """注入 NDFC user prompt extra 的运行时状态文案配置。"""

        title: str = Field(default="# 运行状态", description="状态注入块标题")
        intro: str = Field(
            default="以下内容是系统记录的当前聊天流状态，不是用户发来的消息。你可以参考它保持上下文连续性，但不要原样复述。",
            description="状态注入块开头说明",
        )
        current_state_title: str = Field(default="## 当前状态", description="当前状态小标题")
        mental_log_title: str = Field(default="## 近期心理活动记录", description="心理活动记录小标题")
        memo_title: str = Field(default="## 备忘录", description="备忘录小标题")
        trigger_title: str = Field(default="## 当前触发原因", description="触发原因小标题")
        scene_line: str = Field(default="- 当前场景：{scene}", description="场景事实提示行，支持 {scene} 占位符")

    @config_section("wake_prompt")
    class WakePromptSection(SectionBase):
        """主动唤醒注入未读消息的系统触发文案配置。"""

        trigger_template: str = Field(
            default=(
                "[系统主动唤醒]\n"
                "触发类型：{trigger_type}\n"
                "触发原因：{reason}\n"
                "这是系统在合适时机主动唤醒你回来看一眼这个聊天流，不是用户消息。\n"
                "如果触发类型是 scheduled，说明这是你之前预约的主动思考，按预约时写下的 reason 接上当初的意图；\n"
                "其余情况只是想起这个聊天流，看看有没有值得说的话——有就自然地说，没有就保持沉默，或简单冒个泡打个招呼也行。"
            ),
            description="主动唤醒触发消息模板，可使用 {trigger_type} 与 {reason}",
        )

    plugin: PluginSection = Field(default_factory=PluginSection)
    snapshot: SnapshotSection = Field(default_factory=SnapshotSection)
    memo: MemoSection = Field(default_factory=MemoSection)
    mental: MentalSection = Field(default_factory=MentalSection)
    wait: WaitSection = Field(default_factory=WaitSection)
    proactive: ProactiveSection = Field(default_factory=ProactiveSection)
    action_prompt: ActionPromptSection = Field(default_factory=ActionPromptSection)
    state_prompt: StatePromptSection = Field(default_factory=StatePromptSection)
    wake_prompt: WakePromptSection = Field(default_factory=WakePromptSection)
