#!/usr/bin/env python3
"""
A股恐贪指数 + 行情统一获取层

恐贪指数: 韭圈儿(主) → 自建六因子合成(辅) → CNN(全球参考)
实时行情: 腾讯(主) → 新浪(辅)
涨跌统计: 东方财富(直接拿汇总，不拉个股)
独家数据: AKShare(iVIX/北向/期货/融资)

依赖: pip install akshare baostock requests pandas
"""

import os
import json
import time
import re
import numpy as np
import pandas as pd
import requests
from datetime import datetime
from functools import wraps


# ═══════════════════════════════════════════════════
#  通用工具
# ═══════════════════════════════════════════════════

def retry(max_retries=2, delay=1):
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


def fallback(*sources):
    def wrapper(*args, **kwargs):
        errors = []
        for name, func in sources:
            try:
                result = func(*args, **kwargs)
                if result is None:
                    errors.append(f"{name}: None")
                    continue
                if isinstance(result, pd.DataFrame) and result.empty:
                    errors.append(f"{name}: 空")
                    continue
                if isinstance(result, dict) and not result:
                    errors.append(f"{name}: 空dict")
                    continue
                print(f"    ✅ 数据源: {name}")
                return result
            except Exception as e:
                errors.append(f"{name}: {e}")
                print(f"    ⚠️ {name} 失败: {e}")
        print(f"    ❌ 全部失败: {'; '.join(errors)}")
        return None
    return wrapper


def _market_prefix(code: str) -> str:
    digits = ''.join(filter(str.isdigit, code))
    return 'sh' if digits.startswith(('6', '9')) else 'sz'


def _full_code(code: str) -> str:
    if code.startswith('sh') or code.startswith('sz'):
        return code
    return _market_prefix(code) + code


def _find_col(df, *keywords):
    for kw in keywords:
        for col in df.columns:
            if kw.lower() in str(col).lower():
                return col
    return None


# ═══════════════════════════════════════════════════
#  Part 1: 韭圈儿恐贪指数
#
#  韭圈儿恐贪指数 = 六因子加权(0-100)
#  <20 极度恐惧(买进), >80 极度贪婪(卖出)
#
#  直接从韭圈儿网站抓取，不需要自己合成
# ═══════════════════════════════════════════════════

JQ_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Referer": "https://danjuanfunds.com/",
}

# 韭圈儿可能的接口路径（域名/路径经常变，多试几个）
JQ_URLS_CURRENT = [
    "https://danjuanfunds.com/djapi/index_eva/fg_score",
    "https://danjuanapp.com/djapi/index_eva/fg_score",
    "https://danjuanfunds.com/djapi/index_eva/fg_realtime",
    "https://danjuanapp.com/djapi/index_eva/fg_realtime",
]

JQ_URLS_HISTORY = [
    "https://danjuanfunds.com/djapi/index_eva/fg/detail",
    "https://danjuanapp.com/djapi/index_eva/fg/detail",
    "https://danjuanfunds.com/djapi/index_eva/fg_score/detail",
    "https://danjuanapp.com/djapi/index_eva/fg_score/detail",
]


@retry()
def juquaner_current():
    """韭圈儿: 当日恐贪指数

    返回: {score: int(0-100), rating: str, source: "juquaner"}
    """
    for url in JQ_URLS_CURRENT:
        try:
            resp = requests.get(url, headers=JQ_HEADERS, timeout=10)
            if resp.status_code != 200:
                continue
            data = resp.json()
            payload = data.get("data", data)

            score = None
            rating = ""

            if isinstance(payload, dict):
                score = (payload.get("score") or payload.get("value")
                         or payload.get("fg_score") or payload.get("fear_greed"))
                rating = payload.get("rating", payload.get("level", ""))
            elif isinstance(payload, list) and payload:
                latest = payload[-1]
                score = latest.get("score") or latest.get("y") or latest.get("value")
                rating = latest.get("rating", latest.get("level", ""))

            if score is not None:
                score = int(float(score))
                if not rating:
                    rating = _score_to_rating(score)
                return {"score": score, "rating": rating, "source": "juquaner"}
        except Exception:
            continue

    raise ValueError("韭圈儿所有接口路径均不可用")


@retry()
def juquaner_history(days=30):
    """韭圈儿: 恐贪指数历史

    返回: DataFrame(date, score, rating)
    """
    for url in JQ_URLS_HISTORY:
        try:
            resp = requests.get(url, headers=JQ_HEADERS, timeout=10)
            if resp.status_code != 200:
                continue
            data = resp.json()
            records = data.get("data", data)

            if isinstance(records, dict):
                # 有些接口 data 是 dict，里面 list 在某个 key 下
                for key in ["list", "items", "data", "history"]:
                    if key in records and isinstance(records[key], list):
                        records = records[key]
                        break
                else:
                    continue

            if isinstance(records, list) and records:
                rows = []
                for r in records:
                    rows.append({
                        "date": r.get("date", r.get("x", r.get("trade_date", ""))),
                        "score": r.get("score", r.get("y", r.get("value", r.get("fg_score")))),
                        "rating": r.get("rating", r.get("level", "")),
                    })
                df = pd.DataFrame(rows)
                if not df.empty:
                    return df.tail(days)
        except Exception:
            continue

    raise ValueError("韭圈儿历史数据不可用")


def _score_to_rating(score):
    if score <= 20:
        return "极度恐惧"
    elif score <= 40:
        return "恐惧"
    elif score <= 60:
        return "中性"
    elif score <= 80:
        return "贪婪"
    return "极度贪婪"


def _print_signal(score):
    if score <= 20:
        print("    🔴 极度恐惧 → 买进信号（分批建仓）")
    elif score <= 40:
        print("    🟠 恐惧 → 轻仓试探/逐步建仓")
    elif score <= 60:
        print("    🟡 中性 → 持股观望")
    elif score <= 80:
        print("    🟢 贪婪 → 减持高位股/兑现利润")
    else:
        print("    🔴 极度贪婪 → 全线减仓止盈")


# ═══════════════════════════════════════════════════
#  Part 2: 自建合成恐贪指数（韭圈儿不可用时的降级方案）
#
#  因子及数据源:
#    1. 市场宽度(涨跌比)    ← 东方财富(直接汇总)
#    2. 涨停跌停比          ← 东方财富(直接汇总)
#    3. iVIX 50ETF波动率    ← AKShare index_option_100etf_qvix
#    4. 北向资金偏离        ← AKShare stock_hsgt_fund_flow_summary_em
#    5. 期货升贴水          ← AKShare futures_main_sina + 腾讯指数
#    6. 融资买入占比        ← AKShare macro_china_market_margin_sh/sz
#
#  至少 3 个因子有效才输出
# ═══════════════════════════════════════════════════

MIN_FACTORS = 3

EASTMONEY_URL = "http://push2.eastmoney.com/api/qt/clist/get"


def eastmoney_breadth() -> dict:
    """东方财富: 两市涨跌家数 + 涨停跌停（一次请求）

    这个接口返回全量A股涨跌幅字段(f3)，
    客户端统计即可，不需要拉个股明细。
    """
    all_changes = []
    for market in ["m:0+t:6,m:0+t:80",   # 深A
                    "m:1+t:2,m:1+t:23"]:   # 沪A
        params = {
            "pn": 1, "pz": 6000,
            "po": 1, "np": 1,
            "fields": "f3",
            "fid": "f3",
            "fs": market,
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        }
        try:
            resp = requests.get(EASTMONEY_URL, params=params, timeout=10)
            data = resp.json()
            items = data.get("data", {}).get("diff", [])
            for item in items:
                chg = item.get("f3")
                if chg is not None and chg != "-":
                    try:
                        all_changes.append(float(chg))
                    except (ValueError, TypeError):
                        pass
        except Exception:
            continue

    if len(all_changes) < 100:
        return None

    s = pd.Series(all_changes)
    up = int((s > 0).sum())
    down = int((s < 0).sum())
    flat = int((s == 0).sum())
    total = len(s)
    limit_up = int((s >= 9.8).sum())
    limit_down = int((s <= -9.8).sum())
    up_ratio = up / total if total > 0 else 0.5

    return {
        "up": up, "down": down, "flat": flat, "total": total,
        "limit_up": limit_up, "limit_down": limit_down,
        "up_ratio": round(up_ratio * 100, 1),
        "score": round(max(0, min(100, (up_ratio - 0.3) / 0.4 * 100)), 1),
    }


# ── 各因子函数 ──

def _factor_breadth():
    """因子1: 市场宽度 — 涨跌家数比"""
    r = eastmoney_breadth()
    return r["score"] if r else None


def _factor_limit():
    """因子2: 涨停跌停比"""
    r = eastmoney_breadth()
    if not r:
        return None
    lu = r.get("limit_up", 0) or 0
    ld = r.get("limit_down", 0) or 0
    if lu + ld == 0:
        return 50
    return round(lu / (lu + ld) * 100, 1)


@retry()
def _factor_ivix():
    """因子3: iVIX 50ETF期权波动率"""
    import akshare as ak
    df = ak.index_option_100etf_qvix()
    if df is None or df.empty:
        return None
    col = _find_col(df, "qvix", "close", "收盘", "value", "最新")
    if col is None:
        nums = df.select_dtypes(include=[np.number]).columns
        col = nums[0] if len(nums) > 0 else None
    if col is None:
        return None
    series = pd.to_numeric(df[col], errors='coerce').dropna()
    if len(series) < 5:
        return None
    v = float(series.iloc[-1])
    # iVIX: 10=平静=贪婪, 30=恐慌
    return round(max(0, min(100, (30 - v) / 20 * 100)), 1)


@retry()
def _factor_northbound():
    """因子4: 北向资金偏离 20 日均线"""
    import akshare as ak
    df = ak.stock_hsgt_fund_flow_summary_em()
    if df is None or df.empty:
        return None
    col = _find_col(df, "净买", "净流入", "累计", "net", "flow")
    if col is None:
        nums = df.select_dtypes(include=[np.number]).columns.tolist()
        col = nums[-1] if nums else None
    if col is None:
        return None
    series = pd.to_numeric(df[col], errors='coerce').dropna()
    if len(series) < 20:
        return None
    ma20 = series.rolling(20).mean()
    std20 = series.rolling(20).std()
    dev = (series.iloc[-1] - ma20.iloc[-1]) / std20.iloc[-1]
    if np.isnan(dev):
        return None
    return round(max(0, min(100, 50 + float(dev) * 25)), 1)


@retry()
def _factor_futures():
    """因子5: 沪深300期货升贴水"""
    import akshare as ak
    df = ak.futures_main_sina(symbol="IF0")
    if df is None or df.empty:
        return None
    col = _find_col(df, "收盘", "close", "最新", "price")
    if col is None:
        return None
    fc = float(pd.to_numeric(df[col], errors='coerce').dropna().iloc[-1])

    # 用腾讯拿沪深300实时价
    idx = tencent_index(["000300"])
    if idx is None or idx.empty:
        return None
    ic = float(idx.iloc[0]["price"])
    if ic <= 0:
        return None

    basis = (fc - ic) / ic * 100
    return round(max(0, min(100, 50 + basis * 25)), 1)


@retry()
def _factor_margin():
    """因子6: 融资买入占比"""
    import akshare as ak
    for fn in ["macro_china_market_margin_sh", "macro_china_market_margin_sz"]:
        func = getattr(ak, fn, None)
        if func is None:
            continue
        try:
            df = func()
            if df is None or df.empty:
                continue
            col = _find_col(df, "融资买入", "融资买入额", "rz_buy")
            if col:
                series = pd.to_numeric(df[col], errors='coerce').dropna()
                if len(series) >= 5:
                    latest = float(series.iloc[-1])
                    return round((series <= latest).mean() * 100, 1)
        except Exception:
            continue

    # 降级
    try:
        df = ak.stock_margin_account_info()
        if df is not None and not df.empty:
            col = _find_col(df, "融资", "参与")
            if col:
                series = pd.to_numeric(df[col], errors='coerce').dropna()
                if len(series) >= 5:
                    return round((series <= float(series.iloc[-1])).mean() * 100, 1)
    except Exception:
        pass
    return None


def compute_composite():
    """自建合成恐贪指数

    当韭圈儿不可用时启用，至少 MIN_FACTORS 个因子有效才输出
    """
    factors = {
        "市场宽度(涨跌比)": _factor_breadth,
        "涨停跌停比": _factor_limit,
        "iVIX波动率": _factor_ivix,
        "北向资金偏离": _factor_northbound,
        "期货升贴水": _factor_futures,
        "融资买入占比": _factor_margin,
    }

    scores = {}
    missing = []

    for name, func in factors.items():
        try:
            val = func()
            if val is not None and not np.isnan(val):
                scores[name] = val
                print(f"    ✓ {name}: {val}")
            else:
                missing.append(name)
                print(f"    ✗ {name}: None")
        except Exception as e:
            missing.append(name)
            print(f"    ✗ {name}: {e}")

    if len(scores) < MIN_FACTORS:
        print(f"    ⚠️ 有效因子 {len(scores)} 个, 不足 {MIN_FACTORS}, 不输出")
        return None

    composite = round(sum(scores.values()) / len(scores))
    return {
        "score": composite,
        "rating": _score_to_rating(composite),
        "components": scores,
        "missing": missing,
        "factor_count": len(scores),
        "source": "composite",
    }


# ═══════════════════════════════════════════════════
#  Part 3: CNN Fear & Greed（全球参考）
# ═══════════════════════════════════════════════════

@retry()
def cnn_history(days=365):
    start = (datetime.now() - pd.Timedelta(days=days)).strftime('%Y-%m-%d')
    url = f"https://production.dataviz.cnn.io/index/fearandgreed/graphdata/{start}"
    resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
    if resp.status_code != 200:
        return None
    data = resp.json()
    if 'fear_and_greed_historical' not in data:
        return None
    df = pd.DataFrame(data['fear_and_greed_historical']['data'])
    df['date'] = pd.to_datetime(df['x'], unit='ms').dt.strftime('%Y-%m-%d')
    df = df.rename(columns={'y': 'fear_greed_score'})
    return df[['date', 'rating', 'fear_greed_score']].drop_duplicates('date').sort_values('date')


# ═══════════════════════════════════════════════════
#  Part 4: 腾讯/新浪 实时行情
# ═══════════════════════════════════════════════════

@retry()
def tencent_realtime(codes: list) -> pd.DataFrame:
    """腾讯: 批量个股行情"""
    full = [_full_code(c) for c in codes]
    url = f"http://qt.gtimg.cn/q={','.join(full)}"
    resp = requests.get(url, timeout=5)
    resp.encoding = 'gbk'
    rows = []
    for line in resp.text.strip().split('\n'):
        line = line.strip().rstrip(';')
        if '="' not in line:
            continue
        d = line.split('="')[1].rstrip('"')
        if not d:
            continue
        p = d.split('~')
        if len(p) < 46:
            continue
        def sf(i):
            try:
                return float(p[i]) if p[i] else None
            except (ValueError, IndexError):
                return None
        rows.append({
            'code': p[2], 'name': p[1], 'price': sf(3),
            'pre_close': sf(4), 'open': sf(5),
            'high': sf(33), 'low': sf(34),
            'volume': sf(6), 'amount': sf(37),
            'change_pct': sf(32), 'turnover': sf(38),
            'pe': sf(39), 'pb': sf(46),
            'market_cap': sf(45), 'circ_market_cap': sf(44),
        })
    return pd.DataFrame(rows)


@retry()
def tencent_index(codes: list) -> pd.DataFrame:
    """腾讯: 指数行情"""
    full = []
    for c in codes:
        c = c.lower()
        if not c.startswith(('sh', 'sz')):
            c = _market_prefix(c) + c
        full.append(c)
    url = f"http://qt.gtimg.cn/q={','.join(full)}"
    resp = requests.get(url, timeout=5)
    resp.encoding = 'gbk'
    rows = []
    for line in resp.text.strip().split('\n'):
        line = line.strip().rstrip(';')
        if '="' not in line:
            continue
        d = line.split('="')[1].rstrip('"')
        if not d:
            continue
        p = d.split('~')
        if len(p) < 35:
            continue
        def sf(i):
            try:
                return float(p[i]) if p[i] else None
            except (ValueError, IndexError):
                return None
        rows.append({
            'code': p[2], 'name': p[1], 'price': sf(3),
            'pre_close': sf(4), 'open': sf(5),
            'high': sf(33), 'low': sf(34),
            'change_pct': sf(32), 'volume': sf(6), 'amount': sf(37),
        })
    return pd.DataFrame(rows)


@retry()
def sina_realtime(codes: list) -> pd.DataFrame:
    """新浪: 批量个股行情"""
    full = [_full_code(c) for c in codes]
    url = f"http://hq.sinajs.cn/list={','.join(full)}"
    headers = {"Referer": "https://finance.sina.com.cn/", "User-Agent": "Mozilla/5.0"}
    resp = requests.get(url, headers=headers, timeout=5)
    resp.encoding = 'gbk'
    rows = []
    for line in resp.text.strip().split('\n'):
        line = line.strip().rstrip(';')
        m = re.search(r'hq_str_(\w+)="(.+)"', line)
        if not m:
            continue
        p = m.group(2).split(',')
        if len(p) < 32:
            continue
        def sf(i):
            try:
                return float(p[i]) if p[i] else None
            except (ValueError, IndexError):
                return None
        pc, pr = sf(2), sf(3)
        chg = round((pr - pc) / pc * 100, 2) if pr and pc and pc > 0 else None
        rows.append({
            'code': m.group(1)[2:], 'name': p[0],
            'price': pr, 'open': sf(1), 'pre_close': pc,
            'high': sf(4), 'low': sf(5),
            'volume': sf(8), 'amount': sf(9), 'change_pct': chg,
        })
    return pd.DataFrame(rows)


# ═══════════════════════════════════════════════════
#  Part 5: 北向资金（AKShare，独家数据）
# ═══════════════════════════════════════════════════

@retry()
def ak_northbound():
    import akshare as ak
    df = ak.stock_hsgt_fund_flow_summary_em()
    if df is not None and not df.empty:
        return df
    df = ak.stock_hsgt_hist_em(symbol="沪股通")
    return df


def hexin_northbound() -> dict:
    """同花顺: 北向实时分钟流向"""
    url = "https://data.hexin.cn/market/hsgtApi/method/dayChart/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/117.0.0.0",
        "Host": "data.hexin.cn", "Referer": "https://data.hexin.cn/",
    }
    r = requests.get(url, headers=headers, timeout=10)
    d = r.json()
    times, hgt, sgt = d.get("time", []), d.get("hgt", []), d.get("sgt", [])
    points = [{"time": times[i],
               "hgt_yi": hgt[i] if i < len(hgt) else None,
               "sgt_yi": sgt[i] if i < len(sgt) else None}
              for i in range(len(times))]
    hl = next((p["hgt_yi"] for p in reversed(points) if p["hgt_yi"] is not None), 0)
    sl = next((p["sgt_yi"] for p in reversed(points) if p["sgt_yi"] is not None), 0)
    return {
        "points": len(points),
        "hgt_latest_yi": hl, "sgt_latest_yi": sl,
        "total_latest_yi": round((hl or 0) + (sl or 0), 2),
        "data": points[-10:],
    }


# ═══════════════════════════════════════════════════
#  Part 6: 财经新闻（AKShare）
# ═══════════════════════════════════════════════════

@retry()
def ak_news():
    import akshare as ak
    return ak.stock_news_em(symbol="财经")


# ═══════════════════════════════════════════════════
#  统一接口 — ChinaData
# ═══════════════════════════════════════════════════

class ChinaData:
    """A股恐贪指数 + 行情统一入口

    恐贪: 韭圈儿 → 自建合成 → CNN
    行情: 腾讯 → 新浪
    涨跌: 东方财富(汇总)
    北向: AKShare / 同花顺实时
    """

    def __init__(self):
        sources = ["腾讯行情", "新浪行情", "东方财富"]
        try:
            import akshare
            sources.append("akshare")
        except ImportError:
            pass
        print(f"  📡 可用数据源: {', '.join(sources)}")

    # ── 恐贪指数 ──

    def fear_greed(self, method="auto"):
        """恐贪指数

        method:
          "juquaner"  → 仅韭圈儿
          "composite" → 仅自建合成
          "auto"      → 韭圈儿 → 自建合成 (默认)
        """
        print("\n📊 恐贪指数")

        # 1. 韭圈儿
        if method in ("juquaner", "auto"):
            try:
                result = juquaner_current()
                print(f"    ✅ 数据源: 韭圈儿")
                print(f"    📈 恐贪指数: {result['score']} ({result['rating']})")
                _print_signal(result['score'])
                return result
            except Exception as e:
                if method == "juquaner":
                    print(f"    ❌ 韭圈儿失败: {e}")
                    return None
                print(f"    ⚠️ 韭圈儿失败: {e}")

        # 2. 自建合成
        if method in ("composite", "auto"):
            print("    降级到自建合成...")
            result = compute_composite()
            if result:
                print(f"\n    ✅ 数据源: 自建合成 ({result['factor_count']} 因子)")
                print(f"    📈 恐贪指数: {result['score']} ({result['rating']})")
                _print_signal(result['score'])
                return result

        print("    ❌ 恐贪指数获取失败")
        return None

    def fear_greed_history(self, days=30):
        """恐贪指数历史走势"""
        print(f"\n📊 恐贪指数历史 ({days}天)")

        # 1. 韭圈儿
        try:
            df = juquaner_history(days)
            if df is not None and not df.empty:
                print(f"    ✅ 韭圈儿 ({len(df)} 条)")
                return df
        except Exception as e:
            print(f"    ⚠️ 韭圈儿历史失败: {e}")

        # 2. CNN (全球参考)
        try:
            df = cnn_history(days)
            if df is not None and not df.empty:
                print(f"    ✅ CNN ({len(df)} 条, 美股参考)")
                return df
        except Exception as e:
            print(f"    ⚠️ CNN 失败: {e}")

        print("    ❌ 无历史数据")
        return None

    # ── 涨跌统计（东方财富，直接汇总）──

    def market_breadth(self):
        """两市涨跌家数 + 涨停跌停"""
        print("\n📊 涨跌统计")
        r = eastmoney_breadth()
        if r:
            print(f"    ✅ 东方财富 ({r['total']} 只)")
            print(f"    上涨: {r['up']}  下跌: {r['down']}  平盘: {r['flat']}")
            print(f"    涨停: {r['limit_up']}  跌停: {r['limit_down']}")
            print(f"    上涨比例: {r['up_ratio']}%")
            return r
        print("    ❌ 获取失败")
        return None

    # ── 实时行情（腾讯/新浪）──

    def realtime(self, codes: list):
        """个股实时行情"""
        print(f"\n📊 实时行情: {codes}")
        return fallback(
            ("腾讯", lambda: tencent_realtime(codes)),
            ("新浪", lambda: sina_realtime(codes)),
        )()

    def index_realtime(self, codes: list):
        """指数实时行情"""
        print(f"\n📊 指数实时: {codes}")
        return fallback(
            ("腾讯", lambda: tencent_index(codes)),
        )()

    # ── 北向资金 ──

    def northbound(self):
        """北向资金"""
        print("\n📊 北向资金")
        return fallback(
            ("akshare", ak_northbound),
        )()

    def northbound_realtime(self):
        """北向资金实时分钟流向"""
        print("\n📊 北向实时")
        try:
            r = hexin_northbound()
            print(f"    ✅ 同花顺实时")
            print(f"    沪股通: {r['hgt_latest_yi']}亿  深股通: {r['sgt_latest_yi']}亿")
            print(f"    合计: {r['total_latest_yi']}亿")
            return r
        except Exception as e:
            print(f"    ❌ 失败: {e}")
            return None

    # ── 新闻 ──

    def news(self):
        print("\n📰 财经新闻")
        return fallback(("akshare", ak_news))()


# ═══════════════════════════════════════════════════
#  测试
# ═══════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  🇨🇳 A股恐贪指数 + 行情 测试")
    print("=" * 60)

    data = ChinaData()

    tests = [
        ("恐贪指数(韭圈儿→合成)", lambda: data.fear_greed()),
        ("恐贪指数历史",           lambda: data.fear_greed_history(30)),
        ("涨跌统计(东方财富)",     lambda: data.market_breadth()),
        ("个股行情(腾讯)",         lambda: data.realtime(["600519", "000001"])),
        ("指数行情(腾讯)",         lambda: data.index_realtime(["000001", "000300", "399001"])),
        ("北向实时(同花顺)",       lambda: data.northbound_realtime()),
    ]

    for name, func in tests:
        try:
            result = func()
            if result is not None:
                if isinstance(result, pd.DataFrame):
                    print(f"\n  ✅ {name}: {len(result)} 行")
                    print(result.tail(3).to_string(index=False))
                elif isinstance(result, dict):
                    print(f"\n  ✅ {name}: {json.dumps({k:v for k,v in result.items() if k != 'components'}, ensure_ascii=False)}")
            else:
                print(f"\n  ⚠️ {name}: 无数据")
        except Exception as e:
            print(f"\n  ❌ {name}: {e}")
