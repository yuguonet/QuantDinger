"""P8 指标基线 —— 合成数据与快照采集的**单一真源**。

被两处共用：
  - `tests/test_v8_legacy_indicator_delegation.py`（回归断言）
  - `tmp/p8_snapshot.py --save/--check`（合法变更时重新固化基线）

★ 为什么基线数据用固定公式合成而不落真实行情
  真实行情要么依赖网络、要么依赖数据库快照，两者都会让测试变成"flaky"。
  而这里要守的是**算法口径与语义文案**，用多周期叠加的确定性序列已足够
  触发全部信号分支（超买/超卖/金叉/死叉/数据不足/退化横盘）。

★ 文件名以 `_` 开头：pytest 不会把它当测试用例收集。
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Dict, List

BACKEND = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).parent / "fixtures" / "p8_legacy_indicator_baseline.json"


def series(n: int = 120) -> Dict[str, List[float]]:
    """多周期叠加的确定性 OHLCV（不碰 random / 不碰网络）。"""
    highs: List[float] = []
    lows: List[float] = []
    closes: List[float] = []
    volumes: List[float] = []
    v = 10.0
    for i in range(n):
        v += math.sin(i / 5.0) * 0.35 + math.cos(i / 13.0) * 0.2
        v *= 1.001
        c = round(v, 4)
        amp = round(abs(math.sin(i / 7.0)) * 0.6 + 0.15, 4)
        closes.append(c)
        highs.append(round(c + amp, 4))
        lows.append(round(max(c - amp, 0.01), 4))
        volumes.append(round(1_000_000 + math.sin(i / 3.0) * 300_000 + i * 5_000, 2))
    return {"high": highs, "low": lows, "close": closes, "volume": volumes}


def trend_series(n: int = 80, slope: float = 0.08) -> List[float]:
    """单调序列 —— 触发"无回调"类边界（RSI→100 / 波动率→0 等）。"""
    return [round(10.0 + slope * i, 4) for i in range(n)]


def collect() -> Dict[str, Any]:
    """采集 legacy 侧全部指标函数输出（含 signals/zone/中文研判）。"""
    from app.agent.tools.finance import _analysis_utils as AU       # noqa: E402
    from app.agent.tools.finance import analysis_tools as AT         # noqa: E402

    d = series(120)
    h, l, c, vol = d["high"], d["low"], d["close"], d["volume"]
    t = trend_series(80)
    short = series(12)                       # 触发"数据不足"分支

    out: Dict[str, Any] = {}

    # ── MACD ──
    out["macd_default"] = AT._calc_macd(c)
    out["macd_explicit"] = AT._calc_macd(c, 12, 26, 9)
    out["macd_fast_params"] = AT._calc_macd(c, 5, 10, 4)
    out["macd_insufficient"] = AT._calc_macd(short["close"])

    # ── RSI ──
    out["rsi_6_12_24"] = AT._calc_rsi(c, [6, 12, 24])
    out["rsi_default"] = AT._calc_rsi(c)
    out["rsi_single_14"] = AT._calc_rsi(c, [14])
    out["rsi_monotonic"] = AT._calc_rsi(t, [6, 12, 24])
    out["rsi_insufficient"] = AT._calc_rsi(short["close"], [6, 12, 24])

    # ── BOLL ──
    out["boll_default"] = AT._calc_boll(c)
    out["boll_10_1"] = AT._calc_boll(c, 10, 1.0)
    out["boll_flat"] = AT._calc_boll([10.0] * 40)          # 上下轨重合的退化情形
    out["boll_insufficient"] = AT._calc_boll(short["close"])

    # ── KDJ ──
    out["kdj_default"] = AT._calc_kdj(h, l, c)
    out["kdj_monotonic"] = AT._calc_kdj(t, [x - 0.1 for x in t], t)
    out["kdj_flat"] = AT._calc_kdj([10.0] * 40, [10.0] * 40, [10.0] * 40)
    out["kdj_insufficient"] = AT._calc_kdj(h[:5], l[:5], c[:5])

    # ── ATR ──
    out["atr_default"] = AT._calc_atr(h, l, c)
    out["atr_7"] = AT._calc_atr(h, l, c, 7)
    out["atr_insufficient"] = AT._calc_atr(short["high"], short["low"], short["close"])

    # ── MFI ──
    out["mfi_default"] = AT._calc_mfi(h, l, c, vol)
    out["mfi_10"] = AT._calc_mfi(h, l, c, vol, 10)
    out["mfi_insufficient"] = AT._calc_mfi(
        short["high"], short["low"], short["close"], short["volume"])

    # ── CMF ──
    out["cmf_default"] = AT._calc_cmf(h, l, c, vol)
    out["cmf_10"] = AT._calc_cmf(h, l, c, vol, 10)
    out["cmf_insufficient"] = AT._calc_cmf(
        short["high"], short["low"], short["close"], short["volume"])

    # ── 历史波动率 ──
    out["hist_vol_default"] = AT._calc_hist_volatility(c)
    out["hist_vol_10"] = AT._calc_hist_volatility(c, 10)
    out["hist_vol_flat"] = AT._calc_hist_volatility([10.0] * 40)

    # ── 背离 ──
    out["divergence_20"] = AT._detect_divergence(c, c[::-1], 20)
    out["divergence_30"] = AT._detect_divergence(c, c, 30)
    out["divergence_short_window"] = AT._detect_divergence(c[:10], c[:10], 20)

    # ── 均线收敛 ──
    out["ma_conv_full"] = AT._calc_ma_convergence(c)
    out["ma_conv_short"] = AT._calc_ma_convergence(c[:40])

    # ── OBV ──
    out["obv_default"] = AU._calc_obv(c, vol)
    out["obv_flat"] = AU._calc_obv([10.0] * 40, vol[:40])
    out["obv_insufficient"] = AU._calc_obv(c[:2], vol[:2])
    out["obv_len_mismatch"] = AU._calc_obv(c, vol[:10])

    return out


def diff(a: Any, b: Any, path: str = "") -> List[str]:
    """逐键比对（不是容差）—— 数值与中文文案都必须完全相同。"""
    diffs: List[str] = []
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a:
                diffs.append(f"{path}.{k}: 基线缺失，当前多出 {b[k]!r}")
            elif k not in b:
                diffs.append(f"{path}.{k}: 当前缺失（基线有 {a[k]!r}）")
            else:
                diffs.extend(diff(a[k], b[k], f"{path}.{k}"))
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            diffs.append(f"{path}: 长度 {len(a)} → {len(b)}")
        else:
            for i, (x, y) in enumerate(zip(a, b)):
                diffs.extend(diff(x, y, f"{path}[{i}]"))
    elif a != b:
        # 浮点二进制表示差异不算差异，但必须是"完全相等"而非容差
        if isinstance(a, float) and isinstance(b, float) and repr(a) == repr(b):
            return diffs
        diffs.append(f"{path}: {a!r} → {b!r}")
    return diffs


def load_baseline() -> Dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def save_baseline(data: Dict[str, Any]) -> None:
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
