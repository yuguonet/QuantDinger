# -*- coding: utf-8 -*-
"""股票实体解析器（金融领域特化）。"""
from __future__ import annotations

import logging
import re
from typing import Optional

from .base import EntityResolver, ResolveResult

logger = logging.getLogger(__name__)

# 中文分隔符
_SEPARATORS = re.compile(r'[和、，,\s]+')
# 股票相关动词（清理用）
_STOCK_VERBS = re.compile(r'分析|看看|查一下|怎么样|什么股|股票|推荐|选|买|卖|对比|比较')

# 口语填充词（2026-10-01 根因修复）：_STOCK_VERBS 只剔动词，而最口语的问法
# "帮我分析一下贵州茅台" 剔完"分析"剩 "帮我一下贵州茅台" —— 整段送 search_stocks
# ⇒ 零命中 ⇒ **名称解析在最常见问法上静默失效**：既不解析出标的，也不触发反问，
# 调用方拿到 None 当"没有标的"处理（旧系统这段从未上线，缺陷一直藏着）。
# 只在**片段级**剔除首尾填充词（不整句替换）：股票名不含这些虚词，误伤可忽略。
# ★ 交替项必须**长的在前**：正则交替是有序的，`请` 排在 `请问` 前面时只会吃掉
#   "请" 而留下 "问茅台"（又是一次静默失效）。
_FILLERS = re.compile(
    r'^(?:请问|帮我|麻烦|给我|我想|我要|看一下|看下|一下|看看|瞧瞧|请|来|再)+'
    r'|(?:多少钱|现价|现在|如今|今日|今天|最近|走势|行情|价格|报价|涨跌|涨了|跌了'
    r'|技术面|基本面|资金面|消息面|筹码面|财报|业绩|怎么样|如何'
    r'|的|了|呢|吗|吧|啊|呀|哦|哈)+$')


def _strip_fillers(fragment: str) -> str:
    """剔除片段首尾的口语填充词（可能需多轮：'帮我一下X' → '帮我' → '一下'）。"""
    prev = None
    cur = fragment
    for _ in range(3):          # 上限防病态输入；正常 1~2 轮收敛
        if cur == prev:
            break
        prev = cur
        cur = _FILLERS.sub('', cur).strip()
    return cur

# 消歧（2026-09-13）：名称模糊搜索取前 N 个候选以判定歧义。
# 原实现用 limit=1 调 resolve_stock —— DB 至多回 1 条，歧义在结构上不可见，
# 于是"静默取首个候选"，选错标的风险极高（整份分析作废，甚至据错误结论下单）。
# 现拿多候选，无法用精确同名收敛时反问用户（见 base.ResolveResult 澄清契约）。
_AMBIGUOUS_LIMIT = 5    # 拉取候选数（判定歧义用）
_MAX_SHOWN = 5          # 反问时最多列出几个候选


def _expand_stock_query(user_input: str, entities: list[dict]) -> str:
    """将股票分析指令扩写为完整指令。

    Args:
        user_input: 原始用户消息
        entities: 解析出的实体列表 [{code, name, type}, ...]

    Returns:
        扩写后的指令，如 "分析贵州茅台(600519): 帮我看看最近能不能买，周期：T+3，深度：标准"
    """
    if not entities:
        return user_input

    # 构建实体描述：名称(代码)
    entity_parts = []
    for e in entities:
        if e.get('name') and e.get('code'):
            entity_parts.append(f"{e['name']}({e['code']})")
        elif e.get('code'):
            entity_parts.append(e['code'])
    entity_desc = ",".join(entity_parts)

    # 注入实体信息
    expanded = user_input
    for e in entities:
        if e.get('name') and e.get('code'):
            if e['name'] in expanded:
                expanded = expanded.replace(e['name'], f"{e['name']}({e['code']})", 1)
            elif e['code'] in expanded:
                expanded = expanded.replace(e['code'], f"{e['name']}({e['code']})", 1)

    # 加默认分析参数
    default_params = "周期：T+3（T+1/T+3/1W/1M），深度：标准（简单/标准/深度）"
    return f"{expanded}，{default_params}"


# ── 代码类请求闸门（2026-10-16 新增）────────────────────────────────
# 实测事故：`用 read 工具读一下 app/agent/qd_config.py 的前 20 行…`
#   → 本解析器把片段 "20" 当标的名，`_candidates("20")` 命中 5 只 ⇒ 判"歧义"
#   → 直接反问用户（qd_service 的 clarify 分支 **不进 agent**、不跑预选、0 次工具调用）。
# 与本文档头部"只在有把握时才扩写"同一立场：**解析器不该对明显不是股票问题的输入表态**。
#
# ⚠ 刻意**不复用** preselect 的 `_CODE_HINT_RE`：那个太宽（含"脚本/代码/写个/改一下"），
#   会把「分析 600519 的代码实现」这类真股票请求误挡。这里只挡"**动文件**"的强信号。
_CODE_PATH_RE = re.compile(
    r"\b[\w\-./\\]+\.(?:py|js|ts|tsx|jsx|vue|md|json|ya?ml|toml|ini|cfg|sql|sh|ps1|bat)\b"
    r"|(?:backend_api_python|QuantDinger-Vue|app/agent|app[\\/]routes)"
    r"|tests?[\\/][\w\-]+\.py",
    re.I,
)
# 代码动作词 × 文件/脚本类词：两者**同时**出现才算（避免"帮我写首诗"这类误伤）
_CODE_ACTION_RE = re.compile(
    r"(?:读|看|改|写|重构|重写|调试|跑|执行|迁移|移动|删除|新建|创建|加|实现|优化|修复|"
    r"排查|定位|审计|梳理|整理|落地|接入|接线|收口|补)"
    r"|\b(?:read|edit|write|refactor|rewrite|debug|run|move|rename|fix|install|build|"
    r"compile|lint|migrate|wire)\b",
    re.I,
)
_CODE_OBJECT_RE = re.compile(
    r"(?:文件|目录|脚本|模块|函数|代码|源码|工程|仓库|埋点|依赖|报错|堆栈|逻辑|入参|"
    r"调用链|测试|用例|配置)"
    r"|\b(?:file|dir|docs?|module|function|code|script|repo|patch|diff|logic|test)\b",
    re.I,
)
# 裸代码信号：无需与动作词/对象词搭配，命中即视为代码类请求。
# （实测补："npm install 之后 build 失败" 只有工具链名与失败语义，组合判据不成立。）
_RAW_CODE_RE = re.compile(
    r"(?:编译|构建|打包|类型检查|lint|单测|单元测试)\s*(?:失败|报错|不通过|挂了|过了)"
    r"|(?:报错|报了个错|异常|崩溃|堆栈|traceback|stack ?trace)"
    r"|\b(?:npm|pnpm|yarn|npx|pytest|pip|conda|docker|git|ruff|mypy|flake8|black|isort)\b"
    r"|\b(?:syntaxerror|importerror|modulenotfounderror|typeerror|valueerror|keyerror|"
    r"attributeerror|nameerror|indexerror|runtimeerror|zerodivisionerror)\b"
    r"|\btests?/[\w\-]+\.py\b",
    re.I,
)

# 显式 6 位标的代码（用户给了标的 ⇒ 豁免闸门，正常解析）
_STOCK_CODE_EXPLICIT_RE = re.compile(r"(?<!\d)\d{6}(?!\d)")


def _looks_like_code_request(text: str) -> bool:
    """是否"动文件"的代码类请求（⇒ 实体解析应让路）。

    豁免：文本里出现**显式 6 位标的代码**时不挡 —— 那说明用户确实在谈某只股票
    （如「分析 600519 的代码实现」），解析器该正常工作。
    """
    s = str(text or "")
    if not s.strip():
        return False
    if _STOCK_CODE_EXPLICIT_RE.search(s):
        return False
    if _CODE_PATH_RE.search(s):
        return True
    if _RAW_CODE_RE.search(s):
        return True
    return bool(_CODE_ACTION_RE.search(s) and _CODE_OBJECT_RE.search(s))


class StockResolver(EntityResolver):
    """股票实体解析器。

    解析用户输入中的股票代码或名称，支持多股：
      - "分析300129" → entities=[{code: "300129"}]
      - "分析南威软件和雪天盐业" → entities=[{code: "603636"}, {code: "600929"}]
      - effective_input 含标准分析指令

    无法唯一确定时**反问**而非猜（2026-09-13）：
      - "分析平安" → 匹配多只（中国平安/平安银行…）→ clarify_question，要求用户确认
      - "分析万科A" → 精确同名收敛 → 正常解析
    6 位代码天然唯一，不触发反问。
    """

    def resolve(self, user_input: str) -> Optional[ResolveResult]:
        try:
            from app.utils.basicinfo_db import get_stock_basic_db
        except ImportError:
            return None
        # 直接走底层 canonical 实现 basicinfo_db.search_stocks（resolve_stock 工具即其封装）。
        # 不再 import 工具包装层，避免破坏拔插式单一真相源。
        # akshare 等数据源为同步 DB/HTTP，在 async chat_node 中直接调用会阻塞事件循环
        #（同一 worker 的其他会话全部卡住，审计 P2）。包一层线程隔离。

        def _search(keyword: str, limit: int):
            return get_stock_basic_db().search_stocks(keyword, limit=limit)

        def _resolve_async(keyword: str, limit: int = 1):
            import concurrent.futures as _cf
            with _cf.ThreadPoolExecutor(max_workers=1) as _pool:
                return _pool.submit(_search, keyword, limit).result(timeout=10)

        def _candidates(keyword: str) -> list:
            """名称模糊搜索的候选列表（空=未匹配）。长度>1 表示无法唯一确定。"""
            try:
                rows = _resolve_async(keyword, limit=_AMBIGUOUS_LIMIT)
            except Exception:
                return []
            if not isinstance(rows, list):
                return []
            return [{"code": r.get("symbol"), "name": r.get("name", "")}
                    for r in rows if r.get("symbol")]

        entities = []

        # 1. 提取所有6位股票代码（前后不能紧跟数字）
        codes = re.findall(r'(?<!\d)(\d{6})(?!\d)', user_input)
        for code in codes:
            # 查询代码对应的名称
            name = ""
            try:
                rows = _resolve_async(code, limit=1)
                if isinstance(rows, list) and rows:
                    name = rows[0].get("name", "")
            except Exception:
                pass
            entities.append({"code": code, "name": name, "type": "stock"})

        # 2. 提取中文股票名称（去掉动词后按分隔符拆分）
        clean_input = _STOCK_VERBS.sub('', user_input).strip()
        for code in codes:
            clean_input = clean_input.replace(code, '')
        clean_input = clean_input.strip()

        # 【2026-10-16】代码类请求让路：本解析器不该对"动文件"的请求表态
        # （否则片段里的数字会被当成标的候选 ⇒ 歧义反问 ⇒ 整轮不进执行）。
        if _looks_like_code_request(user_input):
            logger.info("[StockResolver] 代码类请求，跳过实体解析: %s",
                        str(user_input)[:60])
            return None

        ambiguous = []      # [(关键词, [候选, ...]), ...] 无法唯一确定 → 反问
        if clean_input:
            raw_names = [n.strip() for n in _SEPARATORS.split(clean_input)
                         if n.strip() and len(n.strip()) >= 2]
            # 片段先去口语填充词再去查：命中率的关键（见 _FILLERS 注释）。
            # 清洗后变短于 2 字（如"好"）即丢弃——那本来就不是标的名。
            names, _seen_n = [], set()
            for _rn in raw_names:
                _sn = _strip_fillers(_rn)
                if len(_sn) >= 2 and _sn not in _seen_n:
                    _seen_n.add(_sn)
                    names.append(_sn)
            for name in names:
                if name in [e['code'] for e in entities]:
                    continue
                try:
                    cands = _candidates(name)
                except Exception as e:
                    logger.debug("[StockResolver] 解析 '%s' 跳过: %s", name, e)
                    continue
                if not cands:
                    continue
                if len(cands) > 1:
                    # 精确同名优先收敛：只有一个候选与用户原词完全一致时不算歧义
                    # （如"万科A"这类完整名，避免被模糊匹配拖进无谓反问）
                    exact = [c for c in cands if c.get('name') == name]
                    if len(exact) != 1:
                        ambiguous.append((name, cands))
                        continue
                    cands = exact
                entities.append({"code": cands[0]['code'],
                                 "name": cands[0].get('name', ''), "type": "stock"})

        # 去重
        seen = set()
        unique = []
        for e in entities:
            if e['code'] and e['code'] not in seen:
                seen.add(e['code'])
                unique.append(e)

        # 无法唯一确定标的 → 按通用澄清契约反问，不猜着执行（2026-09-13）。
        # 反问优先于"已解析出的部分实体"：信息不全就往下跑，结论可能整体作废。
        if ambiguous:
            parts = []
            for kw, cands in ambiguous:
                opts = "、".join(f"{c['name']}({c['code']})" for c in cands[:_MAX_SHOWN])
                more = f" 等 {len(cands)} 只" if len(cands) > _MAX_SHOWN else ""
                parts.append(f"「{kw}」匹配到 {len(cands)} 只{more}：{opts}")
            question = ("无法唯一确定要分析的标的，请确认：" + "；".join(parts)
                        + "。可直接回复股票代码。")
            logger.info("[StockResolver] 标的歧义反问: %s", question[:120])
            return ResolveResult(
                entities=[{"type": "entity_clarify", "question": question}],
                entity_code="", entity_name="", entity_type="entity_clarify",
                effective_input=f"{user_input} 【标的待澄清】{question}",
                clarify_question=question,
            )

        if not unique:
            return None

        # 构建结果
        entity_code = ",".join(e['code'] for e in unique)
        entity_name = ",".join(e['name'] for e in unique if e['name'])
        entity_type = "stock"

        # 扩写：注入实体信息 + 默认分析参数
        effective_input = _expand_stock_query(user_input, unique)

        return ResolveResult(
            entities=unique,
            entity_code=entity_code,
            entity_name=entity_name,
            entity_type=entity_type,
            effective_input=effective_input,
        )
