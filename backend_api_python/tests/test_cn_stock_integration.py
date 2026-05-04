# -*- coding: utf-8 -*-
"""
CNStock 集成测试 — 真实网络请求（直接 HTTP）

验证腾讯/新浪等免费 API 实际能扯回数据。
不依赖 Provider 层导入（绕过 to_tencent_code 缺失问题），
直接调 HTTP 接口验证数据可达性。

注意:
  - 需要网络连接，离线环境会 skip
  - A股收盘后行情为上一交易日收盘价
  - 只读取，不写入任何数据

运行:
  python3 -m pytest tests/test_cn_stock_integration.py -v --noconftest -s
"""
import sys
import os

import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ── 网络可用性检查 ──

def _tencent_alive() -> bool:
    try:
        r = requests.get("https://qt.gtimg.cn/q=sh600519", timeout=5)
        return r.status_code == 200 and "~" in r.text
    except Exception:
        return False

def _sina_alive() -> bool:
    try:
        r = requests.get(
            "https://hq.sinajs.cn/list=sh600519",
            headers={"Referer": "https://finance.sina.com.cn"},
            timeout=5,
        )
        return r.status_code == 200 and "hq_str_" in r.text
    except Exception:
        return False

skip_no_tencent = pytest.mark.skipif(not _tencent_alive(), reason="腾讯行情接口不可达")
skip_no_sina = pytest.mark.skipif(not _sina_alive(), reason="新浪行情接口不可达")


def _is_trading_hours() -> bool:
    """粗略判断是否在交易时段（周一~五 9:15~15:30 CST）"""
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone(timedelta(hours=8)))
    if now.weekday() >= 5:
        return False
    t = now.hour * 100 + now.minute
    return 915 <= t <= 1530

skip_off_hours = pytest.mark.skipif(not _is_trading_hours(), reason="非交易时段，分钟K线无数据")


# ======================================================================
# 腾讯行情 API
# ======================================================================

@skip_no_tencent
class TestTencentQuote:
    """腾讯 qt.gtimg.cn 实时行情"""

    def test_single_quote_moutai(self):
        """获取贵州茅台实时行情"""
        resp = requests.get("https://qt.gtimg.cn/q=sh600519", timeout=8)
        resp.encoding = "gbk"
        text = resp.text.strip()
        assert "~" in text

        parts = text[text.index('="') + 2 : text.rindex('"')].split("~")
        name, last, prev = parts[1], float(parts[3]), float(parts[4])
        assert last > 0
        assert name  # 应有名称
        print(f"\n  茅台: {name} 最新={last} 昨收={prev}")

    def test_single_quote_pingan(self):
        """获取平安银行实时行情"""
        resp = requests.get("https://qt.gtimg.cn/q=sz000001", timeout=8)
        resp.encoding = "gbk"
        text = resp.text.strip()
        parts = text[text.index('="') + 2 : text.rindex('"')].split("~")
        last = float(parts[3])
        assert last > 0
        print(f"\n  平安银行: {parts[1]} 最新={last}")

    def test_batch_quote(self):
        """批量获取多只股票行情 — 单次 HTTP"""
        codes = "sh600519,sz000001,sh601318"
        resp = requests.get(f"https://qt.gtimg.cn/q={codes}", timeout=8)
        resp.encoding = "gbk"
        lines = [l.strip().rstrip(";") for l in resp.text.strip().split("\n") if "~" in l]
        assert len(lines) >= 2

        for line in lines:
            parts = line[line.index('="') + 2 : line.rindex('"')].split("~")
            name, last = parts[1], float(parts[3])
            assert last > 0
            print(f"\n  {name}: {last}")

    def test_quote_fields_completeness(self):
        """行情应包含完整的字段"""
        resp = requests.get("https://qt.gtimg.cn/q=sh600519", timeout=8)
        resp.encoding = "gbk"
        text = resp.text.strip()
        parts = text[text.index('="') + 2 : text.rindex('"')].split("~")

        # 至少 35 个字段
        assert len(parts) >= 35
        # 关键字段非空
        assert parts[1]  # 名称
        assert parts[3]  # 最新价
        assert parts[4]  # 昨收
        assert parts[5]  # 开盘


# ======================================================================
# 腾讯分钟K线 API
# ======================================================================

@skip_no_tencent
@skip_off_hours
class TestTencentMinuteKline:
    """腾讯 mkline 分钟K线"""

    def test_5min_kline(self):
        """获取 5 分钟K线"""
        resp = requests.get(
            "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/kline/mkline",
            params={"param": "sh600519,m5,10"},
            timeout=8,
        )
        data = resp.json()
        root = (data.get("data") or {}).get("sh600519", {})
        rows = root.get("m5", [])
        assert len(rows) > 0

        last = rows[-1]
        assert len(last) >= 6  # [time, open, close, high, low, volume, ...]
        close = float(last[2])
        assert close > 0
        print(f"\n  茅台5分钟K: {len(rows)} 条, 最新收盘={close}")

    def test_15min_kline(self):
        """获取 15 分钟K线"""
        resp = requests.get(
            "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/kline/mkline",
            params={"param": "sz000001,m15,20"},
            timeout=8,
        )
        data = resp.json()
        root = (data.get("data") or {}).get("sz000001", {})
        rows = root.get("m15", [])
        assert len(rows) > 0
        print(f"\n  平安银行15分钟K: {len(rows)} 条")

    def test_kline_ohlc_valid(self):
        """K线 OHLC 数据合理性"""
        resp = requests.get(
            "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/kline/mkline",
            params={"param": "sh600519,m5,20"},
            timeout=8,
        )
        data = resp.json()
        root = (data.get("data") or {}).get("sh600519", {})
        rows = root.get("m5", [])

        for row in rows:
            o, c, h, l = float(row[1]), float(row[2]), float(row[3]), float(row[4])
            assert h >= l, f"high({h}) < low({l})"
            assert c > 0
            assert o > 0


# ======================================================================
# 新浪行情 API
# ======================================================================

@skip_no_sina
class TestSinaQuote:
    """新浪 hq.sinajs.cn 实时行情"""

    def test_single_quote(self):
        """获取贵州茅台行情"""
        resp = requests.get(
            "https://hq.sinajs.cn/list=sh600519",
            headers={"Referer": "https://finance.sina.com.cn"},
            timeout=8,
        )
        resp.encoding = "gbk"
        text = resp.text.strip()
        if "Forbidden" in text or "Access denied" in text:
            pytest.skip("新浪限流 (Forbidden)")
        assert "hq_str_sh600519" in text

        # 格式: var hq_str_sh600519="名称,今开,昨收,当前,最高,最低,..."
        data = text.split('="')[1].rstrip('";').split(",")
        name, open_p, prev_close, current = data[0], float(data[1]), float(data[2]), float(data[3])
        assert current > 0
        print(f"\n  新浪茅台: {name} 当前={current} 今开={open_p} 昨收={prev_close}")

    def test_batch_quote(self):
        """批量获取多只股票"""
        resp = requests.get(
            "https://hq.sinajs.cn/list=sh600519,sz000001",
            headers={"Referer": "https://finance.sina.com.cn"},
            timeout=8,
        )
        resp.encoding = "gbk"
        text = resp.text.strip()
        if "Forbidden" in text or "Access denied" in text:
            pytest.skip("新浪限流 (Forbidden)")
        lines = [l.strip() for l in text.split("\n") if "hq_str_" in l.strip()]
        assert len(lines) >= 2
        print(f"\n  新浪批量: 返回 {len(lines)} 只")


# ======================================================================
# 数据质量验证
# ======================================================================

@skip_no_tencent
class TestDataQuality:
    """验证返回数据的基本质量"""

    def test_quote_price_positive(self):
        """所有行情价格应为正数"""
        resp = requests.get(
            "https://qt.gtimg.cn/q=sh600519,sz000001,sh601318",
            timeout=8,
        )
        resp.encoding = "gbk"
        for line in resp.text.strip().split("\n"):
            if "~" not in line:
                continue
            parts = line.strip().rstrip(";")
            parts = parts[parts.index('="') + 2 : parts.rindex('"')].split("~")
            last = float(parts[3])
            assert last > 0, f"{parts[1]} price={last} <= 0"

    @skip_off_hours
    def test_batch_kline_sorted(self):
        """分钟K线应按时间升序（仅交易时段）"""
        resp = requests.get(
            "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/kline/mkline",
            params={"param": "sh600519,m5,30"},
            timeout=8,
        )
        data = resp.json()
        root = (data.get("data") or {}).get("sh600519", {})
        rows = root.get("m5", [])
        times = [r[0] for r in rows]
        assert times == sorted(times), "K线未按时间升序"
