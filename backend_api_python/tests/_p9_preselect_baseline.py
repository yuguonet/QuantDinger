# -*- coding: utf-8 -*-
"""tool_preselect 行为基线：采集 / 比对（测试与 tmp 脚本共用同一份，不写两遍）。

★ 为什么要有它
`tool_preselect` 是**预选管线**：它的输出会直接决定模型这一轮能用哪些工具。
整理这类"逻辑混乱但行为正确"的模块，最大的风险不是写错，而是**悄悄改了
输出却不自知**——lint 多裁一个、补位少补一个，线上表现是"某类问题工具面
变窄"，很难归因到某次重构。

故采用「先固化 → 再改 → 再逐键对拍」：判据是**精确相等**，不是"看着差不多"。
返回值里除数值/名单还有中文告警文案（warnings），那些进 trace 被人肉归因，
容差守不住"文案被改掉"。

用法：
    python tests/_p9_preselect_baseline.py --save    # 生成/刷新基线
    python tests/_p9_preselect_baseline.py --check   # 对拍（不一致即退出码 1）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

BASELINE = Path(__file__).resolve().parent / "fixtures" / "p9_tool_preselect_baseline.json"

# ── 确定性输入（合成，不依赖注册表 / 网络 / 时间）──────────────────────
# 工具名取自真实注册表（保证 DATA_DOMAINS 词典命中真实），描述固定。
_DEFS = {
    name: {
        "function": {
            "name": name,
            "description": desc,
            "parameters": {"properties": props, "type": "object"},
        }
    }
    for name, desc, props in (
        ("get_realtime_quote", "获取股票实时行情报价，含现价涨跌幅成交量",
         {"code": {}, "market": {}}),
        ("agent_get_kline", "获取股票日线周线K线历史走势数据",
         {"code": {}, "period": {}, "limit": {}}),
        ("technical_analysis", "技术面综合分析：均线 MACD KDJ 趋势形态指标",
         {"code": {}, "indicators": {}}),
        ("get_fund_flow", "获取个股资金流主力净流入大单超大单数据",
         {"code": {}, "days": {}}),
        ("get_sector_board", "获取板块题材概念行业列表与涨幅", {"type": {}}),
        ("get_sector_fund_flow", "获取板块资金流净额排行", {"limit": {}}),
        ("search_stocks", "按条件选股筛选强势涨停榜单排行", {"filters": {}}),
        ("batch_valuation_compare", "批量估值对比市盈率PE市净率PB财报业绩",
         {"codes": {}}),
        ("get_chip_distribution", "筹码分布成本套牢获利盘集中度", {"code": {}}),
        ("get_dragon_tiger", "龙虎榜席位游资机构专用明细", {"code": {}}),
        ("get_capital_summary", "账户可用资金持仓仓位市值盈亏", {}),
        ("search_intel", "搜索新闻公告研报消息舆情政策", {"query": {}}),
        ("search_knowledge", "检索知识库文档资料笔记", {"query": {}}),
        ("get_index_quote", "获取大盘指数上证深证创业板实时报价", {"code": {}}),
        ("get_market_overview", "两市大盘概览涨跌统计", {}),
        ("get_order_book", "获取盘口买卖五档成交量量比换手", {"code": {}}),
    )
}
NAMES = sorted(_DEFS)

_QUERIES = {
    "quote_kline": "帮我看看 600519 的行情和最近走势",
    "fund_sector": "今天主力资金流入哪些板块",
    "valuation": "对比这几只股票的估值和财报业绩",
    "screen": "帮我选几只强势涨停的股票",
    "weather": "今天北京天气怎么样",
    "empty": "",
    "code_only": "000001",
    "knowledge": "查一下咱们知识库里之前的记录",
}

# 预选返回的原始文本（覆盖容错链各条路径）
_RAW = {
    "json_plan": '{"goal":"查看贵州茅台行情","deliverables":["现价","涨跌幅"],'
                 '"tools":["get_realtime_quote","agent_get_kline"]}',
    "fenced": '```json\n{"goal":"资金流","deliverables":["主力净额"],'
              '"tools":["get_fund_flow"]}\n```',
    "bare_array": '["get_realtime_quote", "technical_analysis"]',
    "dict_no_tools": '{"goal":"看看大盘","deliverables":["指数"]}',
    "dict_alt_key": '{"selected":["get_index_quote"],"goal":"大盘"}',
    "garbage": "我觉得这个问题不需要工具",
    "hallucinated": '{"goal":"行情","tools":["get_realtime_quote","no_such_tool"]}',
    # 模型把工具名写进了 goal 正文、却没给 tools 字段（容错链应当兜住）
    "dict_names_in_goal": '{"goal":"用 get_realtime_quote 查现价和走势",'
                          '"deliverables":["现价"]}',
    # 模型**显式**声明空工具面 ⇒ 必须尊重，不得从正文里捞名字硬塞
    "explicit_empty_tools": '{"goal":"纯聊天","deliverables":[],"tools":[]}',
}

_PLAN_TEXT = "查看贵州茅台最新行情 现价 涨跌幅 成交量"


def _jsonable(o):
    """把任意输出压成可 JSON 序列化的稳定结构（集合/元组 → 排序后列表）。"""
    if isinstance(o, dict):
        return {str(k): _jsonable(o[k]) for k in sorted(o, key=str)}
    if isinstance(o, (set, frozenset, tuple, list)):
        items = [_jsonable(x) for x in o]
        try:
            return sorted(items, key=str)
        except TypeError:
            return items
    if isinstance(o, (str, int, float, bool)) or o is None:
        return o
    return str(o)


def collect() -> dict:
    """采集当前实现的全部可观测输出。"""
    from app.agent.preselect import tool_preselect as T

    out: dict = {}

    # ① 词典与域识别
    out["domains"] = {q: T.detect_domains(t) for q, t in _QUERIES.items()}
    out["has_hint"] = {q: T.has_domain_hint(t) for q, t in _QUERIES.items()}
    out["names_in_text"] = {q: T.names_in_text(t, NAMES) for q, t in _QUERIES.items()}
    out["check_domain_dict"] = T.check_domain_dict(NAMES)
    out["preferred"] = {d: T.DATA_DOMAIN_TOOLS[d][0] for d in T.DATA_DOMAIN_TOOLS}

    # ② 索引（低置信兜底）
    idx = T.build_tool_index(_DEFS)
    out["index"] = {tk: sorted(v) for tk, v in sorted(idx.items())}
    out["index_cached_identity"] = T.get_tool_index(_DEFS) is T.get_tool_index(_DEFS)
    out["index_candidates"] = {
        q: T.index_candidates(t, idx, NAMES) for q, t in _QUERIES.items()
    }

    # ③ 目录
    cat, kept = T.build_catalog(_DEFS)
    out["catalog"] = cat
    out["catalog_kept"] = kept
    grouped, gkept = T.build_catalog_grouped(_DEFS, NAMES[:6], NAMES[6:8])
    out["catalog_grouped"] = grouped
    out["catalog_grouped_kept"] = gkept
    g2, k2 = T.build_catalog_grouped(_DEFS, NAMES[:6], [])
    out["catalog_grouped_no_cap"] = g2
    out["catalog_grouped_no_cap_kept"] = k2

    # ④ 提示词
    out["messages"] = {
        q: T.build_preselect_messages(t, cat, 5, already=["search_tools"],
                                      history_hint="低权重: get_order_book")
        for q, t in _QUERIES.items()
    }
    out["messages_no_hist"] = T.build_preselect_messages("测试", cat, 3)

    # ⑤ 解析（容错链）
    out["parse_selection"] = {k: T.parse_selection(v, NAMES, 5) for k, v in _RAW.items()}
    out["parse_plan"] = {k: T.parse_plan(v, NAMES, 5) for k, v in _RAW.items()}

    # ⑥ lint（四条规则各覆盖一遍）
    lints = {}
    cases = {
        "normal": ("帮我看看 600519 的行情", ["get_realtime_quote", "search_intel"],
                   _PLAN_TEXT, True, (), ()),
        "weather_all_irrelevant": ("今天北京天气怎么样",
                                   ["get_realtime_quote", "technical_analysis",
                                    "get_fund_flow", "get_dragon_tiger"],
                                   "查询北京今日天气", True, (), ()),
        "unparsed_no_cut": ("帮我看看 600519 的行情", ["get_realtime_quote", "search_intel"],
                            "", False, (), ()),
        "cap_yield": ("资金流和主力", ["get_fund_flow"], "主力资金流",
                      True, ("get_fund_flow",), ("get_fund_flow", "get_sector_fund_flow")),
        "protected": ("行情", ["get_realtime_quote", "get_order_book"], "无关描述",
                      True, ("get_realtime_quote", "get_order_book"), ()),
        "dict_gap": ("龙虎榜", ["get_dragon_tiger"], "龙虎榜席位", True, (), ()),
    }
    for name, (q, sel, plan_txt, parsed, caps, doms) in cases.items():
        rep = T.lint_selection(
            q, sel, available_names=NAMES, index=idx, plan_text=plan_txt,
            protected_names=(), parsed=parsed, capability_names=caps,
            domain_names=doms or NAMES,
        )
        lints[name] = {
            "trace": rep.to_trace(),
            "applied": T.apply_lint(list(sel), rep, capability_names=caps),
            "dirty": rep.dirty,
        }
    out["lint"] = lints

    # ⑦ 域兜底
    out["fallback"] = {
        q: T.fallback_domain_tools(t, NAMES) for q, t in _QUERIES.items()
    }
    out["fallback_limit"] = T.fallback_domain_tools("行情和资金流和板块", NAMES, limit=2)

    # ⑧ 能力层接口
    out["capability_domain"] = T.capability_domain()
    out["near_dup"] = {
        f"{a}|{b}": T._near_dup(a, b)
        for a, b in (("get_fund_flow", "get_fund_flow"), ("get_fund_flow", "get_index_quote"),
                     ("a", "b"))
    }
    out["module"] = T.__name__
    # 迁出 tools/ 自发现目录后 `_NOT_TOOLS` 名单整体作废（不再被扫描）
    out["not_tools"] = sorted(getattr(T, "_NOT_TOOLS", ()))
    return _jsonable(out)


def diff(cur: dict, base: dict, path: str = "") -> list:
    """逐键比对，返回差异描述列表（空 = 完全一致）。"""
    if isinstance(base, dict):
        if not isinstance(cur, dict):
            return [f"{path}: 类型变了 {type(base).__name__} → {type(cur).__name__}"]
        bad = []
        for k in sorted(set(base) | set(cur), key=str):
            sub = f"{path}.{k}" if path else str(k)
            if k not in base:
                bad.append(f"{sub}: 多出（基线里没有）")
            elif k not in cur:
                bad.append(f"{sub}: 缺失（基线里有）")
            else:
                bad += diff(cur[k], base[k], sub)
        return bad
    if isinstance(base, list):
        if not isinstance(cur, list) or len(cur) != len(base):
            return [f"{path}: 列表变了\n     基线={base}\n     当前={cur}"]
        bad = []
        for i, (b, c) in enumerate(zip(base, cur)):
            bad += diff(c, b, f"{path}[{i}]")
        return bad
    if base != cur:
        return [f"{path}: {base!r} → {cur!r}"]
    return []


def load_baseline() -> dict:
    return json.loads(BASELINE.read_text(encoding="utf-8"))


def main() -> int:
    args = set(sys.argv[1:])
    if "--save" in args:
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        BASELINE.write_text(json.dumps(collect(), ensure_ascii=False, indent=1,
                                       sort_keys=True), encoding="utf-8")
        print(f"基线已写入 {BASELINE}")
        return 0
    cur = collect()
    base = load_baseline()
    bad = diff(cur, base)
    if bad:
        print(f"对拍不一致 {len(bad)} 处：")
        for x in bad[:40]:
            print("  -", x)
        return 1
    print(f"对拍一致：{len(json.dumps(cur, ensure_ascii=False))} 字符，0 处差异")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
