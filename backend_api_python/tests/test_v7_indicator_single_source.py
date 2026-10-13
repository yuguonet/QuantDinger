# -*- coding: utf-8 -*-
"""P7 指标单一真源 —— 三份实现收编后的防漂移锁。

★ 为什么必须有这个文件
  指标曾有三份实现（utils / legacy `_calc_*` / agentcore 副本）。P7 把
  agentcore 那份收编进 `app/utils/indicators.py`，legacy 那份**暂时保留**
  （它是生产主力，改动面大，留到下一阶段）。
  ⇒ 现在最大的风险不是"还没合并完"，而是**legacy 那份悄悄改了、utils 没跟上**
  —— 于是同一只票两条链路给出不同 MACD，且没人发现（口径分歧的经典形态）。
  本文件用数值对拍把这个风险钉死：legacy 一漂移，这里立刻红。

★ 测试纪律
  对拍用**合成序列**而不是网络取数：网络抖会让测试假红，
  而我们要守的是"算法是否一致"，与数据真假无关。
"""
from __future__ import annotations

import math
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent          # backend_api_python

# 收编进 utils 的派生指标（原 agentcore B 类搬运资产）
DERIVED = ("atr", "mfi", "cmf", "hist_volatility", "ma_convergence", "obv")


# ---------------------------------------------------------------- 数据
def _ohlcv(n: int = 60):
    """合成 OHLCV：趋势 + 双周期波动 + 微噪，足够长到暴露 EMA 初始化差异。"""
    highs, lows, closes, volumes = [], [], [], []
    v = 10.0
    for i in range(n):
        v += math.sin(i / 5.0) * 0.35 + math.cos(i / 13.0) * 0.2
        v *= 1.001
        c = round(v, 4)
        closes.append(c)
        highs.append(round(c + 0.3 + 0.1 * math.sin(i / 3.0), 4))
        lows.append(round(max(0.5, c - 0.3 - 0.1 * math.cos(i / 4.0)), 4))
        volumes.append(int(1_000_000 + 200_000 * math.sin(i / 7.0)))
    return highs, lows, closes, volumes


def _numeric(d):
    out = {}
    for k, v in d.items():
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            out[k] = float(v)
    return out


# ---------------------------------------------------------------- 1. 再导出
# ★ 2026-10-13 P16：`test_agentcore_reexports_utils` 已随 app/agentcore 退役删除。
#   它守的是「agentcore/tools/indicators.py 必须是再导出层」，该层已归档到
#   del/agentcore_retired_20261013/。单一真源本身由下面
#   `test_derived_algorithm_defined_only_in_utils` 继续守（utils 是唯一定义处）。


def test_derived_algorithm_defined_only_in_utils():
    """静态：`def atr(` / `def mfi(` … 在 app/ 下**只能**出现在 utils。

    ★ 这条守的是"别再复制一份"。收编之后如果有人图省事又把算法粘到
      agentcore 或别处，全量测试照样全绿 —— 只有静态扫描拦得住。
    """
    offenders = []
    for py in sorted((ROOT / "app").rglob("*.py")):
        if "__pycache__" in py.parts:
            continue
        rel = str(py.relative_to(ROOT)).replace("\\", "/")
        if rel == "app/utils/indicators.py":
            continue                      # 真源，唯一允许处
        try:
            src = py.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for name in DERIVED:
            if re.search(rf"^def {name}\(", src, flags=re.M):
                offenders.append(f"{rel}: 又定义了一份 {name}()")
    assert not offenders, (
        "派生指标的定义必须只有 utils 一处（其余一律再导出）：\n"
        + "\n".join(offenders)
    )


# ---------------------------------------------------------------- 2. 对拍
def test_derived_match_between_utils_and_legacy():
    """utils 与 legacy `_calc_*`：派生指标数值必须一致（防 legacy 漂移）。"""
    from app.agent.tools.finance import _analysis_utils as AU
    from app.agent.tools.finance import analysis_tools as AT
    from app.utils import indicators as U

    leg = {
        "atr": AT._calc_atr, "mfi": AT._calc_mfi, "cmf": AT._calc_cmf,
        "hist_volatility": AT._calc_hist_volatility,
        "ma_convergence": AT._calc_ma_convergence,
        "obv": AU._calc_obv,
    }
    highs, lows, closes, volumes = _ohlcv()

    for name, leg_fn in leg.items():
        u_fn = getattr(U, name)
        if name == "obv":
            a, b = u_fn(closes, volumes), leg_fn(closes, volumes)
        elif name in ("atr",):
            a, b = u_fn(highs, lows, closes), leg_fn(highs, lows, closes)
        elif name in ("mfi", "cmf"):
            a, b = u_fn(highs, lows, closes, volumes), leg_fn(highs, lows, closes, volumes)
        elif name == "hist_volatility":
            a, b = u_fn(closes), leg_fn(closes)
        else:
            a, b = u_fn(closes), leg_fn(closes)

        na, nb = _numeric(a), _numeric(b)
        common = set(na) & set(nb)
        assert common, f"{name}: 两边返回结构已完全分叉（无共同数值键），必须人工确认"
        worst = max((abs(na[k] - nb[k]) for k in common), default=0.0)
        assert worst < 1e-9, (
            f"{name} 口径漂移：utils 与 legacy 最大差 {worst:.6g} "
            f"—— 指标从此有两个真相，先判定谁对再改"
        )


def test_core_indicators_match_between_utils_and_legacy():
    """MACD / RSI / BOLL：utils 与 legacy 数值必须一致。

    ★ 这三件 legacy 是**自带实现**（`_calc_macd` 等），README §四之二
      点名它是"在 agent 侧重写已有指标"的反面例子。P7 暂不改它，
      但用对拍锁住 —— 一旦有人动它而没动 utils，这里立刻红。
    """
    from app.agent.tools.finance import analysis_tools as AT
    from app.utils import indicators as U

    _, _, closes, _ = _ohlcv(80)

    # ★ 精度口径：legacy 侧 `safe_round` 会截断（dif 4 位 / rsi 2 位 / boll 2 位），
    #   所以比的是"按 legacy 精度对齐后是否完全相等"，而不是给一个模糊容差。
    #   这样既不受浮点误差干扰，判据又比"小于某个 ε"更严 —— 差一个精度位就会红。

    # MACD：utils 返回三序列，legacy 返回最新值 dict ⇒ 比"最后一根"
    u_dif, u_dea, _hist = U.calc_macd(closes, 12, 26, 9)
    l_macd = AT._calc_macd(closes, 12, 26, 9)
    assert round(u_dif[-1], 4) == l_macd["dif"], (
        f"MACD dif 漂移：utils={u_dif[-1]:.6f} legacy={l_macd['dif']:.6f}")
    assert round(u_dea[-1], 4) == l_macd["dea"], (
        f"MACD dea 漂移：utils={u_dea[-1]:.6f} legacy={l_macd['dea']:.6f}")

    # RSI：必须**同周期**比（legacy 默认给 6/12/24，utils 默认 14）
    for period in (6, 14, 24):
        u_rsi = U.rsi(closes, period)
        l_rsi = AT._calc_rsi(closes, [period])[f"rsi{period}"]
        assert round(u_rsi, 2) == l_rsi, (
            f"RSI{period} 漂移：utils={u_rsi:.6f} legacy={l_rsi:.6f}")

    # BOLL：utils 只给带宽（%），legacy 给完整三轨
    u_bw = U.calc_bollinger_bw(closes, 20, 2)
    l_boll = AT._calc_boll(closes, 20, 2.0)
    assert round(u_bw, 2) == l_boll["bandwidth"], (
        f"BOLL 带宽漂移：utils={u_bw:.4f} legacy={l_boll['bandwidth']:.4f}")


def test_ma_convergence_short_series_contract():
    """`ma_convergence` 不足 60 根必须返回 `{"converged": False}` 而不是 error。

    ★ 这是"判断未成立"与"取数失败"的分界，混在一起会让调用方把
      "数据不够"误当成"接口坏了"。
    """
    from app.utils import indicators as U

    out = U.ma_convergence([10.0] * 30)
    assert out == {"converged": False}, out
    assert "error" not in out, "短序列该是'未成立'，不是'失败'"
