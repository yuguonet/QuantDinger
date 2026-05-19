"""AI市场分析卡片 — 综合市场数据生成分析结论（规则引擎，无需 LLM）"""
from datetime import datetime
from ._base import CardMeta, register

meta = CardMeta(
    id="ai_analysis",
    name="AI市场分析",
    endpoint="/ai-analysis",
    refresh_interval=120,
    order=20,
    requires_hub=False,
)


def fetch():
    # 从 overview 卡片拿基础数据
    from app.market_cn.cards.overview import fetch as fetch_overview
    ov = fetch_overview()

    sse_c = ov.get("sse", {}).get("change", 0)
    lup = ov.get("limitUp", 0)
    ldn = ov.get("limitDown", 0)
    emotion = ov.get("emotionIndex", 50)
    up = ov.get("upCount", 0)
    down = ov.get("downCount", 0)
    north = ov.get("northBound", 0)
    broken = ov.get("brokenBoard", 0)

    total = up + down
    up_ratio = up / total if total > 0 else 0.5

    # 阶段判定
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

    # 风险评分
    risk_score = max(0, min(100, 100 - emotion + ldn * 2 + broken))
    risk_level = "高" if risk_score > 70 else ("中" if risk_score > 40 else "低")

    # 热门板块（尝试拉取）
    hot_sectors = _fetch_hot_sectors(lup, north, emotion)

    # 操作建议
    op = _build_advice(lup, ldn, broken, north, emotion)

    return {
        "confidence": min(85, 50 + abs(sse_c) * 30 + max(0, emotion - 50)),
        "phase": phase,
        "temperature": emotion,
        "profitEffect": min(int(up_ratio * 100), 85),
        "riskScore": risk_score,
        "riskLevel": risk_level,
        "advice": advice,
        "hotSectors": hot_sectors[:5],
        "operationAdvice": op[:6],
    }


def _fetch_hot_sectors(lup, north, emotion):
    sectors = []
    if lup > 50:
        sectors.append({"name": "涨停潮", "driver": f"涨停{lup}家", "score": min(95, 60 + lup // 10)})
    if north > 30:
        sectors.append({"name": "北向加仓", "driver": f"净流入{north:.1f}亿", "score": min(85, 60 + int(north))})
    if emotion > 70:
        sectors.append({"name": "市场做多", "driver": "情绪高涨", "score": emotion})
    if not sectors:
        sectors = [{"name": "待观察", "driver": "无明显主线", "score": 40}]

    # 尝试从板块接口补充
    try:
        from app.market_cn.hot_sectors import get_hot_sectors
        data = get_hot_sectors()
        analysis = (data or {}).get("analysis", {})
        for s in (analysis.get("top_industry") or [])[:3]:
            sectors.append({
                "name": s.get("name", ""),
                "driver": f"{s.get('change_pct', 0):+.2f}%",
                "score": min(95, max(30, 50 + (s.get("change_pct", 0) or 0) * 10)),
            })
    except Exception:
        pass

    return sectors


def _build_advice(lup, ldn, broken, north, emotion):
    op = []
    if lup > 50:
        op.append(f"涨停{lup}家，赚钱效应极强，顺势而为")
    elif lup > 30:
        op.append(f"涨停{lup}家，市场赚钱效应较好")
    elif lup > 10:
        op.append(f"涨停{lup}家，赚钱效应一般，精选个股")
    else:
        op.append(f"仅涨停{lup}家，赚钱效应差，建议观望")

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

    return op


register(meta, fetch)
