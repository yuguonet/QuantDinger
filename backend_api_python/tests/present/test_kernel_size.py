"""test_kernel_size.py — 展示层内核规模门禁（可断言，禁静默）。

裁定背景（2026-10-06 用户裁定：`1.接受`）:
    改进方案 §5.5 原文「展示层核心（不含策略、不含 A 系列修复）≤ 500 行」。
    定稿实测 = contract 112 + runner 553 + realtime 89 = 754 行
    （原 503 行的统计口径漏了 `realtime.py` 旁支与后续补齐）。

    用户接受 754 这一实际口径。本测试的意义随之改变：不再是"卡 500"，
    而是**防止内核在无人察觉中膨胀** —— 上限取 800（754 + 6% 余量），
    超限时**硬 FAIL**（不是 skip / 不是 warning）。

易错点:
    - 行数按**物理行**计（`wc -l` 口径，含注释与空行），别用"非空行"口径自欺 ——
      那会让"加注释"变成绕过门禁的手段。
    - 门禁只锁 `core/present/`；策略侧契约方法落在 `strategies/` 不计入
      （方案 §5.5 明写"不含策略"）。
    - 同时设**反向门禁**（总行数 ≥ 600）：内核被误删同样要 FAIL，不能静默通过。
"""

import io
import os
import re

import pytest

# tests/present/test_kernel_size.py → tests/present → tests → backend_api_python
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PRESENT_DIR = os.path.join(_ROOT, "app", "market_cn", "auto", "core", "present")

# (模块, 定稿行数, 上限)
MODULES = [
    ("contract.py", 112, 140),
    ("runner.py", 553, 600),
    ("realtime.py", 89, 110),
]

# 2026-10-07 口径更新（P0/P1 功能性增长，非膨胀）：
#   runner.py fold_range 扩参 stages/ctx_provider/stateful（折叠契约核心，+~30 行）；
#   contract.py 增 begin_day/realtime_shortlist 契约方法 + _trace 门原因通道注释。
#   先压缩了 fold_range 冗余 docstring（22→12 行），再上调总 cap 800→820 并记录。
KERNEL_CAP = 820
KERNEL_FLOOR = 600


def _nlines(name):
    p = os.path.join(PRESENT_DIR, name)
    with io.open(p, encoding="utf-8", newline="") as f:
        txt = f.read()
    n = txt.count("\n") + (0 if txt.endswith("\n") else 1)
    return n


def test_kernel_modules_exist():
    """内核三模块必须存在（删了也别静默 —— 结构性断言而非 import 断言）。"""
    for name, _, _ in MODULES:
        assert os.path.exists(os.path.join(PRESENT_DIR, name)), \
            "展示层内核模块缺失: %s" % name


@pytest.mark.parametrize("name,baseline,cap", MODULES)
def test_module_size(name, baseline, cap):
    n = _nlines(name)
    assert n <= cap, (
        "%s 膨胀: %d 行 > 上限 %d（定稿 %d 行）。若确有必要增长，必须先压缩既有实现，"
        "再回来调高 cap，并同步更新改进方案完成度报告的口径记录。" % (name, n, cap, baseline)
    )


def test_kernel_total_size():
    total = sum(_nlines(n) for n, _, _ in MODULES)
    assert total <= KERNEL_CAP, (
        "展示层内核总计 %d 行 > 上限 %d（定稿 754 行，2026-10-06 用户裁定接受该口径）。"
        % (total, KERNEL_CAP)
    )
    assert total >= KERNEL_FLOOR, (
        "展示层内核仅剩 %d 行 —— 疑似误删，请核对 core/present/ 完整性" % total
    )


def test_no_strategy_content_in_kernel():
    """内核不得含任何策略内容（用户裁定：策略只在 auto/strategies/）。"""
    banned_lit = ("STRATEGY_KEY", "register_strategy",
                  "from app.market_cn.auto.strategies",
                  "import app.market_cn.auto.strategies")
    banned_re = re.compile(r"class\s+\w*Strategy\b")
    for name, _, _ in MODULES:
        with io.open(os.path.join(PRESENT_DIR, name), encoding="utf-8") as f:
            src = f.read()
        for b in banned_lit:
            assert b not in src, "内核 %s 出现策略内容: %s" % (name, b)
        assert banned_re.search(src) is None, "内核 %s 定义了策略类" % name
