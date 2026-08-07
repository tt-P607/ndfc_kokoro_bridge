"""NDFC Kokoro Bridge 上下文快照事件处理器。

订阅框架的 ``before_llm_request`` 生命周期事件，捕获 NDFC 发起的 LLM 请求
完整 payload 链并持久化为快照；重启后首次请求时，把快照反序列化为合法
的多角色 payload 链（user / assistant / tool_result）合并回请求，
实现上下文恢复。

说明：NDFC 的 ``request.payloads`` 是跨轮累积的，框架会在响应后自动把
assistant 与 tool_result 追加回 payloads，因此 ``before_llm_request`` 保存的
快照已经包含完整对话链，无需在 ``after_llm_request`` 额外合并响应。
``after_llm_request`` 重复追加会导致连续 assistant 与快照无限膨胀。

仅处理由 ``neo_default_chatter``（NDFC）发起的请求，其他 chatter 会被
``request_name`` 过滤，本插件工具只跟随 NDFC。
"""

from __future__ import annotations

from typing import Any

from src.app.plugin_system.api.event_api import EventDecision
from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.base import BaseEventHandler
from src.app.plugin_system.types import EventType

from ..snapshot import (
    capture_snapshot_from_payloads,
    deserialize_snapshot,
    extract_stream_id,
    log_snapshot_restored,
)
from ..snapshot_store import SnapshotStore

logger = get_logger("ndfc_kokoro_bridge.snapshot_handler")

#: NDFC chatter 的请求名称（由 BaseChatter.create_request 以 name 作为默认 request_name）。
_NDFC_REQUEST_NAME = "neo_default_chatter"

#: 注入恢复链时应跳过的"非对话历史"角色：system（NDFC 自行提供）与
#: tool（工具 schema 声明）。恢复链插到这些角色之后、第一个对话 payload 之前。
_NON_HISTORY_ROLES = {"system", "tool"}


class NDFCKokoroContextSnapshotHandler(BaseEventHandler):
    """捕获 NDFC 发起的 LLM 请求并维护完整上下文快照。"""

    name: str = "ndfc_kokoro_context_snapshot"
    description: str = "捕获 NDFC 的完整上下文链并持久化为可恢复快照"
    weight: int = 20
    intercept_message: bool = False
    init_subscribe: list[str] = [
        EventType.BEFORE_LLM_REQUEST.value,
    ]

    #: 已在本进程内完成恢复的流集合；重启后为空，从而触发首次恢复。
    _restored_streams: set[str] = set()

    def get_snapshot_store(self) -> SnapshotStore:
        """获取插件级快照存储。"""
        return self.plugin.snapshot_store  # type: ignore[attr-defined]

    def _is_ndfc_request(self, params: dict[str, Any]) -> bool:
        """判断请求是否由 NDFC 发起。

        Args:
            params: 事件参数字典。

        Returns:
            是否为 NDFC 请求。
        """
        request_name = str(params.get("request_name", "") or "")
        if request_name == _NDFC_REQUEST_NAME:
            return True
        # 兼容 request_name 未命中但请求携带 NDFC 特有 meta_data 的情况
        meta_data = params.get("meta_data")
        if isinstance(meta_data, dict) and meta_data.get("_ndfc"):
            return True
        return False

    async def execute(
        self,
        event_name: str,
        params: dict[str, Any],
    ) -> tuple[EventDecision, dict[str, Any]]:
        """处理 LLM 生命周期事件，维护并恢复 NDFC 上下文快照。

        Args:
            event_name: 事件名称。
            params: 事件参数字典。

        Returns:
            事件决策与原参数对象。
        """
        normalized_event = str(getattr(event_name, "value", event_name))
        if normalized_event != EventType.BEFORE_LLM_REQUEST.value:
            return EventDecision.SUCCESS, params
        if not self._is_ndfc_request(params):
            return EventDecision.SUCCESS, params

        stream_id = extract_stream_id(params.get("meta_data"))
        if not stream_id:
            logger.debug("快照：请求缺少 stream_id，跳过")
            return EventDecision.SUCCESS, params

        # 已确认是 NDFC 请求，登记该流，供消息观察器识别 NDFC 流
        self.plugin.mark_ndfc_stream(stream_id)  # type: ignore[attr-defined]

        store = self.get_snapshot_store()

        # 重启后首次请求：把上次运行的完整上下文链合并回本次请求
        if stream_id not in self._restored_streams:
            snapshot = await store.get(stream_id)
            if snapshot is not None and snapshot.entries:
                restored_payloads = deserialize_snapshot(snapshot)
                if restored_payloads:
                    params = self._inject_restored(params, restored_payloads)
                    log_snapshot_restored(snapshot, len(restored_payloads))
            # 无论是否有快照，都标记本进程已处理过该流，避免重复恢复
            self._restored_streams.add(stream_id)

        # 持久化本次请求的完整上下文链（覆盖式更新）
        payloads = params.get("payloads")
        if isinstance(payloads, list) and payloads:
            snapshot = capture_snapshot_from_payloads(stream_id, payloads)
            if snapshot is not None:
                async with store.lock(stream_id):
                    await store.save(snapshot)

        return EventDecision.SUCCESS, params

    @staticmethod
    def _inject_restored(params: dict[str, Any], restored: list[Any]) -> dict[str, Any]:
        """把恢复的历史链注入到请求 payloads 的 system 之后。

        保持 system prompt 位于最前，历史链紧随其后，再拼接 NDFC 本轮
        的其他 payload（如 TOOL 声明、user 未读等），保证角色序列合法。

        Args:
            params: 事件参数字典。
            restored: 反序列化后的历史 payload 列表。

        Returns:
            注入后的事件参数。
        """
        payloads = params.get("payloads")
        if not isinstance(payloads, list):
            params["payloads"] = list(restored)
            return params

        split_at = 0
        for idx, payload in enumerate(payloads):
            role = getattr(payload, "role", None)
            role_value = getattr(role, "value", role)
            if str(role_value or "") not in _NON_HISTORY_ROLES:
                break
            split_at = idx + 1

        params["payloads"] = (
            payloads[:split_at] + list(restored) + payloads[split_at:]
        )
        return params
