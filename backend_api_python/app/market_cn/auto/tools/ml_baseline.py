#!/usr/bin/env python3
# app/market_cn/auto/tools/ml_baseline.py
"""M4 GBDT 基线 (2026-09-11, ML 旁支解冻; 设计见 §13 / tmp/M2-M5前置条件审计.md)

用途: 在统一样本库 (data/ml_samples/unified_*.jsonl.gz) 上训练 lightgbm 二分类
     (y = ret_d1c > 0), 回答闸门问题 —— **决策日特征里有没有超出规则链的判别力**。

设计点:
  - 单遍流式读库 (314 万行驻留 JSON 会 OOM, 同 sample_build 教训): 特征按 chunk
    转 float32, 元数据 (strategy/stage/split) 边解析边映射成 int16 码本;
  - 特征 = features 全部数值键 (win 已在建库时默认丢弃) + board_type 编码 + strategy 码;
    **stage 不进特征** (它是规则链自己的产出, 进了就是"复述规则"; 模型价值用
    per-stage 分组检验: 组内 top 分位 vs 其余的 Δ胜率/Δ均收 才是"超出规则的增量");
  - 时间切分沿用建库时的 split 字段 (70/15/15, 防泄漏), 本工具绝不重切;
  - lightgbm 原生容忍 nan (标签缺失/视野不足记 None → nan), 不猜不删行;
  - 结论判读: test 段 AUC 显著 >0.5 且 top 分位子集 Δ均收>0 两段稳定 → 有 alpha,
    进 M4 收口 (平行验证); 否则方向=扩特征维度, 不加模型。

易错点:
  - 标签量纲 = 百分数 (2.61 即 +2.61%); labels 为空 (censored/实盘探针) 的行 y=nan,
    只进预测不进训练评估;
  - test 段是时间上最后的 15% (2026-05-06 后), 与 09-10 归因结论的窗口口径不同属正常;
  - lightgbm determinism: 固定 seed, feature_fraction/bagging 固定。

用法:
  python -m app.market_cn.auto.tools.ml_baseline \
      [--sample data/ml_samples/unified_01.jsonl.gz] [--top 0.1 0.2] [--out report.md]
"""
from __future__ import annotations

import argparse
import glob
import gzip
import json
import os
import sys
import time

import numpy as np

# 默认取 data/ml_samples/ 下最新的 unified_*.jsonl.gz (或 .jsonl)
_HERE = os.path.dirname(os.path.abspath(__file__))
_ML_DIR = os.path.normpath(os.path.join(_HERE, "..", "..", "..", "..", "data", "ml_samples"))


def _open(path):
    return gzip.open(path, "rt", encoding="utf-8") if path.endswith(".gz") \
        else open(path, "r", encoding="utf-8")


def _iter_samples(path):
    """流式产出 sample 行 (容忍坏行)。"""
    with _open(path) as f:
        for ln in f:
            try:
                d = json.loads(ln)
            except ValueError:
                continue
            if d.get("kind") == "sample":
                yield d


def _y_of(d):
    lab = d.get("labels") or {}
    r = lab.get("ret_d1c")
    return np.nan if r is None else float(r)


def main():
    ap = argparse.ArgumentParser(description="M4 GBDT 基线 (统一库 → lightgbm)")
    ap.add_argument("--sample", default=None, help="统一样本库路径 (默认最新 unified_*.gz)")
    ap.add_argument("--top", type=float, nargs="*", default=[0.1, 0.2],
                    help="top 分位评估档 (默认 0.1 0.2)")
    ap.add_argument("--out", default=None, help="报告输出路径 (默认 tmp/ml_baseline_<tag>.md)")
    ap.add_argument("--strategy", nargs="*", default=None,
                    help="只用指定策略样本训练/评估 (默认全部; v1/lu 占 92% 会淹没 dragon 系, 归因时用)")
    args = ap.parse_args()

    path = args.sample
    if not path:
        cands = sorted(glob.glob(os.path.join(_ML_DIR, "unified_*.jsonl*")))
        if not cands:
            print("no unified sample library found in " + _ML_DIR, file=sys.stderr)
            return 1
        path = cands[-1]
    print(f"sample library: {path}")

    # ---- 流式装载 ----
    t0 = time.time()
    xs, ys, strs, stgs, spls = [], [], [], [], []
    feat_names = None
    books = {"strategy": {}, "stage": {}, "split": {}}
    n = 0
    for d in _iter_samples(path):
        if feat_names is None:
            feat_names = [k for k in sorted(d["features"]) if k != "win"]
            feat_names += ["strategy_code", "board_type_code"]
        feat = d["features"]
        row = np.empty(len(feat_names), dtype=np.float32)
        for j, name in enumerate(feat_names[:-2]):
            x = feat.get(name)
            row[j] = np.nan if isinstance(x, (str, bool)) or x is None or abs(x) > 1e15 else x
        s, st, sp = d.get("strategy") or "?", d.get("stage") or "?", d.get("split") or "?"
        for key, val in (("strategy", s), ("stage", st), ("split", sp)):
            if val not in books[key]:
                books[key][val] = len(books[key])
        bt = feat.get("board_type")
        row[-2] = books["strategy"][s]
        row[-1] = 0 if bt == "main" else 1   # main=0 / gem_star=1 (get_board_type 两值域)
        xs.append(row)
        ys.append(_y_of(d))
        strs.append(books["strategy"][s])
        stgs.append(books["stage"][st])
        spls.append(books["split"][sp])
        n += 1
        if n % 500_000 == 0:
            print(f"  [load] {n} rows ({time.time() - t0:.0f}s)", flush=True)
    X = np.vstack(xs)
    y = np.asarray(ys, dtype=np.float32)
    strat_a = np.asarray(strs)
    stage_a = np.asarray(stgs)
    split_a = np.asarray(spls)
    xs.clear()
    # --strategy 过滤 (归因: 全库训练时 v1/lu 样本占绝对多数, 会淹没小策略信号)
    if args.strategy:
        keep_keys = [books["strategy"][s] for s in args.strategy if s in books["strategy"]]
        keep = np.isin(strat_a, keep_keys)
        X, y = X[keep], y[keep]
        strat_a, stage_a, split_a = strat_a[keep], stage_a[keep], split_a[keep]
        print(f"strategy filter {args.strategy}: kept {int(keep.sum())} rows")
    inv_strategy = {v: k for k, v in books["strategy"].items()}
    inv_stage = {v: k for k, v in books["stage"].items()}
    inv_split = {v: k for k, v in books["split"].items()}
    print(f"loaded: {n} rows x {X.shape[1]} feats ({time.time() - t0:.0f}s), "
          f"strategies={ {inv_strategy[v]: int((strat_a == v).sum()) for v in set(strat_a)} }")

    # ---- 训练/评估 ----
    import lightgbm as lgb
    m_train = (split_a == books["split"].get("train", -1)) & ~np.isnan(y)
    m_valid = (split_a == books["split"].get("valid", -1)) & ~np.isnan(y)
    m_test = (split_a == books["split"].get("test", -1)) & ~np.isnan(y)
    print(f"train={m_train.sum()} valid={m_valid.sum()} test={m_test.sum()} "
          f"(censored excluded: {int(np.isnan(y).sum())})")

    model = lgb.LGBMClassifier(
        objective="binary", n_estimators=400, learning_rate=0.05,
        num_leaves=64, min_child_samples=200, feature_fraction=0.8,
        bagging_fraction=0.8, bagging_freq=1, seed=42, n_jobs=-1, verbose=-1)
    t1 = time.time()
    model.fit(X[m_train], (y[m_train] > 0).astype(int),
              eval_set=[(X[m_valid], (y[m_valid] > 0).astype(int))],
              eval_metric="auc", feature_name=feat_names,
              callbacks=[lgb.early_stopping(50, verbose=False)])
    print(f"trained in {time.time() - t1:.0f}s, best_iter={model.best_iteration_}")

    lines = ["# M4 GBDT 基线报告", "",
             f"- 样本库: `{path}`", f"- 行数: {n} (特征 {X.shape[1] - 2}+2码), "
             f"train/valid/test = {int(m_train.sum())}/{int(m_valid.sum())}/{int(m_test.sum())}",
             f"- 目标: y = ret_d1c > 0 (基线 win={float((y[m_train] > 0).mean()) * 100:.1f}%@train)",
             ""]

    for name, m in (("valid", m_valid), ("test", m_test)):
        from sklearn.metrics import roc_auc_score  # lightgbm 依赖链已带 sklearn 兼容层? 无则手算
        p = model.predict_proba(X[m])[:, 1]
        auc = roc_auc_score((y[m] > 0).astype(int), p)
        lines.append(f"- **AUC@{name} = {auc:.4f}** (n={int(m.sum())})")

    # ---- test 段分位提升 ----
    m = m_test
    p = model.predict_proba(X[m])[:, 1]
    r = y[m]
    order = np.argsort(-p)
    base_win = float((r > 0).mean()) * 100
    base_avg = float(np.nanmean(r))
    lines += ["", "## test 段十分位 (按模型分降序)", "",
              "| decile | n | win% | avg% |", "|---|---|---|---|",
              f"| 全样本 | {len(r)} | {base_win:.1f} | {base_avg:+.2f} |"]
    k = len(r) // 10
    for i in range(10):
        sel = order[i * k:(i + 1) * k] if i < 9 else order[9 * k:]
        rr = r[sel]
        lines.append(f"| D{i + 1} | {len(rr)} | {float((rr > 0).mean()) * 100:.1f} | "
                     f"{float(rr.mean()):+.2f} |")
    lines += ["", "## test 段 top 分位提升", "",
              "| top | n | win% | avg% | Δavg vs 基线 |", "|---|---|---|---|---|"]
    for f in args.top:
        sel = order[:max(int(len(r) * f), 50)]
        rr = r[sel]
        wins, losses = rr[rr > 0], rr[rr <= 0]
        pl = float(wins.mean() / abs(losses.mean())) if len(wins) and len(losses) else float("nan")
        lines.append(f"| {f:.0%} | {len(rr)} | {float((rr > 0).mean()) * 100:.1f} | "
                     f"{float(rr.mean()):+.2f} | {float(rr.mean()) - base_avg:+.2f} (PL={pl:.2f}) |")

    # ---- per-stage 组内检验 (超出规则的增量) ----
    lines += ["", "## per-stage 组内 top25% vs 其余 (test 段)", "",
              "stage 不进特征 — 组内提升 = 特征对规则链之外信息的判别力;", "",
              "| strategy | stage | n | 组内win(全)% | top25 win% | rest win% | top25 avg% | rest avg% | Δavg |",
              "|---|---|---|---|---|---|---|---|---|"]
    p_all = model.predict_proba(X[m_test])[:, 1]
    r_all = y[m_test]
    for sv in sorted(set(strat_a[m_test])):
        for gv in sorted(set(stage_a[m_test])):
            msk = (strat_a[m_test] == sv) & (stage_a[m_test] == gv) & ~np.isnan(r_all)
            if msk.sum() < 300:
                continue
            pv, rv = p_all[msk], r_all[msk]
            o = np.argsort(-pv)
            q = len(rv) // 4
            top, rest = rv[o[:q]], rv[o[q:]]
            lines.append(
                f"| {inv_strategy[sv]} | {inv_stage[gv]} | {len(rv)} | "
                f"{float((rv > 0).mean()) * 100:.1f} | {float((top > 0).mean()) * 100:.1f} | "
                f"{float((rest > 0).mean()) * 100:.1f} | {float(top.mean()):+.2f} | "
                f"{float(rest.mean()):+.2f} | {float(top.mean() - rest.mean()):+.2f} |")

    # ---- 特征重要性 ----
    imp = model.booster_.feature_importance(importance_type="gain")
    names = model.booster_.feature_name()
    top_imp = sorted(zip(names, imp), key=lambda t: -t[1])[:20]
    lines += ["", "## 特征重要性 (gain, top20)", "", "| feature | gain |", "|---|---|"]
    for nm, g in top_imp:
        lines.append(f"| {nm} | {g:.0f} |")
    # strategy 码本对照
    lines += ["", "码本: strategy=" + json.dumps(inv_strategy, ensure_ascii=False)
              + ", split=" + json.dumps(inv_split, ensure_ascii=False)
              + ", stage=" + json.dumps(inv_stage, ensure_ascii=False), ""]

    out = args.out or os.path.join(_ML_DIR, "..", "..", "..", "tmp",
                                   f"ml_baseline_{time.strftime('%m%d_%H%M')}.md")
    out = os.path.normpath(out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("-> " + out)
    print("\n".join(lines[:14]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
