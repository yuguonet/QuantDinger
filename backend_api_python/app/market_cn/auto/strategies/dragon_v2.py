#!/usr/bin/env python3
# app/market_cn/auto/strategies/dragon_v2.py
"""龙回头V2 — 强势龙回头 (2026-09-10 候选规则验证后用户裁定独立成策略)。

用途: 在 dragon_callback 全部入场规则之后追加 C4 组合门 (四道 D0 可知硬条件),
      与原版并行实跑对比。不改原策略任何行为。

设计点:
  - 继承 DragonCallbackStrategy: 找龙/gap/龙强度/拐点/质量排除/去重/预过滤/
    出场模拟 (C2) 全部复用, 本文件只实现 C4 门。
  - **C4 门后置** (super().scan_signals 产出 signal 后再判): ①不改父类代码
    ②被拒样本经探针 trace("c4") 正确归属 stage (PROBE_STAGE_RANK 子类扩展)。
  - C4 四门 (全部只用 <=D0 收盘数据, as-of 安全; 依据: 峰值回撤4%口径 signal
    池 167 笔两段稳定性同向, tmp/龙回头_候选规则逐条验证.md):
      1) 回调期必须有反抽: 回调期 max(high) >= 涨停日收盘 (pb_amp >= 0)
         — 最强单门, 被拒组 31.6%/-2.34%/PL0.63
      2) 单阳不破: 回调期 min(low) >= 涨停日开盘 * (1 - 10%)
      3) 站上 MA10: D0 收盘 >= MA10
      4) 回调期阴线占比 <= 0.4 (原 yin_ratio_exclude=0.6 的收紧)
  - 重锚定预期: C4 拒绝的日不再占用 ±4 去重槽位, 同波后续日可能重新出信号,
    实跑笔数/构成与离线投影 (49笔) 有差异属正常, 以实跑为准。

易错点:
  - bars 在 super() 内可能被 as_of 切片, 本方法收到的 bars 已是切片 → i=len-1 即 D0;
  - lu_date 反查 _find_bar_idx 找不到时放行 (与父类"缺失不误杀"哲学一致);
  - extra 缺 yin_ratio 等字段时不判该项 (防御, 正常路径必有)。
"""
from app.market_cn.auto.strategies import register
from app.market_cn.auto.strategies.dragon_callback import (
    DRAGON_CB_PARAMS,
    DragonCallbackStrategy,
    _find_bar_idx,
)
from app.market_cn.auto.strategies.base import ScanSpec

STRATEGY_KEY = "dragon_v2"
STRATEGY_LABEL = "龙回头V2"

DRAGON_V2_PARAMS = dict(
    DRAGON_CB_PARAMS,
    # --- C4 组合门 (2026-09-10 验证) ---
    # 09-10 晚放宽复验 (tmp/check_v2_relax.py + 4 变体 600d 实跑): 单阳不破与
    # 反抽+MA10 冗余 (其判别力被两门覆盖, 阈值 -6~-20 全段平滑), 删除后
    # n 30→49, 65.3%/+3.47%/ret_per_day+36%, 两段稳定 → 默认停用, 保留参数可调回
    pb_amp_min=0.0,        # 回调期反抽门: 回调max_high/涨停收盘-1 (%) 下界
    lu_open_keep=None,     # 单阳不破: 回调min_low/涨停日开盘-1 (%) 下界; None=停用
    d0_ma10_min=0.0,       # D0收盘 vs MA10 (%) 下界
    yin_ratio_strict=0.6,  # 回调期阴线占比上界 (09-10晚用户裁定流量档: 引擎实跑
                           #   86笔 59.3%/+2.70%/PL1.61/rpd1.33 两段稳;
                           #   0.5~0.6~放开无差异, 0.6=父类原排除线对齐)
)


@register
class DragonV2Strategy(DragonCallbackStrategy):
    key = STRATEGY_KEY
    name = STRATEGY_LABEL
    scan_spec = ScanSpec(kind="daily_close")
    default_params = dict(DRAGON_V2_PARAMS)
    PROBE_STAGE_RANK = {**DragonCallbackStrategy.PROBE_STAGE_RANK, "c4": 11}

    # ---- 信号判定: 父类全链 + C4 后置门 ----
    def scan_signals(self, bars, code, *, as_of=None, ctx=None, limit_ups=None,
                     use_tech_score=True, probe=None, **params):
        sigs = super().scan_signals(
            bars, code, as_of=as_of, ctx=ctx, limit_ups=limit_ups,
            use_tech_score=use_tech_score, probe=probe, **params)
        if not sigs:
            return sigs
        p = self.merged_params(params or None)
        kept = []
        for sig in sigs:
            ok, info = self._c4_gate(bars, sig, p)
            if ok:
                kept.append(sig)
                continue
            if probe is not None:
                probe.trace("c4", code=code,
                            d0_date=str(bars[-1]["time"])[:10],
                            lu_date=sig.extra.get("lu_date"),
                            fail=info.pop("fail"), **info)
        return kept

    def _c4_gate(self, bars, sig, p):
        """C4 四门判定 (仅用 <=D0 收盘数据)。返回 (是否通过, 判定细节 dict)。"""
        extra = sig.extra or {}
        info = {"pb_amp": None, "low_vs_lu_open": None,
                "d0_vs_ma10": None, "yin_ratio": extra.get("yin_ratio")}
        i = len(bars) - 1
        lu_idx = _find_bar_idx(bars, extra.get("lu_date") or "")
        if lu_idx is None or not (0 < lu_idx < i):
            return True, info          # 反查失败放行 (不误杀)
        lu = bars[lu_idx]
        lu_close = float(lu["close"] or 0)
        lu_open = float(lu["open"] or 0)
        if lu_close <= 0 or lu_open <= 0:
            return True, info
        highs = [float(bars[k]["high"] or 0) for k in range(lu_idx + 1, i + 1)]
        lows = [float(bars[k]["low"] or 0) for k in range(lu_idx + 1, i + 1)]
        if not highs:
            return True, info
        info["pb_amp"] = round((max(highs) / lu_close - 1) * 100, 2)
        info["low_vs_lu_open"] = round((min(lows) / lu_open - 1) * 100, 2)
        if i >= 9:
            ma10 = sum(float(b["close"] or 0) for b in bars[i - 9:i + 1]) / 10
            if ma10 > 0:
                info["d0_vs_ma10"] = round((float(bars[i]["close"]) / ma10 - 1) * 100, 2)
        # --- 四门 (或-关系内的每门独立判, 全过才留) ---
        if info["pb_amp"] < p["pb_amp_min"]:
            return False, {**info, "fail": "pb_amp"}
        if p["lu_open_keep"] is not None and info["low_vs_lu_open"] < p["lu_open_keep"]:
            return False, {**info, "fail": "lu_open_keep"}
        if info["d0_vs_ma10"] is not None and info["d0_vs_ma10"] < p["d0_ma10_min"]:
            return False, {**info, "fail": "d0_ma10"}
        yin = info.get("yin_ratio")
        if yin is not None and yin >= p["yin_ratio_strict"]:
            return False, {**info, "fail": "yin_ratio"}
        return True, info
