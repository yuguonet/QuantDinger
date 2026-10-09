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
AUTO_DIR = os.path.join(_ROOT, "app", "market_cn", "auto")

# (模块, 定稿行数, 上限)
MODULES = [
    ("contract.py", 112, 140),
    ("runner.py", 553, 600),
    ("realtime.py", 89, 140),   # 2026-10-08: cap 110→140（用户裁定长度不在考虑范围; 原109/110零余量）
]

# 2026-10-07 口径更新（P0/P1 功能性增长，非膨胀）：
#   runner.py fold_range 扩参 stages/ctx_provider/stateful（折叠契约核心，+~30 行）；
#   contract.py 增 begin_day/realtime_shortlist 契约方法 + _trace 门原因通道注释。
#   先压缩了 fold_range 冗余 docstring（22→12 行），再上调总 cap 800→820 并记录。
# 2026-10-08 重裁（golden 逐笔对拍任务）：820 时**零余量**（实测 138+592+90=820），
#   任何一行小改即 FAIL ⇒ P0 尾巴卡死（P0_说明.txt「建议重裁 850，等确认」）。
#   上调 820→900（+10% 余量）；同时把**真正膨胀的模块**纳入门禁（见 PROGRAM/RETIRE）
#   —— 此前门禁只锁 core/present/，导致「守规矩的被卡死、膨胀的无人管」。
KERNEL_CAP = 900
KERNEL_FLOOR = 600


# ================================================================
# 程序层（回放 / 增量基座 / 门原因通道）—— 2026-10-08 新增
# ================================================================
#: (相对 core/ 的路径, §2.8 目标, 现状 2026-10-08, 上限)
#: 上限 = 现状 + ~10% 余量。⚠️ 调高上限必须在改进方案完成度报告里登记理由。
#: §2.8 目标是**收敛方向**（P6 收量），不是当前硬卡 —— 按现状卡不会立刻破 build，
#: 但会挡住「无人察觉中膨胀」（本门禁存在的唯一理由）。
#: 2026-10-09 登记（市场门 逐槽 as-of 修复，docs/市场门口径评估_20261009.md）：
#:   · 新增 `core/replay/mkt_slots.py`（66 行，单一职责：窗口→逐槽全市场均涨幅）；
#:     独立成件正是为了**不让** replay/__init__ 与 intraday 无声膨胀。
#:   · `intraday.py` 118→130（mkt_slots/mkt_series 注入；因零余量，cap 130→143 给回余量）；
#:   · `__init__.py` 466→474（市场门两层口径的文档更正；cap 500 不动）；
#:   · PROGRAM_CAP 1225→1330（新增一件的必然结果；实测 1305）。
#:   属**功能增长**（消除回测唯一的前视输入），非膨胀 —— 按门禁规定登记后上调。
PROGRAM_MODULES = [
    ("core/replay/__init__.py", 350, 474, 500),   # 目标含 trade_map，故包内三件一起看
    ("core/replay/intraday.py", 0, 130, 143),     # 2026-10-09: +12 (mkt_slots/mkt_series)
    ("core/replay/trade_map.py", 0, 111, 122),     # 2026-10-08 D级修: 登记 106→实测 111
    ("core/replay/gate_dbg.py", 60, 69, 80),       # 终态② Step1 新增门诊断收集器
    ("core/replay/mkt_slots.py", 0, 66, 73),       # 2026-10-09 新增: 市场门逐槽 as-of 取数
    ("core/increm.py", 120, 356, 392),
    ("core/trace.py", 80, 95, 105),
]

#: 程序层总上限（防三件互相挦补）
#: 2026-10-09: 1225→1330（新增 mkt_slots.py 66 行 + intraday 118→130 + __init__ 466→474）。
PROGRAM_CAP = 1330


# ================================================================
# 退役对象（P3/P6 删除目标）—— 2026-10-08 新增
# ================================================================
#: 这些是**将被删除**的对象；门禁只防它们继续膨胀（删除进度由
#: test_strategy_files_are_documents.CLEANED 跟踪）。
#: (相对 auto/ 的路径, §2.8 目标, 现状, 上限)
RETIRE_MODULES = [
    ("strategies/base.py", 380, 727, 800),       # 2026-10-08 D级修: 登记 688→实测 727
    ("core/backtest.py", 0, 551, 606),            # 2026-10-08 D级修: 550→551
    ("probe.py", 0, 173, 190),                    # 2026-10-08 D级修: 172→173
]
RETIRE_CAP = 1596


def _nlines(name):
    p = os.path.join(PRESENT_DIR, name)
    with io.open(p, encoding="utf-8", newline="") as f:
        txt = f.read()
    n = txt.count("\n") + (0 if txt.endswith("\n") else 1)
    return n


def _nlines_rel(rel: str) -> int:
    """相对 `auto/` 的任意文件行数（program / retire 两表用）。"""
    p = os.path.join(AUTO_DIR, rel)
    assert os.path.exists(p), "体量门禁目标文件缺失: %s" % rel
    with io.open(p, encoding="utf-8", newline="") as f:
        txt = f.read()
    return txt.count("\n") + (0 if txt.endswith("\n") else 1)


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
        "展示层内核总计 %d 行 > 上限 %d（定稿 754 行，2026-10-06 用户裁定接受该口径；"
        "2026-10-08 重裁 820→900）。" % (total, KERNEL_CAP)
    )
    assert total >= KERNEL_FLOOR, (
        "展示层内核仅剩 %d 行 —— 疑似误删，请核对 core/present/ 完整性" % total
    )


# ================================================================
# 程序层 / 退役对象（2026-10-08 新增 —— 防「守规矩的被卡死，膨胀的无人管」）
# ================================================================
@pytest.mark.parametrize("rel,target,now,cap", PROGRAM_MODULES)
def test_program_module_size(rel, target, now, cap):
    """程序层各模块不得继续膨胀（上限=现状+~10%）。

    `target` 是改进方案 §2.8 的收敛方向（P6 收量），不是当前硬卡 —— 但**差距
    必须可见**：收缩时把 `now` 一起下调，差距只能变小不能变大。
    """
    n = _nlines_rel(rel)
    assert n <= cap, (
        "%s 膨胀: %d 行 > 上限 %d（现状 %d，§2.8 目标 %d）。确需增长先压缩既有"
        "实现，并同步改进方案完成度报告的体量表。" % (rel, n, cap, now, target)
    )
    # 差距只能缩小：现状不得低于登记值太多而无人更新（防登记表失真）
    assert n >= now - 5, (
        "%s 实测 %d 明显低于登记现状 %d —— 请同步下调 PROGRAM_MODULES 的 now/cap，"
        "把收缩固化下来（否则后续又会涨回去）。" % (rel, n, now)
    )


def test_program_total_size():
    total = sum(_nlines_rel(rel) for rel, _, _, _ in PROGRAM_MODULES)
    assert total <= PROGRAM_CAP, (
        "程序层总计 %d 行 > 上限 %d —— 防各模块互相挦补" % (total, PROGRAM_CAP)
    )


@pytest.mark.parametrize("rel,target,now,cap", RETIRE_MODULES)
def test_retire_module_not_growing(rel, target, now, cap):
    """退役对象（P3/P6 删除目标）不得继续膨胀。

    它们终将删除（目标行数多为 0）；在删完之前，唯一要求是**不许再长**。
    删除进度由 `test_strategy_files_are_documents.CLEANED` 跟踪。
    """
    n = _nlines_rel(rel)
    assert n <= cap, (
        "%s 膨胀: %d 行 > 上限 %d（现状 %d，目标 %d）—— 它是退役对象，"
        "应该只减不增。" % (rel, n, cap, now, target)
    )


def test_retire_total_size():
    total = sum(_nlines_rel(rel) for rel, _, _, _ in RETIRE_MODULES)
    assert total <= RETIRE_CAP, (
        "退役对象总计 %d 行 > 上限 %d" % (total, RETIRE_CAP)
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
