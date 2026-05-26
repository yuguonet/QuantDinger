#!/usr/bin/env python3
"""
板块轮动耦合分析 — 预测明日热门板块，给回测信号做+/-评估

核心思路:
  1. 从 sector_daily_stats 加载历史板块热度时序
  2. 计算板块间滞后相关性 (A今天热 → B明天热?)
  3. 构建转移矩阵 (A热完后谁接棒?)
  4. 对每笔回测信号: 查当天所属板块状态 + 预测明天热度 → 打分
  5. 对比高分 vs 低分信号的表现差异

用法:
  cd D:\\QuantDinger
  python scripts/sector_rotation.py
  python scripts/sector_rotation.py --lag 1 --min-history 60
"""
import sys
import json
import argparse
import os
from pathlib import Path
from collections import defaultdict
from datetime import datetime, timedelta
import math

_root = Path(__file__).resolve().parent.parent
_backend_root = str(_root / "backend_api_python")
if _backend_root not in sys.path:
    sys.path.insert(0, _backend_root)
sys.path.insert(0, str(_root))

try:
    from dotenv import load_dotenv
    for p in [os.path.join(_backend_root, '.env'), os.path.join(str(_root), '.env')]:
        if os.path.isfile(p):
            load_dotenv(p, override=False)
            break
except ImportError:
    pass


# ============================================================
# 数据加载
# ============================================================

def load_sector_daily(start_date=None, end_date=None):
    """从 sector_daily_stats 加载历史板块热度
    返回: {(date, sector_type, sector_name): {heat_score, limit_up_count, ...}}
    以及按 sector_type 分组的时序: {sector_type: {sector_name: [(date, heat_score), ...]}}
    """
    from app.utils.db_market import get_market_db_manager
    mgr = get_market_db_manager()
    mgr.ensure_market_db("CNStock")
    pool = mgr._get_pool("CNStock")

    conditions = []
    params = []
    if start_date:
        conditions.append("date >= %s")
        params.append(start_date)
    if end_date:
        conditions.append("date <= %s")
        params.append(end_date)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    with pool.cursor() as cur:
        cur.execute(f"""
            SELECT date, sector_type, sector_name, heat_score, limit_up_count,
                   advance_pct, avg_return, stock_count
            FROM sector_daily_stats {where}
            ORDER BY date, sector_type, sector_name
        """, params)
        rows = cur.fetchall()

    if not rows:
        print("⚠️  sector_daily_stats 无数据, 请先运行 sync_sector_daily.py --backfill")
        return None, None

    # 按 (type, name) 建时序
    # {sector_type: {sector_name: [(date_str, heat, limit_up, adv_pct, avg_ret, count), ...]}}
    series = defaultdict(lambda: defaultdict(list))
    for row in rows:
        date_str = str(row[0])[:10]
        stype = row[1]
        sname = row[2]
        heat = float(row[3] or 0)
        lu = int(row[4] or 0)
        adv = float(row[5] or 0)
        avg_ret = float(row[6] or 0)
        cnt = int(row[7] or 0)
        series[stype][sname].append((date_str, heat, lu, adv, avg_ret, cnt))

    dates = sorted(set(str(r[0])[:10] for r in rows))
    print(f"  加载 {len(rows)} 条记录, 覆盖 {len(dates)} 个交易日 ({dates[0]} ~ {dates[-1]})")
    for stype, sectors in series.items():
        print(f"  {stype}: {len(sectors)} 个板块")

    return series, dates


def load_stock_info():
    from app.utils.basicinfo_db import get_stock_basic_db
    db = get_stock_basic_db()
    pool = db._get_pool()
    with pool.cursor() as cur:
        cur.execute(
            "SELECT symbol, name, industry, concepts "
            "FROM stock_basic_info WHERE status='active'"
        )
        rows = cur.fetchall()
    result = {}
    for row in rows:
        industries = [c.strip() for c in (row[2] or '').split(',') if c.strip()]
        concepts = [c.strip() for c in (row[3] or '').split(',') if c.strip()]
        result[row[0]] = {
            'name': row[1] or '',
            'industries': industries,
            'concepts': concepts,
        }
    return result


# ============================================================
# 板块热度标准化
# ============================================================

def normalize_heat(series):
    """将每个板块的 heat_score 标准化为 z-score (跨时间)
    消除板块间绝对热度差异, 只看相对变化
    """
    normalized = {}
    for stype, sectors in series.items():
        normalized[stype] = {}
        for sname, points in sectors.items():
            heats = [p[1] for p in points]
            if len(heats) < 5:
                continue
            mean = sum(heats) / len(heats)
            std = (sum((h - mean) ** 2 for h in heats) / len(heats)) ** 0.5
            if std < 0.01:
                continue
            normalized[stype][sname] = [
                (p[0], (p[1] - mean) / std, p[2], p[3], p[4], p[5])
                for p in points
            ]
    return normalized


# ============================================================
# 滞后相关性分析
# ============================================================

def calc_lag_correlations(series, dates, lags=[1, 2, 3, 5]):
    """计算板块间滞后相关性
    对每个 (type, name) 对, 计算 A[t] vs B[t+lag] 的相关系数
    返回: {lag: [(A_type, A_name, B_type, B_name, corr, overlap_days), ...]}
    """
    results = {}

    for stype, sectors in series.items():
        # 建日期→热度索引
        date_index = {}  # {sname: {date: heat}}
        for sname, points in sectors.items():
            date_index[sname] = {p[0]: p[1] for p in points}

        sname_list = list(sectors.keys())

        for lag in lags:
            pairs = []
            for i, a_name in enumerate(sname_list):
                a_data = date_index[a_name]
                for j, b_name in enumerate(sname_list):
                    if i == j:
                        continue
                    b_data = date_index[b_name]

                    # 找 A[t] 和 B[t+lag] 都有数据的日期对
                    a_vals, b_vals = [], []
                    for d in dates:
                        d_lag_idx = dates.index(d) + lag
                        if d_lag_idx >= len(dates):
                            break
                        d_lag = dates[d_lag_idx]
                        if d in a_data and d_lag in b_data:
                            a_vals.append(a_data[d])
                            b_vals.append(b_data[d_lag])

                    if len(a_vals) < 20:
                        continue

                    # Pearson 相关系数
                    n = len(a_vals)
                    ma = sum(a_vals) / n
                    mb = sum(b_vals) / n
                    cov = sum((a_vals[k] - ma) * (b_vals[k] - mb) for k in range(n)) / n
                    sa = (sum((v - ma) ** 2 for v in a_vals) / n) ** 0.5
                    sb = (sum((v - mb) ** 2 for v in b_vals) / n) ** 0.5
                    if sa < 1e-9 or sb < 1e-9:
                        continue
                    corr = cov / (sa * sb)

                    if abs(corr) >= 0.15:  # 只保留有意义的相关性
                        pairs.append((stype, a_name, stype, b_name, round(corr, 3), n))

            pairs.sort(key=lambda x: -abs(x[4]))
            if lag not in results:
                results[lag] = []
            results[lag].extend(pairs[:50])  # 每个 lag 取 top 50

    return results


# ============================================================
# 转移矩阵
# ============================================================

def build_transition_matrix(series, dates, heat_quantile=0.7):
    """构建板块热度转移矩阵
    当板块A今天处于"热"状态(heat > quantile), 统计明天哪些板块最常也变热
    返回: {sector_type: {hot_sector: {next_sector: count}}}
    """
    matrices = {}

    for stype, sectors in series.items():
        date_index = {}
        for sname, points in sectors.items():
            date_index[sname] = {p[0]: p[1] for p in points}

        # 计算每个板块的热度分位数阈值
        thresholds = {}
        for sname, points in sectors.items():
            heats = sorted([p[1] for p in points])
            if len(heats) < 10:
                continue
            idx = int(len(heats) * heat_quantile)
            thresholds[sname] = heats[idx]

        # 统计转移
        trans = defaultdict(lambda: defaultdict(int))
        for di in range(len(dates) - 1):
            d_today = dates[di]
            d_tomorrow = dates[di + 1]

            # 找今天热的板块
            hot_today = []
            for sname, thresh in thresholds.items():
                heat_today = date_index.get(sname, {}).get(d_today)
                if heat_today is not None and heat_today >= thresh:
                    hot_today.append(sname)

            # 找明天热的板块
            hot_tomorrow = []
            for sname, thresh in thresholds.items():
                heat_tomorrow = date_index.get(sname, {}).get(d_tomorrow)
                if heat_tomorrow is not None and heat_tomorrow >= thresh:
                    hot_tomorrow.append(sname)

            # 转移计数
            for a in hot_today:
                for b in hot_tomorrow:
                    trans[a][b] += 1

        matrices[stype] = trans

    return matrices


def predict_tomorrow(series, dates, trans_matrices, top_n=10, today_date=None):
    """基于今天的板块状态 + 转移矩阵, 预测明天热门板块
    today_date: 指定"今天"是哪天, 默认用 dates[-1]
    返回: {sector_type: [(sector_name, score), ...]}
    """
    today = today_date or (dates[-1] if dates else None)
    if not today:
        return {}

    predictions = {}

    for stype, sectors in series.items():
        date_index = {}
        for sname, points in sectors.items():
            date_index[sname] = {p[0]: p[1] for p in points}

        # 找今天热的板块 (用全局分位数阈值)
        thresholds = {}
        for sname, points in sectors.items():
            heats = sorted([p[1] for p in points])
            if len(heats) < 10:
                continue
            thresholds[sname] = heats[int(len(heats) * 0.7)]

        hot_today = []
        for sname, thresh in thresholds.items():
            h = date_index.get(sname, {}).get(today)
            if h is not None and h >= thresh:
                hot_today.append(sname)

        # 用转移矩阵打分
        trans = trans_matrices.get(stype, {})
        scores = defaultdict(float)
        for a in hot_today:
            for b, cnt in trans.get(a, {}).items():
                scores[b] += cnt

        # 排序取 top
        ranked = sorted(scores.items(), key=lambda x: -x[1])[:top_n]
        predictions[stype] = ranked

    return predictions


# ============================================================
# 信号评分
# ============================================================

def score_signals(trades, series, dates, stock_info, predictions_by_date, lag=1):
    """给每笔回测信号打分
    评分维度:
      1. 当天板块热度 (该板块今天是否热?)
      2. 预测热度 (该板块明天是否被预测为热?)
      3. 板块趋势 (近N天热度是否在上升?)

    返回: 原 trades 列表, 每笔增加 _score 字段
    """
    # 建日期索引: {sector_type: {sector_name: {date: (heat, lu, adv)}}}
    date_idx = defaultdict(lambda: defaultdict(dict))
    for stype, sectors in series.items():
        for sname, points in sectors.items():
            for p in points:
                date_idx[stype][sname][p[0]] = (p[1], p[2], p[3])

    scored_trades = []
    for t in trades:
        code = t['code']
        entry_date = t['entry_date']
        info = stock_info.get(code, {})
        industries = info.get('industries', [])
        concepts = info.get('concepts', [])

        # 找 entry_date 在 dates 中的位置
        try:
            di = dates.index(entry_date)
        except ValueError:
            t['_score'] = 0
            t['_score_detail'] = {}
            scored_trades.append(t)
            continue

        # 明天的日期
        d_tomorrow = dates[di + 1] if di + 1 < len(dates) else None

        scores = {}

        # ── 维度1: 当天板块热度 ──
        # 该股票所属板块今天 heat_score 的平均值
        today_heats = []
        for ind in industries:
            h = date_idx.get('industry', {}).get(ind, {}).get(entry_date)
            if h:
                today_heats.append(h[0])
        for con in concepts:
            h = date_idx.get('concept', {}).get(con, {}).get(entry_date)
            if h:
                today_heats.append(h[0])

        avg_heat_today = sum(today_heats) / len(today_heats) if today_heats else 0
        scores['today_heat'] = round(avg_heat_today, 2)

        # ── 维度2: 明天预测热度 ──
        # 该板块是否在明天的预测热门列表中
        pred_score = 0
        if d_tomorrow and predictions_by_date:
            preds = predictions_by_date.get(entry_date, {})
            for ind in industries:
                for stype_pred, ranked in preds.items():
                    for sname, sc in ranked:
                        if sname == ind:
                            pred_score += sc
            for con in concepts:
                for stype_pred, ranked in preds.items():
                    for sname, sc in ranked:
                        if sname == con:
                            pred_score += sc
        scores['pred_heat'] = round(pred_score, 2)

        # ── 维度3: 板块趋势 (近5天热度变化) ──
        trend_scores = []
        for ind in industries:
            heats_5d = []
            for offset in range(-4, 1):
                idx = di + offset
                if 0 <= idx < len(dates):
                    h = date_idx.get('industry', {}).get(ind, {}).get(dates[idx])
                    if h:
                        heats_5d.append(h[0])
            if len(heats_5d) >= 3:
                # 简单线性斜率
                n = len(heats_5d)
                x_mean = (n - 1) / 2
                y_mean = sum(heats_5d) / n
                num = sum((k - x_mean) * (heats_5d[k] - y_mean) for k in range(n))
                den = sum((k - x_mean) ** 2 for k in range(n))
                slope = num / den if den > 0 else 0
                trend_scores.append(slope)

        avg_trend = sum(trend_scores) / len(trend_scores) if trend_scores else 0
        scores['trend'] = round(avg_trend, 2)

        # ── 综合评分 ──
        # 权重: 当天热度 40% + 预测热度 40% + 趋势 20%
        # 归一化: heat 用绝对值, pred 用排名分, trend 用斜率
        composite = (
            scores['today_heat'] * 0.4 +
            scores['pred_heat'] * 0.4 +
            scores['trend'] * 0.2
        )
        scores['composite'] = round(composite, 2)

        t['_score'] = scores['composite']
        t['_score_detail'] = scores
        scored_trades.append(t)

    return scored_trades


# ============================================================
# 评估
# ============================================================

def evaluate_scored_trades(trades):
    """对比不同分数段的信号表现"""
    if not trades:
        return

    # 按 composite 分数分组
    scores = [t['_score'] for t in trades]
    scores_sorted = sorted(scores)
    n = len(scores_sorted)

    # 用分位数划分
    q33 = scores_sorted[int(n * 0.33)]
    q67 = scores_sorted[int(n * 0.67)]

    groups = {
        '低分(<P33)': [t for t in trades if t['_score'] < q33],
        '中分(P33-P67)': [t for t in trades if q33 <= t['_score'] < q67],
        '高分(≥P67)': [t for t in trades if t['_score'] >= q67],
    }

    print(f"\n{'='*90}")
    print(f"📊 板块轮动评分 vs 信号表现")
    print(f"{'='*90}")
    print(f"  分数区间: 低分<{q33:.1f}  中分{q33:.1f}~{q67:.1f}  高分≥{q67:.1f}")

    for label, seg in groups.items():
        if not seg:
            continue
        n_t = len(seg)
        wins = sum(1 for t in seg if t['return_pct'] > 0)
        avg_ret = sum(t['return_pct'] for t in seg) / n_t
        avg_peak = sum(t['peak_return_pct'] for t in seg) / n_t
        wr = wins / n_t * 100
        avg_score = sum(t['_score'] for t in seg) / n_t

        ws = [t['return_pct'] for t in seg if t['return_pct'] > 0]
        ls = [t['return_pct'] for t in seg if t['return_pct'] <= 0]
        pr = (sum(ws)/len(ws)) / (abs(sum(ls))/len(ls)) if ws and ls else (999 if ws else 0)
        pr_str = f"{pr:.2f}" if pr < 999 else "∞"

        print(f"\n  {label}:")
        print(f"    {n_t:>3}笔  胜率{wr:>5.1f}%  均收益{avg_ret:>+6.2f}%  "
              f"均峰值{avg_peak:>+6.2f}%  盈亏比{pr_str:>6}  均分{avg_score:>6.1f}")

    # 按策略分别看
    print(f"\n{'='*90}")
    print(f"📊 分策略评分对比")
    print(f"{'='*90}")

    for pl in ["龙回头", "V1", "断板"]:
        seg = [t for t in trades if t['path_label'] == pl]
        if len(seg) < 10:
            continue

        scores_seg = sorted([t['_score'] for t in seg])
        n_seg = len(scores_seg)
        q33_s = scores_seg[int(n_seg * 0.33)]
        q67_s = scores_seg[int(n_seg * 0.67)]

        print(f"\n  ── {pl} ({n_seg}笔) ──")

        for label, filt in [('低分', lambda s: s < q33_s),
                             ('中分', lambda s: q33_s <= s < q67_s),
                             ('高分', lambda s: s >= q67_s)]:
            sub = [t for t in seg if filt(t['_score'])]
            if not sub:
                continue
            n_t = len(sub)
            wr = sum(1 for t in sub if t['return_pct'] > 0) / n_t * 100
            avg = sum(t['return_pct'] for t in sub) / n_t
            print(f"    {label}: {n_t:>3}笔  胜率{wr:>5.1f}%  均收益{avg:>+6.2f}%")

    # Top/Bottom 信号
    trades_sorted = sorted(trades, key=lambda x: x['_score'])
    print(f"\n{'='*90}")
    print(f"📊 最高/最低分信号 Top 10")
    print(f"{'='*90}")

    print(f"\n  🔥 最高分信号:")
    for t in trades_sorted[-10:]:
        detail = t['_score_detail']
        print(f"    {t['code']} [{t['path_label']}] {t['entry_date']} "
              f"分{t['_score']:>6.1f} 收益{t['return_pct']:>+6.2f}% "
              f"(热{detail['today_heat']:.0f} 预测{detail['pred_heat']:.0f} 趋势{detail['trend']:.1f})")

    print(f"\n  💀 最低分信号:")
    for t in trades_sorted[:10]:
        detail = t['_score_detail']
        print(f"    {t['code']} [{t['path_label']}] {t['entry_date']} "
              f"分{t['_score']:>6.1f} 收益{t['return_pct']:>+6.2f}% "
              f"(热{detail['today_heat']:.0f} 预测{detail['pred_heat']:.0f} 趋势{detail['trend']:.1f})")


def print_coupling_insights(lag_corr, top_n=15):
    """打印板块耦合关系洞察"""
    print(f"\n{'='*90}")
    print(f"# 板块耦合关系 (滞后相关性)")
    print(f"{'='*90}")

    for lag, pairs in sorted(lag_corr.items()):
        if not pairs:
            continue
        print(f"\n  ── Lag {lag} 天 (A今天热 → B在{lag}天后热?) ──")
        print(f"  {'A板块':<16} → {'B板块':<16} {'相关系数':>8} {'样本':>6}")
        positive = [(a, b, c, n) for st, a, st2, b, c, n in pairs if c > 0][:top_n]
        negative = [(a, b, c, n) for st, a, st2, b, c, n in pairs if c < 0][:5]

        for a, b, c, n in positive:
            print(f"  {a:<16} → {b:<16} {c:>+8.3f} {n:>6}天")

        if negative:
            print(f"  --- 负相关 (反向轮动) ---")
            for a, b, c, n in negative:
                print(f"  {a:<16} → {b:<16} {c:>+8.3f} {n:>6}天")


def print_transition_insights(trans_matrices, top_n=10):
    """打印转移矩阵洞察"""
    print(f"\n{'='*90}")
    print(f"# 板块热度转移矩阵 (今天热 → 明天谁接棒?)")
    print(f"{'='*90}")

    for stype, trans in trans_matrices.items():
        if not trans:
            continue
        print(f"\n  ── {stype} ──")

        # 找转移频率最高的 pair
        all_pairs = []
        for a, nexts in trans.items():
            for b, cnt in nexts.items():
                if a != b:
                    all_pairs.append((a, b, cnt))
        all_pairs.sort(key=lambda x: -x[2])

        print(f"  {'今天热':<16} → {'明天热':<16} {'次数':>6}")
        for a, b, cnt in all_pairs[:top_n]:
            print(f"  {a:<16} → {b:<16} {cnt:>6}")


# ============================================================
# 主流程
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="板块轮动耦合分析")
    parser.add_argument("--result", default="test_dragon_callback_result.json")
    parser.add_argument("--start", type=str, default="2024-01-01")
    parser.add_argument("--end", type=str, default="")
    parser.add_argument("--lags", type=str, default="1,2,3,5", help="滞后期(逗号分隔)")
    parser.add_argument("--min-history", type=int, default=30, help="最少历史天数")
    args = parser.parse_args()

    lags = [int(x) for x in args.lags.split(",")]
    end_date = args.end or datetime.now().strftime("%Y-%m-%d")

    # ── 加载数据 ──
    print(f"[1/6] 加载板块历史 ({args.start} ~ {end_date})...")
    series, dates = load_sector_daily(args.start, end_date)
    if series is None:
        return

    if len(dates) < args.min_history:
        print(f"⚠️  历史数据不足 ({len(dates)}天 < {args.min_history}天), 请先 backfill")
        return

    print(f"\n[2/6] 加载 stock_basic_info...")
    stock_info = load_stock_info()
    print(f"  {len(stock_info)} 只股票")

    print(f"\n[3/6] 标准化热度...")
    norm_series = normalize_heat(series)

    # ── 滞后相关性 ──
    print(f"\n[4/6] 计算滞后相关性 (lags={lags})...")
    lag_corr = calc_lag_correlations(norm_series, dates, lags)
    print_coupling_insights(lag_corr)

    # ── 转移矩阵 ──
    print(f"\n[5/6] 构建转移矩阵...")
    trans_matrices = build_transition_matrix(series, dates)
    print_transition_insights(trans_matrices)

    # ── 信号评分 ──
    print(f"\n[6/6] 信号评分...")
    with open(args.result, encoding='utf-8') as f:
        trades = json.load(f)

    # 逐日预测: 用全局转移矩阵 + 逐日变化的"今天谁热"输入
    # 板块轮动模式是结构性关系, 不需要逐日重建
    # 只需要每天根据当天板块状态查转移矩阵, 得到明天预测
    trade_dates = sorted(set(t['entry_date'] for t in trades))
    print(f"  回测信号覆盖 {len(trade_dates)} 个交易日")

    predictions_by_date = {}
    for td in trade_dates:
        predictions_by_date[td] = predict_tomorrow(series, dates, trans_matrices, today_date=td)

    print(f"  生成 {len(predictions_by_date)} 天的预测")

    scored = score_signals(trades, series, dates, stock_info, predictions_by_date)
    evaluate_scored_trades(scored)

    # 导出
    out_path = Path(args.result).stem + "_scored.json"
    # 只保留评分字段, 不重复存全部数据
    export = []
    for t in scored:
        export.append({
            'code': t['code'],
            'path_label': t['path_label'],
            'entry_date': t['entry_date'],
            'return_pct': t['return_pct'],
            'peak_return_pct': t['peak_return_pct'],
            '_score': t['_score'],
            '_score_detail': t['_score_detail'],
        })
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(export, f, ensure_ascii=False, indent=2)
    print(f"\n💾 已导出: {out_path}")


if __name__ == "__main__":
    main()
