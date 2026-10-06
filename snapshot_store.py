"""NDFC Kokoro Bridge 上下文快照持久化存储。

基于插件 JSON 存储 API 按 ``stream_id`` 分文件保存 :class:`ContextSnapshot`，
并为同一流的快照读写提供互斥锁，避免 Scheduler 与 LLM 生命周期事件竞态。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from src.app.plugin_system.api import storage_api
from src.app.plugin_system.api.log_api import get_logger

from .models import ContextSnapshot

logger = get_logger("ndfc_kokoro_bridge.snapshot_store")

#: 快照存储命名空间，与 BridgeSession 存储隔离。
_STORE_NAME = "ndfc_kokoro_bridge_snapshots"

#: 快照索引键，记录已存在的 stream_id 列表。
_INDEX_KEY = "_index"


class SnapshotStore:
    """按 ``stream_id`` 索引的上下文快照存储。

    内存缓存 + 插件 JSON 存储落盘，并为每个流提供独立 asyncio 锁。
    """

    def __init__(self) -> None:
        """初始化快照存储。"""
        self._snapshots: dict[str, ContextSnapshot] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _get_lock(self, stream_id: str) -> asyncio.Lock:
        """获取指定 stream 的互斥锁。

        Args:
            stream_id: 聊天流 ID。

        Returns:
            asyncio.Lock 实例。
        """
        if stream_id not in self._locks:
            self._locks[stream_id] = asyncio.Lock()
        return self._locks[stream_id]

    @asynccontextmanager
    async def lock(self, stream_id: str) -> AsyncIterator[None]:
        """锁定指定 stream 的快照读写周期。

        Args:
            stream_id: 聊天流 ID。

        Yields:
            None。
        """
        async with self._get_lock(stream_id):
            yield

    async def get(self, stream_id: str) -> ContextSnapshot | None:
        """获取指定流的快照，不存在时返回 None。

        Args:
            stream_id: 聊天流 ID。

        Returns:
            ContextSnapshot 或 None。
        """
        cached = self._snapshots.get(stream_id)
        if cached is not None:
            return cached

        try:
            data = await storage_api.load_json(_STORE_NAME, stream_id)
        except Exception as exc:
            logger.warning(f"加载快照失败 stream={stream_id[:8]}: {exc}")
            return None

        if not isinstance(data, dict):
            return None

        snapshot = ContextSnapshot.from_dict(data)
        if snapshot.stream_id and snapshot.stream_id != stream_id:
            snapshot.stream_id = stream_id
        self._snapshots[stream_id] = snapshot
        return snapshot

    async def save(self, snapshot: ContextSnapshot) -> None:
        """保存快照到内存缓存与磁盘。

        Args:
            snapshot: 待保存的快照。
        """
        if not snapshot.stream_id:
            return
        self._snapshots[snapshot.stream_id] = snapshot
        try:
            await storage_api.save_json(
                _STORE_NAME, snapshot.stream_id, snapshot.to_dict()
            )
            await self._update_index(snapshot.stream_id)
        except Exception as exc:
            logger.warning(f"保存快照失败 stream={snapshot.stream_id[:8]}: {exc}")
        if len(self._locks) > 100:
            self.cleanup_inactive_locks()

    async def delete(self, stream_id: str) -> None:
        """删除指定流的快照文件、内存缓存和索引项。

        Args:
            stream_id: 聊天流 ID；调用方需持有该流的快照锁。
        """
        await storage_api.delete_json(_STORE_NAME, stream_id)
        self._snapshots.pop(stream_id, None)
        index = await storage_api.load_json(_STORE_NAME, _INDEX_KEY)
        if isinstance(index, dict) and stream_id in index:
            del index[stream_id]
            await storage_api.save_json(_STORE_NAME, _INDEX_KEY, index)

    async def list_all_stream_ids(self) -> list[str]:
        """列出所有已持久化快照的 stream_id。

        Returns:
            list[str]: stream_id 列表。
        """
        try:
            keys = await storage_api.list_json(_STORE_NAME)
        except Exception as exc:
            logger.warning(f"列举快照失败: {exc}")
            return []
        return [key for key in keys if not key.startswith("_")]

    def get_all_cached(self) -> dict[str, ContextSnapshot]:
        """获取当前内存缓存中的全部快照。

        Returns:
            dict[str, ContextSnapshot]: stream_id 到快照的映射。
        """
        return dict(self._snapshots)

    def cleanup_inactive_locks(self) -> int:
        """清理不活跃的 stream 锁。

        Returns:
            清理的锁数量。
        """
        stale = [
            stream_id
            for stream_id, lock in self._locks.items()
            if stream_id not in self._snapshots and not lock.locked()
        ]
        for stream_id in stale:
            del self._locks[stream_id]
        return len(stale)

    async def _update_index(self, stream_id: str) -> None:
        """更新人类可读索引。

        Args:
            stream_id: 已保存快照的流 ID。
        """
        try:
            index = await storage_api.load_json(_STORE_NAME, _INDEX_KEY)
        except Exception:
            index = None
        if not isinstance(index, dict):
            index = {}
        index[stream_id] = {"updated_at": self._snapshots[stream_id].updated_at}
        try:
            await storage_api.save_json(_STORE_NAME, _INDEX_KEY, index)
        except Exception as exc:
            logger.debug(f"快照索引写入失败: {exc}")
