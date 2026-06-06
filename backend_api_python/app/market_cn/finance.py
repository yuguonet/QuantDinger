"""
个股财务 / F10 / 除权除息 — mootdx 主源

功能:
  1. 财务摘要   — 总资产/净资产/营收/净利润/每股净资产/股东人数等
  2. F10 信息   — 公司概况/财务分析/股东研究/股本结构/研报/龙虎榜等 15 个栏目
  3. 除权除息   — 分红/送转/配股历史记录

依赖: pip install mootdx
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from app.utils.logger import get_logger

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════
#  mootdx 客户端
# ══════════════════════════════════════════════════════════════

_client = None
_client_ts = 0
_CLIENT_TTL = 3600


def _get_client():
    global _client, _client_ts
    if _client is not None and (time.time() - _client_ts) < _CLIENT_TTL:
        try:
            if not _client.closed:
                return _client
        except Exception:
            pass
        _client = None

    try:
        from mootdx.quotes import Quotes
        _client = Quotes.factory(market='std', timeout=10, heartbeat=True)
        _client_ts = time.time()
        logger.info("[mootdx:f10] 连接成功")
        return _client
    except Exception as e:
        logger.warning("[mootdx:f10] 连接失败: %s", e)
        _client = None
        return None


def _market(code: str) -> int:
    """代码 → 通达信市场号 (1=沪 0=深)。"""
    return 1 if code[:3] in ("000", "88", "99") else 0


# ══════════════════════════════════════════════════════════════
#  1. 财务摘要
# ══════════════════════════════════════════════════════════════

def get_finance(code: str) -> Dict[str, Any]:
    """获取个股基础财务数据

    Args:
        code: 股票代码，如 "600519"

    Returns:
        {
            code, market,
            zongguben,        # 总股本
            liutongguben,     # 流通股本
            zongzichan,       # 总资产
            jingzichan,       # 净资产 (股东权益)
            zhuyingshouru,    # 主营收入
            zhuyinglirun,     # 主营利润
            yingyelirun,      # 营业利润
            jinglirun,        # 净利润
            meigujingzichan,  # 每股净资产
            gudongrenshu,     # 股东人数
            liudongzichan,    # 流动资产
            gudingzichan,     # 固定资产
            liudongfuzhai,    # 流动负债
            changqifuzhai,    # 长期负债
            jingyingxianjinliu, # 经营现金流
            zongxianjinliu,   # 总现金流
            weifenpeilirun,   # 未分配利润
            ipo_date,         # 上市日期
            updated_date,     # 数据更新日期
            ...
        }
    """
    cli = _get_client()
    if cli is None:
        return {"code": code, "error": "mootdx 不可用"}

    try:
        mkt = _market(code)
        result = cli.client.get_finance_info(mkt, code)
        if not result:
            return {"code": code, "error": "无数据"}

        # result 是 list of dict 或单个 dict
        r = result[0] if isinstance(result, list) and result else result
        if isinstance(r, dict):
            r["code"] = code
            return r

        return {"code": code, "error": "数据格式异常"}
    except Exception as e:
        logger.warning("[mootdx] 财务数据失败(%s): %s", code, e)
        return {"code": code, "error": str(e)}


# ══════════════════════════════════════════════════════════════
#  2. F10 信息
# ══════════════════════════════════════════════════════════════

# F10 栏目 → 描述
F10_CATEGORIES = {
    "最新提示": "最新公告提示、风险提示",
    "公司概况": "公司简介、主营业务、注册地址",
    "财务分析": "三大报表关键指标、同比环比",
    "股东研究": "十大股东、十大流通股东、股东人数变化",
    "股本结构": "总股本/流通股/限售股构成",
    "资本运作": "并购重组、增发、回购",
    "业内点评": "券商研报评级、目标价",
    "行业分析": "行业景气度、竞争格局",
    "公司大事": "重大事项、诉讼、担保",
    "研究报告": "机构研报摘要",
    "经营分析": "主营构成、毛利率、业务展望",
    "主力追踪": "机构持仓、基金持仓变化",
    "分红扩股": "历史分红送转、扩股方案",
    "高层治理": "董事监事高管信息、薪酬",
    "龙虎榜单": "上榜记录、席位明细",
}


def get_f10_categories(code: str) -> List[Dict[str, Any]]:
    """获取 F10 信息栏目列表

    Args:
        code: 股票代码，如 "600519"

    Returns:
        [{name, filename, start, length, description}, ...]
    """
    cli = _get_client()
    if cli is None:
        return []

    try:
        mkt = _market(code)
        cats = cli.client.get_company_info_category(mkt, code)
        if not cats:
            return []

        out = []
        for item in cats:
            name = item.get("name", "")
            out.append({
                "name": name,
                "filename": item.get("filename", ""),
                "start": item.get("start", 0),
                "length": item.get("length", 0),
                "description": F10_CATEGORIES.get(name, ""),
            })
        return out
    except Exception as e:
        logger.warning("[mootdx] F10 栏目失败(%s): %s", code, e)
        return []


def get_f10_content(code: str, category: str = "", max_length: int = 50000) -> Dict[str, Any]:
    """获取 F10 指定栏目内容

    Args:
        code: 股票代码，如 "600519"
        category: 栏目名称，如 "公司概况"、"财务分析"。为空则返回全部栏目摘要
        max_length: 单栏目最大读取字节数，默认 50000

    Returns:
        {code, category, content, categories: [...]}
    """
    cli = _get_client()
    if cli is None:
        return {"code": code, "error": "mootdx 不可用"}

    try:
        mkt = _market(code)
        cats = cli.client.get_company_info_category(mkt, code)
        if not cats:
            return {"code": code, "error": "无 F10 数据"}

        # 不指定栏目 → 返回栏目列表
        if not category:
            return {
                "code": code,
                "categories": [
                    {
                        "name": c.get("name", ""),
                        "description": F10_CATEGORIES.get(c.get("name", ""), ""),
                    }
                    for c in cats
                ],
            }

        # 找到目标栏目
        target = None
        for c in cats:
            if c.get("name") == category:
                target = c
                break

        if target is None:
            return {
                "code": code,
                "error": f"未找到栏目: {category}",
                "available": [c.get("name", "") for c in cats],
            }

        # 读取内容
        read_len = min(target["length"], max_length)
        content = cli.client.get_company_info_content(
            mkt, code, target["filename"], target["start"], read_len
        )

        text = ""
        if content:
            if isinstance(content, bytes):
                text = content.decode("gbk", errors="ignore")
            elif isinstance(content, str):
                text = content
            elif isinstance(content, list):
                # 有些版本返回 list of dict
                text = "".join(
                    item.get("content", "") if isinstance(item, dict) else str(item)
                    for item in content
                )

        return {
            "code": code,
            "category": category,
            "description": F10_CATEGORIES.get(category, ""),
            "content": text.strip(),
            "total_length": target["length"],
            "read_length": read_len,
        }
    except Exception as e:
        logger.warning("[mootdx] F10 内容失败(%s, %s): %s", code, category, e)
        return {"code": code, "error": str(e)}


def get_f10_all(code: str, max_per_category: int = 20000) -> Dict[str, Any]:
    """获取 F10 全部栏目摘要（每栏目截取前 N 字节）

    Args:
        code: 股票代码
        max_per_category: 每个栏目最大字节数，默认 20000

    Returns:
        {code, sections: [{name, description, content}, ...]}
    """
    cli = _get_client()
    if cli is None:
        return {"code": code, "error": "mootdx 不可用"}

    try:
        mkt = _market(code)
        cats = cli.client.get_company_info_category(mkt, code)
        if not cats:
            return {"code": code, "error": "无 F10 数据"}

        sections = []
        for c in cats:
            name = c.get("name", "")
            read_len = min(c.get("length", 0), max_per_category)
            try:
                content = cli.client.get_company_info_content(
                    mkt, code, c["filename"], c["start"], read_len
                )
                text = ""
                if content:
                    if isinstance(content, bytes):
                        text = content.decode("gbk", errors="ignore")
                    elif isinstance(content, str):
                        text = content
                sections.append({
                    "name": name,
                    "description": F10_CATEGORIES.get(name, ""),
                    "content": text.strip(),
                })
            except Exception as e:
                sections.append({
                    "name": name,
                    "description": F10_CATEGORIES.get(name, ""),
                    "content": "",
                    "error": str(e),
                })

        return {"code": code, "sections": sections}
    except Exception as e:
        logger.warning("[mootdx] F10 全量失败(%s): %s", code, e)
        return {"code": code, "error": str(e)}


# ══════════════════════════════════════════════════════════════
#  3. 除权除息
# ══════════════════════════════════════════════════════════════

def get_dividend(code: str) -> List[Dict[str, Any]]:
    """获取个股除权除息历史记录

    Args:
        code: 股票代码，如 "600519"

    Returns:
        [{year, month, day, category, name, fenhong, songzhuangu, peigu, peigujia, ...}, ...]
        category: 1=除权除息, 2=送配股上市, 3=非流通股上市, 4=未知股本变动, 5=股本变化, ...
        fenhong:     每10股派息(元)
        songzhuangu: 每10股送转股数
        peigu:       每10股配股数
        peigujia:    配股价(元)
    """
    cli = _get_client()
    if cli is None:
        return []

    try:
        mkt = _market(code)
        result = cli.client.get_xdxr_info(mkt, code)
        if not result:
            return []

        out = []
        for item in result:
            if not isinstance(item, dict):
                continue
            out.append({
                "year": item.get("year"),
                "month": item.get("month"),
                "day": item.get("day"),
                "date": f"{item.get('year')}-{item.get('month', 0):02d}-{item.get('day', 0):02d}",
                "category": item.get("category"),
                "name": item.get("name", ""),
                "fenhong": item.get("fenhong"),           # 每10股派息
                "songzhuangu": item.get("songzhuangu"),   # 每10股送转
                "peigu": item.get("peigu"),               # 每10股配股
                "peigujia": item.get("peigujia"),         # 配股价
                "suogu": item.get("suogu"),
                "panqianliutong": item.get("panqianliutong"),
                "panhouliutong": item.get("panhouliutong"),
                "qianzongguben": item.get("qianzongguben"),
                "houzongguben": item.get("houzongguben"),
            })
        return out
    except Exception as e:
        logger.warning("[mootdx] 除权除息失败(%s): %s", code, e)
        return []
