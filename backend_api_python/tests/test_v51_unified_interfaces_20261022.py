# -*- coding: utf-8 -*-
"""v51 — 死代码删除 + 4 组冗余统一接口的契约守卫（2026-10-22）。

覆盖四个统一接口的行为契约（收敛前分散在各调用方，改一处不会红 ⇒ 固化）：
  ① `chain/_jsonl.read_jsonl`   —— JSONL 读：缺文件 []/空行跳/坏行跳/非 dict 行原样
  ② `tools/finance/_analysis_utils.classify_vol_price` —— 量价四分支阈值口径 + 四套标签逐字
  ③ `rag/_doc_item.doc_item`    —— content 500 截断 + metadata 信封 + extra 透传 + None 守卫
  ④ `tools/finance/analysis_tools._batch_run` —— 单股错误隔离 + count/data 信封 + supplement 钩子

另守两条删除侧断言：活代码零引用已删符号；`_batch_run` 是 analysis_tools 唯一批量循环。
"""
import ast
import io
import os
from typing import Optional

import pytest

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENT = os.path.join(BACKEND, "app", "agent")


# ── ① read_jsonl ────────────────────────────────────────────────

def test_read_jsonl_contract(tmp_path):
    from app.agent.chain._jsonl import read_jsonl
    p = tmp_path / "x.jsonl"
    p.write_text('{"a": 1}\n\n  \nnot-json\n{"b": 2}\n', encoding="utf-8")
    assert read_jsonl(p) == [{"a": 1}, {"b": 2}]
    assert read_jsonl(tmp_path / "missing.jsonl") == []


def test_read_jsonl_is_single_implementation():
    """chain 下不允许再出现第二份 JSONL（行循环）读取实现（三处收敛的唯一性守卫）。

    判据取「splitlines + json.loads」行循环形态；整文件 JSON 读
    （backtest_bridge/calib_feed 的 json.loads(read_text)）不是同一契约，不误伤。
    """
    dup = []
    for root, dirs, files in os.walk(os.path.join(AGENT, "chain")):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for f in files:
            if not f.endswith(".py") or f == "_jsonl.py":
                continue
            src = io.open(os.path.join(root, f), encoding="utf-8-sig").read()
            if "splitlines" in src and "json.loads" in src:
                dup.append(f)
    assert not dup, f"又出现了 JSONL 行循环读取实现: {dup}"


# ── ② classify_vol_price ────────────────────────────────────────

def test_classify_vol_price_thresholds():
    from app.agent.tools.finance._analysis_utils import classify_vol_price
    # 阈值口径唯一：放量 >1.3 / 缩量 <0.7 / 其余「一般」
    assert classify_vol_price(1.0, 1.5) == "量价齐升（健康上涨）"
    assert classify_vol_price(1.0, 0.5) == "缩量上涨（上涨乏力）"
    assert classify_vol_price(-1.0, 1.5) == "放量下跌（恐慌抛售）"
    assert classify_vol_price(-1.0, 0.5) == "缩量下跌（动能减弱）"
    assert classify_vol_price(1.0, 1.0) == "量价配合一般"
    assert classify_vol_price(0.0, 2.0) == "量价配合一般"  # 零涨跌不算放量上涨
    assert classify_vol_price(1.0, 1.3) == "量价配合一般"  # 边界：严格大于才放量
    assert classify_vol_price(1.0, 0.7) == "量价配合一般"  # 边界：严格小于才缩量


def test_classify_vol_price_label_sets_preserved():
    """四套标签常量逐字保留收敛前各站文案（改输出文案须单独拍板）。"""
    from app.agent.tools.finance._analysis_utils import (
        _VPR_LABELS, _VPR_LABELS_INTRADAY, _VPR_LABELS_BRIEF, _VPR_LABELS_ALERT)
    assert _VPR_LABELS["up_weak"] == "缩量上涨（上涨乏力）"
    assert _VPR_LABELS_INTRADAY["up_strong"] == "放量上涨（盘中）"
    assert _VPR_LABELS_BRIEF["down_strong"] == "放量下跌"
    assert _VPR_LABELS_ALERT["up_weak"] == "缩量上涨（上涨乏力，需警惕）"


# ── ③ doc_item ──────────────────────────────────────────────────

def test_doc_item_contract():
    import datetime
    from app.agent.rag._doc_item import doc_item
    d = datetime.datetime(2026, 10, 22, 8, 0, 0)
    item = doc_item(decision="买入", symbol="600519", summary="摘要",
                    created_at=d, source="analysis_memory")
    assert item["content"] == "买入 600519 摘要"
    assert item["metadata"] == {"source": "analysis_memory", "symbol": "600519",
                                "decision": "买入", "date": "2026-10-22 08:00:00"}
    # extra 透传（fts 的 score/retrieval_route）
    item2 = doc_item(decision=None, symbol=None, summary=None, created_at=None,
                     source="postgres_fts", score=3, retrieval_route="postgres_fts")
    assert item2["content"] == "  " and item2["metadata"]["date"] == ""
    assert item2["score"] == 3 and item2["retrieval_route"] == "postgres_fts"
    # 500 截断
    long_item = doc_item(decision="d", symbol="s", summary="x" * 800,
                         created_at=None, source="s")
    assert len(long_item["content"]) == 500


# ── ④ _batch_run ────────────────────────────────────────────────

def test_batch_run_error_isolation_and_envelope():
    from app.agent.tools.finance.analysis_tools import _batch_run

    def one(code):
        if code == "bad":
            raise ValueError("boom")
        return {"code": code}

    r = _batch_run(["a", "bad", "c"], one)
    assert r == {"count": 3, "data": {"a": {"code": "a"},
                                      "bad": {"error": "boom"},
                                      "c": {"code": "c"}}}


def test_batch_run_supplement_hook():
    from app.agent.tools.finance.analysis_tools import _batch_run

    def one(code):
        return {"code": code}

    def supp(code, res):
        if code == "a":
            res["tag"] = "supp"
        if code == "c":
            raise RuntimeError("supp-boom")
        return res

    r = _batch_run(["a", "b", "c"], one, supplement=supp)
    assert r["data"]["a"] == {"code": "a", "tag": "supp"}
    assert r["data"]["c"] == {"error": "supp-boom"}


def test_batch_run_is_only_batch_loop():
    """analysis_tools 内批量循环只允许存在于 _batch_run（防再次各写一遍）。"""
    src = io.open(os.path.join(AGENT, "tools", "finance", "analysis_tools.py"),
                  encoding="utf-8-sig").read()
    tree = ast.parse(src)
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.For) and "code_list" in ast.dump(node.iter):
            fn = None
            for parent in ast.walk(tree):
                if isinstance(parent, ast.FunctionDef) and any(
                        n is node for n in ast.walk(parent)):
                    fn = parent.name
                    break
            hits.append(fn)
    assert hits == ["_batch_run"], f"批量循环散落外部: {hits}"


# ── 删除侧：活代码零引用已删符号 ─────────────────────────────────

# (module, name) 精确制导——项目死代码硬流程：同名异物不算引用
# （context_frame._INDEX_CODE / resolver._env_flag / web_search_tools._format_output
#   是各自模块的活份，本守卫不得误伤）。
DELETED_BY_MODULE = {
    "app.agent.chain.store": ["query_latest_root", "query_latest_root_by_chain",
                              "mark_root_wrong", "mark_root_good", "delete_tree",
                              "get_penalty_count", "get_penalty_count_by_chain",
                              "get_chain_weights"],
    "app.agent.chain.schema": ["is_root", "is_leaf", "is_valid", "is_veto",
                               "get_skill_report", "to_skill_report"],
    "app.agent.chain.skill_brewer": ["_find_entity_codes"],
    "app.agent.cron.cron_worker": ["cron_matches", "_parse_cron_field"],
    "app.agent.cron.guards": ["seconds_delay_to_at"],
    "app.agent.chain.evaluator": ["stop_eval_worker"],
    "app.agent.utils.budget": ["panel_from_trace", "tokens_from_agent",
                               "tool_calls_from_agent"],
    "app.agent.agent": ["LLM_SEED", "_seed_env"],
    "app.agent.chain.claims": ["_RANGE_RE", "_env_flag"],
    "app.agent.chain.kline_feed": ["_INDEX_CODE"],
    "app.agent.domain_registry": ["specs", "get_spec"],
    "app.agent.flask_app": ["register_agent_routes", "create_agent_blueprint"],
    "app.agent.preselect.tool_preselect": ["clear_name_cache"],
    "app.agent.qd_service": ["_SCOPE_STRAT"],
    "app.agent.utils.grounding": ["collect_grounding_corpus", "check_claims"],
    "app.agent.utils.md_format": ["_format_final_md", "_lookup_stock_name",
                                  "_format_output"],
    "app.agent.utils.prescan": ["_return_hint", "prescan_skills"],
}
DELETED_NAMES = {n for names in DELETED_BY_MODULE.values() for n in names}


def _is_module(dotted: str) -> bool:
    """dotted 是否是仓库内真实模块（判 `from X import Y` 的 Y 是模块还是符号）。"""
    p = os.path.join(BACKEND, dotted.replace(".", os.sep))
    return os.path.exists(p + ".py") or os.path.isdir(p)


def _resolve_relative(file_module: str, level: int, module: Optional[str]) -> str:
    """相对 import 按**导入文件所在包**解析（ImportFrom.module 无前导点）。

    level=0 是绝对 import，module 即完整模块路径，不得拼文件包前缀
    （变异检验实测：拼了会把 tests._tmp 探针的 import 解析成
    tests.app.agent.chain.store ⇒ 守卫假绿，2026-10-22）。
    """
    if level == 0:
        return module or ""
    pkg = file_module.split(".")[:-1]  # 去掉文件名段
    if level > 1:
        pkg = pkg[:-(level - 1)] if len(pkg) >= level - 1 else []
    base = ".".join(pkg)
    if not module:
        return base
    # base 为空（相对 import 逃出包根，如 tests/_tmp_pkg 里 `from ...app.x import y`）
    # 不得拼出前导点——变异检验实测漏判，2026-10-22
    return f"{base}.{module}" if base else module


def _dotted_base(node: ast.AST) -> Optional[str]:
    """把 Attribute 链还原成点号路径（app.agent.chain.store.get_spec → 全串）。"""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _module_level_bound(tree: ast.Module) -> set:
    """模块级绑定名（def/class/赋值/import）——本文件自有副本的判定依据。"""
    bound = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    bound.add(t.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            bound.add(node.target.id)
        elif isinstance(node, ast.Import):
            for a in node.names:
                bound.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                bound.add(a.asname or a.name)
    return bound


def _iter_py(base):
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in
                   ("__pycache__", "del", "tmp", ".venv", "node_modules")]
        for f in sorted(files):
            if f.endswith(".py"):
                yield os.path.join(root, f)


@pytest.mark.parametrize("mod,name", sorted(
    (m, n) for m, names in DELETED_BY_MODULE.items() for n in names))
def test_dead_symbols_not_referenced_in_live_code(mod, name):
    """已删 (module, name) 不得在活代码里再被 import / 别名属性调用。

    只查跨模块引用形态（import 三形式按包解析 + 别名模块属性链）；
    本文件自有同名副本（module-level bound）按同名异物豁免——正是项目
    死代码硬流程的口径。裸 Name 引用不可能跨模块（无 star import，已另有断言）。
    """
    pair = (mod, name)
    bad = []
    for base in (AGENT, os.path.join(BACKEND, "tests"), os.path.join(BACKEND, "scripts")):
        for p in _iter_py(base):
            rel = os.path.relpath(p, BACKEND).replace(os.sep, ".")
            file_module = rel[:-3]  # 去 .py
            src = io.open(p, encoding="utf-8-sig", errors="replace").read()
            try:
                tree = ast.parse(src)
            except SyntaxError:
                continue
            local_bound = _module_level_bound(tree)
            # import 绑定：local_name -> 源模块/模块全路径
            bindings = {}
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    src_mod = _resolve_relative(file_module, node.level, node.module)
                    for a in node.names:
                        if pair == (src_mod, a.name):
                            bad.append(f"{p}:{node.lineno} importfrom")
                        # from X import Y：Y 是子模块 ⇒ 绑定到 X.Y；是符号 ⇒ 绑定到 X
                        # （不区分会把 `from app.agent.chain import store as _s`
                        #   的 _s 绑成 app.agent.chain ⇒ _s.get_chain_weights() 漏判，
                        #   变异检验实测，2026-10-22）
                        cand = f"{src_mod}.{a.name}" if src_mod else a.name
                        bindings[a.asname or a.name] = (
                            cand if _is_module(cand) else src_mod)
                elif isinstance(node, ast.Import):
                    for a in node.names:
                        bindings[a.asname or a.name.split(".")[0]] = a.name
            for node in ast.walk(tree):
                if (isinstance(node, ast.Attribute) and node.attr == name
                        and isinstance(node.ctx, ast.Load)):
                    dotted = _dotted_base(node)
                    if dotted is None:
                        continue
                    head, rest = dotted.split(".", 1) if "." in dotted else (dotted, "")
                    # 头名绑定到某模块 ⇒ 全串即模块路径 + 属性
                    if head in bindings:
                        full = f"{bindings[head]}.{rest}" if rest else bindings[head]
                        if full == mod or full.startswith(mod + "."):
                            bad.append(f"{p}:{node.lineno} attr {dotted}")
                    elif dotted == name and name not in local_bound:
                        bad.append(f"{p}:{node.lineno} bare-attr")
    assert not bad, f"已删符号 {pair} 仍被引用: {bad[:5]}"


def test_no_star_imports():
    """无 star import ⇒ 裸 Name 引用不可能跨模块（上面守卫据此豁免 Name 形态）。"""
    star = []
    for base in (AGENT, os.path.join(BACKEND, "tests"), os.path.join(BACKEND, "scripts")):
        for p in _iter_py(base):
            src = io.open(p, encoding="utf-8-sig", errors="replace").read()
            try:
                tree = ast.parse(src)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and any(a.name == "*" for a in node.names):
                    star.append(f"{p}:{node.lineno}")
    assert not star, f"出现 star import（裸 Name 守卫失效）: {star}"
