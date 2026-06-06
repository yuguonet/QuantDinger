"""
个股财务 / F10 / 除权除息 — mootdx 主源

功能:
  1. 财务摘要   — 总资产/净资产/营收/净利润/每股净资产/股东人数等核心指标
  2. F10 信息   — 公司概况/财务分析/股东研究/股本结构/研报/龙虎榜等 15 个栏目
  3. 除权除息   — 分红/送转/配股历史记录

数据源:
  全部通过 mootdx(TCP) 从通达信服务器拉取，无备用数据源。
  通达信 F10 数据来源于各上市公司公告，数据权威性高。

依赖: pip install mootdx
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from app.utils.logger import get_logger

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════
#  mootdx 客户端管理（独立单例，与 index/tape 模块互不干扰）
# ══════════════════════════════════════════════════════════════

_client = None       # mootdx Quotes 实例
_client_ts = 0       # 上次连接成功的时间戳
_CLIENT_TTL = 3600   # 连接有效期: 1小时


def _get_client():
    """获取 mootdx 客户端单例（finance 模块专用）。

    Returns:
        mootdx.quotes.Quotes 实例，或 None（连接失败时）
    """
    global _client, _client_ts

    # 检查现有连接: 未过期 + 未关闭 → 复用
    if _client is not None and (time.time() - _client_ts) < _CLIENT_TTL:
        try:
            if not _client.closed:
                return _client
        except Exception:
            pass
        _client = None

    # 创建新连接
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
    """股票代码 → 通达信市场号。

    通达信市场编码:
      1 = 上海证券交易所（60xxxx 开头的股票）
      0 = 深圳证券交易所（00xxxx/30xxxx 开头的股票）

    Args:
        code: 6位股票代码

    Returns:
        0 或 1（通达信市场号）
    """
    return 1 if code[:3] in ("000", "88", "99") else 0


# ══════════════════════════════════════════════════════════════
#  1. 财务摘要
# ══════════════════════════════════════════════════════════════

def get_finance(code: str) -> Dict[str, Any]:
    """获取个股基础财务数据（通达信 get_finance_info 接口）

    返回通达信内置的财务摘要数据，包含基本面核心指标。
    数据来源于上市公司定期报告（年报/半年报/季报）。

    常用字段说明:
      zongguben         总股本（万股）— 公司发行的全部股份
      liutongguben      流通股本（万股）— 可在二级市场交易的股份
      zongzichan        总资产（万元）— 公司拥有的全部资产
      jingzichan        净资产/股东权益（万元）— 总资产 - 总负债
      zhuyingshouru     主营收入（万元）— 核心业务收入
      zhuyinglirun      主营利润（万元）— 核心业务利润
      yingyelirun       营业利润（万元）— 含投资收益等
      jinglirun         净利润（万元）— 最终利润
      meigujingzichan   每股净资产（元）— 净资产 / 总股本
      gudongrenshu      股东人数 — 持股股东总数（筹码集中度参考）
      liudongzichan     流动资产（万元）— 一年内可变现的资产
      gudingzichan      固定资产（万元）— 厂房设备等长期资产
      liudongfuzhai     流动负债（万元）— 一年内需偿还的债务
      changqifuzhai     长期负债（万元）— 超过一年的债务
      jingyingxianjinliu 经营现金流（万元）— 日常经营产生的现金
      zongxianjinliu    总现金流（万元）— 含投资/筹资现金流
      weifenpeilirun    未分配利润（万元）— 累计留存利润
      ipo_date          上市日期
      updated_date      数据更新日期

    Args:
        code: 股票代码，如 "600519"

    Returns:
        成功: {code, market, zongguben, liutongguben, ...}（字段见上方说明）
        失败: {code, error: "..."}

    Example:
        >>> fin = get_finance("600519")
        >>> print(f"净资产: {fin.get('jingzichan', 0)} 万元")
        >>> print(f"每股净资产: {fin.get('meigujingzichan', 0)} 元")
    """
    cli = _get_client()
    if cli is None:
        return {"code": code, "error": "mootdx 不可用"}

    try:
        mkt = _market(code)
        # get_finance_info: 通达信财务数据接口
        # 返回 list of dict 或单个 dict
        result = cli.client.get_finance_info(mkt, code)
        if not result:
            return {"code": code, "error": "无数据"}

        # 兼容返回格式: list 或 dict
        r = result[0] if isinstance(result, list) and result else result
        if isinstance(r, dict):
            r["code"] = code
            return r

        return {"code": code, "error": "数据格式异常"}
    except Exception as e:
        logger.warning("[mootdx] 财务数据失败(%s): %s", code, e)
        return {"code": code, "error": str(e)}


# ══════════════════════════════════════════════════════════════
#  2. F10 信息（公司资料大全）
# ══════════════════════════════════════════════════════════════

# F10 栏目名称 → 中文描述
# F10 是通达信的经典功能，汇集了上市公司全方位信息
F10_CATEGORIES = {
    "最新提示": "最新公告提示、风险提示、业绩预告等重要通知",
    "公司概况": "公司简介、主营业务、注册地址、法人代表、联系方式",
    "财务分析": "三大报表（利润表/资产负债表/现金流量表）关键指标、同比环比",
    "股东研究": "十大股东、十大流通股东、股东人数变化趋势",
    "股本结构": "总股本/流通股/限售股构成、股本变动历史",
    "资本运作": "并购重组、增发配股、回购计划、股权激励",
    "业内点评": "券商研报评级、目标价、盈利预测",
    "行业分析": "行业景气度、竞争格局、上下游关系",
    "公司大事": "重大事项、诉讼仲裁、对外担保、关联交易",
    "研究报告": "机构研报摘要、深度报告核心观点",
    "经营分析": "主营构成、各业务毛利率、产能利用率、业务展望",
    "主力追踪": "机构持仓、基金持仓变化、北向资金持仓",
    "分红扩股": "历史分红送转记录、扩股方案、派息日",
    "高层治理": "董事监事高管信息、薪酬、持股变动",
    "龙虎榜单": "上榜记录、买卖席位明细、游资动向",
}


def get_f10_categories(code: str) -> List[Dict[str, Any]]:
    """获取 F10 信息的栏目列表。

    先查询有哪些栏目可用，再按需拉取具体内容。
    不同板块（主板/创业板/科创板）的 F10 栏目可能略有差异。

    Args:
        code: 股票代码，如 "600519"

    Returns:
        成功: [{name, filename, start, length, description}, ...]
              name: 栏目名称（如 "公司概况"）
              filename: 通达信内部文件名（用于拉取内容）
              start: 内容在文件中的起始偏移量
              length: 内容长度（字节）
              description: 栏目中文描述
        失败: []（mootdx 不可用或无数据）

    Example:
        >>> cats = get_f10_categories("600519")
        >>> for c in cats:
        ...     print(f"{c['name']}: {c['description']}")
    """
    cli = _get_client()
    if cli is None:
        return []

    try:
        mkt = _market(code)
        # get_company_info_category: 获取 F10 栏目列表
        cats = cli.client.get_company_info_category(mkt, code)
        if not cats:
            return []

        out = []
        for item in cats:
            name = item.get("name", "")
            out.append({
                "name": name,
                "filename": item.get("filename", ""),  # 通达信内部文件名
                "start": item.get("start", 0),          # 内容起始偏移
                "length": item.get("length", 0),        # 内容字节数
                "description": F10_CATEGORIES.get(name, ""),
            })
        return out
    except Exception as e:
        logger.warning("[mootdx] F10 栏目失败(%s): %s", code, e)
        return []


def get_f10_content(code: str, category: str = "", max_length: int = 50000) -> Dict[str, Any]:
    """获取 F10 指定栏目的详细内容。

    通过通达信的文件系统接口读取 F10 原始文本数据。
    数据以 GBK 编码存储，需要解码为 Unicode。

    Args:
        code: 股票代码，如 "600519"
        category: 栏目名称，如 "公司概况"、"财务分析"。
                  为空字符串时返回栏目列表（不读取内容）。
        max_length: 单栏目最大读取字节数，默认 50000（约 50KB）。
                    防止超大栏目（如财务分析）一次性读取过多数据。

    Returns:
        指定栏目时: {
            code, category, description,
            content: "原始文本内容...",
            total_length: 12345,  # 栏目总字节数
            read_length: 50000,   # 实际读取字节数
        }
        未指定栏目时: {code, categories: [{name, description}, ...]}
        失败: {code, error: "..."}

    Example:
        >>> result = get_f10_content("600519", "公司概况")
        >>> print(result["content"][:500])  # 打印前500字
    """
    cli = _get_client()
    if cli is None:
        return {"code": code, "error": "mootdx 不可用"}

    try:
        mkt = _market(code)
        cats = cli.client.get_company_info_category(mkt, code)
        if not cats:
            return {"code": code, "error": "无 F10 数据"}

        # 未指定栏目 → 返回栏目列表
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

        # 查找目标栏目
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

        # 读取栏目内容
        # get_company_info_content: 通达信文件读取接口
        # 参数: 市场号, 代码, 文件名, 起始偏移, 读取长度
        read_len = min(target["length"], max_length)
        content = cli.client.get_company_info_content(
            mkt, code, target["filename"], target["start"], read_len
        )

        # 解码: 通达信 F10 数据以 GBK 编码存储
        text = ""
        if content:
            if isinstance(content, bytes):
                text = content.decode("gbk", errors="ignore")
            elif isinstance(content, str):
                text = content
            elif isinstance(content, list):
                # 某些 mootdx 版本返回 list of dict
                text = "".join(
                    item.get("content", "") if isinstance(item, dict) else str(item)
                    for item in content
                )

        return {
            "code": code,
            "category": category,
            "description": F10_CATEGORIES.get(category, ""),
            "content": text.strip(),
            "total_length": target["length"],  # 栏目总大小
            "read_length": read_len,           # 实际读取大小
        }
    except Exception as e:
        logger.warning("[mootdx] F10 内容失败(%s, %s): %s", code, category, e)
        return {"code": code, "error": str(e)}


def get_f10_all(code: str, max_per_category: int = 20000) -> Dict[str, Any]:
    """获取 F10 全部栏目的内容摘要（每栏目截取前 N 字节）。

    一次性拉取所有栏目，适合快速浏览公司全貌。
    每个栏目截取前 max_per_category 字节，避免数据量过大。

    Args:
        code: 股票代码
        max_per_category: 每个栏目最大读取字节数，默认 20000（约 20KB）。

    Returns:
        成功: {code, sections: [{name, description, content}, ...]}
        失败: {code, error: "..."}

    Example:
        >>> all_info = get_f10_all("600519")
        >>> for sec in all_info["sections"]:
        ...     print(f"=== {sec['name']} ===")
        ...     print(sec["content"][:200])
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
                # 单个栏目读取失败不影响其他栏目
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
    """获取个股除权除息历史记录。

    除权除息是上市公司利润分配的方式:
      - 除息: 现金分红（派息），股价下调对应金额
      - 除权: 送股/转增/配股，股价按比例下调

    category 字段含义（通达信编码）:
      1 = 除权除息日 — 最常用的，表示分红/送转/配股生效日
      2 = 送配股上市日 — 送转的股份可以上市交易的日期
      3 = 非流通股上市日 — 限售股解禁上市
      4 = 未知股本变动 — 未分类的股本变化
      5 = 股本变化 — 其他股本变动（如增发）
      6 = 增发新股 — 定向增发或公开增发
      7 = 股份回购 — 公司回购注销股份
      8 = 高管持股变动 — 董监高增减持

    常用计算:
      每股分红 = fenhong / 10  （每10股派息 fenhong 元）
      送转比例 = songzhuangu / 10  （每10股送转 songzhuangu 股）
      配股比例 = peigu / 10  （每10股配 peigu 股）

    Args:
        code: 股票代码，如 "600519"

    Returns:
        成功: [{
            year: 2024, month: 6, day: 28,
            date: "2024-06-28",       # 格式化日期
            category: 1,              # 类型编码（见上方说明）
            name: "除权除息日",        # 类型名称
            fenhong: 30.87,           # 每10股派息（元）
            songzhuangu: 0,           # 每10股送转（股）
            peigu: 0,                 # 每10股配股（股）
            peigujia: 0,              # 配股价（元）
            suogu: None,              # 缩股比例
            panqianliutong: None,     # 盘前流通股本
            panhouliutong: None,      # 盘后流通股本
            qianzongguben: None,      # 前总股本
            houzongguben: None,       # 后总股本
        }, ...]
        失败: []（mootdx 不可用或无数据）

    Example:
        >>> divs = get_dividend("600519")
        >>> for d in divs:
        ...     if d["fenhong"] and d["category"] == 1:
        ...         print(f"{d['date']}: 每10股派{d['fenhong']}元")
    """
    cli = _get_client()
    if cli is None:
        return []

    try:
        mkt = _market(code)
        # get_xdxr_info: 通达信除权除息数据接口
        # xdxr = 除权除息的拼音缩写
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
                # 拼接格式化日期，补零对齐
                "date": f"{item.get('year')}-{item.get('month', 0):02d}-{item.get('day', 0):02d}",
                "category": item.get("category"),         # 类型编码（1-8）
                "name": item.get("name", ""),              # 类型名称
                "fenhong": item.get("fenhong"),            # 每10股派息（元）
                "songzhuangu": item.get("songzhuangu"),    # 每10股送转（股）
                "peigu": item.get("peigu"),                # 每10股配股（股）
                "peigujia": item.get("peigujia"),          # 配股价（元）
                "suogu": item.get("suogu"),                # 缩股比例
                "panqianliutong": item.get("panqianliutong"),  # 变动前流通股本
                "panhouliutong": item.get("panhouliutong"),    # 变动后流通股本
                "qianzongguben": item.get("qianzongguben"),    # 变动前总股本
                "houzongguben": item.get("houzongguben"),      # 变动后总股本
            })
        return out
    except Exception as e:
        logger.warning("[mootdx] 除权除息失败(%s): %s", code, e)
        return []
