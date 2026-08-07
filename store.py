"""NDFC Kokoro Bridge 会话持久化存储。

本模块基于插件 JSON 存储 API 保存每个 stream_id 对应的 BridgeSession，
并为同一 stream 的并发读写提供互斥锁。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from src.app.plugin_system.api import storage_api
from src.app.plugin_system.api.log_api import get_logger

from .models import BridgeSession

logger = get_logger("ndfc_kokoro_bridge.store")

_STORE_NAME = "ndfc_kokoro_bridge"
_INDEX_KEY = "_index"


class BridgeSessionStore:
    """BridgeSession 的 JSON 持久化存储。"""

    def __init__(self) -> None:
        """初始化会话存储。"""
        self._sessions: dict[str, BridgeSession] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _get_lock(self, stream_id: str) -> asyncio.Lock:
        """获取指定 stream 的互斥锁。"""
        if stream_id not in self._locks:
            self._locks[stream_id] = asyncio.Lock()
        return self._locks[stream_id]

    @asynccontextmanager
    async def lock(self, stream_id: str) -> AsyncIterator[None]:
        """锁定指定 stream 的读写周期。

        Args:
            stream_id: 聊天流 ID。

        Yields:
            None。
        """
        async with self._get_lock(stream_id):
            yield

    async def get_or_create(self, stream_id: str) -> BridgeSession:
        """获取或创建会话状态。

        Args:
            stream_id: 聊天流 ID。

        Returns:
            BridgeSession 实例。
        """
        cached = self._sessions.get(stream_id)
        if cached is not None:
            return cached

        loaded = await self.get(stream_id)
        if loaded is not None:
            return loaded

        session = BridgeSession(stream_id=stream_id)
        self._sessions[stream_id] = session
        return session

    async def get(self, stream_id: str) -> BridgeSession | None:
        """获取已存在的会话状态，不存在时返回 None。

        Args:
            stream_id: 聊天流 ID。

        Returns:
            BridgeSession 或 None。
        """
        cached = self._sessions.get(stream_id)
        if cached is not None:
            return cached

        try:
            data = await storage_api.load_json(_STORE_NAME, stream_id)
        except Exception as exc:
            logger.warning(f"加载 BridgeSession 失败 stream={stream_id[:8]}: {exc}")
            return None

        if not isinstance(data, dict):
            return None

        session = BridgeSession.from_dict(data)
        if not session.stream_id:
            session.stream_id = stream_id
        self._sessions[stream_id] = session
        return session

    async def peek(self, stream_id: str) -> BridgeSession | None:
        """从磁盘查看会话状态，不主动创建。

        Args:
            stream_id: 聊天流 ID。

        Returns:
            BridgeSession 或 None。
        """
        cached = self._sessions.get(stream_id)
        if cached is not None:
            return cached
        try:
            data = await storage_api.load_json(_STORE_NAME, stream_id)
        except Exception as exc:
            logger.warning(f"peek BridgeSession 失败 stream={stream_id[:8]}: {exc}")
            return None
        if not isinstance(data, dict):
            return None
        session = BridgeSession.from_dict(data)
        if not session.stream_id:
            session.stream_id = stream_id
        return session

    async def save(self, session: BridgeSession) -> None:
        """保存会话状态。

        Args:
            session: 待保存会话。
        """
        self._sessions[session.stream_id] = session
        try:
            await storage_api.save_json(_STORE_NAME, session.stream_id, session.to_dict())
            await self._update_index(session)
        except Exception as exc:
            logger.warning(f"保存 BridgeSession 失败 stream={session.stream_id[:8]}: {exc}")
        if len(self._locks) > 100:
            self.cleanup_inactive_locks()

    def get_all_cached(self) -> dict[str, BridgeSession]:
        """获取当前内存缓存中的全部会话。"""
        return dict(self._sessions)

    async def list_all_stream_ids(self) -> list[str]:
        """列出所有已持久化会话 stream_id。"""
        try:
            keys = await storage_api.list_json(_STORE_NAME)
        except Exception as exc:
            logger.warning(f"列举 BridgeSession 失败: {exc}")
            return []
        return [key for key in keys if not key.startswith("_")]

    def cleanup_inactive_locks(self) -> int:
        """清理不活跃的 stream 锁。

        Returns:
            清理的锁数量。
        """
        stale = [
            stream_id
            for stream_id, lock in self._locks.items()
            if stream_id not in self._sessions and not lock.locked()
        ]
        for stream_id in stale:
            del self._locks[stream_id]
        return len(stale)

    async def _update_index(self, session: BridgeSession) -> None:
        """更新人类可读索引。

        Args:
            session: 已保存会话。
        """
        try:
            index = await storage_api.load_json(_STORE_NAME, _INDEX_KEY)
        except Exception:
            index = None
        if not isinstance(index, dict):
            index = {}
        index[session.stream_id] = {
            "platform": session.platform,
            "user_id": session.user_id,
            "chat_type": session.chat_type,
            "last_activity_at": session.last_activity_at,
            "scheduled_at": session.scheduled_at,
        }
        await storage_api.save_json(_STORE_NAME, _INDEX_KEY, index)
