# app/market_cn/auto/probe.py
"""统一调试探针层 (2026-09-10, 用户裁定设计)。

定位与原则:
  - 回测只负责验证, 不负责调试; 探针负责 debug 形态的数据输出。
  - 探针代码放各策略文件内 (类似 C 的 TRACE 宏): probe=None (默认) 时零开销,
    判定行为与签名语义完全不变; 只有显式开启 debug 才生效。
  - 捕获的数据存档 (JSONL) 交给 AI 离线分析, 用于反推规则改进 — 目标维度:
    高可操作性 / 高胜率 / 高单位时间收益比 / 高盈亏比 / 高均峰值,
    因此记录须覆盖: 决策依据(特征) + 规则轨迹(为何落选) + 未来收益(标签)。

三种形态 (可按需扩展):
  trace(stage, **kw)  判定步落点: 每个候选在每个过滤门的位置 (为何没过)
  sample(**kw)        决策日样本: 特征 + 标签 + 当日最深判定阶段 (一行一决策日)
  shell(name, **kw)   数据外壳: 把输入数据 (bars 尾部 / ctx / 快照) 整块套壳存档

存档: 项目根 tmp/probes/<strategy>_<tag>_<时间戳>.jsonl (文件卫生约定);
close() 时打印计数汇总。推荐用法:

    from app.market_cn.auto.probe import Probe
    with Probe("dragon_callback", tag="panic") as pr:
        run_all("dragon_callback", ..., probe=pr)

易错点:
  - 探针记录含未来信息 (labels), 只能离线分析, 绝不能回流进判定/实盘路径;
  - sample 只在到达完整判定的决策日产出 (廉价预筛跳过的日不采样, 否则纯噪声);
  - 浮点不取整会显著增大文件, 特征/标签请在策略侧先 round;
  - day-stage 优先级由各策略自持: 类属性 PROBE_STAGE_RANK = {stage: rank}
    (框架不知道策略的门名; 引擎/回测钩子用 getattr 读取做 day 级归属)。
"""
import json
import os
import time

# 项目根 tmp/probes/ (probe.py 位于 backend_api_python/app/market_cn/auto/, 上溯4级到根)
_PROBE_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__),
                                           "..", "..", "..", "..", "tmp", "probes"))


class DayTrace:
    """按调用聚合判定步的 shim (与 Probe.trace 同签名)。

    用法: 调用方把 DayTrace() 当 probe 传进 scan_signals, trace 只进内存不落盘,
    调用方按 (股,日/槽位) 聚合出 stage (取 PROBE_STAGE_RANK 最深) 后再 probe.sample。
    """

    def __init__(self):
        self.items = []

    def trace(self, stage, **kw):
        self.items.append({"stage": stage, **kw})


def sample_feats(bars, i, code, stock_info=None):
    """决策日样本的通用 特征 + 标签 数据体 (各策略共用; 策略可在 dict 上再加专属键)。

    特征只含 <=D0(=bars[i]) 收盘可知信息; 标签以 D+1 开盘为入场基准 (未来数据,
    只能离线分析, 绝不能回流判定/实盘路径); 视野不足记 None (=censored)。
    易错点: stock_info 是单票 info dict (run_all 已按股取好, 非全量映射)。
    """
    from app.market_cn.auto.common.indicators import rsi as _rsi
    from app.market_cn.auto.common.market import get_board_type
    d0 = bars[i]
    closes = [float(b["close"]) for b in bars[:i + 1]]
    prev_c = closes[-2] if len(closes) >= 2 else 0
    inf = stock_info or {}
    circ = inf.get("circ_shares") or 0
    vol0 = float(d0["volume"] or 0)
    vol_prev = float(bars[i - 1]["volume"] or 0) if i > 0 else 0
    features = {
        "win": [[round(float(b["open"]), 3), round(float(b["high"]), 3),
                 round(float(b["low"]), 3), round(float(b["close"]), 3),
                 round(float(b["volume"] or 0) / 100, 1)]
                for b in bars[max(0, i - 29):i + 1]],
        "d0_pct_chg": round((float(d0["close"]) / prev_c - 1) * 100, 2) if prev_c > 0 else None,
        "vol_r": round(vol0 / vol_prev, 2) if vol_prev > 0 else None,
        "turnover_d0": round(vol0 / circ * 100, 2) if circ > 0 else None,
        "circ_mv_yi": round(float(d0["close"]) * circ / 1e8, 2) if circ > 0 else None,
        "rsi6": _rsi(closes, period=6),
        "board_type": get_board_type(code),
    }
    labels = {}
    n = len(bars)
    if i + 1 < n:
        entry = float(bars[i + 1]["open"] or 0)
        labels["entry_d1o"] = entry
        if entry > 0:
            labels["ret_d1c"] = round((float(bars[i + 1]["close"]) / entry - 1) * 100, 2)
            if i + 2 < n:
                labels["ret_d2o"] = round((float(bars[i + 2]["open"]) / entry - 1) * 100, 2)
            if i + 6 < n:
                labels["ret_d5o"] = round((float(bars[i + 6]["open"]) / entry - 1) * 100, 2)
            if i + 11 < n:
                labels["ret_d10o"] = round((float(bars[i + 11]["open"]) / entry - 1) * 100, 2)
            highs = [float(bars[k]["high"]) for k in range(i + 2, min(i + 7, n))]
            lows = [float(bars[k]["low"]) for k in range(i + 2, min(i + 7, n))]
            if highs:
                labels["peak5"] = round((max(highs) / entry - 1) * 100, 2)
                labels["mae5"] = round((min(lows) / entry - 1) * 100, 2)
    return {"features": features, "labels": labels}


class Probe:
    """探针收集器 (TRACE 式: 策略函数收 probe=None 即零开销)。"""

    def __init__(self, strategy, tag=None, out_dir=None):
        ts = time.strftime("%Y%m%d_%H%M%S")
        d = out_dir or _PROBE_DIR
        os.makedirs(d, exist_ok=True)
        self.strategy = strategy
        self.path = os.path.join(d, f"{strategy}_{tag or 'dbg'}_{ts}.jsonl")
        self.counts = {}
        self._fh = open(self.path, "w", encoding="utf-8")

    def _write(self, kind, rec):
        self.counts[kind] = self.counts.get(kind, 0) + 1
        self._fh.write(json.dumps(
            {"kind": kind, "strategy": self.strategy,
             "ts": time.strftime("%H:%M:%S"), **rec},
            ensure_ascii=False, default=str) + "\n")

    def trace(self, stage, **kw):
        """判定步落点 (stage 见 STAGE_RANK)。"""
        self._write("trace", {"stage": stage, **kw})

    def sample(self, **kw):
        """决策日样本: 特征 + 标签 + stage + 规则轨迹。"""
        self._write("sample", kw)

    def shell(self, name, **kw):
        """数据外壳: 输入数据整块套壳存档 (供 IDE 框架统一检查引用数据)。"""
        self._write("shell", {"name": name, **kw})

    def close(self):
        if self._fh.closed:
            return
        self._fh.close()
        total = sum(self.counts.values())
        print(f"[probe] {self.strategy}: {total} 条 -> {self.path} ({self.counts})")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
