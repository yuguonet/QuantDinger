"""龙虎榜卡片 — 数据来源: cnd_dragon_tiger_list（数据库最后一个有数据交易日的快照）

⚠ 2026-10-16 变更: 不再经过 dragon_limit 的内存缓存 (get_dragon_tiger)，直接读库。
关键设计点 / 易错点（勿改回缓存优先）:
  龙虎榜是交易所**收盘后**才发布的日频数据（LHB(D) 约 17:00-17:30 发布），盘中不存在
  "当日"榜单。原链路里填充缓存的 refresh_dragon_tiger 只挂在盘后批次（15:30）跑一次，
  且用空日期调 AkShare stock_lhb_detail_em —— 拿到的只是"上一个已发布交易日"的数据，
  与数据库里已有的内容完全一致；而当日数据由 dragon_hot_daily（17:00）落库后，再通过
  load_dragon_tiger_from_db 从库里回填进缓存。
  即: 缓存里唯一比库"新"的数据恰恰来自数据库，缓存对卡片没有任何新鲜度增益，
  只多出两个失败模式 —— ① 后端重启后缓存为空 → 卡片空白；② 网络/AkShare 不可用 →
  缓存被空列表覆盖 → 卡片空白。
  直接读库后: 展示不依赖交易时段与时段任务、不依赖外部数据源、重启后立即可见。
"""
import time
from datetime import datetime
from typing import Any, Dict, List

from ._base import CardMeta, register

meta = CardMeta(
    id="dragon_tiger",
    name="龙虎榜",
    endpoint="/dragon-tiger",
    refresh_interval=120,
    order=70,
    requires_hub=False,
)

# 进程内短 TTL 记忆: 前端每 120s 轮询一次卡片，多客户端会把"一次读库"放大成每 120s
# 一次全表查询。龙虎榜是日频数据，60s 内重复读没有意义 —— 仅用于削峰，不是数据源
# （数据源恒为数据库；17:00 落库后最多 60s 即可见）。
_MEMO_TTL_SEC = 60
_memo_rows: List[Dict[str, Any]] = []
_memo_ts = 0.0


def _latest_from_db() -> List[Dict[str, Any]]:
    """读数据库最后一个有数据交易日的龙虎榜（带进程内短 TTL 记忆）

    查询失败时 query_dragon_tiger_latest 内部已告警并返回 []，此处不再二次兜底。
    """
    global _memo_rows, _memo_ts
    now = time.time()
    if _memo_rows and (now - _memo_ts) < _MEMO_TTL_SEC:
        return _memo_rows
    from app.market_cn.dragon_tiger_store import query_dragon_tiger_latest
    _memo_rows = query_dragon_tiger_latest() or []
    _memo_ts = now
    return _memo_rows


def fetch():
    raw = _latest_from_db()
    if not raw:
        return _empty()

    from app.data_sources.normalizer import safe_float

    result = []
    for s in raw:
        ch = safe_float(s.get("change_rate") or s.get("change_percent"), 0)
        price = safe_float(s.get("new_price") or s.get("close_price") or s.get("price"), 0)
        result.append({
            "code": s.get("code") or s.get("stock_code", ""),
            "name": s.get("name") or s.get("stock_name", ""),
            "industry": s.get("industry") or s.get("sector", ""),
            "change": f"{'+' if ch >= 0 else ''}{ch:.2f}%",
            "new_price": f"{price:.2f}",
            "turnoverrate": f"{safe_float(s.get('turnoverrate') or s.get('turnover_rate'), 0):.2f}",
            "deal_amount": s.get("deal_amount") or s.get("amount") or "",
            "total_market_cap": s.get("total_market_cap") or "",
        })

    return {"timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "dragonTigerList": result}


def _empty():
    return {"timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "dragonTigerList": []}


register(meta, fetch)
