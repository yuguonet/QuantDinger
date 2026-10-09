"""test_rebuild_json_keys.py — rebuild `--json` 结构化落盘不得崩（2026-10-09 审计 B-4）。

事故
----
投影键自 2026-10-07 A2 起是**四元组** `(trade_date, strategy, code, entry_style)`
（`build_expected` / `load_actual` / `store.load_projection` 三处同步改过），
但 `main` 的 `--json` 分支仍写着：

    "missing": ["%s|%s|%s" % k for k in d["missing"]]

⇒ 对四元组执行三元格式串抛 `TypeError: not all arguments converted`
⇒ **整个结构化落盘路径必崩**（而 render 分支早已改显式索引，唯独 json 漏改）。

修法：新增 `rebuild.fmt_key` 作为**唯一实现**（设计 §2.3「只允许有一份实现」），
json 分支改走它；顺带避免 render 与 json 各写一份格式化导致再次分叉。

本测试**零 DB**：直接对 `fmt_key` 与「旧写法」做对照，并静态确认 json 分支已改用它
（静态段防的是「有人把 `fmt_key` 留着不用、json 仍走旧格式串」）。
"""

from __future__ import annotations

import ast
import json
import os

import pytest

from app.market_cn.auto import rebuild

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REBUILD_PY = os.path.join(_ROOT, "app", "market_cn", "auto", "rebuild.py")


# ================================================================
# 1. fmt_key 行为
# ================================================================
def test_fmt_key_renders_four_tuple():
    k = ("2026-06-17", "break", "600000", "a")
    assert rebuild.fmt_key(k) == "2026-06-17|break|600000|a"


def test_fmt_key_tolerates_none_component():
    """分量可能含 None（entry_style 缺失虽归 'a'，但 strategy/code 未必）。

    这也是**不用 `"|".join`** 的原因：join 对None 会抛TypeError。
    """
    k = ("2026-06-17", "dragon", None, "a")
    assert rebuild.fmt_key(k) == "2026-06-17|dragon|None|a"


def test_old_three_placeholder_format_would_crash():
    """反证：旧写法确实崩 —— 说明本门禁针对的是真 bug 而非风格偏好。"""
    k = ("2026-06-17", "break", "600000", "a")
    with pytest.raises(TypeError):
        "%s|%s|%s" % k          # noqa: SFS101 —— 正是被替换掉的写法


def test_fmt_key_is_used_by_json_branch():
    """静态：main 的 --json 分支必须走 fmt_key，不得残留三元格式串。"""
    src = open(REBUILD_PY, encoding="utf-8").read()
    tree = ast.parse(src)
    main_fn = next(n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == "main")
    body = ast.unparse(main_fn)
    assert "fmt_key(" in body, "main 的 --json 分支未使用 fmt_key"
    assert '"%s|%s|%s"' not in body and "'%s|%s|%s'" not in body, (
        "main 内仍残留三元格式串 —— 四元键下必崩"
    )


# ================================================================
# 2. 端到端：json 序列化一个真实形状的报告 dict
# ================================================================
def test_json_dump_of_four_tuple_keys_succeeds():
    """整段 json.dump 结构（含 missing/ghost/drift）必须可序列化。"""
    d = {"missing": [("2026-06-17", "break", "600000", "a")],
         "ghost": [("2026-06-18", "g56", "000001", "b")],
         "drift": [(("2026-06-19", "knife_catch", "300750", "a"),
                    [("signal_price", 10.0, 10.5)])]}
    payload = {
        "meta": {"days": 30},
        "missing": [rebuild.fmt_key(k) for k in d["missing"]],
        "ghost": [rebuild.fmt_key(k) for k in d["ghost"]],
        "drift": [{"key": rebuild.fmt_key(k),
                   "fields": [{"f": x[0], "db": str(x[1]), "exp": str(x[2])}
                              for x in f]} for k, f in d["drift"]],
    }
    txt = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    assert "2026-06-17|break|600000|a" in txt
    assert json.loads(txt)["drift"][0]["key"] == "2026-06-19|knife_catch|300750|a"


def test_no_duplicate_key_formatting_in_rebuild():
    """唯一实现：除 fmt_key 外不得再有别处自行拼投影键。

    用 **AST** 判断实际代码节点（而非文本搜索）—— 文本搜索会误报
    docstring 里引用旧写法的说明文字，以及 fmt_key 自身的实现。
    """
    tree = ast.parse(open(REBUILD_PY, encoding="utf-8").read())

    # 先定位 fmt_key 的实现节点（白名单），靠函数名而非行号
    fmt_nodes = set()
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef) and fn.name == "fmt_key":
            fmt_nodes.update(id(n) for n in ast.walk(fn) if isinstance(n, ast.BinOp))

    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.Mod):
            continue
        left = node.left
        if isinstance(left, ast.Constant) and isinstance(left.value, str) \
                and "|" in left.value and "%s" in left.value:
            if id(node) in fmt_nodes:
                continue                      # fmt_key 自身实现
            offenders.append((node.lineno, ast.unparse(node)))

    assert not offenders, (
        "rebuild.py 中出现自行拼投影键的格式串（应统一走 fmt_key）:\n  "
        + "\n  ".join(f"L{ln}: {seg}" for ln, seg in offenders)
    )
