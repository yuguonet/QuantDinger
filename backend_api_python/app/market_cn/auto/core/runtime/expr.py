"""ide/expr.py — 受控表达式求值器 (安全 + 可静态证明 as-of)。

设计纪律 (docs/自动策略IDE架构设计.md §3.3 / §4)：
- 门表表达式**只允许**调内核函数与参数，**禁止**任意 Python 语句 / import / IO / 循环 /
  跨股票引用 / 属性访问 / 下标。
- **偏移语法** `chg(-1)` = 前一日；只允许 ≤ 0 的偏移。正偏移在求值器层直接拒绝，
  且可由 `static_asof_check` 在加载期**静态报出** —— 这是把 as-of 从"靠实测"变成
  "可证明"的关键。

实现：基于 `ast` 的白名单解释器。只允许下述节点；任何其它节点（Attribute / Subscript /
  Lambda / ListComp / Import / Call 非命名函数 等）一律拒绝。
"""

from __future__ import annotations

import ast
import functools
from dataclasses import dataclass
from typing import Any, Callable, Dict, List


class ExprError(Exception):
    """表达式非法（语法越界 / 未授权函数 / 未来偏移）。"""


@functools.lru_cache(maxsize=1024)
def _parse(expr: str) -> ast.Expression:
    """解析门表表达式并缓存 AST (纯读, 不 mutate)。

    门表表达式在全市场回测中会被反复求值 (同一 expr 数百万次) —— 缓存 AST 避免重复
    parse, 语义完全不变 (AST 只被 _check_tree/static_asof_check/_ev 只读访问)。
    """
    return ast.parse(expr, mode="eval")


# 允许出现的 AST 节点白名单
_ALLOWED = (
    ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not, ast.USub,
    ast.UAdd, ast.BinOp, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod, ast.Pow,
    ast.Compare, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
    ast.Name, ast.Load, ast.Constant, ast.Call,
)


def parse(expr: str) -> ast.Expression:
    """公开解析入口（含缓存）：供静态分析器复用同一份解析语义。

    目前的使用方是 ide/present.py 的决策日依赖推导 (`reads_decision_bar`) —— 它要
    遍历 AST 判断门是否读决策日数据。走本入口可复用 _parse 的 lru_cache，避免
    在全市场批量扫描里重复 parse 同一表达式数百万次。
    """
    return _parse(expr)


def _check_tree(node: ast.AST) -> None:
    """遍历 AST，拒绝任何非白名单节点。"""
    for n in ast.walk(node):
        if not isinstance(n, _ALLOWED):
            raise ExprError(f"不允许的语法: {type(n).__name__}")
        if isinstance(n, ast.Call):
            if not isinstance(n.func, ast.Name):
                raise ExprError("只允许调用命名函数 (func 必须是 Name)")
            if n.keywords:
                raise ExprError("函数调用不允许使用关键字参数")


@functools.lru_cache(maxsize=4096)
def _checked_tree(expr: str) -> ast.Expression:
    """解析 + 白名单校验 (纯函数, 按 expr 缓存)。

    `evaluate()` 在回测/展示里对**同一条 expr** 会求值数百万次 (全市场 × 历史日 × 候选
    lu)。原实现在每次求值都 `ast.walk` 重跑一遍白名单校验 —— 实测占单次求值耗时的
    ~85% (86µs / 102µs), 是门求值的真正热点。校验结果只依赖 expr 本身 (AST 是只读的),
    缓存与逐次重跑**逐位等价**, 零语义变化。

    异常不缓存 (lru_cache 不缓存异常) —— 非法表达式每次都会重新抛出 ExprError, 与
    原行为一致 (加载期暴露问题)。
    """
    tree = _parse(expr)
    _check_tree(tree)
    return tree


def static_asof_check(expr: str, offset_funcs: set) -> List[tuple]:
    """静态 as-of 检查：扫描偏移函数的**字面量正偏移**（未来函数）并报告。

    返回 [(函数名, 正偏移值), ...]。运行期 `functions.Ctx` 的偏移函数也会在调用时
    再次拒绝正偏移（双保险）；本函数是"加载期即可发现"的那一道。

    **约定**：所有偏移函数的偏移量 `k` 必须作为**第一个位置参数**（如 `chg(-1)`、
    `ma(0,"close",5)`）。本函数据此只检查 `args[0]`，避免把窗口/字段等后续参数
    （如 `rsi` 的 `n=14`）误判为未来偏移。若偏移来自参数（如 `chg(k)` 且 k 来自
    param），运行期约束（k<=0）兜底，静态检查不报。
    """
    tree = _parse(expr)
    problems: List[tuple] = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) \
                and n.func.id in offset_funcs and n.args:
            a = n.args[0]   # 偏移量 k 必须是第一个位置参数
            if isinstance(a, ast.Constant) and isinstance(a.value, (int, float)) \
                    and a.value > 0:
                problems.append((n.func.id, a.value))
    return problems


def evaluate(expr: str, env: Dict[str, Any], funcs: Dict[str, Callable]) -> Any:
    """求值一个受控表达式。

    env   : 参数命名空间（如 max_last_chg / min_vol_ratio 等，解析为数值）。
    funcs : 已授权的内核函数（名称 → 可调用），由 Ctx 提供。
    返回表达式标量结果（bool / float / int）。
    """
    tree = _checked_tree(expr)          # 解析 + 白名单校验 (按 expr 缓存 —— 见该函数)
    return _ev(tree.body, env, funcs)


def _ev(node: ast.AST, env: Dict[str, Any],
        funcs: Dict[str, Callable]) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in env:
            return env[node.id]
        raise ExprError(f"未知变量: {node.id}")
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            return all(_ev(v, env, funcs) for v in node.values)
        return any(_ev(v, env, funcs) for v in node.values)
    if isinstance(node, ast.UnaryOp):
        v = _ev(node.operand, env, funcs)
        if isinstance(node.op, ast.Not):
            return not v
        if isinstance(node.op, ast.USub):
            return -v
        return +v
    if isinstance(node, ast.BinOp):
        l = _ev(node.left, env, funcs)
        r = _ev(node.right, env, funcs)
        op = node.op
        if isinstance(op, ast.Add):
            return l + r
        if isinstance(op, ast.Sub):
            return l - r
        if isinstance(op, ast.Mult):
            return l * r
        if isinstance(op, ast.Div):
            return l / r if r else 0.0
        if isinstance(op, ast.Mod):
            return l % r if r else 0.0
        if isinstance(op, ast.Pow):
            return l ** r
        raise ExprError(f"不支持的二元运算: {type(op).__name__}")
    if isinstance(node, ast.Compare):
        left = _ev(node.left, env, funcs)
        for op, comp in zip(node.ops, node.comparators):
            right = _ev(comp, env, funcs)
            if not _cmp(op, left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.Call):
        fname = node.func.id
        if fname not in funcs:
            raise ExprError(f"未授权函数: {fname}")
        args = [_ev(a, env, funcs) for a in node.args]
        return funcs[fname](*args)
    raise ExprError(f"不支持的表达式节点: {type(node).__name__}")


def _cmp(op: ast.cmpop, l: Any, r: Any) -> bool:
    if isinstance(op, ast.Eq):
        return l == r
    if isinstance(op, ast.NotEq):
        return l != r
    if isinstance(op, ast.Lt):
        return l < r
    if isinstance(op, ast.LtE):
        return l <= r
    if isinstance(op, ast.Gt):
        return l > r
    if isinstance(op, ast.GtE):
        return l >= r
    raise ExprError(f"不支持的比较运算: {type(op).__name__}")
