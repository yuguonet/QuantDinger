"""conftest — 集成树内测试路径。

旧参照（等价性基线）= 本树 `app/market_cn/auto/strategies/` 旧版文件（切换前）；
Phase 2 删除旧策略后，用 AUTO_SLIM_REF 指向独立基线树运行。
"""

import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))       # .../tests/present
ROOT = os.path.abspath(os.path.join(_here, "..", ".."))  # backend_api_python
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest


@pytest.fixture(scope="session")
def ref_path():
    return os.environ.get("AUTO_SLIM_REF")
