# -*- coding: utf-8 -*-
"""常设·契约对账 + 死接线扫描（方案常设 1/2 + C11）。

用法（离线、零 LLM）:
    python -m app.agent.utils.standing_checks

检查：
  A. 工具 Returns: 契约 vs 真实调用键（dry 能力才测；缺 dry 跳过并记缺口）
  B. env.example × os.getenv 比对（死声明/未声明）
  C. 注册表条目零消费（trace_collector 类）
  D. MASK 范围：JSONL / log.py 是否走脱敏
"""
from __future__ import annotations

import ast
import os
import re
from pathlib import Path
from typing import Dict, List, Set

AGENT = Path(__file__).resolve().parents[1]
BACKEND = AGENT.parents[1]
ENV_EXAMPLE = BACKEND / "env.example"
if not ENV_EXAMPLE.is_file():
    ENV_EXAMPLE = BACKEND / "backend_api_python" / "env.example"


def _py_files(root: Path):
    for p in sorted(root.rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        yield p


def check_env_declarations() -> Dict[str, List[str]]:
    """env.example × os.getenv。"""
    declared: Set[str] = set()
    if ENV_EXAMPLE.is_file():
        for line in ENV_EXAMPLE.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                declared.add(line.split("=", 1)[0].strip())
    used: Set[str] = set()
    for p in _py_files(AGENT):
        src = p.read_text(encoding="utf-8", errors="ignore")
        for m in re.finditer(r'os\.getenv\(\s*["\']([A-Z0-9_]+)["\']', src):
            used.add(m.group(1))
        for m in re.finditer(r'os\.environ(?:\.get)?\(\s*["\']([A-Z0-9_]+)["\']', src):
            used.add(m.group(1))
    dead_declared = sorted(declared - used)  # 声明未用
    undeclared = sorted(used - declared)     # 用未声明
    return {"dead_declared": dead_declared, "undeclared": undeclared, "used": sorted(used)}


def check_zero_consumer_symbols() -> List[str]:
    """全仓定义但零消费的顶层符号（抓 trace_collector 类死接线）。"""
    defined: Dict[str, str] = {}
    used_names: Set[str] = set()
    for p in _py_files(AGENT):
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            continue
        src = p.read_text(encoding="utf-8", errors="ignore")
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = node.name
                if name.startswith("_"):
                    continue
                defined[name] = str(p.relative_to(AGENT))
        # 粗粒度：名字出现次数
        for name in list(defined):
            if name in src:
                used_names.add(name)
    # recount globally
    all_src = "\n".join(p.read_text(encoding="utf-8", errors="ignore") for p in _py_files(AGENT))
    zero = [n for n in defined if all_src.count(n) <= 1]
    return [f"{n} @ {defined[n]}" for n in sorted(zero)[:30]]


def check_mask_coverage() -> Dict[str, bool]:
    """MASK 是否覆盖 JSONL 与 log 输出。"""
    tracing = (AGENT / "utils" / "tracing.py").read_text(encoding="utf-8", errors="ignore") if (AGENT / "utils" / "tracing.py").is_file() else ""
    logpy = (AGENT / "log.py").read_text(encoding="utf-8", errors="ignore") if (AGENT / "log.py").is_file() else ""
    return {
        "mask_in_tracing": bool(re.search(r"mask|MASK|脱敏|redact", tracing, re.I)),
        "mask_in_log": bool(re.search(r"mask|MASK|脱敏|redact", logpy, re.I)),
    }


def check_returns_contracts() -> Dict[str, List[str]]:
    """Returns: 契约 token 存在性（不做真调用——真 dry 需工具面；只查文档锚点）。"""
    missing = []
    checked = 0
    for p in _py_files(AGENT / "tools"):
        src = p.read_text(encoding="utf-8", errors="ignore")
        if "Returns:" not in src and "Returns：" not in src:
            # 文件级：只要有一个工具缺 Returns 就记（粗）
            if "def " in src and "Tool" in src:
                missing.append(str(p.relative_to(AGENT)))
        checked += 1
    return {"checked": checked, "missing_returns_doc": missing[:20]}


def check_skill_tool_phantoms() -> Dict[str, List[str]]:
    """skills/SKILL.md tools: 与 backtick 调用名 ⊆ tools 注册表（幻象治理）。"""
    import ast as _ast
    reg: Set[str] = set()
    tools_root = AGENT / "tools"
    for p in _py_files(tools_root):
        try:
            tree = _ast.parse(p.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            continue
        for n in tree.body:
            if isinstance(n, _ast.FunctionDef) and not n.name.startswith("_"):
                reg.add(n.name)
    META = {"final_answer", "print", "len", "dir", "type", "list", "dict", "str", "int",
            "float", "bool", "range", "enumerate", "isinstance", "getattr", "list_tools",
            "search_tools", "format_result", "web_search", "python_interpreter",
            "read_skill_resource"}
    reg |= META
    out: Dict[str, List[str]] = {}
    for p in sorted((AGENT / "skills").rglob("SKILL.md")):
        t = p.read_text(encoding="utf-8", errors="ignore")
        names: Set[str] = set()
        m = re.search(r"(?m)^tools:\s*\[([^\]]*)\]", t)
        if m:
            names |= {x.strip().strip("'\"") for x in m.group(1).split(",") if x.strip()}
        # 技能本地 run.py 公开函数视为合法
        run_py = p.parent / "run.py"
        if run_py.is_file():
            try:
                rt = _ast.parse(run_py.read_text(encoding="utf-8", errors="ignore"))
                for n in rt.body:
                    if isinstance(n, _ast.FunctionDef) and not n.name.startswith("_"):
                        reg.add(n.name)
            except Exception:
                pass
        ph = sorted(n for n in names if n not in reg)
        if ph:
            out[str(p.relative_to(AGENT))] = ph
    return out


def check_weight_getter_consumers() -> Dict[str, List[str]]:
    """qd_agent_weights 各层 getter 必须有真实消费点（A1a 防断链看门狗）。

    项目高发 bug：getter 写了+导出了，但没有任何运行时消费（如 get_factor_weights
    此前零消费）。本检查对 skill/factor/tool/chain 四层 getter，统计除定义点(store.py)
    和导出点(__init__.py)之外的出现次数；<=1 视为断链。
    """
    GETTERS = ["get_skill_weights", "get_factor_weights",
               "get_tool_weights", "get_chain_weights"]
    occurrences: Dict[str, List[str]] = {g: [] for g in GETTERS}
    for p in _py_files(AGENT):
        rel = str(p.relative_to(AGENT))
        src = p.read_text(encoding="utf-8", errors="ignore")
        for g in GETTERS:
            # 排除定义文件(chain/store.py)和导出文件(chain/__init__.py)
            if rel.endswith("chain/store.py") or rel.endswith("chain/__init__.py"):
                continue
            if g in src:
                occurrences[g].append(rel)
    zero = [g for g, files in occurrences.items() if len(files) == 0]
    return {"zero_consumer_getters": zero, "consumer_files": occurrences}


# A5：副作用类工具必须声明 dry_run 参数（预防性看门狗）
# 本项目金融工具均为只读分析；若未来引入下单/写入类工具，必须支持 dry_run 才能进工具集。
_SIDE_EFFECT_HINTS = (
    "place_order", "submit_order", "create_order", "cancel_order",
    "send_order", "trade", "execute_order",
    "write_db", "insert", "update_db", "delete_",
    "send_message", "publish", "notify",
)


def check_dry_contract() -> Dict[str, List[str]]:
    """副作用类工具必须有 dry_run 参数（A5 预防性契约扫描）。

    扫描 tools/ 下所有公开函数：若函数名含副作用关键词（下单/写入/发送），
    则其签名必须包含 dry_run 参数，否则报 missing_dry_run。
    当前项目无此类工具，结果应为空；此检查防止未来引入无 dry_run 的副作用工具。
    """
    import inspect
    missing: List[str] = []
    for p in _py_files(AGENT / "tools"):
        rel = str(p.relative_to(AGENT))
        src = p.read_text(encoding="utf-8", errors="ignore")
        # 简单 AST：找 def name(...) 公开函数
        for m in __import__("re").finditer(r"^def\s+([a-zA-Z_]\w*)\s*\(([^)]*)\)", src, __import__("re").M):
            fname, params = m.group(1), m.group(2)
            if fname.startswith("_"):
                continue
            # get_ 前缀语义为只读查询，排除
            if fname.startswith("get_"):
                continue
            if not any(h in fname for h in _SIDE_EFFECT_HINTS):
                continue
            if "dry_run" not in params:
                missing.append(f"{rel}::{fname}")
    return {"missing_dry_run": missing}


def run_all() -> Dict:
    return {
        "env": check_env_declarations(),
        "zero_consumer": check_zero_consumer_symbols(),
        "mask": check_mask_coverage(),
        "returns": check_returns_contracts(),
        "skill_phantoms": check_skill_tool_phantoms(),
        "weight_getter_consumers": check_weight_getter_consumers(),
        "dry_contract": check_dry_contract(),
    }


def main() -> None:
    import json
    print(json.dumps(run_all(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
