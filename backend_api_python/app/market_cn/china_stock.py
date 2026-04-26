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
def ts_gdp():
    """Tushare: GDP"""
    pro = _tushare_api()
    df = pro.cn_gdp()
    return df.sort_values("quarter").tail(12)


@retry()
def ts_cpi():
    """Tushare: CPI"""
    pro = _tushare_api()
    df = pro.cn_cpi()
    return df.sort_values("month").tail(12)


@retry()
def ts_ppi():
    """Tushare: PPI"""
    pro = _tushare_api()
    df = pro.cn_ppi()
    return df.sort_values("month").tail(12)


@retry()
def ts_pmi():
    """Tushare: PMI"""
    pro = _tushare_api()
    df = pro.cn_pmi()
    return df.sort_values("month").tail(12)


@retry()
def ts_m2():
    """Tushare: M2"""
    pro = _tushare_api()
    df = pro.cn_m()
    return df.sort_values("month").tail(12)


@retry()
def ts_money_supply():
    """Tushare: 货币供应"""
    pro = _tushare_api()
    df = pro.cn_money()
    return df.sort_values("month").tail(12)


@retry()
def ts_lpr():
    """Tushare: LPR 利率"""
    pro = _tushare_api()
    df = pro.lpr()
    return df.sort_values("date").tail(12)


@retry()
def ts_index_daily(symbol="000300.SH"):
    """Tushare: 指数日线"""
    pro = _tushare_api()
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - pd.Timedelta(days=200)).strftime("%Y%m%d")
    df = pro.index_daily(ts_code=symbol, start_date=start, end_date=end)
    return df.sort_values("trade_date")


@retry()
def ts_stock_daily(ts_code):
    """Tushare: 个股日线"""
    pro = _tushare_api()
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - pd.Timedelta(days=200)).strftime("%Y%m%d")
    df = pro.daily(ts_code=ts_code, start_date=start, end_date=end)
    return df.sort_values("trade_date")


@retry()
def ts_stock_basic():
    """Tushare: 全部A股列表"""
    pro = _tushare_api()
    return pro.stock_basic(exchange='', list_status='L', fields='ts_code,name,industry,market')


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
def ak_gdp():
    """AKShare: GDP"""
    import akshare as ak
    return ak.macro_china_gdp_yearly()

@retry()
def ak_cpi():
    """AKShare: CPI"""
    import akshare as ak
    return ak.macro_china_cpi_monthly()

@retry()
def ak_ppi():
    """AKShare: PPI"""
    import akshare as ak
    return ak.macro_china_ppi_yearly()

@retry()
def ak_pmi():
    """AKShare: PMI"""
    import akshare as ak
    return ak.macro_china_pmi()

@retry()
def ak_m2():
    """AKShare: M2"""
    import akshare as ak
    return ak.macro_china_m2_yearly()

@retry()
def ak_index_daily(code="sh000300"):
    """AKShare: 指数日线"""
    import akshare as ak
    return ak.stock_zh_index_daily(symbol=code)

@retry()
def ak_stock_daily(code="sh600519"):
    """AKShare: 个股日线"""
    import akshare as ak
    return ak.stock_zh_a_hist(symbol=code.replace("sh","").replace("sz",""), period="daily", adjust="qfq")

@retry()
def ak_stock_basic():
    """AKShare: A股列表"""
    import akshare as ak
    return ak.stock_zh_a_spot_em()

@retry()
def ak_northbound():
    """AKShare: 北向资金"""
    import akshare as ak
    return ak.stock_hsgt_north_net_flow_in_em(symbol="北上")

@retry()
def ak_lpr():
    """AKShare: LPR"""
    import akshare as ak
    return ak.macro_china_lpr()

@retry()
def ak_social_financing():
    """AKShare: 社融"""
    import akshare as ak
    return ak.macro_china_shrzgm()

@retry()
def ak_trade():
    """AKShare: 进出口"""
    import akshare as ak
    return ak.macro_china_trade_balance()

@retry()
def ak_news():
    """AKShare: 财经新闻"""
    import akshare as ak
    return ak.stock_news_em(symbol="财经")


# ═══════════════════════════════════════════════════
#  BaoStock 数据源
# ═══════════════════════════════════════════════════

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


def bs_stock_daily(code="sh.600519"):
    """BaoStock: 个股日线"""
    return bs_index_daily(code)


def bs_stock_basic():
    """BaoStock: A股列表"""
    import baostock as bs
    bs.login()
    try:
        rs = bs.query_stock_basic()
        data = []
        while rs.next():
            data.append(rs.get_row_data())
        return pd.DataFrame(data, columns=rs.fields)
    finally:
        bs.logout()


# ═══════════════════════════════════════════════════
#  直接爬官方数据 (最稳定)
# ═══════════════════════════════════════════════════

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json, text/html",
}

@retry()
def official_lpr():
    """中国人民银行: LPR 利率 (直接爬取)"""
    url = "https://www.pbc.gov.cn/zhengcehuobisi/125207/125213/125440/index.html"
    resp = requests.get(url, headers=HEADERS, timeout=10)
    # LPR 数据通常在页面表格中，这里返回原始 HTML 供解析
    # 实际使用中可配合 BeautifulSoup 解析
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(resp.text, "html.parser")
    tables = soup.find_all("table")
    rows = []
    for table in tables:
        for tr in table.find_all("tr"):
            cells = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
            if cells:
                rows.append(cells)
    if rows:
        return pd.DataFrame(rows[1:], columns=rows[0])
    return None


@retry()
def official_stats_gdp():
    """国家统计局: GDP 数据 (通过 API)"""
    # 国家统计局数据查询接口
    url = "https://data.stats.gov.cn/easyquery.htm"
    params = {
        "m": "QueryData",
        "dbcode": "hgnd",
        "rowcode": "zb",
        "colcode": "sj",
        "wds": json.dumps([{"wdcode": "zb", "valuecode": "A0201"}]),
        "dfwds": json.dumps([{"wdcode": "sj", "valuecode": "LAST5"}]),
    }
    resp = requests.get(url, params=params, headers=HEADERS, timeout=15)
    data = resp.json()
    if "returndata" in data:
        datanodes = data["returndata"].get("datanodes", [])
        rows = []
        for node in datanodes:
            rows.append({
                "period": node.get("wds", [{}])[-1].get("valuecode", ""),
                "value": node.get("data", {}).get("data", ""),
                "name": node.get("cname", ""),
            })
        return pd.DataFrame(rows)
    return None


@retry()
def official_stats_cpi():
    """国家统计局: CPI"""
    url = "https://data.stats.gov.cn/easyquery.htm"
    params = {
        "m": "QueryData",
        "dbcode": "hgyd",
        "rowcode": "zb",
        "colcode": "sj",
        "wds": json.dumps([{"wdcode": "zb", "valuecode": "A0901"}]),
        "dfwds": json.dumps([{"wdcode": "sj", "valuecode": "LAST12"}]),
    }
    resp = requests.get(url, params=params, headers=HEADERS, timeout=15)
    data = resp.json()
    if "returndata" in data:
        datanodes = data["returndata"].get("datanodes", [])
        rows = []
        for node in datanodes:
            rows.append({
                "period": node.get("wds", [{}])[-1].get("valuecode", ""),
                "value": node.get("data", {}).get("data", ""),
                "name": node.get("cname", ""),
            })
        return pd.DataFrame(rows)
    return None


@retry()
def official_stats_pmi():
    """国家统计局: PMI"""
    url = "https://data.stats.gov.cn/easyquery.htm"
    params = {
        "m": "QueryData",
        "dbcode": "hgyd",
        "rowcode": "zb",
        "colcode": "sj",
        "wds": json.dumps([{"wdcode": "zb", "valuecode": "A0101"}]),
        "dfwds": json.dumps([{"wdcode": "sj", "valuecode": "LAST12"}]),
    }
    resp = requests.get(url, params=params, headers=HEADERS, timeout=15)
    data = resp.json()
    if "returndata" in data:
        datanodes = data["returndata"].get("datanodes", [])
        rows = []
        for node in datanodes:
            rows.append({
                "period": node.get("wds", [{}])[-1].get("valuecode", ""),
                "value": node.get("data", {}).get("data", ""),
                "name": node.get("cname", ""),
            })
        return pd.DataFrame(rows)
    return None


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

    def gdp(self):
        """GDP 季度数据"""
        print("\n📊 GDP")
        return fallback(
            ("tushare", ts_gdp),
            ("akshare", ak_gdp),
            ("official-stats", official_stats_gdp),
        )()

    def cpi(self):
        """CPI 月度"""
        print("\n📊 CPI")
        return fallback(
            ("tushare", ts_cpi),
            ("akshare", ak_cpi),
            ("official-stats", official_stats_cpi),
        )()

    def ppi(self):
        """PPI 月度"""
        print("\n📊 PPI")
        return fallback(
            ("tushare", ts_ppi),
            ("akshare", ak_ppi),
        )()

    def pmi(self):
        """PMI"""
        print("\n📊 PMI")
        return fallback(
            ("tushare", ts_pmi),
            ("akshare", ak_pmi),
            ("official-stats", official_stats_pmi),
        )()

    def m2(self):
        """M2 货币供应"""
        print("\n📊 M2")
        return fallback(
            ("tushare", ts_m2),
            ("akshare", ak_m2),
            ("tushare-money", ts_money_supply),
        )()

    def lpr(self):
        """LPR 利率"""
        print("\n📊 LPR")
        return fallback(
            ("tushare", ts_lpr),
            ("akshare", ak_lpr),
            ("official-pbc", official_lpr),
        )()

    def social_financing(self):
        """社会融资规模"""
        print("\n📊 社融")
        return fallback(
            ("akshare", ak_social_financing),
        )()

    def trade(self):
        """进出口贸易"""
        print("\n📊 进出口")
        return fallback(
            ("akshare", ak_trade),
        )()

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

    def stock_daily(self, code="600519.SH"):
        """个股日线"""
        print(f"\n📊 个股日线: {code}")
        bs_code = ("sh." if code.endswith(".SH") else "sz.") + code[:6]
        ak_code = code[:6].lower()
        return fallback(
            ("tushare", lambda: ts_stock_daily(code)),
            ("akshare", lambda: ak_stock_daily("sh" + ak_code if code.endswith(".SH") else "sz" + ak_code)),
            ("baostock", lambda: bs_stock_daily(bs_code)),
        )()

    def stock_list(self):
        """全A股列表"""
        print("\n📊 A股列表")
        return fallback(
            ("tushare", ts_stock_basic),
            ("akshare", ak_stock_basic),
            ("baostock", bs_stock_basic),
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
        ("GDP", data.gdp),
        ("CPI", data.cpi),
        ("PMI", data.pmi),
        ("沪深300", lambda: data.index_daily("000300.SH")),
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
