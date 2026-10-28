"""
northbound — 北向资金 (沪深港通) 持股扩展参数

数据源: 东方财富沪深港通持股 API

自动注入 df 的列：
    north_hold_vol      北向持股数量 (股)
    north_hold_pct      北向持股占流通比 (%)
    north_hold_vol_chg  北向持股数量变化 (股, 正=增持)

脚本可用变量：
    stock_north_hold     最新北向持股数量
    stock_north_pct      最新北向持股占比 (%)
    stock_north_chg      最新持股变化
    stock_north_trend    近期趋势 ("增持"/"减持"/"持平")
"""
import logging
from datetime import datetime, timedelta

from . import provider
from app.utils.trading_calendar import trading_days_count

logger = logging.getLogger(__name__)

# 北向持股数据仅当天有效（1 个交易日）
_MAX_STALE_TRADING_DAYS = 1

_cache = {}




def _is_north_data_stale(north_data: list, df, max_stale_days: int = _MAX_STALE_TRADING_DAYS) -> bool:
    """校验北向数据是否过期。

    使用 trading_calendar.trading_days_count 计算北向数据最新日期
    与 df 最新日期之间相隔的交易日数，超过 max_stale_days 则视为过期。
    """
    if not north_data:
        return True

    latest_north_date = north_data[0].get('date', '')
    if not latest_north_date:
        return True

    north_date_str = latest_north_date[:10]

    # 取 df 最新日期
    try:
        if 'date' in df.columns:
            df_latest = str(df['date'].iloc[-1])[:10]
        else:
            df_latest = str(df.index[-1])[:10]
    except Exception:
        return True

    # 北向日期比 df 最新还新 → 不过期
    if north_date_str >= df_latest:
        return False

    # 用交易日历精确计算相隔交易日数
    gap = trading_days_count(north_date_str, df_latest) - 1  # -1: 包含两端需减一

    if gap > max_stale_days:
        logger.info(
            "northbound: 数据过期 — 北向最新 %s, df最新 %s, 相隔 %d 个交易日",
            north_date_str, df_latest, gap
        )
        return True

    return False


def _fetch_north_hold(symbol: str, days: int = 10) -> list:
    """获取个股沪深港通持股历史数据（带缓存）。

    取数经 `coord_index("northbound_stock_hold")`（provider_index/em_datacenter：
    东财 RPT_MUTUAL_HOLD_DET，按交易日聚合后按日期降序）。
    """
    cache_key = f"{symbol}_{days}"
    if cache_key in _cache:
        return _cache[cache_key]

    if not symbol:
        return []

    try:
        from app.data_sources.coord_index import coordinate

        out = coordinate("northbound_stock_hold", code=symbol, days=days)
        results = [r for r in ((out.get("data") or {}).get("rows") or []) if isinstance(r, dict)]
        if not out.get("ok"):
            logger.debug("northbound(%s) 取数失败: %s", symbol, out.get("error"))

        _cache[cache_key] = results
        logger.info("northbound(%s): 获取 %d 日北向持股", symbol, len(results))
        return results

    except Exception as e:
        logger.debug("northbound(%s) 获取失败: %s", symbol, e)
        _cache[cache_key] = []
        return []


@provider
def register(ctx: dict) -> dict:
    symbol = ctx.get('symbol', '')
    df = ctx.get('df')

    extras = {}

    if symbol and df is not None and len(df) > 0:
        north_data = _fetch_north_hold(symbol, days=20)

        if north_data and not _is_north_data_stale(north_data, df):
            # 构建日期映射（同一天多个机构 → 按日期聚合求和）
            north_map = {}
            for nd in north_data:
                d = str(nd['date']).replace('-', '')[:8]
                if d in north_map:
                    north_map[d]['hold_vol'] += nd.get('hold_vol', 0)
                    north_map[d]['hold_pct'] += nd.get('hold_pct', 0)
                    north_map[d]['hold_vol_chg'] += nd.get('hold_vol_chg', 0)
                    north_map[d]['hold_market_value'] += nd.get('hold_market_value', 0)
                else:
                    north_map[d] = {
                        'hold_vol': nd.get('hold_vol', 0),
                        'hold_pct': nd.get('hold_pct', 0),
                        'hold_vol_chg': nd.get('hold_vol_chg', 0),
                        'hold_market_value': nd.get('hold_market_value', 0),
                    }

            hold_vol_list = []
            hold_pct_list = []
            hold_chg_list = []

            for idx in range(len(df)):
                try:
                    if 'date' in df.columns:
                        d = str(df['date'].iloc[idx]).replace('-', '')[:8]
                    else:
                        d = str(df.index[idx]).replace('-', '')[:8]
                    nd = north_map.get(d, {})
                    hold_vol_list.append(nd.get('hold_vol', 0))
                    hold_pct_list.append(nd.get('hold_pct', 0))
                    hold_chg_list.append(nd.get('hold_vol_chg', 0))
                except Exception:
                    hold_vol_list.append(0)
                    hold_pct_list.append(0)
                    hold_chg_list.append(0)

            df['north_hold_vol'] = hold_vol_list
            df['north_hold_pct'] = hold_pct_list
            df['north_hold_vol_chg'] = hold_chg_list

            # 暴露最新值
            latest = north_data[0] if north_data else {}  # 已按日期降序
            extras['stock_north_hold'] = latest.get('hold_vol', 0)
            extras['stock_north_pct'] = latest.get('hold_pct', 0)
            extras['stock_north_chg'] = latest.get('hold_vol_chg', 0)

            # 趋势判断：看最近3日变化方向
            recent_chgs = [nd.get('hold_vol_chg', 0) for nd in north_data[:3] if nd.get('hold_vol_chg', 0) != 0]
            if recent_chgs:
                avg_chg = sum(recent_chgs) / len(recent_chgs)
                if avg_chg > 0:
                    extras['stock_north_trend'] = '增持'
                elif avg_chg < 0:
                    extras['stock_north_trend'] = '减持'
                else:
                    extras['stock_north_trend'] = '持平'
            else:
                extras['stock_north_trend'] = '未知'

        else:
            for col in ['north_hold_vol', 'north_hold_pct', 'north_hold_vol_chg']:
                df[col] = 0.0
            extras['stock_north_hold'] = 0
            extras['stock_north_pct'] = 0.0
            extras['stock_north_chg'] = 0
            extras['stock_north_trend'] = '未知'

    return extras
