#!/usr/bin/env python3
# app/market_cn/auto/tools/sample_build.py
"""M2 统一样本构建器 (2026-09-11, ML 旁支解冻, 设计见 §13/审计报告 tmp/M2-M5前置条件审计.md)

用途: 把回测探针存档 (tmp/probes/*.jsonl 的 kind=sample 行, 五策略 schema 已统一)
     构建为跨策略统一训练样本库, 并合并 M3 指数环境特征。

设计点:
  - 消费不生产: 判定/个股特征/标签全部来自探针 (回测 --probe 产出), 本工具零规则零判定;
    probe.py 保持零 diff (框架层不因 ML 旁支变化, 遵守调试代码归属铁律);
  - 环境特征严格 as-of: daily_close 策略 D0 收盘后判定 → 指数日线取 <=D0;
    intraday_window 策略盘中判定 → D0 指数收盘当时还是未来 → 取 <=D0 前一交易日
    (经注册表 scan_spec.kind 区分, 不按策略名硬编码);
  - 时间切分防泄漏: 全体 d0_date 排序后 70/15/15 切 train/valid/test, 永不打乱;
  - 默认丢弃 features.win (30 根 OHLCV 窗口, 体积大头, --keep-win 可保留);
  - 输出 jsonl 默认 data/ml_samples/unified_<tag>.jsonl + 终端摘要。

易错点:
  - 同一 (strategy,code,d0_date) 可能出现在多个存档 → 默认去重 (保留第一次出现, --dup keep-all 可关);
  - 指数远端只保证最近 ~800 根, 600d 样本够用, 更长窗口环境特征会缺 (缺失记 None 不猜);
  - d0_date 不在指数日历 (停牌对齐) → 按 as-of 切片取最后一根, 全无则特征为 None;
  - **两遍流式 (2026-09-11)**: 百万行级存档全量驻留内存会 OOM (实测 4 存档 314 万行被杀) →
    第一遍只扫 日期集合/策略集合/去重键 (小内存) 定全局切分边界, 第二遍逐行组装
    环境特征直接落盘, 任何时刻不持有全量行; 去重语义 = 保留第一次出现, 两遍间用同一序对齐;
  - **去重键存 64 位哈希**: 300 万级元组键集实测被沙箱内存上限杀 (两次均 2m01s 死于 v1 段) →
    键集改存 hash(key) 整数, 内存 ~1/3; 碰撞率 n²/2⁶⁴ ≈ 5e-7 (300 万键), 误杀 ≤1 行可忽略;
  - **输出默认 gzip**: 300 万行 jsonl 明文 ~5GB (D 盘曾只剩 2.3GB 放不下), gzip 后 ~1/6
    (JSON 文本压缩比 5~8x, rule_trace/特征重复度高); 训练侧 pandas read_json 直读 .gz。

用法:
  python -m app.market_cn.auto.tools.sample_build \
      --probe tmp/probes/dragon_v2_v2r3y06_600d_*.jsonl \
      --probe tmp/probes/dragon_callback_tr4_*.jsonl \
      --index 000001 --index 000300 [--keep-win] [--dup keep-all] [--out path]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys

from collections import Counter, defaultdict

# ---- 环境特征 (per index, 全部只用 <=决策时点 的指数日线) ----

def _env_features(bars, as_of_date):
    """指数日线切片 [.., as_of_date] → 环境特征 dict; 数据不足返回 None 值不猜。"""
    xs = [b for b in bars if b["date"] <= str(as_of_date)[:10]]
    n = len(xs)
    if n < 21:
        return {k: None for k in (
            "ret_1", "ret_5", "ret_10", "ret_20", "ma20_slope", "dist_ma20",
            "vol_ratio", "above_ma20_20d")}
    c = [b["close"] for b in xs]
    v = [b["volume"] for b in xs]
    ma20 = sum(c[-20:]) / 20.0
    ma20_prev = sum(c[-25:-5]) / 20.0
    out = {
        "ret_1": c[-1] / c[-2] - 1,
        "ret_5": c[-1] / c[-6] - 1,
        "ret_10": c[-1] / c[-11] - 1,
        "ret_20": c[-1] / c[-21] - 1,
        "ma20_slope": c[-1] / ma20_prev - 1 if ma20_prev > 0 else None,
        "dist_ma20": c[-1] / ma20 - 1 if ma20 > 0 else None,
        "vol_ratio": (v[-1] / (sum(v[-20:]) / 20.0)) if sum(v[-20:]) > 0 else None,
        "above_ma20_20d": sum(1 for i in range(-20, 0) if c[i] > sum(c[i - 19:i + 1]) / 20.0) / 20.0,
    }
    return out


def _resolve_files(probe_paths):
    files = []
    for path in probe_paths:
        if any(ch in path for ch in "*?["):
            files.extend(sorted(glob.glob(path)))
        else:
            files.append(path)
    return files


def _split_by_date(dates):
    """d0_date 升序 → {date: split} 70/15/15 (时间切分防泄漏)。"""
    ds = sorted(dates)
    n = len(ds)
    b1, b2 = ds[int(n * 0.70)], ds[int(n * 0.85)]
    def which(d):
        return "train" if d < b1 else ("valid" if d < b2 else "test")
    return {d: which(d) for d in ds}, b1, b2


def build(probe_paths, indexes, keep_win=False, dup_mode="dedup", out_path=None):
    """两遍流式: 全量行不驻留内存 (314 万行实测 OOM 教训, 见文件头易错点)。"""
    files = _resolve_files(probe_paths)

    # ---- Pass 1: 预扫 —— 只留 日期集合/策略集合/去重键, 不留行 ----
    from app.market_cn.auto.strategies import get_strategy
    seen = set()          # 存 64 位哈希 (内存 ~1/3, 见文件头)
    dates = set()
    strategies = set()
    dropped = 0
    for p in files:
        for ln in open(p, encoding="utf-8"):
            try:
                d = json.loads(ln)
            except ValueError:
                continue
            if d.get("kind") != "sample":
                continue
            h = hash((d.get("strategy"), d.get("code"), d.get("d0_date")))
            if h in seen:
                dropped += 1
                if dup_mode == "dedup":
                    continue
            seen.add(h)
            dates.add(d.get("d0_date"))
            strategies.add(d.get("strategy"))
    if not dates:
        print("no kind=sample rows found in given probes", file=sys.stderr)
        return None
    del seen   # pass1 键集用完即释放, pass2 用独立 emitted 集合 (避免双持 300 万级键集)

    # 策略判定时点类别 (注册表 scan_spec.kind, 不硬编码策略名)
    kinds = {}
    for s in strategies:
        try:
            kinds[s] = getattr(get_strategy(s), "scan_spec").kind
        except Exception:
            kinds[s] = "intraday_window"   # 注册表查不到 → 保守按盘中口径 (as-of 到 D-1, 严不松)

    # 指数日线 (进程内按 code 只拉一次)
    from app.market_cn.auto.data import hub
    idx_bars = {}
    for code in indexes:
        bars = hub.index_daily(code, days=800)
        idx_bars[code] = bars
        print(f"index {code}: {len(bars)} bars", flush=True)

    splits, b1, b2 = _split_by_date(dates)

    # ---- Pass 2: 流式组装 → 直接落盘 ----
    env_cache = {}
    emitted = set() if dup_mode == "dedup" else None   # 独立于 pass1, 存 64 位哈希, 只记已产出键
    by_ss = Counter()
    by_sp = Counter()
    lbl_n = lbl_win = 0
    lbl_sum = 0.0
    total = 0
    if out_path:
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        if out_path.endswith(".gz"):
            import gzip
            fout = gzip.open(out_path, "wt", encoding="utf-8", compresslevel=6)
        else:
            fout = open(out_path, "w", encoding="utf-8")
    for p in files:
        for ln in open(p, encoding="utf-8"):
            try:
                d = json.loads(ln)
            except ValueError:
                continue
            if d.get("kind") != "sample":
                continue
            strat = d.get("strategy")
            d0 = d.get("d0_date")
            key = hash((strat, d.get("code"), d0))
            if emitted is not None:
                if key in emitted:
                    continue   # 去重语义 = 保留第一次出现 (与原驻留版一致, 两遍序相同)
                emitted.add(key)
            env = {}
            for code, bars in idx_bars.items():
                # intraday 判定在盘中 → D0 指数收盘不可知 → as-of 到 D0 前一交易日
                as_of = d0
                if kinds.get(strat) == "intraday_window":
                    prev = [b["date"] for b in bars if b["date"] < d0]
                    as_of = prev[-1] if prev else "1900-01-01"
                ck = (code, as_of)
                if ck not in env_cache:
                    env_cache[ck] = _env_features(bars, as_of)
                env.update({f"env_{code}_{k}": v for k, v in env_cache[ck].items()})
            feat = {k: v for k, v in (d.get("features") or {}).items() if keep_win or k != "win"}
            r = {
                "strategy": strat, "code": d.get("code"), "d0_date": d0,
                "stage": d.get("stage"), "kind": kinds.get(strat),
                "features": {**feat, **env},
                "labels": d.get("labels") or {},
                "rule_trace": d.get("rule_trace"),
                "split": splits[d0],
            }
            if out_path:
                fout.write(json.dumps(r, ensure_ascii=False) + "\n")
            total += 1
            by_ss[(strat, r["stage"])] += 1
            by_sp[(strat, r["split"])] += 1
            r1 = r["labels"].get("ret_d1c")
            if r1 is not None:
                lbl_n += 1
                lbl_sum += r1
                lbl_win += (r1 > 0)
        print(f"  [pass2] {os.path.basename(p)} done (cumulative {total} rows)", flush=True)
    if out_path:
        fout.close()
    if emitted is not None:
        emitted.clear()   # 释放去重键集

    # ---- 摘要 ----
    print(f"samples={total} (dup dropped={dropped})")
    print("split boundaries:", b1, b2)
    print("-- strategy x stage --")
    for (s, st), n in sorted(by_ss.items()):
        print(f"  {s:20s} {st:12s} {n}")
    print("-- strategy x split --")
    for (s, sp), n in sorted(by_sp.items()):
        print(f"  {s:20s} {sp:6s} {n}")
    if lbl_n:
        # 标签量纲=百分数, 2.61 即 +2.61%
        print(f"-- label quickcheck (ret_d1c, n={lbl_n}): "
              f"win={lbl_win / lbl_n * 100:.1f}% avg={lbl_sum / lbl_n:+.2f}%")
    if out_path:
        print(f"-> {out_path} ({os.path.getsize(out_path) / 1e6:.1f} MB)")
    return total


def main():
    here = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
    ap = argparse.ArgumentParser(description="M2 统一样本构建器 (探针存档 → 训练样本库)")
    ap.add_argument("--probe", action="append", required=True,
                    help="探针存档路径 (可多次, 支持 glob)")
    ap.add_argument("--index", action="append", default=None,
                    help="环境特征指数代码 (默认上证指数; 可多次, 如 000300)")
    ap.add_argument("--keep-win", action="store_true", help="保留 features.win (30根OHLCV)")
    ap.add_argument("--dup", choices=["dedup", "keep-all"], default="dedup")
    ap.add_argument("--out", default=None,
                    help="输出 jsonl 路径 (默认 data/ml_samples/unified_<n>.jsonl.gz; .gz 后缀自动压缩)")
    args = ap.parse_args()
    # .env 自举 (python -m 也要 load_dotenv, hub 指数远端/缓存需要)
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(here, ".env"), override=False)
    except ImportError:
        pass
    out = args.out
    indexes = list(dict.fromkeys(args.index or ["000001"]))   # 去重保序
    if not out:
        seq = 1
        ml_dir = os.path.join(here, "data", "ml_samples")
        while os.path.exists(os.path.join(ml_dir, f"unified_{seq:02d}.jsonl.gz")) or \
                os.path.exists(os.path.join(ml_dir, f"unified_{seq:02d}.jsonl")):
            seq += 1
        out = os.path.join(ml_dir, f"unified_{seq:02d}.jsonl.gz")
    build(args.probe, indexes, keep_win=args.keep_win,
          dup_mode=args.dup, out_path=out)


if __name__ == "__main__":
    main()
