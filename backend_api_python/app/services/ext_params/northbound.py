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
import requests

from . import provider

logger = logging.getLogger(__name__)

_cache = {}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://data.eastmoney.com/",
}


def _to_secid(symbol: str) -> str:
    """6 位代码 -> 东方财富 secid"""
    s = symbol.strip()
    if not s:
        return ''
    if s.startswith(('6', '9')):
        return f'1.{s}'
    return f'0.{s}'


def _safe_float(val, default=0.0):
    try:
        if val is None or val == '' or val == '-':
            return default
        return float(val)
    except (TypeError, ValueError):
        return default


def _fetch_north_hold(symbol: str, days: int = 10) -> list:
    """获取个股沪深港通持股历史数据（带缓存）。"""
    cache_key = f"{symbol}_{days}"
    if cache_key in _cache:
        return _cache[cache_key]

    secid = _to_secid(symbol)
    if not secid:
        return []

    try:
        url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
        params = {
            "sortColumns": "TRADE_DATE",
            "sortTypes": "-1",
            "pageSize": days,
            "pageNumber": 1,
            "reportName": "RPT_MUTUAL_HOLD_DET",
            "columns": "ALL",
            "filter": f"(SECURITY_CODE=\"{symbol}\")",
        }
        resp = requests.get(url, params=params, headers=HEADERS, timeout=15)
        data = resp.json()

        items = (data.get("result") or {}).get("data") or []
        results = []
        for item in items:
            results.append({
                "date": str(item.get("TRADE_DATE", ""))[:10],
                "hold_vol": _safe_float(item.get("HOLD_SHARES")),           # 持股数量 (股)
                "hold_pct": _safe_float(item.get("A_SHARES_RATIO")),        # 持股占流通比 (%)
                "hold_vol_chg": _safe_float(item.get("HOLD_SHARES_CHG")),   # 持股变化 (股)
                "hold_market_value": _safe_float(item.get("HOLD_MARKET")),  # 持股市值 (元)
            })

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

        if north_data:
            # 构建日期映射
            north_map = {}
            for nd in north_data:
                d = str(nd['date']).replace('-', '')[:8]
                north_map[d] = nd

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
