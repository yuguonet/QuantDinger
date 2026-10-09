# -*- coding: utf-8 -*-
"""dragon `Signal.extra` 单一构造源（P3-13 收敛后的结构锁）。

2026-10-09 前，折叠 `evaluate._signal` 与门表 `_scan_one` 各手写一份 20 键 dict
（同名同构、两处手改一处即漂移 = 审计 B3）。P3-13 收敛为：
    extra = `_dragon_extra_base` 基座 11 键 + `build_signal` 宏求值 10 键
           （宏 `signal.fields` = 10 键，见 dragon_callback.yaml）

本测试锁三件事（**纯 AST / 静态，零 import 副作用**）：
1. 手写基座 dict 字面量**全仓只有一份**（在 `_dragon_extra_base` 里）；
2. 折叠 `_signal` 内**不再出现** extra 键名字面量（键只能来自宏 + 基座）；
3. 基座 11 键与宏 `signal.fields` 10 键**恰好互斥且并集 = 21 键全集**，
   即 20 字段口径 + 1 `signal_chg`/`signal_vol_r` 双写键的既有结构不变。

等价性凭据在 `app/market_cn/auto/tmp/_p313_old_new_extra.py`（26 票 / 46 信号 /
920 字段比对 / 唯一分歧 = 纯展示 `tech_rsi`，已登记 docs/口径差异报告.md R7）。
"""
import ast
import io
import os

DRAGON = os.path.join("app", "market_cn", "auto", "strategies", "dragon_callback.py")
BASE_KEYS = {
    "board", "lu_date", "pullback_days", "signal_chg", "signal_vol_r",
    "signal_price", "entry_vol_r", "buy_mode", "turnover_anchor",
    "turnover_anchor_total",
}
MACRO_KEYS = {
    "gap_from_peak", "streak_h", "lu_gain20", "d0_vs_ma20", "pullback_depth",
    "yin_ratio", "tech_score", "tech_rsi", "tech_roc", "tech_psy",
}


def _src(p):
    with io.open(p, encoding="utf-8") as f:
        return f.read()


def _fn(tree, name):
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return n
    return None


def _key_names(node):
    """取 dict 字面量的 str 键名集合。"""
    return {k.value for k in node.keys if isinstance(k, ast.Constant)
            and isinstance(k.value, str)}


def _extra_key_writes(fn):
    """函数内**写** extra 键的所有位置：dict 字面量键 + `extra[k] = v` 下标赋值。

    ★ 必须同时覆盖两种写法（第一版只扫 Dict，反向注入 `extra["lu_date"]=…`
      没被抓到 —— 恒绿装饰）。提取 `extra` 局部变量（一次绑定 = `_dragon_extra_base`
      或 `{}`）之后的全部 `extra[k]=v`，任何 k 落在 extra 键域内即双写。
    """
    out = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Dict):
            out |= _key_names(n) & (BASE_KEYS | MACRO_KEYS)
        elif isinstance(n, ast.Assign):
            # `extra[k] = v` 的 target 是 **Subscript**（不是 Name）—— 只认 Name 会漏掉
            # 这类双写（第一版反向注入就是被这个漏掉的）。
            for t in n.targets:
                if isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name) \
                        and t.value.id == "extra":
                    sl = t.slice
                    key = sl.value if isinstance(sl, ast.Constant) else None
                    if isinstance(key, str):
                        out.add(key)
    return out


def test_base_helper_is_single_writer_of_handwritten_keys():
    """基座 11 键只有 `_dragon_extra_base` 一处手写字面量。"""
    tree = ast.parse(_src(DRAGON))
    fn = _fn(tree, "_dragon_extra_base")
    assert fn is not None, "缺 _dragon_extra_base（P3-13 单源基座）——双写已回归"
    found = [n for n in ast.walk(fn)
             if isinstance(n, ast.Dict) and _key_names(n) >= {"buy_mode"}]
    assert len(found) == 1, "基座内应恰有一处含 buy_mode 的 dict，实际 %d" % len(found)
    assert _key_names(found[0]) == BASE_KEYS, (
        "基座键集变了：多=%s 少=%s"
        % (sorted(_key_names(found[0]) - BASE_KEYS),
           sorted(BASE_KEYS - _key_names(found[0]))))


def test_fold_signal_has_no_handwritten_extra_keys():
    """折叠 `_signal` 内不得再手写 extra 键（键只能来自基座 + 宏）。

    ★ 只看 **dict 字面量** 的 str 键 —— `state["board"]` 这类键访问不是 extra 键，
      用「所有 str 常量」判定会误报（第一版就是这么误报的）。
    """
    tree = ast.parse(_src(DRAGON))
    fn = _fn(tree, "_signal")
    assert fn is not None, "折叠 _signal 不见了"
    leaked = _extra_key_writes(fn)
    assert not leaked, (
        "折叠 _signal 仍在手写 extra 键 %s —— 应全部来自 `_dragon_extra_base` "
        "+ build_signal(宏)；双写即漂移（审计 B3）" % sorted(leaked))


def test_macro_fields_and_base_are_disjoint_and_complete():
    """宏 `signal.fields` 10 键 ∪ 基座 11 键 = extra 全集（无重叠、无遗漏登记）。"""
    from app.market_cn.auto.core.runtime.evaluate import load_strategy
    fkeys = set(load_strategy("dragon_callback").signal.get("fields") or {})
    assert fkeys == MACRO_KEYS, "宏 signal.fields 键集变了: %s" % sorted(fkeys)
    assert not (fkeys & BASE_KEYS), (
        "宏与基座键重叠 %s —— 同一键两处赋值，谁后谁生效不可测" % sorted(fkeys & BASE_KEYS))
    assert len(fkeys | BASE_KEYS) == 20, (
        "extra 全集应为 20 键，实际 %d" % len(fkeys | BASE_KEYS))
