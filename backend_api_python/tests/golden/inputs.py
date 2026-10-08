"""tests/golden/inputs.py — golden 基线的**固定输入集**（改进方案 §4-P1-⑤）。

来源：golden 逐笔对拍任务 | 产出：2026-10-07 | 维护：改配方必须重新冻结基线

设计原则：
  1. **确定性**：纯 Python，无随机、无 DB、无网络 —— 任何机器上重跑得到同一份 bars。
  2. **必须非空**：每份输入都要保证至少产出 1 笔 trade（本项目最常踩的假绿就是
     「空集对空集恒相等」，见 test_strategy_files_are_documents / test_projection 头注）。
  3. **真实数据优先**：`break_real_000032()` 是 000032（深南电A）2026-04-15~07-07 的
     真实 56 根日 K（从 tests/present/test_projection._RAW 提取，零 DB 依赖）；
     合成配方仅用于覆盖真实片段覆盖不到的门链。

⚠️ 为什么 dragon 走 `use_prefilter=False`：
    U1~U4 统一前置过滤要求「换手率≥3%」+「流通市值 20~500 亿」，合成 bars 的 volume
    与 stock_info 很难同时满足 ⇒ 实测 `u_fails=['U2换手0.2','U3市值922亿']` 直接拒。
    既有 `test_dragon.test_dragon_backtest_trades_match_old` 同样用 `use_prefilter=False`
    绕开（预过滤是策略层前置门，不是回测引擎语义），golden 沿用同一口径。
    ⚠️ 这意味着 golden **不覆盖 U1~U4**：预过滤的等价性由 test_dragon/test_g56 单独背书。

⚠️ g56 不在本输入集内：它的 `begin_day` 需要横截面池，池聚合依赖 psycopg2（本沙箱无
   psycopg2，实测 `池聚合失败, 当日横截面门不可用: No module named 'psycopg2'`）。
   g56 的 golden 必须在有库/有 psycopg2 的环境另生成 —— 见 docs/口径差异报告.md 的「缺口」。
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))          # .../tests/golden
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))     # backend_api_python
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# ================================================================
# 真实数据片段（000032 深南电A，2026-04-15 ~ 2026-07-07，56 根）
# ================================================================
#: 与 tests/present/test_projection._RAW / test_strategy_files_are_documents._RAW 同源。
#: 含 2026-06-17 断板确认日 —— break 的门链（min_streak/pre20/vol_r/confirm_chg/entry_gate）
#: 合成极难命中，故必须用真实片段（否则零事件 = 假绿）。
BREAK_REAL_RAW = (
    ('2026-04-15', 18.561, 19.19, 18.3713, 18.571, 27834791.0),
    ('2026-04-16', 18.581, 19.1701, 18.581, 18.9904, 24363596.0),
    ('2026-04-17', 18.7707, 18.9304, 18.4812, 18.5211, 20165075.0),
    ('2026-04-20', 18.4712, 19.0103, 18.4612, 18.7308, 16148131.0),
    ('2026-04-21', 18.561, 18.6209, 18.0019, 18.1517, 21116897.0),
    ('2026-04-22', 18.2216, 18.5211, 17.9719, 18.4013, 17663246.0),
    ('2026-04-23', 18.3913, 18.601, 18.1517, 18.2914, 15272831.0),
    ('2026-04-24', 18.1616, 18.2715, 17.6225, 17.8921, 18924133.0),
    ('2026-04-27', 17.7323, 18.1716, 17.4727, 17.952, 16937065.0),
    ('2026-04-28', 17.7723, 18.1517, 17.4528, 17.8421, 19529928.0),
    ('2026-04-29', 17.8621, 17.952, 17.6524, 17.7623, 13983465.0),
    ('2026-04-30', 17.7423, 17.8022, 17.2131, 17.313, 19817513.0),
    ('2026-05-06', 17.3928, 18.1517, 17.3928, 17.8122, 28832509.0),
    ('2026-05-07', 17.9719, 18.2914, 17.962, 18.1916, 18417367.0),
    ('2026-05-08', 18.0718, 18.4412, 17.8821, 18.2615, 16689880.0),
    ('2026-05-11', 18.4812, 19.23, 18.1417, 19.0303, 41928467.0),
    ('2026-05-12', 18.9704, 19.23, 18.6709, 18.7907, 21602462.0),
    ('2026-05-13', 18.6209, 19.4297, 18.591, 19.22, 30037808.0),
    ('2026-05-14', 19.3498, 19.4197, 18.3214, 18.3214, 24608320.0),
    ('2026-05-15', 18.2815, 18.7108, 18.1816, 18.3613, 20966907.0),
    ('2026-05-18', 18.7407, 19.2999, 18.6709, 18.8705, 31062396.0),
    ('2026-05-19', 18.8705, 19.9289, 18.7707, 19.5994, 37734026.0),
    ('2026-05-20', 19.9389, 21.3167, 19.7192, 19.9688, 52250554.0),
    ('2026-05-21', 20.1485, 20.4081, 19.0103, 19.1002, 40982577.0),
    ('2026-05-22', 19.22, 19.4297, 18.4712, 19.1601, 35502153.0),
    ('2026-05-25', 19.1601, 19.23, 18.4812, 18.8705, 26494683.0),
    ('2026-05-26', 18.7407, 18.7607, 17.8421, 18.2815, 28725348.0),
    ('2026-05-27', 18.1417, 18.4013, 17.3829, 17.4228, 25909500.0),
    ('2026-05-28', 17.4228, 17.7024, 16.9735, 17.6025, 28728056.0),
    ('2026-05-29', 17.6025, 17.7223, 16.3045, 16.4443, 33710557.0),
    ('2026-06-01', 16.4443, 16.8737, 16.3145, 16.4643, 19561122.0),
    ('2026-06-02', 16.4643, 16.5741, 15.7454, 16.015, 20542190.0),
    ('2026-06-03', 15.97, 16.28, 15.81, 15.94, 16529950.0),
    ('2026-06-04', 15.89, 17.34, 15.66, 16.36, 30995395.0),
    ('2026-06-05', 16.46, 16.48, 15.9, 15.9, 22912160.0),
    ('2026-06-08', 15.49, 15.73, 14.96, 15.1, 20090709.0),
    ('2026-06-09', 15.33, 15.65, 15.15, 15.54, 14586998.0),
    ('2026-06-10', 15.43, 16.54, 15.35, 15.64, 22551796.0),
    ('2026-06-11', 15.8, 17.2, 15.46, 17.2, 36421092.0),
    ('2026-06-12', 17.4, 18.92, 16.6, 18.92, 105555592.0),
    ('2026-06-15', 19.2, 20.81, 18.75, 20.81, 100480861.0),
    ('2026-06-16', 21.47, 22.89, 20.99, 22.89, 101039698.0),
    ('2026-06-17', 23.21, 23.72, 21.8, 23.22, 160479476.0),
    ('2026-06-18', 23.35, 25.54, 23.23, 24.6, 147395482.0),
    ('2026-06-22', 24.46, 25.2, 23.9, 24.59, 90944797.0),
    ('2026-06-23', 24.8, 24.8, 22.51, 23.04, 83519604.0),
    ('2026-06-24', 23.39, 25.34, 22.98, 25.34, 104299285.0),
    ('2026-06-25', 26.48, 27.19, 24.5, 25.68, 110324253.0),
    ('2026-06-26', 25.39, 25.74, 24.17, 24.22, 82946577.0),
    ('2026-06-29', 24.98, 25.22, 22.39, 24.08, 85614561.0),
    ('2026-06-30', 24.05, 25.48, 23.76, 24.95, 68662200.0),
    ('2026-07-01', 25.19, 25.6, 23.82, 24.0, 64562700.0),
    ('2026-07-02', 23.04, 23.52, 21.6, 21.9, 68514900.0),
    ('2026-07-03', 21.63, 22.1, 20.87, 20.97, 49602700.0),
    ('2026-07-06', 20.97, 21.68, 20.05, 21.28, 57592300.0),
    ('2026-07-07', 21.46, 22.39, 20.82, 21.36, 60263800.0),
)

#: 000032 的 stock_info（深南电A 实际流通盘量级；U1~U3 由此满足）
STOCK_INFO_BREAK = {"circ_shares": 5.5e8, "total_shares": 5.5e8, "name": "深南电A"}

#: dragon 合成配方用的 stock_info（与 tests/present/test_dragon.SI 同源）
STOCK_INFO_DRAGON = {"circ_shares": 5e8, "total_shares": 8e8}


# ================================================================
# 输入集
# ================================================================
def _mk(i: int, o, h, l, c, v: float = 1e6, start: str = "2025-01-01") -> dict:
    from datetime import date, timedelta
    y, m, d = map(int, start.split("-"))
    return {"time": str(date(y, m, d) + timedelta(days=i)), "open": round(o, 3),
            "high": round(h, 3), "low": round(l, 3), "close": round(c, 3),
            "volume": v}


def break_real_000032() -> list[dict]:
    """break 的真实数据输入（56 根，含 2026-06-17 断板确认日）。

    实测产出 **1 笔 trade**（signal_date=2026-06-17, entry=23.35, exit=24.22,
    exit_day=6, return_pct=3.73, peak_return_pct=16.45, exit_rule=trail）。
    """
    return [{"time": d, "open": round(o, 3), "high": round(h, 3), "low": round(l, 3),
             "close": round(c, 3), "volume": v} for d, o, h, l, c, v in BREAK_REAL_RAW]


def dragon_crafted() -> list[dict]:
    """dragon 的合成配方（crafted 龙回头波次 + fuzz 尾段）。

    沿用 `test_dragon.test_dragon_backtest_trades_match_old` 的构造：
    crafted_bars() 打出 lu=25 四连板 + 深度回调 → D0=32 全门过；尾接 fuzz 40 根给出
    足够的出场视野。配 `use_prefilter=False`（见模块头注）。
    """
    from tests.present.test_dragon import _fuzz_bars, crafted_bars
    base = crafted_bars()
    tail = _fuzz_bars(99, 40)
    off = len(base)
    return base + [{**b, "time": _mk(off + j, 0, 0, 0, 0)["time"]} for j, b in enumerate(tail)]


def crafted_bars_for_divergence() -> list[dict]:
    """「末日未平」分歧探针用：只截到入场日（bar[32]），尾段由测试自造。

    ⚠️ 必须**截到 bar[32]（含入场日）**：再多一根 bar[33] 就是原配方的深跌腿，
    追踪止损会在 d=2 必然触发 ⇒ 根本走不到未平分支（2026-10-08 实测踩过）。
    """
    from tests.present.test_dragon import crafted_bars
    return crafted_bars()[:33]


def dragon_d1_stop_hit() -> list[dict]:
    """★口径差异探针：**D1 止损触线**——切口 8 唯一改变行为的情形。

    §2.2.1-4 裁定的语义：「D2 起评」= **D1 的止损触线不产生出场**（当日不可卖，
    触线不可行动）；另一派是「D1 记触发、D2 开盘成交」。本方案选前者。
    本输入把 D1（入场日）打成深跌腿（低点 125 vs 入场 155，-19%），让止损线
    （stop_loss=-8% → 142.6）在 D1 当天被击穿：
      · 旧口径（min_sell_day=1）→ D1 即出场，exit_day=1；
      · 新口径（min_sell_day=2）→ D1 不可卖，顺延到 D2 及以后。
    ⚠️ 不加这个样本，D1/D2 两口径在所有样本上都逐位一致 ⇒ 口径差异报告 = 空证。
    """
    from tests.present.test_dragon import crafted_bars
    bars = crafted_bars()
    out = list(bars)
    # ⚠ 入场日 = bars[32]（d0=bar[31], entry=bar[32], 即 D1）。改 bars[-1] 是 D2，
    #   止损根本不落在 D1 ⇒ 两口径逐位一致 = 空证（2026-10-07 实测踩过）。
    out[32] = {**bars[32], "open": 156.0, "high": 157.0, "low": 125.0, "close": 128.0}
    return out


def dragon_unclosed() -> list[dict]:
    """极端案例：**短尾段**（入场后很快到数据尽头）。

    ⚠️ 名不副实修正（2026-10-07 实测）：原命名 `dragon_unclosed`（末日未平）
    实际上 exit_day=2 就出场了，**并未**走到未平分支。现改名为如实描述的
    「短尾段」；真正的「末日未平」覆盖仍是缺口 —— 见 docs/口径差异报告.md「缺口」。
    """
    from tests.present.test_dragon import crafted_bars
    base = crafted_bars()
    # 只留 2 根尾段：D0=32 入场 D1=33，之后一根即尽头
    return base + [{**b, "time": _mk(len(base) + j, 0, 0, 0, 0)["time"]}
                   for j, b in enumerate(base[-1:])]


def dragon_gap_break() -> list[dict]:
    """极端案例：**日期断档（停牌 gap）** —— 触发「日期不连续 → 整票重建」。

    注意：gap 影响的是**生命周期 runner 的重建判定**，不是 trade 本身；本输入用于
    验证 gap 存在时 golden 输出仍稳定（不因重算路径不同而漂移）。
    """
    bars = dragon_crafted()
    out = []
    for i, b in enumerate(bars):
        if i == 40:                      # 挖掉一根 → 日期断档
            continue
        out.append(b)
    return out


def g56_universe(seed: int = 1) -> dict[str, list[dict]]:
    """g56 的合成宇宙（多票横截面池）—— 与 `test_g56._gen` 同源同种子。

    ⚠️ g56 是**横截面策略**：`begin_day` 需要同日其他票做分位 ⇒ 单票 golden 无意义，
    必须整个宇宙一起喂（`replay_batch`）。种子 1/7/12 各含 ≥1 笔真实交易
    （`test_g56_has_positive_signals` 实测 total >= 3），其余种子为负例对照。

    ⚠️ 旧侧取数需 `hub.all_codes` 返回池内代码 + `prewarm(universe, date)`；
    这些是**取数接线**，不是规则，冻结器负责接好（不进输入集本身）。
    """
    from tests.present.test_g56 import _gen
    return _gen(seed)


#: g56 种子集（只取含正例的三个；负例种子不入 golden —— 空对空是假绿）
G56_SEEDS = (1, 7, 12)


#: golden 输入集注册表
#:   单票条目用 `build`；池化条目用 `universe` + `pooled=True`。
#:   use_prefilter=False 见模块头注（U1~U4 是策略前置门，不属回测引擎语义）
INPUT_SETS = (
    {"name": "break_real_000032", "strategy": "break", "code": "000032",
     "build": break_real_000032, "stock_info": STOCK_INFO_BREAK, "kwargs": {}},
    {"name": "dragon_crafted", "strategy": "dragon_callback", "code": "600000",
     "build": dragon_crafted, "stock_info": STOCK_INFO_DRAGON,
     "kwargs": {"use_prefilter": False}},
    {"name": "dragon_d1_stop_hit", "strategy": "dragon_callback", "code": "600000",
     "build": dragon_d1_stop_hit, "stock_info": STOCK_INFO_DRAGON,
     "kwargs": {"use_prefilter": False}},
    {"name": "dragon_short_tail", "strategy": "dragon_callback", "code": "600000",
     "build": dragon_unclosed, "stock_info": STOCK_INFO_DRAGON,
     "kwargs": {"use_prefilter": False}},
    {"name": "dragon_gap_break", "strategy": "dragon_callback", "code": "600000",
     "build": dragon_gap_break, "stock_info": STOCK_INFO_DRAGON,
     "kwargs": {"use_prefilter": False}},
    # ---- 池化（g56，横截面） ----
    *({"name": "g56_universe_seed%d" % s, "strategy": "g56", "code": "*",
       "build": None, "universe": (lambda s=s: g56_universe(s)),
       "stock_info": None, "kwargs": {}, "pooled": True}
      for s in G56_SEEDS),
)


# ================================================================
# P1.5-② 盘中策略合成条目（2026-10-08，补齐 knife/tail golden 覆盖）
# 来源：改进方案_v2.1 §4-P1.5-② | 配方：tests/present/common（已验证会触发 14:56 ready）
# 旧侧 = run_all_intraday 时间线引擎（合成帧），新侧 = replay + 快照 ctx（同一 evaluate）
# ================================================================
from tests.present.common import (KNIFE_HIST_CLOSES, TAIL_HIST_CLOSES,  # noqa: E402
                                  gen_hist_bars, knife_day_rows, tail_day_rows)

_D0 = {"time": "2026-10-05", "open": 96.4, "high": 96.5, "low": 84.0,
       "close": 85.2, "volume": 1200.0}
_D1 = {"time": "2026-10-06", "open": 86.5, "high": 87.0, "low": 85.0,
       "close": 86.2, "volume": 900.0}


def _knife_bars():
    return gen_hist_bars("600001", KNIFE_HIST_CLOSES) + [_D0, _D1]


def _knife_rows():
    return {_D0["time"]: knife_day_rows(_D0["time"], pc=100.0)}


def _tail_bars():
    return gen_hist_bars("600001", TAIL_HIST_CLOSES) + [_D0, _D1]


def _tail_rows():
    return {_D0["time"]: tail_day_rows(_D0["time"], pc=100.0)}


def _intraday_spec(name, strategy, build, rows_fn):
    import hashlib
    import json
    bars = build()
    fp = hashlib.sha256(json.dumps([bars, rows_fn()], sort_keys=True,
                                   default=str).encode("utf-8")).hexdigest()
    return {
        "name": name, "strategy": strategy, "code": "600001",
        "stock_info": {}, "kwargs": {}, "build": build,
        "intraday": True, "day_rows": rows_fn,
        "mkt_gain": -3.0, "pc": 100.0,
        "n_bars": len(bars), "fingerprint": fp,
    }


INPUT_SETS = INPUT_SETS + (
    _intraday_spec("knife_synth_1456", "knife_catch", _knife_bars, _knife_rows),
    _intraday_spec("tail_synth_1456", "tail_oversold", _tail_bars, _tail_rows),
)
