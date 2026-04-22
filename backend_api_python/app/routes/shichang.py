"""
市场看板后端 API
每个栏目独立接口，调用 Interface 层获取数据。
数据源统一通过 DataSourceFactory 获取（与路线 B 共享同一出口）。
"""
import threading
import time
from flask import Blueprint, jsonify, make_response, request
import logging
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import ThreadPoolExecutor
from app.data_sources.factory import DataSourceFactory
from app.data_sources.normalizer import safe_float, safe_int

logger = logging.getLogger(__name__)

shichang_bp = Blueprint('shichang', __name__)


def _build_hub():
    """通过 DataSourceFactory 获取 AStockDataSource，构造 Interface 层统一入口

    路线 A（shichang）和路线 B（market_data_collector/kline/strategy）
    的数据源统一从 DataSourceFactory 出口获取，不再各自独立实例化。
    """
    from app.interfaces.cn_stock_extent import AShareDataHub

    source = DataSourceFactory.get_source("CNStock")  # 返回 AStockDataSource
    return AShareDataHub(sources=[source])


# 模块导入时初始化，避免每次请求重复创建
_hub = _build_hub()

# ============================================================
#  请求级防抖 (Request Coalescing)
#  多个并发请求同一接口时，只放一个去执行，其余共享结果。
# ============================================================
_inflight = {}   # {endpoint_key: (event, result_dict)}
_inflight_lock = threading.Lock()


def _coalesce(key, fn, timeout=30):
    """执行 fn 并缓存结果；并发同 key 请求等待并共享结果。"""
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
            # 延迟清理，防止清理后新请求又重复执行
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


def _get_previous_trading_day():
    """获取上一个交易日 YYYY-MM-DD"""
    today = datetime.now()
    days_back = 1
    if today.weekday() == 0:       # 周一 → 上周五
        days_back = 3
    elif today.weekday() == 6:     # 周日 → 上周五
        days_back = 2
    elif today.weekday() == 5:     # 周六 → 上周五
        days_back = 1
    else:
        days_back = 1
    return (today - timedelta(days=days_back)).strftime("%Y-%m-%d")


def _make_resp(data):
    resp = make_response(jsonify(data))
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Cache-Control'] = 'no-cache'
    return resp


# ============================================================
#  市场总览：指数 + 涨停 + 跌停 + 炸板 + 北向 + 情绪
# ============================================================

def _do_fetch_overview(zt_data_today=None):
    hub = _hub

    # 指数行情
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

    # 涨停池 → 涨停数 + 连板高度（复用传入的 zt_data_today，避免重复请求）
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

    # 跌停数
    limit_down = 0
    try:
        limit_down = hub.limit_down.get_count()
    except Exception as e:
        logger.error(f"获取跌停池失败: {e}")

    # 炸板数
    broken_board = 0
    try:
        broken_board = hub.broken_board.get_count()
    except Exception as e:
        logger.error(f"获取炸板池失败: {e}")

    # 市场快照 → 北向 + 情绪 + 涨跌家数
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

    # 市场热度
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
        data = _coalesce("overview", _run)
    except Exception as e:
        logger.error(f"overview 失败: {e}")
        data = None
    return _make_resp(data or {})


# ============================================================
#  连板：今日 + 昨日
# ============================================================

def _parse_streak(zt_list):
    """从涨停池列表中解析连板数据"""
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
        data = _coalesce("streak", _run)
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
        data = _coalesce("dragon", _do_fetch_dragon)
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
        data = _coalesce("hot", _do_fetch_hot)
    except Exception as e:
        logger.error(f"hot 失败: {e}")
        data = None
    return _make_resp(data or {})


# ============================================================
#  强势股（从涨停池取 Top N，按连板数+涨幅排序）
# ============================================================

def _do_fetch_strong(limit=50, zt_data_today=None):
    hub = _hub

    result = []
    try:
        if zt_data_today is None:
            zt_data_today = hub.zt_pool.get_realtime()
        if zt_data_today:
            # 过滤 ST / 退市
            filtered = [
                item for item in zt_data_today
                if not any(tag in str(item.get("stock_name", ""))
                           for tag in ("ST", "st", "退", "*"))
            ]
            # 按连板天数降序、再按涨跌幅降序
            sorted_zt = sorted(
                filtered or zt_data,
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
        data = _coalesce("strong", _run)
    except Exception as e:
        logger.error(f"strong 失败: {e}")
        data = None
    return _make_resp(data or {})


# ============================================================
#  兼容旧接口（汇总所有子接口数据，串行调用避免并发重复请求）
# ============================================================

@shichang_bp.route('/')
def market_data():
    # 一次获取今日涨停池，供 overview / streak / strong 共享，避免重复请求数据源
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
    """基于市场数据生成分析结论（规则引擎，可后续接入 LLM 增强）"""
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

    # 市场阶段判断（多因子综合）
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

    # 风险评分（综合情绪、跌停数、炸板数）
    risk_score = max(0, min(100, 100 - emotion + ldn * 2 + broken))
    if risk_score > 70:
        risk_level = "高"
    elif risk_score > 40:
        risk_level = "中"
    else:
        risk_level = "低"

    # 热门板块分析
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

    # 操作建议
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


# ============================================================
#  国内大环境分析 — market_cn 模块 API
# ============================================================

@shichang_bp.route('/china-macro')
def china_macro():
    """国内宏观经济数据: GDP, CPI, PPI, PMI, M2, 社融, 进出口, LPR"""
    def _run():
        from app.market_cn.data_sources import ChinaData
        data = ChinaData()

        fetchers = [
            ("gdp", data.gdp),
            ("cpi", data.cpi),
            ("ppi", data.ppi),
            ("pmi", data.pmi),
            ("m2", data.m2),
            ("lpr", data.lpr),
            ("social_financing", data.social_financing),
            ("trade", data.trade),
        ]

        macro = {}

        def _fetch_one(name, fn):
            try:
                df = fn()
                if df is not None and len(df) > 0:
                    records = df.tail(6).fillna("").to_dict(orient="records")
                    return name, {"columns": list(df.columns), "latest": records, "count": len(df)}
                else:
                    return name, {"columns": [], "latest": [], "count": 0}
            except Exception as e:
                logger.error("china-macro %s 失败: %s", name, e)
                return name, {"columns": [], "latest": [], "count": 0, "error": str(e)}

        # 并行获取（每个指标独立线程，互不阻塞）
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(_fetch_one, n, f): n for n, f in fetchers}
            for fut in as_completed(futures, timeout=60):
                name, result = fut.result(timeout=5)
                macro[name] = result

        return {
            "code": 1,
            "msg": "success",
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "data": macro,
        }

    try:
        data = _coalesce("china_macro", _run, timeout=60)
    except Exception as e:
        logger.error("china-macro 失败: %s", e)
        data = None
    return _make_resp(data or {"code": 0, "msg": "获取失败", "data": {}})


@shichang_bp.route('/china-fear-greed')
def china_fear_greed():
    """A股市场贪婪恐惧指数 (7维度综合)"""
    def _run():
        from app.market_cn.fear_greed_index import fear_greed_index
        return fear_greed_index()

    try:
        data = _coalesce("china_fg", _run, timeout=30)
    except Exception as e:
        logger.error("china-fear-greed 失败: %s", e)
        data = None
    return _make_resp({"code": 1 if data else 0, "msg": "success" if data else str(e), "data": data or {}})


@shichang_bp.route('/china-policy')
def china_policy():
    """AI政策解读（关键词版，无需 LLM API Key）"""
    def _run():
        from app.market_cn.policy_analysis import get_policy_keywords, analyze_policy_impact
        policy_items = get_policy_keywords()
        impacts = analyze_policy_impact(policy_items) if policy_items else []
        return {
            "code": 1, "msg": "success",
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "data": {"policy_items": policy_items[:30], "impacts": impacts[:20]},
        }

    try:
        data = _coalesce("china_policy", _run, timeout=30)
    except Exception as e:
        logger.error("china-policy 失败: %s", e)
        data = None
    return _make_resp(data or {"code": 0, "msg": "获取失败", "data": {}})


@shichang_bp.route('/hot-sectors')
def hot_sectors():
    """热门板块 & 概念板块实时分析"""
    def _run():
        from app.market_cn.hot_sectors import get_all_hot_sectors
        return get_all_hot_sectors(industry_limit=15, concept_limit=15)

    try:
        data = _coalesce("hot_sectors", _run, timeout=20)
    except Exception as e:
        logger.error("hot-sectors 失败: %s", e)
        data = None
    return _make_resp({"code": 1 if data else 0, "msg": "success" if data else str(e), "data": data or {}})


@shichang_bp.route('/sector-detail/<board_code>')
def sector_detail(board_code):
    """板块内个股详情"""
    # 防注入：只允许字母数字
    if not board_code.isalnum():
        return _make_resp({"code": 0, "msg": "非法板块代码", "data": []})
    try:
        from app.market_cn.hot_sectors import get_sector_detail
        stocks = get_sector_detail(board_code, limit=15)
        return _make_resp({"code": 1, "msg": "success", "data": stocks})
    except Exception as e:
        logger.error("sector-detail %s 失败: %s", board_code, e)
        return _make_resp({"code": 0, "msg": str(e), "data": []})


# ============================================================
#  板块历史分析 — 趋势 / 周期 / 预测
# ============================================================

@shichang_bp.route('/sector-trend')
def sector_trend():
    """板块1个月趋势分析 + 6个月周期 + 今日预测（汇总接口）"""
    board_type = request.args.get("type", "industry")
    cache_key = f"sector_trend_{board_type}"

    def _run():
        from app.interfaces.cache_file import cache_db
        from app.market_cn.sector_history import get_sector_trend
        db = cache_db()
        return get_sector_trend(db, board_type=board_type)

    try:
        data = _coalesce(cache_key, _run, timeout=30)
    except Exception as e:
        logger.error("sector-trend 失败: %s", e)
        data = None
    return _make_resp({"code": 1 if data else 0, "msg": "success" if data else str(e), "data": data or {}})


@shichang_bp.route('/sector-prediction')
def sector_prediction():
    """今日热门板块预测（基于趋势+季节性+最新排名）"""
    def _run():
        from app.interfaces.cache_file import cache_db
        from app.market_cn.sector_history import SectorAnalyzer
        db = cache_db()
        analyzer = SectorAnalyzer(db)

        industry = analyzer.full_analysis("industry")
        concept = analyzer.full_analysis("concept")

        return {
            "code": 1, "msg": "success",
            "data": {
                "industry": industry.get("prediction", {}),
                "concept": concept.get("prediction", {}),
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        }

    try:
        data = _coalesce("sector_prediction", _run, timeout=30)
    except Exception as e:
        logger.error("sector-prediction 失败: %s", e)
        data = None
    return _make_resp(data or {"code": 0, "msg": "获取失败", "data": {}})


@shichang_bp.route('/sector-history')
def sector_history():
    """板块历史排名数据（供前端图表使用）"""
    try:
        board_type = request.args.get("type", "industry")
        days = request.args.get("days", 30, type=int)
        days = min(max(days, 1), 200)  # clamp 1~200

        from app.interfaces.cache_file import cache_db
        from app.market_cn.sector_history import get_sector_history

        db = cache_db()
        rows = get_sector_history(db, board_type=board_type, days=days)
        return _make_resp({"code": 1, "msg": "success", "count": len(rows), "data": rows})
    except Exception as e:
        logger.error("sector-history 失败: %s", e)
        return _make_resp({"code": 0, "msg": str(e), "data": []})


@shichang_bp.route('/sector-cycle')
def sector_cycle():
    """板块6个月周期分析"""
    board_type = request.args.get("type", "industry")
    cache_key = f"sector_cycle_{board_type}"

    def _run():
        from app.interfaces.cache_file import cache_db
        from app.market_cn.sector_history import SectorAnalyzer
        db = cache_db()
        analyzer = SectorAnalyzer(db)
        result = analyzer.full_analysis(board_type)
        return {
            "code": 1, "msg": "success",
            "data": {
                "cycle": result.get("cycle", {}),
                "data_days": result.get("data_days", 0),
                "date_range": result.get("date_range", {}),
            }
        }

    try:
        data = _coalesce(cache_key, _run, timeout=30)
    except Exception as e:
        logger.error("sector-cycle 失败: %s", e)
        data = None
    return _make_resp(data or {"code": 0, "msg": "获取失败", "data": {}})


# ============================================================
#  情绪历史图表接口
# ============================================================

@shichang_bp.route('/emotion/history')
def emotion_history():
    """查询情绪指数历史数据（供前端情绪图表使用）"""
    try:
        hours = request.args.get('hours', type=int)
        date = request.args.get('date')

        from app.interfaces.cache_file import cache_db
        from app.interfaces.emotion_scheduler import query_emotion_history

        db = cache_db()
        history = query_emotion_history(db, date=date, hours=hours)
        return jsonify({"code": 1, "msg": "success", "history": history})
    except Exception as e:
        logger.error(f"查询情绪历史失败: {e}")
        return jsonify({"code": 0, "msg": str(e), "history": []}), 500
