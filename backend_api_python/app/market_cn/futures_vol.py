"""
国内期货波动率 — 新浪期货实时行情

数据源: 新浪财经 hq.sinajs.cn (1次HTTP拿全部合约)
覆盖品种:
  股指期货: IF(沪深300) IC(中证500) IH(上证50) IM(中证1000)
  国债期货: T(10Y) TF(5Y) TS(2Y)
  黑色系:   rb(螺纹) i(铁矿) hc(热卷) j(焦炭) jm(焦煤)
  有色:     cu(铜) al(铝) zn(锌) ni(镍) sn(锡)
  能化:     sc(原油) fu(燃油) eg(乙二醇) pg(LPG) nr(20号胶)
  农产品:   m(豆粕) y(豆油) p(棕榈油) c(玉米) CF(棉花) SR(白糖) AP(苹果)
  贵金属:   au(黄金) ag(白银)

波动率计算: 日内振幅 = (最高-最低) / 开盘 * 100%
筛选: 振幅 > threshold% 的品种
"""
import re
import logging
from datetime import datetime
from .utils import get_session, retry, safe_float

logger = logging.getLogger(__name__)

# 新浪期货主力合约代码
_SINA_FUTURES = {
    # 股指
    "IF0": "沪深300", "IC0": "中证500", "IH0": "上证50", "IM0": "中证1000",
    # 国债
    "T0": "十年国债", "TF0": "五年国债", "TS0": "两年国债",
    # 黑色
    "rb0": "螺纹钢", "i0": "铁矿石", "hc0": "热卷", "j0": "焦炭", "jm0": "焦煤",
    # 有色
    "cu0": "铜", "al0": "铝", "zn0": "锌", "ni0": "镍", "sn0": "锡",
    # 能化
    "sc0": "原油", "fu0": "燃油", "eg0": "乙二醇", "pg0": "LPG", "nr0": "20号胶",
    # 农产品
    "m0": "豆粕", "y0": "豆油", "p0": "棕榈油", "c0": "玉米",
    "CF0": "棉花", "SR0": "白糖", "AP0": "苹果",
    # 贵金属
    "au0": "黄金", "ag0": "白银",
}


def _parse_sina_line(line: str) -> dict | None:
    """解析新浪期货一行数据。

    两种格式:
      股指/国债期货 (IF0等): 数字,数字,...,中文名 (名字在最后)
      商品期货 (RB0等): 中文名,数字,数字,... (名字在最前)
    """
    m = re.match(r'var hq_str_(\w+)="(.+)";', line.strip())
    if not m:
        return None
    symbol = m.group(1)
    fields = m.group(2).split(",")
    if len(fields) < 5:
        return None

    # 自动判断格式: 第一个字段是否为数字
    try:
        float(fields[0])
        is_numeric_first = True
    except (ValueError, TypeError):
        is_numeric_first = False

    if is_numeric_first:
        # 股指/国债期货: [0]开 [1]高 [2]低 [3]最新 ... [-1]名字
        open_price = safe_float(fields[0], None)
        high_price = safe_float(fields[1], None)
        low_price = safe_float(fields[2], None)
        last_price = safe_float(fields[3], None)
        name = fields[-1].strip()
    else:
        # 商品期货: [0]名字 [1]开 [2]高 [3]低 [4]最新
        name = fields[0].strip()
        open_price = safe_float(fields[1], None)
        high_price = safe_float(fields[2], None)
        low_price = safe_float(fields[3], None)
        last_price = safe_float(fields[4], None) if len(fields) > 4 else None

    if not all([open_price, high_price, low_price]) or open_price <= 0:
        return None

    amplitude = (high_price - low_price) / open_price * 100
    change_pct = (last_price - open_price) / open_price * 100 if last_price and open_price else 0

    return {
        "symbol": symbol,
        "name": name,
        "open": round(open_price, 2),
        "high": round(high_price, 2),
        "low": round(low_price, 2),
        "last": round(last_price, 2) if last_price else None,
        "amplitude": round(amplitude, 2),
        "change_pct": round(change_pct, 2),
    }


@retry(max_retries=2, delay=1)
def _fetch_all_futures() -> list[dict]:
    """新浪期货1次HTTP拿全部品种。"""
    session = get_session()
    symbols = ",".join(f"nf_{s}" for s in _SINA_FUTURES)
    url = f"http://hq.sinajs.cn/list={symbols}"
    resp = session.get(url, timeout=15)
    resp.raise_for_status()

    # 新浪返回 gbk 编码
    text = resp.content.decode("gbk", errors="ignore")
    results = []
    for line in text.strip().split("\n"):
        parsed = _parse_sina_line(line)
        if parsed:
            # 用中文名覆盖
            code = parsed["symbol"]
            # 去掉合约年月后缀，取基础品种名
            base = re.sub(r'\d+$', "", code)
            if base in _SINA_FUTURES:
                parsed["name"] = _SINA_FUTURES[base]
            results.append(parsed)

    return results


def fetch_futures_volatility(threshold: float = 0.5) -> dict:
    """获取期货波动率。

    Args:
        threshold: 振幅筛选阈值(%)，默认 0.5%

    Returns:
        {
            "contracts": [...],          # 全部合约明细
            "volatile": [...],           # 振幅 > threshold 的合约
            "volatile_count": 5,         # 高波动品种数
            "total_count": 30,           # 总品种数
            "avg_amplitude": 0.82,       # 全品种平均振幅
            "timestamp": "...",
            "source": "sina",
        }
    """
    contracts = _fetch_all_futures()

    if not contracts:
        return {
            "contracts": [], "volatile": [],
            "volatile_count": 0, "total_count": 0,
            "avg_amplitude": 0, "timestamp": datetime.now().isoformat(),
            "source": "sina", "error": "数据获取失败",
        }

    # 振幅排序
    contracts.sort(key=lambda x: x["amplitude"], reverse=True)

    amplitudes = [c["amplitude"] for c in contracts if c["amplitude"] > 0]
    avg_amp = round(sum(amplitudes) / len(amplitudes), 2) if amplitudes else 0

    volatile = [c for c in contracts if c["amplitude"] >= threshold]

    return {
        "contracts": contracts,
        "volatile": volatile,
        "volatile_count": len(volatile),
        "total_count": len(contracts),
        "avg_amplitude": avg_amp,
        "timestamp": datetime.now().isoformat(),
        "source": "sina",
    }
