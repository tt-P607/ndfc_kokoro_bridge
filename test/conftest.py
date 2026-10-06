"""NDFC Kokoro Bridge 测试配置。

确保直接运行插件自带测试时，项目根目录可被导入（使 ``plugins`` 包可导入）。
"""

from __future__ import annotations

import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

#: 项目根目录，用于把 ``plugins`` 与 ``src`` 加入 sys.path。
_PROJECT_ROOT = Path(__file__).resolve().parents[3]

if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


@pytest.fixture(autouse=True)
def isolated_json_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    """用独立键值存储隔离插件测试，避免写入运行中的数据目录。"""
    from src.app.plugin_system.api import storage_api

    records: dict[tuple[str, str], dict[str, Any]] = {}

    async def load_json(namespace: str, name: str) -> dict[str, Any] | None:
        """读取隔离存储中的 JSON 数据。"""
        return deepcopy(records.get((namespace, name)))

    async def save_json(namespace: str, name: str, data: dict[str, Any]) -> None:
        """写入隔离存储中的 JSON 数据。"""
        records[namespace, name] = deepcopy(data)

    async def delete_json(namespace: str, name: str) -> bool:
        """删除隔离存储中的 JSON 数据。"""
        return records.pop((namespace, name), None) is not None

    async def list_json(namespace: str) -> list[str]:
        """列出隔离命名空间中的键。"""
        return [name for store_name, name in records if store_name == namespace]

    monkeypatch.setattr(storage_api, "load_json", load_json)
    monkeypatch.setattr(storage_api, "save_json", save_json)
    monkeypatch.setattr(storage_api, "delete_json", delete_json)
    monkeypatch.setattr(storage_api, "list_json", list_json)
