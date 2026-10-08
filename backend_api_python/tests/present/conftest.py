"""conftest — 集成树内测试路径。

⚠️ **修正（2026-10-08）**：原注释称「旧参照（等价性基线）= 本树
`app/market_cn/auto/strategies/` 旧版文件（切换前）；Phase 2 删除旧策略后，
用 `AUTO_SLIM_REF` 指向独立基线树运行」—— **该机制从未实现**：本树的策略文件就是
当前实现，不是「旧版」，拿它当参照 = 自己跟自己比（假绿）。

真正的独立真相源现已落地：**`tests/golden/baselines/*.json`**（冻结时点的旧引擎输出，
D2 口径），门禁 `tests/golden/test_golden_parity.py`。见 `docs/口径差异报告.md`。

**`AUTO_SLIM_REF` 现已有真实消费者（2026-10-08，方案 §2.2.3）**：
  · `tools/legacy_trade_diff.py --ref`（外部参照模式，独立子进程跑旧版树）；
  · `tests/golden/test_ref_parity.py`（参照树活代码 vs 冻结基线）。
语义：指向**解压后的旧版树**（`QuantDinger-auto展示层正常.zip`），不进版本库、只读。
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
