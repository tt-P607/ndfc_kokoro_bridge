# NDFC Kokoro Bridge 验证说明

## 1. 启用插件

确认 [`manifest.json`](../manifest.json) 存在后启动应用。首次加载配置时会生成 `config/plugins/ndfc_kokoro_bridge/config.toml`。

## 2. 私聊状态注入验证

1. 在私聊里向 NDFC 发送普通消息。
2. 诱导模型调用 [`set_reply_wait`](../actions/set_reply_wait.py) 并填写 `thought` 参数，或调用 [`schedule_thought`](../actions/schedule_thought.py) 预约思考。
3. 下一轮私聊触发 NDFC。

预期：`neo_default_chatter_user_prompt` 的 `values.extra` 中追加"私聊运行状态"，包含等待/预约状态与触发原因。

## 3. 回复等待验证

1. 让模型先调用 `send_text` 发出一个需要对方回应的问题。
2. 同轮或后续调用 [`set_reply_wait`](../actions/set_reply_wait.py)，设置较短等待时间。
3. 在等待期内回复，或等待超时。

预期：

- 用户提前回复时，等待状态被 [`NDFCKokoroMessageObserver`](../handlers/message_observer.py) 清除。
- 用户未回复且等待超时后，调度器通过 [`wake_stream()`](../wake.py) 注入系统触发消息。

## 4. 预约思考验证

1. 调用 [`schedule_thought`](../actions/schedule_thought.py)，测试时可把配置中的 `proactive.schedule_min_minutes` 调小。
2. 等待预约到期。

预期：到期后 [`NDFCKokoroScheduler`](../scheduler.py) 清除预约状态，并向私聊流注入"心理状态触发"消息，NDFC 被唤醒后自然决策是否主动开口。

## 5. 沉默主动验证

1. 测试环境把 `proactive.silence_threshold` 调短。
2. 把 `proactive.trigger_probability` 设为 `1.0`。
3. 保持私聊沉默超过阈值。

预期：调度器触发沉默主动思考，并注入触发消息。

## 6. 清空上下文与重启

1. 在一个 NDFC 聊天流中产生对话并确认已有上下文快照。
2. 使用官方 `/清空上下文` 命令，或对应的群聊、私聊、批量清空子命令。
3. 重启 Bot，在被清空的流中发送新消息。
4. 让新上下文保存为快照，再次重启并继续该流。

预期：第一次重启不恢复清空前的历史；第二次重启可以恢复清空后的新上下文。
只清空一个流时，其他流的快照不受影响，被清空流的备忘录和预约仍然保留。

清空后不立即重启时，本插件不会重置 NDFC 自身的内存请求链，但该旧链不会
重新保存为可恢复快照，因此后续重启仍不会恢复已清空的历史。
