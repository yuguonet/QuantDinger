"""
tsohlcv — TSOHLCV 模式匹配扩展参数

TSOHLCV 说明:
    T = bar 位置 (0=第一根K线, 1=第二根, ...)
    S = 最低相似度阈值 (0~100)，每条 bar 独立设定
    OHLCV = open/high/low/close/volume 的**百分比比例**值

    OHLCV 百分比语义:
        O = (open - pre_close) / pre_close * 100
        H = (high - pre_close) / pre_close * 100
        L = (low  - pre_close) / pre_close * 100
        C = (close - pre_close) / pre_close * 100
        V = (volume - pre_volume) / pre_volume * 100

        例: O=-5 表示开盘比前收跌5%, H=+3 表示最高比前收涨3%

    指定 T 位置的 bar 必须满足该条的 S 阈值才算匹配，未指定的 T 位置跳过。
    所有指定位置都通过 → 返回整体平均相似度
    任一位置不通过 → 相似度返回 0

用法 (在策略脚本中):

    # 方式一：手动传参调用
    results = match_tsohlcvs(
        tsohlcv_data=[
            {"T": 0, "S": 90, "O": -5,  "H": -2,  "L": -8,  "C": -3,  "V": 20},
            {"T": 1, "S": 90, "O": -3,  "H": 1,   "L": -5,  "C": 0,   "V": 50},
            {"T": 2, "S": 90, "O": 0,   "H": 3,   "L": -2,  "C": 2,   "V": 30},
            {"T": 6, "S": 80, "O": 2,   "H": 8,   "L": -1,  "C": 5,   "V": -20},
        ],
    )

    if results['matched']:
        matches = results['matches']  # [{bar_index, similarity}]

    # 方式二：通过 backtest_params 自动触发
    # 在 backtest_params 中设置:
    #   tsohlcv_template: [...]  → TSOHLCV 模板数据
    # 插件会自动扫描并注入以下变量:
    #   tsohlcv_matched     → bool, 当前标的是否有匹配
    #   tsohlcv_similarity  → float, 整体相似度 (0~100)
    #   tsohlcv_matches     → list, 匹配明细 [{bar_index, similarity}]
"""
import logging
from typing import Any, Dict, List

from . import provider

logger = logging.getLogger(__name__)


def _dim_similarity(template_val: float, stock_val: float) -> float:
    """计算单个维度的相似度 (0~100)。

    当 |template| >= 1 时用相对偏差，< 1 时用绝对差(放大系数)避免小值失真。
    """
    abs_t = abs(template_val)
    diff = abs(template_val - stock_val)
    if abs_t >= 1.0:
        return max(0.0, 100.0 - diff / max(abs_t, 1e-10) * 100.0)
    else:
        return max(0.0, 100.0 - diff * 50.0)


def _bar_similarity(tmpl: dict, o_pct: float, h_pct: float, l_pct: float,
                     c_pct: float, v_pct: float) -> float:
    """计算单根 bar 的 OHLCV 百分比相似度 (0~100)。"""
    dims = [
        _dim_similarity(tmpl.get('O', 0), o_pct),
        _dim_similarity(tmpl.get('H', 0), h_pct),
        _dim_similarity(tmpl.get('L', 0), l_pct),
        _dim_similarity(tmpl.get('C', 0), c_pct),
        _dim_similarity(tmpl.get('V', 0), v_pct),
    ]
    return sum(dims) / len(dims)


def _find_required_ts(tsohlcv_data: list) -> list:
    """提取并排序需要检测的 T 位置。"""
    return sorted(set(int(item['T']) for item in tsohlcv_data))


def _build_ts_map(tsohlcv_data: list) -> dict:
    """构建 T → {S, O, H, L, C, V} 映射。"""
    return {int(item['T']): item for item in tsohlcv_data}


def _safe_pct(cur: float, prev: float) -> float:
    """安全计算百分比变化。"""
    if prev == 0:
        return 0.0
    return (cur - prev) / abs(prev) * 100.0


def match_tsohlcvs(
    tsohlcv_data: List[Dict[str, Any]],
    symbol: str = '',
    df=None,
) -> Dict[str, Any]:
    """在 OHLCV 数据上扫描 TSOHLCV 模式。

    Args:
        tsohlcv_data: TSOHLCV 模板列表, 每项包含 T/S/O/H/L/C/V
            O/H/L/C = (价格 - 前收盘) / 前收盘 * 100
            V       = (成交量 - 前成交量) / 前成交量 * 100
        symbol: 标的代码 (仅用于日志)
        df: K 线 DataFrame (需含 open/high/low/close/volume 列)

    Returns:
        dict: {
            'matched': bool,
            'similarity': float,      # 整体相似度 (0~100, 无匹配时0)
            'matches': list,          # [{bar_index, similarity}]
        }
    """
    empty_result = {
        'matched': False, 'similarity': 0.0, 'matches': [],
    }

    if df is None or len(df) == 0 or not tsohlcv_data:
        return empty_result

    required_ts = _find_required_ts(tsohlcv_data)
    ts_map = _build_ts_map(tsohlcv_data)

    if not required_ts:
        return empty_result

    max_t = max(required_ts)
    n = len(df)

    # 需要 start-1 存在 (作为 T=0 的 pre_close)，所以 start 最小为 1
    # 需要 start+max_t < n，所以 start 最大为 n-max_t-1
    if n < max_t + 2:
        logger.debug("tsohlcv(%s): 数据不足, 需要 %d 根, 仅 %d 根", symbol, max_t + 2, n)
        return empty_result

    # 提取 OHLCV 序列
    opens = df['open'].astype('float64').values
    highs = df['high'].astype('float64').values
    lows = df['low'].astype('float64').values
    closes = df['close'].astype('float64').values
    volumes = df['volume'].astype('float64').values

    matches = []

    # 滑动窗口扫描 (start 从 1 开始，保证 start-1 存在)
    for start in range(1, n - max_t):
        # T=0 的前一根 bar 作为参考基准
        pre_close = closes[start - 1]
        pre_volume = volumes[start - 1]

        if pre_close <= 0:
            continue

        all_pass = True
        sim_sum = 0.0
        sim_count = 0

        for t in required_ts:
            idx = start + t
            if idx >= n:
                all_pass = False
                break

            tmpl = ts_map[t]

            # 计算当前 bar 相对 pre_close / pre_volume 的百分比变化
            o_pct = _safe_pct(opens[idx], pre_close)
            h_pct = _safe_pct(highs[idx], pre_close)
            l_pct = _safe_pct(lows[idx], pre_close)
            c_pct = _safe_pct(closes[idx], pre_close)
            v_pct = _safe_pct(volumes[idx], pre_volume)

            bar_sim = _bar_similarity(tmpl, o_pct, h_pct, l_pct, c_pct, v_pct)
            min_sim = float(tmpl['S'])

            if bar_sim < min_sim:
                all_pass = False
                break

            sim_sum += bar_sim
            sim_count += 1

        if all_pass and sim_count > 0:
            avg_sim = sim_sum / sim_count
            matches.append({
                'bar_index': start,
                'similarity': round(avg_sim, 2),
            })

    # 汇总统计
    result = dict(empty_result)

    if matches:
        result['matched'] = True
        result['matches'] = matches
        result['similarity'] = round(
            sum(m['similarity'] for m in matches) / len(matches), 2
        )

    logger.info(
        "tsohlcv(%s): 扫描 %d 根 bar, 匹配 %d 次, 相似度 %.1f%%",
        symbol, n, len(matches), result['similarity'],
    )

    return result


@provider
def register(ctx: dict) -> dict:
    """注册 TSOHLCV 扩展参数。"""
    symbol = ctx.get('symbol', '')
    df = ctx.get('df')
    bp = ctx.get('backtest_params') or {}

    extras = {}

    # 方式一: 始终暴露 match_tsohlcvs 函数供策略脚本手动调用
    def _match_tsohlcvs(tsohlcv_data):
        return match_tsohlcvs(
            tsohlcv_data=tsohlcv_data,
            symbol=symbol,
            df=df,
        )
    extras['match_tsohlcvs'] = _match_tsohlcvs

    # 方式二: 若 backtest_params 中配置了模板, 自动扫描并注入结果变量
    template = bp.get('tsohlcv_template')
    if template and isinstance(template, list) and df is not None and len(df) > 0:
        try:
            result = match_tsohlcvs(
                tsohlcv_data=template,
                symbol=symbol,
                df=df,
            )
            extras['tsohlcv_matched'] = result['matched']
            extras['tsohlcv_similarity'] = result['similarity']
            extras['tsohlcv_matches'] = result['matches']
        except Exception as e:
            logger.debug("tsohlcv(%s) 自动扫描失败: %s", symbol, e)
            extras['tsohlcv_matched'] = False
            extras['tsohlcv_similarity'] = 0.0
            extras['tsohlcv_matches'] = []

    return extras
