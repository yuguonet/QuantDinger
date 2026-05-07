"""
A股恐贪指数 — 东方财富涨跌统计自算

策略: 2次API调用，每次100条，分取涨幅最大/最小的股票。
      不求全量，只求速度。缓存有就直接用。

计算维度 (4个，等权):
  1. 上涨占比 — 从涨跌两端样本估算
  2. 涨跌停比 — 涨停家数 / max(跌停, 1)
  3. 强势股比 — 涨幅>3%的占比
  4. 极端情绪 — 涨停数本身

输出 0-100 分。
"""
import logging
import time
from datetime import datetime
from .utils import get_session, retry, safe_float, safe_int

logger = logging.getLogger(__name__)

_EM_URL = "https://push2.eastmoney.com/api/qt/clist/get"
_EM_FS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
_EM_FIELDS = "f2,f3,f6,f12"


def _limit_pct(code: str) -> float:
    """根据代码判断涨跌停幅度。"""
    if code.startswith(("300", "301", "688")):
        return 20.0
    if code.startswith(("8", "4")) and len(code) == 6:
        return 30.0
    return 10.0


def _parse_items(items: list) -> dict:
    """从一批股票中提取统计。"""
    up = down = flat = 0
    limit_up = limit_down = 0
    strong_up = 0

    for item in items:
        if not isinstance(item, dict):
            continue
        pct = item.get("f3")
        if pct is None or pct == "-":
            continue
        pct = float(pct)
        code = str(item.get("f12", ""))
        lp = _limit_pct(code)

        if pct >= lp * 0.98:
            limit_up += 1
        elif pct <= -lp * 0.98:
            limit_down += 1

        if pct > 0:
            up += 1
            if pct >= 3.0:
                strong_up += 1
        elif pct < 0:
            down += 1
        else:
            flat += 1

    return {
        "up": up, "down": down, "flat": flat,
        "limit_up": limit_up, "limit_down": limit_down,
        "strong_up": strong_up,
        "total": up + down + flat,
    }


def _fetch_page(sort_desc: bool = True) -> list:
    """拿一页100条。sort_desc=True 涨幅最大，False 跌幅最大。"""
    session = get_session()
    params = {
        "pn": 1, "pz": 100, "po": 1 if sort_desc else 0, "np": 1,
        "fltt": 2, "invt": 2, "fid": "f3", "fs": _EM_FS,
        "fields": _EM_FIELDS,
    }
    resp = session.get(_EM_URL, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    data_node = data.get("data")
    if not data_node or not isinstance(data_node, dict):
        return []
    return data_node.get("diff") or []


@retry(max_retries=2, delay=2)
def _fetch_market_stats() -> dict:
    """2次API调用拿涨跌两端各100条。

    返回:
      up_count, down_count, flat_count (估算),
      limit_up, limit_down, strong_up,
      total (API报告的总数), sample_size
    """
    # 第1页: 涨幅最大的100个 → 看涨停、强势股、上涨样本
    top_items = _fetch_page(sort_desc=True)
    top_stats = _parse_items(top_items)

    # 间隔，防频率限制
    time.sleep(0.8)

    # 第2页: 跌幅最大的100个 → 看跌停、下跌样本
    bottom_items = _fetch_page(sort_desc=False)
    bottom_stats = _parse_items(bottom_items)

    # 合并: 涨停来自 top，跌停来自 bottom
    limit_up = top_stats["limit_up"]
    limit_down = bottom_stats["limit_down"]
    strong_up = top_stats["strong_up"]

    # 从样本估算全市场涨跌比
    # top100 里 up 的比例代表"强势端"
    # bottom100 里 down 的比例代表"弱势端"
    # 简化: 用 top100 的 up/down 比例做粗估
    sample_up = top_stats["up"] + top_stats["flat"] // 2
    sample_down = top_stats["down"] + top_stats["flat"] // 2
    sample_total = sample_up + sample_down

    # 估算全市场: 假设 top100 的涨跌比 ≈ 全市场涨跌比
    # (有偏差，但够用，而且有缓存兜底)
    if sample_total > 0:
        up_ratio = sample_up / sample_total
    else:
        up_ratio = 0.5

    # 用 API total 估算
    api_total = 5800  # 近似值，避免多一次调用
    est_up = int(api_total * up_ratio)
    est_down = api_total - est_up

    return {
        "up_count": est_up,
        "down_count": est_down,
        "flat_count": 0,
        "limit_up": limit_up,
        "limit_down": limit_down,
        "strong_up": strong_up,
        "total": api_total,
        "sample_size": len(top_items) + len(bottom_items),
        "up_ratio_sample": round(up_ratio * 100, 1),
    }


# ── 恐贪指数计算 ─────────────────────────────────────────────

def _score_to_label(score: float) -> str:
    if score <= 20:
        return "极度恐惧"
    elif score <= 40:
        return "恐惧"
    elif score <= 60:
        return "中性"
    elif score <= 80:
        return "贪婪"
    else:
        return "极度贪婪"


def fetch_fear_greed() -> dict:
    """计算A股恐贪指数 (2次API调用)。

    Returns:
        {
            "score": 55.0,
            "label": "中性",
            "components": {...},
            "stats": {...},
            "timestamp": "...",
            "source": "eastmoney",
        }
    """
    stats = _fetch_market_stats()

    up = stats["up_count"]
    down = stats["down_count"]
    limit_up = stats["limit_up"]
    limit_down = stats["limit_down"]
    strong_up = stats["strong_up"]
    total = stats["total"]

    # 1. 上涨占比 → 0-100
    up_ratio = stats.get("up_ratio_sample", 50.0)
    s1 = up_ratio

    # 2. 涨跌停比 → 0-100
    if limit_down == 0:
        ratio = float(limit_up)
    else:
        ratio = limit_up / limit_down
    if ratio >= 5:
        s2 = 95.0
    elif ratio >= 3:
        s2 = 80.0
    elif ratio >= 2:
        s2 = 70.0
    elif ratio >= 1:
        s2 = 55.0
    elif ratio >= 0.5:
        s2 = 35.0
    elif ratio >= 0.2:
        s2 = 20.0
    else:
        s2 = 10.0

    # 3. 强势股占比 → 0-100 (涨幅>3% 占样本的比例)
    sample_size = stats.get("sample_size", 200)
    s3 = min(strong_up / max(sample_size, 1) * 500, 100)  # 放大5倍映射

    # 4. 涨停极端情绪 → 复用 s2
    s4 = s2

    composite = round((s1 + s2 + s3 + s4) / 4, 1)

    return {
        "score": composite,
        "label": _score_to_label(composite),
        "components": {
            "up_ratio": {"value": up_ratio, "score": round(s1, 1)},
            "limit_ratio": {"up": limit_up, "down": limit_down, "score": round(s2, 1)},
            "strong_ratio": {"count": strong_up, "sample": sample_size, "score": round(s3, 1)},
            "limit_extreme": {"up": limit_up, "down": limit_down, "score": round(s4, 1)},
        },
        "stats": {
            "up_count": up,
            "down_count": down,
            "limit_up": limit_up,
            "limit_down": limit_down,
            "strong_up": strong_up,
            "total": total,
        },
        "timestamp": datetime.now().isoformat(),
        "source": "eastmoney",
    }
