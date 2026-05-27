"""
money_flow — 个股资金流向扩展参数

数据源: 东方财富个股资金流向 API（直接 HTTP）

自动注入 df 的列：
    main_net_inflow     主力净流入 (元)
    super_net_inflow    超大单净流入 (元)
    big_net_inflow      大单净流入 (元)
    mid_net_inflow      中单净流入 (元)
    small_net_inflow    小单净流入 (元)
    main_net_pct        主力净流入占比 (%)

脚本可用变量：
    stock_main_inflow       最新主力净流入 (元)
    stock_main_inflow_pct   最新主力净流入占比 (%)
    stock_inflow_detail     近5日资金流向明细 [{date, main_net, super_net, big_net}]
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
    """将 6 位代码转为东方财富 secid (如 '600519' -> '1.600519')"""
    s = symbol.strip()
    if not s:
        return ''
    if s.startswith(('6', '9')):
        return f'1.{s}'
    elif s.startswith(('0', '2', '3')):
        return f'0.{s}'
    elif s.startswith(('4', '8')):
        return f'0.{s}'
    return f'1.{s}'


def _safe_float(val, default=0.0):
    try:
        if val is None or val == '' or val == '-':
            return default
        return float(val)
    except (TypeError, ValueError):
        return default


def _fetch_money_flow_kline(symbol: str, days: int = 10) -> list:
    """获取个股资金流向日K线数据（带缓存）。"""
    cache_key = f"{symbol}_{days}"
    if cache_key in _cache:
        return _cache[cache_key]

    secid = _to_secid(symbol)
    if not secid:
        return []

    try:
        url = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
        params = {
            "lmt": days,
            "klt": 101,
            "secid": secid,
            "fields1": "f1,f2,f3,f7",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
            "ut": "b2884a393a59ad64002292a3e90d46a5",
            "_": "1",
        }
        resp = requests.get(url, params=params, headers=HEADERS, timeout=10)
        data = resp.json()

        klines = (data.get("data") or {}).get("klines") or []
        results = []
        for line in klines:
            parts = line.split(',')
            if len(parts) < 7:
                continue
            results.append({
                "date": parts[0],
                "main_net_inflow": _safe_float(parts[1]),      # 主力净流入 (元)
                "small_net_inflow": _safe_float(parts[2]),     # 小单净流入 (元)
                "mid_net_inflow": _safe_float(parts[3]),       # 中单净流入 (元)
                "big_net_inflow": _safe_float(parts[4]),       # 大单净流入 (元)
                "super_net_inflow": _safe_float(parts[5]),     # 超大单净流入 (元)
            })

        _cache[cache_key] = results
        logger.info("money_flow(%s): 获取 %d 日资金流向", symbol, len(results))
        return results

    except Exception as e:
        logger.debug("money_flow(%s) 获取失败: %s", symbol, e)
        _cache[cache_key] = []
        return []


def _fetch_realtime_flow(symbol: str) -> dict:
    """获取个股实时资金流向。"""
    secid = _to_secid(symbol)
    if not secid:
        return {}

    try:
        url = "https://push2.eastmoney.com/api/qt/stock/get"
        params = {
            "secid": secid,
            "fields": "f62,f66,f69,f70,f72,f184,f66,f69",
            "ut": "b2884a393a59ad64002292a3e90d46a5",
        }
        resp = requests.get(url, params=params, headers=HEADERS, timeout=10)
        data = resp.json()
        d = data.get("data") or {}

        return {
            "main_net_inflow": _safe_float(d.get("f62")),      # 主力净流入 (元)
            "super_net_inflow": _safe_float(d.get("f66")),     # 超大单净流入 (元)
            "big_net_inflow": _safe_float(d.get("f69")),       # 大单净流入 (元)
            "mid_net_inflow": _safe_float(d.get("f70")),       # 中单净流入 (元)
            "small_net_inflow": _safe_float(d.get("f72")),     # 小单净流入 (元)
            "main_net_pct": _safe_float(d.get("f184")),        # 主力净占比 (%)
        }

    except Exception as e:
        logger.debug("money_flow realtime(%s) 失败: %s", symbol, e)
        return {}


@provider
def register(ctx: dict) -> dict:
    symbol = ctx.get('symbol', '')
    df = ctx.get('df')

    extras = {}

    if symbol and df is not None and len(df) > 0:
        # 获取历史资金流向
        flow_data = _fetch_money_flow_kline(symbol, days=20)

        if flow_data:
            # 构建 DataFrame 用于合并
            import pandas as pd
            flow_df = pd.DataFrame(flow_data)

            # 尝试按日期匹配到 df
            if 'date' in df.columns or df.index.name == 'date' or hasattr(df.index, 'date'):
                # df 有日期列或索引
                df_dates = df['date'] if 'date' in df.columns else df.index

                # 转换日期格式统一
                flow_map = {}
                for fd in flow_data:
                    d = str(fd['date']).replace('-', '')[:8]
                    flow_map[d] = fd

                main_list = []
                super_list = []
                big_list = []
                mid_list = []
                small_list = []
                pct_list = []

                for idx in range(len(df)):
                    try:
                        if 'date' in df.columns:
                            d = str(df['date'].iloc[idx]).replace('-', '')[:8]
                        else:
                            d = str(df.index[idx]).replace('-', '')[:8]
                        fd = flow_map.get(d, {})
                        main_list.append(fd.get('main_net_inflow', 0))
                        super_list.append(fd.get('super_net_inflow', 0))
                        big_list.append(fd.get('big_net_inflow', 0))
                        mid_list.append(fd.get('mid_net_inflow', 0))
                        small_list.append(fd.get('small_net_inflow', 0))
                        total = abs(fd.get('main_net_inflow', 0)) + abs(fd.get('small_net_inflow', 0)) + abs(fd.get('mid_net_inflow', 0))
                        pct_list.append(round(fd.get('main_net_inflow', 0) / total * 100, 2) if total > 0 else 0)
                    except Exception:
                        main_list.append(0)
                        super_list.append(0)
                        big_list.append(0)
                        mid_list.append(0)
                        small_list.append(0)
                        pct_list.append(0)

                df['main_net_inflow'] = main_list
                df['super_net_inflow'] = super_list
                df['big_net_inflow'] = big_list
                df['mid_net_inflow'] = mid_list
                df['small_net_inflow'] = small_list
                df['main_net_pct'] = pct_list

            else:
                # 无日期列，用最后一行填充最新数据
                latest = flow_data[-1] if flow_data else {}
                for col in ['main_net_inflow', 'super_net_inflow', 'big_net_inflow', 'mid_net_inflow', 'small_net_inflow']:
                    df[col] = latest.get(col, 0)
                total = abs(latest.get('main_net_inflow', 0)) + abs(latest.get('small_net_inflow', 0)) + abs(latest.get('mid_net_inflow', 0))
                df['main_net_pct'] = round(latest.get('main_net_inflow', 0) / total * 100, 2) if total > 0 else 0

            # 暴露最新值和明细
            latest = flow_data[-1] if flow_data else {}
            extras['stock_main_inflow'] = latest.get('main_net_inflow', 0)
            extras['stock_main_inflow_pct'] = float(df['main_net_pct'].iloc[-1]) if 'main_net_pct' in df.columns else 0.0
            extras['stock_inflow_detail'] = flow_data[-5:] if len(flow_data) >= 5 else flow_data

        else:
            # 无数据时填 0
            for col in ['main_net_inflow', 'super_net_inflow', 'big_net_inflow', 'mid_net_inflow', 'small_net_inflow', 'main_net_pct']:
                df[col] = 0.0
            extras['stock_main_inflow'] = 0.0
            extras['stock_main_inflow_pct'] = 0.0
            extras['stock_inflow_detail'] = []

    return extras
