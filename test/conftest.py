"""NDFC Kokoro Bridge 测试配置。

确保直接运行插件自带测试时，项目根目录可被导入（使 ``plugins`` 包可导入）。
"""

from __future__ import annotations

import sys
from pathlib import Path

#: 项目根目录，用于把 ``plugins`` 与 ``src`` 加入 sys.path。
_PROJECT_ROOT = Path(__file__).resolve().parents[3]

if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
