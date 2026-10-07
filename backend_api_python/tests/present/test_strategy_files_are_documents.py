"""test_strategy_files_are_documents.py — P3 门禁：策略文件 = 文档（改进方案 §418-419）。

Excel 比喻：策略文件是「宏文件」，内核是「Excel 程序」。宏文件里不该有
程序性钩子（回测引擎 / 探针 / 调试编排）——那属于程序的功能。

验三件事：
  1. **禁止恶化**：各策略文件的程序性符号数 ≤ 基线（P3 起只允许下降，不许新增）。
     基线 = 2026-10-07 实测值，逐文件锁定；删完一个就下调一个，回退即 FAIL。
  2. **break 事件链完备**（§2.6c）：stateless 只产 ready（生产口径零变化）；
     stateful 产出 ready→exec→exit 完整链且字段完备。
  3. **结算分支不污染生产**：prev=None 时 evaluate 绝不产 exec/exit。

易错点：
  - 别用 `importorskip`（静默 skip = 假绿）；本文件零外部依赖，全部真跑。
  - 断言必须**非空**：空集对空集恒相等，是本项目最常踩的假绿。
  - 门禁 1 是「上限」不是「目标」：目标态 0，靠 P3-④ 逐个收敛，不在此硬卡。
"""

from __future__ import annotations

import os
import re

import pytest

from app.market_cn.auto.core.present.contract import DayInput, Progress

# tests/present → tests → backend_api_python
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STRAT_DIR = os.path.join(_ROOT, "app", "market_cn", "auto", "strategies")

#: 程序性符号（策略文件不该有的"程序功能"；门/判定/状态才是策略内容）
PROCEDURAL_PATTERNS = (
    r"\bbacktest_stock\b",          # 回测引擎钩子
    r"\bday_prefilter\b",           # 回测预筛编排
    r"\b_probe_day\b",              # 探针日级归属
    r"\bPROBE_STAGE_RANK\b",        # 探针 stage 排名表
    r"probe\.(trace|sample|shell)\(",   # 探针打点调用
    r"\bprobe\s*=\s*probe\b",       # 探针透传形参
)

#: 逐文件基线（2026-10-07 实测）。删一个钩子 → 下调对应数字（只允许下降）。
BASELINE = {
    "base.py": 21,
    "dragon_callback.py": 25,
    "break.py": 22,
    "v1.py": 19,
    "lead_chase.py": 6,
    "knife_catch.py": 6,
    "g56.py": 6,
    "tail_oversold.py": 5,
    "relay3.py": 5,
}

#: 已清零的文件（P3-④ 完成后逐个加入；一旦加入，回退即 FAIL）
CLEANED: tuple = ()


def _count(name: str) -> int:
    with open(os.path.join(STRAT_DIR, name), encoding="utf-8") as f:
        src = f.read()
    return sum(len(re.findall(p, src)) for p in PROCEDURAL_PATTERNS)


@pytest.mark.parametrize("name,baseline", sorted(BASELINE.items()))
def test_no_new_procedural_hooks(name, baseline):
    """程序性符号数 ≤ 基线（禁止恶化）；已清零文件必须恒为 0。"""
    n = _count(name)
    cap = 0 if name in CLEANED else baseline
    assert n <= cap, (
        "%s 程序性符号 %d > 上限 %d（基线 %d）。策略文件应只含 init_state/step/"
        "evaluate + 门规则，回测/探针/调试编排属内核职责（改进方案 §418）。"
        % (name, n, cap, baseline)
    )


def test_baseline_covers_all_strategy_files():
    """基线必须覆盖全部策略文件（漏一个 = 该文件新增钩子时门禁静默放行）。"""
    actual = {f for f in os.listdir(STRAT_DIR)
              if f.endswith(".py") and f != "__init__.py"}
    assert actual == set(BASELINE), (
        "策略文件清单与基线不符（新增/删除未同步 BASELINE）: 多=%s 少=%s"
        % (sorted(actual - set(BASELINE)), sorted(set(BASELINE) - actual))
    )


# ================================================================
# break 事件链完备性（§2.6c）
# ================================================================
def _break_strategy_cls():
    """⚠ `break` 是关键字 ⇒ 不能 `from ...strategies.break import X`，走 importlib。"""
    import importlib
    mod = importlib.import_module("app.market_cn.auto.strategies.break")
    return mod.BreakStrategy


def _mk(i, o, h, l, c, v=1e6, start="2025-01-01"):
    from datetime import date, timedelta
    y, m, d = map(int, start.split("-"))
    return {"time": str(date(y, m, d) + timedelta(days=i)), "open": round(o, 3),
            "high": round(h, 3), "low": round(l, 3), "close": round(c, 3),
            "volume": v}


#: 000032 真实片段（2026-04-15 ~ 2026-07-07，56 根）—— 含 2026-06-17 断板确认日。
#: 用真实数据而非合成配方：break 的门链（min_streak/pre20/vol_r/confirm_chg/entry_gate）
#: 合成极难命中，且合成配方失效会产生"零事件 = 假绿"。
_RAW = (
    ("2026-04-15", 18.561, 19.19, 18.3713, 18.571, 27834791.0),
    ("2026-04-16", 18.581, 19.1701, 18.581, 18.9904, 24363596.0),
    ("2026-04-17", 18.7707, 18.9304, 18.4812, 18.5211, 20165075.0),
    ("2026-04-20", 18.4712, 19.0103, 18.4612, 18.7308, 16148131.0),
    ("2026-04-21", 18.561, 18.6209, 18.0019, 18.1517, 21116897.0),
    ("2026-04-22", 18.2216, 18.5211, 17.9719, 18.4013, 17663246.0),
    ("2026-04-23", 18.3913, 18.601, 18.1517, 18.2914, 15272831.0),
    ("2026-04-24", 18.1616, 18.2715, 17.6225, 17.8921, 18924133.0),
    ("2026-04-27", 17.7323, 18.1716, 17.4727, 17.952, 16937065.0),
    ("2026-04-28", 17.7723, 18.1517, 17.4528, 17.8421, 19529928.0),
    ("2026-04-29", 17.8621, 17.952, 17.6524, 17.7623, 13983465.0),
    ("2026-04-30", 17.7423, 17.8022, 17.2131, 17.313, 19817513.0),
    ("2026-05-06", 17.3928, 18.1517, 17.3928, 17.8122, 28832509.0),
    ("2026-05-07", 17.9719, 18.2914, 17.962, 18.1916, 18417367.0),
    ("2026-05-08", 18.0718, 18.4412, 17.8821, 18.2615, 16689880.0),
    ("2026-05-11", 18.4812, 19.23, 18.1417, 19.0303, 41928467.0),
    ("2026-05-12", 18.9704, 19.23, 18.6709, 18.7907, 21602462.0),
    ("2026-05-13", 18.6209, 19.4297, 18.591, 19.22, 30037808.0),
    ("2026-05-14", 19.3498, 19.4197, 18.3214, 18.3214, 24608320.0),
    ("2026-05-15", 18.2815, 18.7108, 18.1816, 18.3613, 20966907.0),
    ("2026-05-18", 18.7407, 19.2999, 18.6709, 18.8705, 31062396.0),
    ("2026-05-19", 18.8705, 19.9289, 18.7707, 19.5994, 37734026.0),
    ("2026-05-20", 19.9389, 21.3167, 19.7192, 19.9688, 52250554.0),
    ("2026-05-21", 20.1485, 20.4081, 19.0103, 19.1002, 40982577.0),
    ("2026-05-22", 19.22, 19.4297, 18.4712, 19.1601, 35502153.0),
    ("2026-05-25", 19.1601, 19.23, 18.4812, 18.8705, 26494683.0),
    ("2026-05-26", 18.7407, 18.7607, 17.8421, 18.2815, 28725348.0),
    ("2026-05-27", 18.1417, 18.4013, 17.3829, 17.4228, 25909500.0),
    ("2026-05-28", 17.4228, 17.7024, 16.9735, 17.6025, 28728056.0),
    ("2026-05-29", 17.6025, 17.7223, 16.3045, 16.4443, 33710557.0),
    ("2026-06-01", 16.4443, 16.8737, 16.3145, 16.4643, 19561122.0),
    ("2026-06-02", 16.4643, 16.5741, 15.7454, 16.015, 20542190.0),
    ("2026-06-03", 15.97, 16.28, 15.81, 15.94, 16529950.0),
    ("2026-06-04", 15.89, 17.34, 15.66, 16.36, 30995395.0),
    ("2026-06-05", 16.46, 16.48, 15.9, 15.9, 22912160.0),
    ("2026-06-08", 15.49, 15.73, 14.96, 15.1, 20090709.0),
    ("2026-06-09", 15.33, 15.65, 15.15, 15.54, 14586998.0),
    ("2026-06-10", 15.43, 16.54, 15.35, 15.64, 22551796.0),
    ("2026-06-11", 15.8, 17.2, 15.46, 17.2, 36421092.0),
    ("2026-06-12", 17.4, 18.92, 16.6, 18.92, 105555592.0),
    ("2026-06-15", 19.2, 20.81, 18.75, 20.81, 100480861.0),
    ("2026-06-16", 21.47, 22.89, 20.99, 22.89, 101039698.0),
    ("2026-06-17", 23.21, 23.72, 21.8, 23.22, 160479476.0),
    ("2026-06-18", 23.35, 25.54, 23.23, 24.6, 147395482.0),
    ("2026-06-22", 24.46, 25.2, 23.9, 24.59, 90944797.0),
    ("2026-06-23", 24.8, 24.8, 22.51, 23.04, 83519604.0),
    ("2026-06-24", 23.39, 25.34, 22.98, 25.34, 104299285.0),
    ("2026-06-25", 26.48, 27.19, 24.5, 25.68, 110324253.0),
    ("2026-06-26", 25.39, 25.74, 24.17, 24.22, 82946577.0),
    ("2026-06-29", 24.98, 25.22, 22.39, 24.08, 85614561.0),
    ("2026-06-30", 24.05, 25.48, 23.76, 24.95, 68662200.0),
    ("2026-07-01", 25.19, 25.6, 23.82, 24.0, 64562700.0),
    ("2026-07-02", 23.04, 23.52, 21.6, 21.9, 68514900.0),
    ("2026-07-03", 21.63, 22.1, 20.87, 20.97, 49602700.0),
    ("2026-07-06", 20.97, 21.68, 20.05, 21.28, 57592300.0),
    ("2026-07-07", 21.46, 22.39, 20.82, 21.36, 60263800.0),
)


def _break_bars():
    return [{"time": d, "open": o, "high": h, "low": l, "close": c,
             "volume": v} for d, o, h, l, c, v in _RAW]


def _fold(strategy, bars, code, stateful):
    """最小折叠（同 fold_range 序；stateful 决定是否传 prev）。"""
    state, j = None, 0
    from app.market_cn.auto.core.present.contract import InsufficientHistory
    while j < len(bars):
        try:
            state = strategy.init_state(code, bars[:j])
            break
        except InsufficientHistory:
            j += 1
    assert state is not None, "seed 失败"
    evs, prev = [], None
    for k in range(j, len(bars)):
        got = strategy.evaluate(state, DayInput(code, bars[k], None), prev) or []
        evs.extend(got)
        if got and stateful:
            prev = got[-1]
        state = strategy.step(state, bars[k])
    return evs


def test_break_stateless_produces_only_ready():
    """stateless（生产投影口径）只产 ready —— 结算分支不得污染生产。"""
    s = _break_strategy_cls()()
    evs = _fold(s, _break_bars(), "600000", stateful=False)
    assert evs, "crafted bars 未产出任何事件（配方失效 = 假绿）"
    stages = {e.stage for e in evs}
    assert "exec" not in stages and "exit" not in stages, (
        "stateless 下产出 exec/exit —— 结算分支污染了生产投影口径（prev=None 必须只判 ready）"
    )
    assert stages == {"ready"}


def test_break_stateful_produces_full_chain():
    """stateful 产出 ready→exec→exit 完整链，且 trade 关键字段完备。"""
    from app.market_cn.auto.core.replay import DailyFeed, replay, TradesCollector
    s = _break_strategy_cls()()
    bars = _break_bars()
    evs = _fold(s, bars, "600000", stateful=True)
    stages = [e.stage for e in evs]
    assert "ready" in stages, "无 ready（配方失效 = 假绿）"
    assert "exec" in stages, "无 exec —— break 结算事件未补（§2.6c）"
    assert stages.index("exec") > stages.index("ready"), "exec 必须晚于 ready（T+1）"

    coll = TradesCollector(code="600000", strategy="break")
    res = replay(s, "600000", DailyFeed(bars), collectors=[coll])
    assert res.trades, "完整链未闭合成 trade"
    t = res.trades[0]
    for k in ("entry_date", "entry_price", "exit_date", "exit_price",
              "exit_reason", "return_pct"):
        assert t.get(k) is not None, "trade 缺字段 %s（载荷未对齐 canonical）" % k
    assert float(t["entry_price"]) > 0 and float(t["exit_price"]) > 0
    # T+1：入场必须晚于信号日
    assert t["entry_date"] > t["d0_date"], "入场未满足 T+1"
