#!/usr/bin/env python3
"""
中国金融数据统一获取层 — 多源降级
优先级: Tushare → AKShare → BaoStock → 直接爬官方

依赖: pip install tushare baostock akshare requests beautifulsoup4 pandas

Tushare Token 配置:
  export TUSHARE_TOKEN=your_token
  或写入 ~/.llm_config.json: {"tushare": {"token": "xxx"}}
"""

import os
import json
import time
import pandas as pd
import requests
from datetime import datetime
from functools import wraps

# ═══════════════════════════════════════════════════
#  通用工具
# ═══════════════════════════════════════════════════

def retry(max_retries=2, delay=1):
    """重试装饰器"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_err = None
            for i in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_err = e
                    if i < max_retries:
                        time.sleep(delay)
            raise last_err
        return wrapper
    return decorator


def _check_available(name):
    """快速检查数据源是否可用（import + token），不走 retry。"""
    try:
        if name == "tushare":
            import tushare
            return bool(_get_tushare_token())
        elif name == "akshare":
            import akshare
            return True
        elif name == "baostock":
            import baostock
            return True
    except ImportError:
        return False
    return True


def fallback(*sources):
    """降级链: 按顺序尝试数据源，第一个成功即返回。
    快速跳过不可用的源（未安装 / 无 token），不浪费 retry 时间。
    """
    # 预检查可用性，过滤掉不可用的源
    available = []
    for name, func in sources:
        # 从 name 中提取库名（如 "tushare"、"akshare"）
        lib = name.split("-")[0] if "-" in name else name
        if lib in ("tushare", "akshare", "baostock"):
            if not _check_available(lib):
                print(f"    ⏭️  {name} 跳过 (未安装或无 token)")
                continue
        available.append((name, func))

    def wrapper(*args, **kwargs):
        errors = []
        for name, func in available:
            try:
                result = func(*args, **kwargs)
                if result is None:
                    errors.append(f"{name}: 返回 None")
                    continue
                # 空 DataFrame 也视为失败
                if isinstance(result, pd.DataFrame) and result.empty:
                    errors.append(f"{name}: 返回空 DataFrame")
                    continue
                print(f"    ✅ 数据源: {name}")
                return result
            except Exception as e:
                errors.append(f"{name}: {e}")
                print(f"    ⚠️ {name} 失败: {e}")
        print(f"    ❌ 所有数据源均失败: {'; '.join(errors)}")
        return None
    return wrapper


# ═══════════════════════════════════════════════════
#  Tushare 数据源
# ═══════════════════════════════════════════════════

def _get_tushare_token():
    token = os.environ.get("TUSHARE_TOKEN")
    if not token:
        cfg = os.path.expanduser("~/.llm_config.json")
        if os.path.exists(cfg):
            with open(cfg) as f:
                token = json.load(f).get("tushare", {}).get("token")
    return token


def _tushare_api():
    import tushare as ts
    token = _get_tushare_token()
    if not token:
        raise ValueError("TUSHARE_TOKEN 未配置")
    return ts.pro_api(token)



@retry()
def ts_index_daily(symbol="000300.SH"):
    """Tushare: 指数日线"""
    pro = _tushare_api()
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - pd.Timedelta(days=200)).strftime("%Y%m%d")
    df = pro.index_daily(ts_code=symbol, start_date=start, end_date=end)
    return df.sort_values("trade_date")



@retry()
def ts_northbound():
    """Tushare: 北向资金"""
    pro = _tushare_api()
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - pd.Timedelta(days=30)).strftime("%Y%m%d")
    return pro.moneyflow_hsgt(start_date=start, end_date=end)


# ═══════════════════════════════════════════════════
#  AKShare 数据源 (备选)
# ═══════════════════════════════════════════════════






@retry()
def ak_index_daily(code="sh000300"):
    """AKShare: 指数日线"""
    import akshare as ak
    return ak.stock_zh_index_daily(symbol=code)

@retry()
def ak_northbound():
    """AKShare: 北向资金"""
    import akshare as ak
    return ak.stock_hsgt_north_net_flow_in_em(symbol="北上")


def hexin_northbound() -> dict:
    """同花顺: 北向资金实时分钟流向 (hsgtApi)。

    沪股通/深股通当日分钟级净买入（~262个时间点），盘中实时更新。
    与 ts_northbound/ak_northbound（日级历史）互补。
    """
    url = "https://data.hexin.cn/market/hsgtApi/method/dayChart/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/117.0.0.0 Safari/537.36",
        "Host": "data.hexin.cn",
        "Referer": "https://data.hexin.cn/",
    }
    r = requests.get(url, headers=headers, timeout=10)
    d = r.json()
    times = d.get("time", [])
    hgt = d.get("hgt", [])
    sgt = d.get("sgt", [])

    n = len(times)
    points = []
    for i in range(n):
        points.append({
            "time": times[i],
            "hgt_yi": hgt[i] if i < len(hgt) else None,
            "sgt_yi": sgt[i] if i < len(sgt) else None,
        })

    hgt_latest = next((p["hgt_yi"] for p in reversed(points) if p["hgt_yi"] is not None), 0)
    sgt_latest = next((p["sgt_yi"] for p in reversed(points) if p["sgt_yi"] is not None), 0)

    return {
        "points": len(points),
        "hgt_latest_yi": hgt_latest,
        "sgt_latest_yi": sgt_latest,
        "total_latest_yi": round((hgt_latest or 0) + (sgt_latest or 0), 2),
        "data": points[-10:],
    }


def northbound_daily():
    """北向资金日级数据（Tushare → AKShare fallback）。返回 DataFrame。"""
    return fallback(
        ("tushare", ts_northbound),
        ("akshare", ak_northbound),
    )()





@retry()
def ak_news():
    """AKShare: 财经新闻"""
    import akshare as ak
    return ak.stock_news_em(symbol="财经")


# ═══════════════════════════════════════════════════
#  BaoStock 数据源
# ═══════════════════════════════════════════════════

@retry()
def bs_index_daily(code="sh.000300"):
    """BaoStock: 指数日线"""
    import baostock as bs
    bs.login()
    try:
        end = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - pd.Timedelta(days=200)).strftime("%Y-%m-%d")
        rs = bs.query_history_k_data_plus(
            code, "date,open,high,low,close,volume,amount",
            start_date=start, end_date=end, frequency="d", adjustflag="3"
        )
        data = []
        while rs.next():
            data.append(rs.get_row_data())
        df = pd.DataFrame(data, columns=rs.fields)
        for col in ['open', 'high', 'low', 'close', 'volume', 'amount']:
            df[col] = pd.to_numeric(df[col], errors='coerce')
        return df
    finally:
        bs.logout()


# ═══════════════════════════════════════════════════
#  直接爬官方数据 (最稳定)
# ═══════════════════════════════════════════════════

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json, text/html",
}






# ═══════════════════════════════════════════════════
#  统一接口 — 自动降级
# ═══════════════════════════════════════════════════

class ChinaData:
    """中国金融数据统一入口，多源自动降级
    降级链: Tushare → AKShare → BaoStock → 官方直爬
    """

    def __init__(self):
        self._check_sources()

    def _check_sources(self):
        """检查可用数据源"""
        self.available = []
        try:
            import tushare
            if _get_tushare_token():
                self.available.append("tushare")
        except ImportError:
            pass
        try:
            import akshare
            self.available.append("akshare")
        except ImportError:
            pass
        try:
            import baostock
            self.available.append("baostock")
        except ImportError:
            pass
        self.available.append("official")  # 官方源永远可用
        print(f"  📡 可用数据源: {', '.join(self.available)}")


    def index_daily(self, code="000300.SH"):
        """指数日线 (沪深300)"""
        print(f"\n📊 指数日线: {code}")
        bs_code = "sh." + code[:6] if code.endswith(".SH") else "sz." + code[:6]
        ak_code = code[:6].lower()
        return fallback(
            ("tushare", lambda: ts_index_daily(code)),
            ("akshare", lambda: ak_index_daily("sh" + ak_code if code.endswith(".SH") else "sz" + ak_code)),
            ("baostock", lambda: bs_index_daily(bs_code)),
        )()

    def northbound(self):
        """北向资金"""
        print("\n📊 北向资金")
        return fallback(
            ("tushare", ts_northbound),
            ("akshare", ak_northbound),
        )()

    def news(self):
        """财经新闻"""
        print("\n📰 财经新闻")
        return fallback(
            ("akshare", ak_news),
        )()


# ═══════════════════════════════════════════════════
#  测试
# ═══════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  🇨🇳 数据源可用性测试")
    print("=" * 60)

    data = ChinaData()

    # 测试各接口
    tests = [
        ("沪深300", lambda: data.index_daily("000300.SH")),
        ("北向资金", data.northbound),
    ]

    for name, func in tests:
        try:
            df = func()
            if df is not None:
                print(f"\n  ✅ {name}: {len(df)} 行")
                print(df.tail(3).to_string(index=False))
            else:
                print(f"\n  ⚠️ {name}: 无数据")
        except Exception as e:
            print(f"\n  ❌ {name}: {e}")
