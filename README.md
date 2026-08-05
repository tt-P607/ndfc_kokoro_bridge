# NDFC Kokoro Bridge

**NDFC Kokoro Bridge** 是一个为 Neo-MoFox 框架中的 `neo_default_chatter`（NDFC）量身定制的"旁路增强"插件。它旨在不修改 NDFC 源码的前提下，为其提供类似 KokoroFlowChatter（KFC）的高级私聊心理状态管理能力。

## 核心功能

- **运行时状态注入**：通过 `on_prompt_build` 事件，将私聊的实时状态（如等待剩余时间、预约信息、触发原因、近期心理活动）注入 NDFC 的 User Prompt。
- **回复等待（Reply Wait）**：允许模型在发完消息后登记"心理等待"状态。支持三阶段引导提示和硬性连续超时限制。
- **预约思考（Scheduled Thought）**：允许模型预约未来某个时间点的主动唤醒，用于维持长线联系节律。
- **主动触发（Proactive Trigger）**：在私聊沉默较久时，根据概率和勿扰时段自动唤醒模型。
- **心理日志（Mental Log）**：记录模型在调用 Bridge 工具时的内心思考，并在后续对话中作为上下文参考。

## 快速开始

1. 确保已安装并启用 `neo_default_chatter` 插件。
2. 将本插件放入 `plugins` 目录。
3. 插件会自动识别私聊环境并向 NDFC 暴露以下工具：
   - `set_reply_wait`：设置回复等待。
   - `schedule_thought`：预约下一次主动思考。

## 组件清单

本插件由以下组件组成（与 [`manifest.json`](manifest.json) 中 `include` 声明一一对应）：

| 组件类型 | 组件名 | 职责 |
| --- | --- | --- |
| Action | `set_reply_wait` | 登记发完消息后的回复等待状态与内心思考 |
| Action | `schedule_thought` | 预约/取消下一次主动思考 |
| EventHandler | `ndfc_kokoro_message_observer` | 观察私聊消息收发，维护会话时间戳与等待状态 |
| EventHandler | `ndfc_kokoro_prompt_state_injector` | 在 NDFC user prompt 构建时注入运行状态 |
| Service | `ndfc_kokoro_state` | 统一封装会话读写、等待/预约与状态渲染 |

## 配置说明

配置文件位于 `config/plugins/ndfc_kokoro_bridge/config.toml`，首次启动时自动生成。各配置节说明如下。

### `[plugin]` 插件基础配置

- `enabled`：是否启用插件（默认 `true`）。
- `debug_log`：是否输出调试日志（默认 `false`）。
- `private_only`：是否仅在私聊中生效（默认 `true`）。
- `target_prompt`：目标 prompt 名称，用于定位 NDFC 的 user prompt 构建事件（默认 `neo_default_chatter_user_prompt`）。
- `enable_prompt_state_injection`：是否把运行时状态注入 NDFC user prompt extra（默认 `true`）。

### `[mental]` 心理活动记录配置

- `max_log_entries`：每个私聊保留的最大心理活动条数（默认 `50`）。
- `inject_recent_entries`：每次注入最近多少条心理活动（默认 `6`）。
- `max_entry_chars`：单条心理活动注入时的最大字符数（默认 `240`）。

### `[wait]` 回复等待配置

- `enabled`：是否启用回复等待（默认 `true`）。
- `min_seconds` / `max_seconds`：等待时长归一化范围（默认 `10` / `600` 秒）。
- `max_consecutive_timeouts`：连续超时上限（默认 `3`），达到上限后强制结束等待。
- `timeout_stage_X_prompt`：各阶段的引导提示词，支持自定义。

### `[proactive]` 主动思考与预约配置

- `enabled`：是否启用主动思考（默认 `true`）。
- `silence_threshold`：沉默多久后可触发主动思考（默认 `7200` 秒）。
- `trigger_probability`：沉默主动触发概率（默认 `0.3`）。
- `min_interval`：两次主动触发最小间隔（默认 `1800` 秒）。
- `quiet_hours_start` / `quiet_hours_end`：勿扰时段（默认 `23:00` ~ `07:00`）。
- `check_interval`：调度检查间隔（默认 `60` 秒）。
- `schedule_min_minutes` / `schedule_max_minutes`：预约分钟数限制（默认 `30` ~ `1440`）。
- `wake_cold_stream`：预约到期时是否允许冷启动流循环（默认 `true`）。

### `[action_prompt]` 工具提示词配置

- `set_reply_wait_description`：`set_reply_wait` Action 的工具描述。
- `schedule_thought_description`：`schedule_thought` Action 的工具描述。
- `schedule_guidance`：追加到预约描述后的使用指导，留空则不追加。

### `[state_prompt]` 状态注入文案配置

- `title` / `intro` / `current_state_title` / `mental_log_title` / `trigger_title` / `private_chat_line`：状态注入块的标题与说明文案，可自定义。

### `[wake_prompt]` 主动唤醒文案配置

- `trigger_template`：主动唤醒触发消息模板，支持 `{trigger_type}` 与 `{reason}` 占位符。

## 设计哲学

本插件遵循"外科手术式增强"原则：

- **零侵入**：不修改框架核心代码，不修改 NDFC 代码。
- **状态隔离**：Bridge 维护独立于 NDFC 的 Session 状态，仅在 Prompt 构建阶段进行事实注入。
- **私聊专用**：所有 Bridge 工具和状态注入仅在私聊（Private Chat）中生效。

## 开发与验证

- 代码规范遵循仓库 [`代码规范.md`](../../../代码规范.md) 与 [`AI插件编写规范.md`](../../../AI插件编写规范.md)。
- 组件统一使用 `name` / `description` 属性标识（不使用已弃用的 `action_name` / `handler_name` 等旧属性名）。
- 手动验证步骤见 [`test/manual_verification.md`](test/manual_verification.md)。
- 单元测试位于 `test/` 目录，运行：

```bash
uv run pytest plugins/ndfc_kokoro_bridge/test/
```
