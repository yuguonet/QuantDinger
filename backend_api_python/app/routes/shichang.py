"""
市场看板后端 API
所有市场路由统一在此注册：
  - shichang_bp   → /api/shichang   A股看板 + 国内宏观
  - global_market_bp → /api/global-market  国际市场

数据源通过统一数据入口获取（market_cn.china_market / data_providers.global_market），
缓存机制内聚在各模块内部，路由层不接触缓存。
"""
import json as _json
import threading
import time
from flask import Blueprint, jsonify, make_response, request
import logging
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from app.data_sources.factory import DataSourceFactory
from app.data_sources.normalizer import safe_float, safe_int

logger = logging.getLogger(__name__)

shichang_bp = Blueprint('shichang', __name__)


# ============================================================
#  A股市场看板 — 本地缓存配置
# ============================================================

CACHE_TTL_SH = {
    "overview":           60,
    "streak":             60,
    "dragon":             120,
    "hot":                120,
    "strong":             60,
}

STALE_AFTER_SH = {
    "overview":           30,
    "streak":             30,
    "dragon":             60,
    "hot":                60,
    "strong":             30,
}

_refresh_locks = {}
_refresh_locks_guard = threading.Lock()


def _get_refresh_lock(key):
    with _refresh_locks_guard:
        if key not in _refresh_locks:
            _refresh_locks[key] = threading.Lock()
        return _refresh_locks[key]


# ============================================================
#  A股市场看板 — 缓存读写 + 后台刷新
# ============================================================

def _sh_cache_key(endpoint):
    return f"dp:shichang_{endpoint}"


def _read_cache_sh(endpoint):
    try:
        from app.utils.cache import CacheManager
        cm = CacheManager()
        key = _sh_cache_key(endpoint)
        if cm.is_redis:
            raw = cm._client.get(key)
            if raw:
                entry = _json.loads(raw)
                return entry.get("data"), entry.get("ts", 0)
        else:
            with cm._client._lock:
                entry = cm._client._cache.get(key)
                if entry:
                    parsed = _json.loads(entry[0])
                    return parsed.get("data"), parsed.get("ts", 0)
    except Exception:
        pass
    return None, 0


def _write_cache_sh(endpoint, data):
    ttl = CACHE_TTL_SH.get(endpoint, 120)
    payload = _json.dumps({"data": data, "ts": time.time()}, ensure_ascii=False, default=str)
    try:
        from app.utils.cache import CacheManager
        cm = CacheManager()
        key = _sh_cache_key(endpoint)
        cm.set(key, payload, ttl=ttl)
    except Exception as e:
        logger.warning("写缓存失败 [%s]: %s", endpoint, e)


def _maybe_refresh_bg_sh(endpoint, fetch_fn, cached_ts):
    if not cached_ts:
        return
    elapsed = time.time() - cached_ts
    wait = STALE_AFTER_SH.get(endpoint, 120)
    if elapsed < wait:
        return
    lock = _get_refresh_lock(endpoint)

    def _bg():
        if not lock.acquire(blocking=False):
            return
        try:
            logger.info("[shichang] 后台刷新 %s (过期 %ds > %ds)", endpoint, elapsed, wait)
            result = fetch_fn()
            if result is not None:
                _write_cache_sh(endpoint, result)
        except Exception as e:
            logger.error("[shichang] 后台刷新 %s 失败: %s", endpoint, e)
        finally:
            lock.release()

    threading.Thread(target=_bg, daemon=True).start()


# ============================================================
#  A股市场看板 — 请求防抖
# ============================================================
_inflight = {}
_inflight_lock = threading.Lock()


def _coalesce(key, fn, timeout=30):
    event = None
    should_run = False
    with _inflight_lock:
        if key in _inflight:
            event = _inflight[key][0]
        else:
            event = threading.Event()
            _inflight[key] = (event, None)
            should_run = True

    if should_run:
        result = None
        try:
            result = fn()
        except Exception as e:
            logger.error(f"coalesce [{key}] 执行异常: {e}")
        finally:
            with _inflight_lock:
                _inflight[key] = (event, result)
                event.set()

            def _cleanup():
                time.sleep(3)
                with _inflight_lock:
                    _inflight.pop(key, None)
            threading.Thread(target=_cleanup, daemon=True).start()
        return result
    else:
        event.wait(timeout=timeout)
        with _inflight_lock:
            entry = _inflight.get(key)
            return entry[1] if entry else None


def _cached_fetch_sh(endpoint, fetch_fn, timeout=30):
    """A股市场看板专用缓存读取"""
    data, cached_ts = _read_cache_sh(endpoint)
    if data is not None:
        _maybe_refresh_bg_sh(endpoint, fetch_fn, cached_ts)
        return data
    result = _coalesce(endpoint, fetch_fn, timeout=timeout)
    if result is not None:
        _write_cache_sh(endpoint, result)
    return result


# ============================================================
#  数据源入口
# ============================================================

def _build_hub():
    """通过 DataSourceFactory 获取 AStockDataSource，构造 Interface 层统一入口"""
    from app.interfaces.cn_stock_extent import AShareDataHub
    source = DataSourceFactory.get_source("CNStock")
    return AShareDataHub(sources=[source])


_hub = _build_hub()


def _get_previous_trading_day():
    today = datetime.now()
    if today.weekday() == 0:
        days_back = 3
    elif today.weekday() == 6:
        days_back = 2
    elif today.weekday() == 5:
        days_back = 1
    else:
        days_back = 1
    return (today - timedelta(days=days_back)).strftime("%Y-%m-%d")


def _make_resp(data):
    resp = make_response(jsonify(data))
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Cache-Control'] = 'no-cache'
    return resp


# ############################################################
#  以下为 A 股市场看板路由
# ############################################################

# ============================================================
#  市场总览：指数 + 涨停 + 跌停 + 炸板 + 北向 + 情绪
# ============================================================

def _do_fetch_overview(zt_data_today=None):
    hub = _hub

    sse, sse_c, szse, szse_c, cyse, cyse_c, bzse, bzse_c = (
        0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0
    )
    try:
        indices = hub.index.get_realtime()
        code_map = {item["code"]: item for item in (indices or [])}
        idx_conf = {
            "000001": ("sse",  "sse_c"),
            "399001": ("szse", "szse_c"),
            "399006": ("cyse", "cyse_c"),
            "899050": ("bzse", "bzse_c"),
        }
        for code, item in code_map.items():
            if code in idx_conf:
                price_key, pct_key = idx_conf[code]
                locals()[price_key] = safe_float(item.get("price"))
                locals()[pct_key] = safe_float(item.get("change_percent"))
    except Exception as e:
        logger.error(f"获取指数失败: {e}")

    limit_up = 0
    streak_height = 0
    try:
        if zt_data_today is None:
            zt_data_today = hub.zt_pool.get_realtime()
        if zt_data_today:
            limit_up = len(zt_data_today)
            streak_height = max(
                (safe_int(item.get("continuous_zt_days", 1)) for item in zt_data_today),
                default=0
            )
    except Exception as e:
        logger.error(f"获取涨停池失败: {e}")

    limit_down = 0
    try:
        limit_down = hub.limit_down.get_count()
    except Exception as e:
        logger.error(f"获取跌停池失败: {e}")

    broken_board = 0
    try:
        broken_board = hub.broken_board.get_count()
    except Exception as e:
        logger.error(f"获取炸板池失败: {e}")

    north_net = 0.0
    emotion = 50
    up_count = 0
    down_count = 0
    try:
        snap = hub.market_snapshot.get_realtime()
        north_net = safe_float(snap.get("north_net_flow", 0))
        emotion = safe_int(snap.get("emotion", 50))
        up_count = safe_int(snap.get("up_count", 0))
        down_count = safe_int(snap.get("down_count", 0))
    except Exception as e:
        logger.error(f"获取市场快照失败: {e}")

    if -0.3 <= sse_c <= 0.3:
        heat = "平淡"
    elif sse_c > 0.8:
        heat = "火热"
    elif sse_c < -0.8:
        heat = "寒冷"
    else:
        heat = "温热" if sse_c > 0 else "偏冷"

    return {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "sse": {"index": f"{sse:.2f}", "change": sse_c, "code": "000001"},
        "szse": {"index": f"{szse:.2f}", "change": szse_c, "code": "399001"},
        "cyse": {"index": f"{cyse:.2f}", "change": cyse_c, "code": "399006"},
        "bzse": {"index": f"{bzse:.2f}", "change": bzse_c, "code": "899050"},
        "heat": heat,
        "upCount": up_count,
        "downCount": down_count,
        "limitUp": limit_up,
        "limitDown": limit_down,
        "streakHeight": streak_height,
        "brokenBoard": broken_board,
        "northBound": north_net,
        "emotionIndex": emotion,
    }


@shichang_bp.route('/overview')
def overview():
    def _run():
        zt_today = _hub.zt_pool.get_realtime()
        return _do_fetch_overview(zt_data_today=zt_today)
    try:
        data = _cached_fetch_sh("overview", _run)
    except Exception as e:
        logger.error(f"overview 失败: {e}")
        data = None
    return _make_resp(data or {})


# ============================================================
#  连板：今日 + 昨日
# ============================================================

def _parse_streak(zt_list):
    stocks = []
    height = 0
    if not zt_list:
        return stocks, height
    for item in zt_list:
        days = safe_int(item.get("continuous_zt_days", 1))
        if days < 2:
            continue
        price = safe_float(item.get("price", 0))
        ch = safe_float(item.get("change_percent", 0))
        stocks.append({
            "code": str(item.get("stock_code", "")),
            "name": str(item.get("stock_name", "")),
            "days": days,
            "price": f"{price:.2f}",
            "change": f"{'+' if ch >= 0 else ''}{ch:.2f}%",
            "sector": str(item.get("sector", "")),
            "reason": str(item.get("reason", "")),
            "seal_amount": safe_float(item.get("seal_amount", 0)),
            "turnover_rate": safe_float(item.get("turnover_rate", 0)),
            "zt_time": str(item.get("zt_time", "")),
            "open_count": safe_int(item.get("open_count", 0)),
            "volume": safe_float(item.get("volume", 0)),
            "amount": safe_float(item.get("amount", 0)),
        })
        height = max(height, days)
    stocks.sort(key=lambda x: x["days"], reverse=True)
    return stocks, height


def _do_fetch_streak(zt_data_today=None):
    hub = _hub

    today_stocks, today_h = [], 0
    try:
        if zt_data_today is None:
            zt_data_today = hub.zt_pool.get_realtime()
        today_stocks, today_h = _parse_streak(zt_data_today)
    except Exception as e:
        logger.error(f"获取今日连板失败: {e}")

    yest_stocks, yest_h = [], 0
    try:
        prev_date = _get_previous_trading_day()
        yest_zt = hub.zt_pool.get_realtime(prev_date)
        yest_stocks, yest_h = _parse_streak(yest_zt)
    except Exception as e:
        logger.error(f"获取昨日连板失败: {e}")

    return {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "streakStocks": today_stocks,
        "streakHeight": today_h,
        "yesterdayStreakStocks": yest_stocks,
        "yesterdayStreakHeight": yest_h,
    }


@shichang_bp.route('/streak')
def streak():
    def _run():
        zt_today = _hub.zt_pool.get_realtime()
        return _do_fetch_streak(zt_data_today=zt_today)
    try:
        data = _cached_fetch_sh("streak", _run)
    except Exception as e:
        logger.error(f"streak 失败: {e}")
        data = None
    return _make_resp(data or {})


# ============================================================
#  龙虎榜
# ============================================================

def _do_fetch_dragon():
    hub = _hub
    today = datetime.now().strftime("%Y-%m-%d")

    result = []
    try:
        start_date = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
        lhb = hub.dragon_tiger.get_history(start_date, today)
        if lhb:
            for item in lhb:
                code = str(item.get("stock_code", ""))
                name = str(item.get("stock_name", ""))
                reason = str(item.get("reason", ""))[:50]
                ch = safe_float(item.get("change_percent", 0))
                buy_amount = safe_float(item.get("buy_amount", 0))
                sell_amount = safe_float(item.get("sell_amount", 0))
                net_amount = safe_float(item.get("net_amount", 0))
                result.append({
                    "code": code,
                    "name": name,
                    "reason": reason,
                    "change": f"{'+' if ch >= 0 else ''}{ch:.2f}%",
                    "trade_date": str(item.get("trade_date", "")),
                    "buy_amount": buy_amount,
                    "sell_amount": sell_amount,
                    "net_amount": net_amount,
                    "buy_seat_count": safe_int(item.get("buy_seat_count", 0)),
                    "sell_seat_count": safe_int(item.get("sell_seat_count", 0)),
                })
    except Exception as e:
        logger.error(f"获取龙虎榜失败: {e}")

    return {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "dragonTigerList": result,
    }


@shichang_bp.route('/dragon')
def dragon():
    try:
        data = _cached_fetch_sh("dragon", _do_fetch_dragon)
    except Exception as e:
        logger.error(f"dragon 失败: {e}")
        data = None
    return _make_resp(data or {})


# ============================================================
#  热榜/人气板
# ============================================================

def _do_fetch_hot(limit=50):
    hub = _hub

    result = []
    try:
        hot_list = hub.hot_rank.get_realtime()
        if hot_list:
            for item in hot_list[:limit]:
                code = str(item.get("stock_code", ""))
                name = str(item.get("stock_name", ""))
                hot = safe_float(item.get("popularity_score", 0))
                ch = safe_float(item.get("change_percent", 0))
                price = safe_float(item.get("price", 0))
                rank = safe_int(item.get("rank", 0))
                result.append({
                    "rank": rank,
                    "code": code,
                    "name": name,
                    "hot": f"{hot:.0f}",
                    "change": f"{'+' if ch >= 0 else ''}{ch:.2f}%",
                    "price": f"{price:.2f}",
                    "current_rank_change": str(item.get("current_rank_change", "")),
                })
    except Exception as e:
        logger.error(f"获取热榜失败: {e}")

    return {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "hotList": result,
    }


@shichang_bp.route('/hot')
def hot():
    try:
        data = _cached_fetch_sh("hot", _do_fetch_hot)
    except Exception as e:
        logger.error(f"hot 失败: {e}")
        data = None
    return _make_resp(data or {})


# ============================================================
#  强势股
# ============================================================

def _do_fetch_strong(limit=50, zt_data_today=None):
    hub = _hub

    result = []
    try:
        if zt_data_today is None:
            zt_data_today = hub.zt_pool.get_realtime()
        if zt_data_today:
            filtered = [
                item for item in zt_data_today
                if not any(tag in str(item.get("stock_name", ""))
                           for tag in ("ST", "st", "退", "*"))
            ]
            sorted_zt = sorted(
                filtered or zt_data_today,
                key=lambda x: (
                    safe_int(x.get("continuous_zt_days", 1)),
                    safe_float(x.get("change_percent", 0))
                ),
                reverse=True
            )
            for i, item in enumerate(sorted_zt[:limit], 1):
                code = str(item.get("stock_code", ""))
                name = str(item.get("stock_name", ""))
                price = safe_float(item.get("price", 0))
                ch = safe_float(item.get("change_percent", 0))
                days = safe_int(item.get("continuous_zt_days", 1))
                result.append({
                    "rank": i,
                    "code": code,
                    "name": name,
                    "price": f"{price:.2f}",
                    "gain": f"{'+' if ch >= 0 else ''}{ch:.2f}%",
                    "days": days,
                    "sector": str(item.get("sector", "")),
                    "reason": str(item.get("reason", "")),
                    "volume": safe_float(item.get("volume", 0)),
                    "amount": safe_float(item.get("amount", 0)),
                    "turnover_rate": safe_float(item.get("turnover_rate", 0)),
                    "seal_amount": safe_float(item.get("seal_amount", 0)),
                    "zt_time": str(item.get("zt_time", "")),
                    "open_count": safe_int(item.get("open_count", 0)),
                })
    except Exception as e:
        logger.error(f"获取强势股失败: {e}")

    return {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "strongStocks": result,
    }


@shichang_bp.route('/strong')
def strong():
    def _run():
        zt_today = _hub.zt_pool.get_realtime()
        return _do_fetch_strong(zt_data_today=zt_today)
    try:
        data = _cached_fetch_sh("strong", _run)
    except Exception as e:
        logger.error(f"strong 失败: {e}")
        data = None
    return _make_resp(data or {})


# ============================================================
#  兼容旧接口（汇总所有子接口数据）
# ============================================================

@shichang_bp.route('/')
def market_data():
    zt_today = None
    try:
        zt_today = _hub.zt_pool.get_realtime()
    except Exception as e:
        logger.error(f"market_data 预取涨停池失败: {e}")

    try:
        ov = _do_fetch_overview(zt_data_today=zt_today)
    except Exception as e:
        logger.error(f"market_data overview 失败: {e}")
        ov = {}
    try:
        sk = _do_fetch_streak(zt_data_today=zt_today)
    except Exception as e:
        logger.error(f"market_data streak 失败: {e}")
        sk = {}
    try:
        dg = _do_fetch_dragon()
    except Exception as e:
        logger.error(f"market_data dragon 失败: {e}")
        dg = {}
    try:
        ht = _do_fetch_hot()
    except Exception as e:
        logger.error(f"market_data hot 失败: {e}")
        ht = {}
    try:
        st = _do_fetch_strong(zt_data_today=zt_today)
    except Exception as e:
        logger.error(f"market_data strong 失败: {e}")
        st = {}

    if sk.get('streakHeight'):
        ov['streakHeight'] = sk['streakHeight']

    data = {**ov, **sk, **dg, **ht, **st}
    data['aiAnalysis'] = _build_ai_analysis(ov, sk)

    resp = make_response(jsonify(data))
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Cache-Control'] = 'no-cache'
    return resp


def _build_ai_analysis(ov, sk):
    """基于市场数据生成分析结论（规则引擎）"""
    sse_c = ov.get('sse', {}).get('change', 0)
    lup = ov.get('limitUp', 0)
    ldn = ov.get('limitDown', 0)
    streak_h = sk.get('streakHeight', 0)
    emotion = ov.get('emotionIndex', 50)
    up = ov.get('upCount', 0)
    down = ov.get('downCount', 0)
    north = ov.get('northBound', 0)
    broken = ov.get('brokenBoard', 0)

    total = up + down
    up_ratio = up / total if total > 0 else 0.5

    if sse_c > 1.0 and up_ratio > 0.7:
        phase, advice = "强势上攻", "持股待涨，可适当加仓"
    elif sse_c > 0.3 and up_ratio > 0.55:
        phase, advice = "震荡上行", "持股待涨，精选个股"
    elif sse_c > -0.3 and 0.4 < up_ratio < 0.6:
        phase, advice = "窄幅震荡", "高抛低吸，控制仓位"
    elif sse_c > -0.8:
        phase, advice = "震荡下行", "减仓观望，等待企稳"
    else:
        phase, advice = "弱势下跌", "控制仓位，防御为主"

    risk_score = max(0, min(100, 100 - emotion + ldn * 2 + broken))
    if risk_score > 70:
        risk_level = "高"
    elif risk_score > 40:
        risk_level = "中"
    else:
        risk_level = "低"

    hot_sectors = []
    if lup > 50:
        hot_sectors.append({"name": "涨停潮", "driver": f"涨停{lup}家", "score": min(95, 60 + lup // 10)})
    if streak_h >= 5:
        hot_sectors.append({"name": "连板龙头", "driver": f"{streak_h}连板", "score": 85})
    if streak_h >= 3:
        hot_sectors.append({"name": "接力效应", "driver": f"高度{streak_h}板", "score": min(80, 50 + streak_h * 5)})
    if north > 30:
        hot_sectors.append({"name": "北向加仓", "driver": f"净流入{north:.1f}亿", "score": min(85, 60 + int(north))})
    if emotion > 70:
        hot_sectors.append({"name": "市场做多", "driver": "情绪高涨", "score": emotion})
    if not hot_sectors:
        hot_sectors = [{"name": "待观察", "driver": "无明显主线", "score": 40}]

    op = []
    if lup > 50:
        op.append(f"涨停{lup}家，赚钱效应极强，顺势而为")
    elif lup > 30:
        op.append(f"涨停{lup}家，市场赚钱效应较好")
    elif lup > 10:
        op.append(f"涨停{lup}家，赚钱效应一般，精选个股")
    else:
        op.append(f"仅涨停{lup}家，赚钱效应差，建议观望")

    if streak_h >= 5:
        op.append(f"连板高度{streak_h}板，龙头效应强，关注接力机会")
    elif streak_h >= 3:
        op.append(f"连板高度{streak_h}板，关注龙头接力")

    if ldn > 30:
        op.append(f"跌停{ldn}家，高位补跌风险大，回避高位股")
    elif ldn > 20:
        op.append(f"跌停{ldn}家，注意回避高位补跌风险")

    if broken > 20:
        op.append(f"炸板{broken}家，封板成功率低，追涨需谨慎")

    if north > 50:
        op.append(f"北向净流入{north:.1f}亿，外资看好，可适当乐观")
    elif north < -50:
        op.append(f"北向净流出{abs(north):.1f}亿，外资撤退，注意风险")

    if emotion > 80:
        op.append("情绪极度高涨，注意高位风险，分批止盈")
    elif emotion > 60:
        op.append("情绪偏高，保持理性，控制追涨冲动")
    elif emotion < 20:
        op.append("情绪极度低迷，可关注超跌反弹机会，左侧布局")
    elif emotion < 35:
        op.append("情绪低迷，耐心等待转机")

    return {
        'confidence': 75,
        'phase': phase,
        'temperature': emotion,
        'profitEffect': min(int(up_ratio * 100), 85),
        'riskScore': risk_score,
        'riskLevel': risk_level,
        'advice': advice,
        'hotSectors': hot_sectors[:5],
        'operationAdvice': op[:6],
    }


# ############################################################
#  国内大环境分析路由 — 全部走 market_cn.china_market，缓存不外露
# ############################################################

from app.market_cn.china_market import (
    get_china_macro,
    get_fear_greed,
    get_policy,
    get_hot_sectors as _get_hot_sectors,
    get_sector_stocks,
    get_sector_trend as _get_sector_trend,
    get_sector_prediction,
    get_sector_history as _get_sector_history,
    get_sector_cycle,
    get_emotion_history as _get_emotion_history,
    refresh as refresh_cn,
)


@shichang_bp.route('/china-macro')
def china_macro():
    """国内宏观经济数据: GDP, CPI, PPI, PMI, M2, 社融, 进出口, LPR"""
    try:
        data = get_china_macro()
    except Exception as e:
        logger.error("china-macro 失败: %s", e)
        data = {"code": 0, "msg": "获取失败", "data": {}}
    return _make_resp(data)


@shichang_bp.route('/china-fear-greed')
def china_fear_greed():
    """A股市场贪婪恐惧指数 (7维度综合)"""
    try:
        data = get_fear_greed()
    except Exception as e:
        logger.error("china-fear-greed 失败: %s", e)
        data = {"code": 0, "msg": str(e), "data": {}}
    return _make_resp(data)


@shichang_bp.route('/china-policy')
def china_policy():
    """AI政策解读（关键词版，无需 LLM API Key）"""
    try:
        data = get_policy()
    except Exception as e:
        logger.error("china-policy 失败: %s", e)
        data = {"code": 0, "msg": "获取失败", "data": {}}
    return _make_resp(data)


@shichang_bp.route('/hot-sectors')
def hot_sectors():
    """热门板块 & 概念板块实时分析"""
    try:
        data = _get_hot_sectors()
    except Exception as e:
        logger.error("hot-sectors 失败: %s", e)
        data = {"code": 0, "msg": str(e), "data": {}}
    return _make_resp(data)


@shichang_bp.route('/sector-detail/<board_code>')
def sector_detail(board_code):
    """板块内个股详情"""
    try:
        data = get_sector_stocks(board_code)
    except Exception as e:
        logger.error("sector-detail %s 失败: %s", board_code, e)
        data = {"code": 0, "msg": str(e), "data": []}
    return _make_resp(data)


@shichang_bp.route('/sector-trend')
def sector_trend():
    """板块1个月趋势分析 + 6个月周期 + 今日预测"""
    board_type = request.args.get("type", "industry")
    try:
        data = _get_sector_trend(board_type=board_type)
    except Exception as e:
        logger.error("sector-trend 失败: %s", e)
        data = {"code": 0, "msg": str(e), "data": {}}
    return _make_resp(data)


@shichang_bp.route('/sector-prediction')
def sector_prediction():
    """今日热门板块预测"""
    try:
        data = get_sector_prediction()
    except Exception as e:
        logger.error("sector-prediction 失败: %s", e)
        data = {"code": 0, "msg": "获取失败", "data": {}}
    return _make_resp(data)


@shichang_bp.route('/sector-history')
def sector_history():
    """板块历史排名数据（供前端图表使用）"""
    board_type = request.args.get("type", "industry")
    days = request.args.get("days", 30, type=int)
    try:
        data = _get_sector_history(board_type=board_type, days=days)
    except Exception as e:
        logger.error("sector-history 失败: %s", e)
        data = {"code": 0, "msg": str(e), "data": []}
    return _make_resp(data)


@shichang_bp.route('/sector-cycle')
def sector_cycle():
    """板块6个月周期分析"""
    board_type = request.args.get("type", "industry")
    try:
        data = get_sector_cycle(board_type=board_type)
    except Exception as e:
        logger.error("sector-cycle 失败: %s", e)
        data = {"code": 0, "msg": "获取失败", "data": {}}
    return _make_resp(data)


@shichang_bp.route('/emotion/history')
def emotion_history():
    """情绪指数历史数据"""
    hours = request.args.get('hours', type=int)
    date = request.args.get('date')
    try:
        data = _get_emotion_history(hours=hours, date=date)
    except Exception as e:
        logger.error("查询情绪历史失败: %s", e)
        data = {"code": 0, "msg": str(e), "history": []}
    return _make_resp(data)


# ############################################################
#  手动刷新（统一入口，A股 + 国内宏观）
# ############################################################

@shichang_bp.route('/refresh', methods=['POST'])
def refresh_data():
    """手动触发远端拉取 → 写缓存"""
    body = request.get_json(silent=True) or {}
    target = body.get("target", "all")

    # A 股市场看板
    A_SHARE_MAP = {
        "overview":   lambda: _do_fetch_overview(zt_data_today=_hub.zt_pool.get_realtime()),
        "streak":     lambda: _do_fetch_streak(zt_data_today=_hub.zt_pool.get_realtime()),
        "dragon":     _do_fetch_dragon,
        "hot":        _do_fetch_hot,
        "strong":     lambda: _do_fetch_strong(zt_data_today=_hub.zt_pool.get_realtime()),
    }

    results = {}

    # 刷新 A 股
    if target == "all":
        a_targets = list(A_SHARE_MAP.keys())
    elif target in A_SHARE_MAP:
        a_targets = [target]
    else:
        a_targets = []

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {}
        for t in a_targets:
            futures[pool.submit(_safe_refresh_sh, t, A_SHARE_MAP[t])] = t
        for fut in as_completed(futures, timeout=120):
            key = futures[fut]
            try:
                results[key] = "ok" if fut.result() else "failed"
            except Exception as e:
                results[key] = f"error: {e}"

    # 刷新国内宏观（委托 china_market）
    cn_targets = target if target not in A_SHARE_MAP else "all"
    if target == "all" or target not in A_SHARE_MAP:
        cn_results = refresh_cn(target=cn_targets)
        results.update(cn_results)

    return jsonify({"code": 1, "msg": "refresh done", "results": results})


def _safe_refresh_sh(endpoint, fn):
    """A 股市场看板专用刷新"""
    lock = _get_refresh_lock(endpoint)
    if not lock.acquire(blocking=False):
        return False
    try:
        data = _coalesce(endpoint, fn, timeout=60)
        if data is not None:
            _write_cache_sh(endpoint, data)
            return True
        return False
    finally:
        lock.release()


# ############################################################
#  国际市场路由 — 全部走 data_providers.global_market，缓存不外露
# ############################################################

from app.data_providers.global_market import (
    get_sentiment,
    get_indices as _get_indices,
    get_heatmap as _get_heatmap,
    get_news as _get_news,
    get_calendar as _get_calendar,
    get_opportunities as _get_opportunities,
    refresh as refresh_intl,
)

global_market_bp = Blueprint('global_market', __name__)


@global_market_bp.route("/sentiment", methods=["GET"])
def market_sentiment():
    """情绪指标 + 大宗商品"""
    try:
        return jsonify(get_sentiment())
    except Exception as e:
        logger.error("sentiment 失败: %s", e, exc_info=True)
        return jsonify({"code": 0, "msg": str(e), "data": {}})


@global_market_bp.route("/indices", methods=["GET"])
def market_indices():
    """全球股指 + 外汇 + 加密货币"""
    try:
        return jsonify(_get_indices())
    except Exception as e:
        logger.error("indices 失败: %s", e, exc_info=True)
        return jsonify({"code": 0, "msg": str(e), "data": {}})


@global_market_bp.route("/heatmap", methods=["GET"])
def market_heatmap():
    """市场热力图"""
    try:
        return jsonify(_get_heatmap())
    except Exception as e:
        logger.error("heatmap 失败: %s", e, exc_info=True)
        return jsonify({"code": 0, "msg": str(e), "data": {}})


@global_market_bp.route("/news", methods=["GET"])
def market_news():
    """财经新闻"""
    lang = request.args.get("lang", "all")
    try:
        return jsonify(_get_news(lang=lang))
    except Exception as e:
        logger.error("news 失败: %s", e, exc_info=True)
        return jsonify({"code": 0, "msg": str(e), "data": {}})


@global_market_bp.route("/calendar", methods=["GET"])
def economic_calendar():
    """经济日历"""
    try:
        return jsonify(_get_calendar())
    except Exception as e:
        logger.error("calendar 失败: %s", e, exc_info=True)
        return jsonify({"code": 0, "msg": str(e), "data": []})


@global_market_bp.route("/opportunities", methods=["GET"])
def trading_opportunities():
    """交易机会扫描"""
    try:
        return jsonify(_get_opportunities())
    except Exception as e:
        logger.error("opportunities 失败: %s", e, exc_info=True)
        return jsonify({"code": 0, "msg": str(e), "data": []})


@global_market_bp.route("/refresh", methods=["POST"])
def global_refresh_data():
    """手动刷新国际市场"""
    body = request.get_json(silent=True) or {}
    target = body.get("target", "all")
    try:
        results = refresh_intl(target=target)
        ok = all(v == "ok" for v in results.values())
        return jsonify({"code": 1 if ok else 0, "msg": "refreshed", "results": results})
    except Exception as e:
        logger.error("refresh 失败: %s", e, exc_info=True)
        return jsonify({"code": 0, "msg": str(e), "results": {}})
