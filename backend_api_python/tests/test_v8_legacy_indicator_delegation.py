"""P8′（2026-10-13 P12 修订）：finance 指标层**自包含**，且与权威层**口径一致**。

★ 判据为什么变了
  P8 曾把 finance 的 11 个 `_calc_*` 压成一行转调，并删掉 `_calc_kdj` 的
  m1/m2 平滑参数、`_sma`、`_calc_obv` 的斜率与顶底背离研判。
  用户裁定（2026-10-13）：**finance 目录不做修改** —— 指标实现要保持
  「算法 + 中文研判语义」自包含；抽成转调壳会让函数体消失、语义受损（降智）。
  P12 已把实现完整恢复（`app/agent/tools/finance/` 回到自包含版本）。

  于是本文件的判据从「legacy 必须是薄转调」换成三条：
    ① 行为不降智 —— finance 输出与固化基线**逐键完全一致**（数值 + signals/zone）。
    ② 口径不漂移 —— finance 与 `app/utils/indicators.py` 对同一组数据给出同一组数。
       ★ 这是回退后真正该守的：两份实现并存可以（finance 自包含给工具层，
         utils 给回测/策略），但**不许各写各的**。
    ③ 新增收编项真源唯一 —— `bollinger` 等只允许在权威层定义一次。

★ 为什么用"固化基线 + 逐键比对"而不是"容差对拍"
  finance 是生产主力，返回值里的中文研判会被 UI 与研判链路直接消费。
  容差只能守住数字，守不住"信号文案被改掉"；而"换内核不动语义"这类改造的
  全部风险恰恰就在语义层。

★ ② 的对拍为什么用 round(2) 再比
  两侧取整位数不同（finance `_safe_round(x)` 默认 2 位，utils 有的不取整）。
  统一到 2 位再比，既消掉取整差异，又能抓出真正的算法口径分歧
  （口径不同通常差在整数位，不是小数点后第 3 位）。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests._p8_indicator_baseline import collect, diff, load_baseline, series  # noqa: E402

BACKEND = Path(__file__).resolve().parents[1]
FINANCE = BACKEND / "app" / "agent" / "tools" / "finance"
AT_PATH = FINANCE / "analysis_tools.py"
AU_PATH = FINANCE / "_analysis_utils.py"
UTILS = BACKEND / "app" / "utils" / "indicators.py"

# 全仓只允许在权威层出现的算法（P8 收编项；atr/mfi/cmf 等 finance 侧自带一份，
# 由下面的对拍守口径，不在此列）
_SINGLE_SOURCE_FUNCS = ("bollinger",)


def _top_func(path: Path, name: str) -> ast.FunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{path.name} 里找不到顶层函数 {name}")


# ── ① 行为契约 ────────────────────────────────────────────────
def test_legacy_indicator_output_matches_baseline():
    """finance 指标输出必须与固化基线逐键完全一致。

    ★ 这条是"不降智"的总闸：任何数值变化、任何 signals 文案变化都会红。
      若确属**有意为之**的行为变更，用 `tmp/p8_snapshot.py --save` 重新固化，
      并在提交信息里写明为什么 —— 不允许悄悄改。
    """
    base = load_baseline()
    cur = collect()
    assert set(base) == set(cur), (
        f"快照点集合变了：新增 {sorted(set(cur) - set(base))} / "
        f"丢失 {sorted(set(base) - set(cur))}")

    diffs = diff(base, cur)
    assert not diffs, (
        f"finance 指标输出相对基线有 {len(diffs)} 处差异（恢复自包含不该改变任何输出）：\n  "
        + "\n  ".join(diffs[:30]))


# ── ② 口径不漂移：finance ↔ 权威层逐项对拍 ─────────────────────
def _r2(v):
    """统一取整到 2 位再比（消掉两侧取整位数差异）。"""
    return round(float(v), 2)


def _data():
    d = series(120)
    return d["high"], d["low"], d["close"], d["volume"]


def _pair_atr():
    from app.agent.tools.finance import analysis_tools as AT
    from app.utils import indicators as U
    h, l, c, _v = _data()
    return AT._calc_atr(h, l, c, 14), U.atr(h, l, c, 14), ("atr", "atr_pct")


def _pair_mfi():
    from app.agent.tools.finance import analysis_tools as AT
    from app.utils import indicators as U
    h, l, c, v = _data()
    return AT._calc_mfi(h, l, c, v, 14), U.mfi(h, l, c, v, 14), ("mfi",)


def _pair_cmf():
    from app.agent.tools.finance import analysis_tools as AT
    from app.utils import indicators as U
    h, l, c, v = _data()
    return AT._calc_cmf(h, l, c, v, 20), U.cmf(h, l, c, v, 20), ("cmf",)


def _pair_hist_volatility():
    from app.agent.tools.finance import analysis_tools as AT
    from app.utils import indicators as U
    _h, _l, c, _v = _data()
    return (AT._calc_hist_volatility(c, 20), U.hist_volatility(c, 20),
            ("daily_volatility", "annualized_volatility"))


def _pair_ma_convergence():
    from app.agent.tools.finance import analysis_tools as AT
    from app.utils import indicators as U
    _h, _l, c, _v = _data()
    return (AT._calc_ma_convergence(c), U.ma_convergence(c),
            ("converged", "max_deviation_pct"))


def _pair_obv():
    from app.agent.tools.finance import _analysis_utils as AU
    from app.utils import indicators as U
    _h, _l, c, v = _data()
    return AU._calc_obv(c, v), U.obv(c, v), ("obv", "obv_slope")


def _pair_boll():
    from app.agent.tools.finance import analysis_tools as AT
    from app.utils import indicators as U
    _h, _l, c, _v = _data()
    return AT._calc_boll(c, 20, 2.0), U.bollinger(c, 20, 2.0), ("upper", "mid", "lower")


def _pair_kdj():
    """utils.kdj 返回三序列元组，finance 返回 dict —— 取末值比。"""
    from app.agent.tools.finance import analysis_tools as AT
    from app.utils import indicators as U
    h, l, c, _v = _data()
    fin = AT._calc_kdj(h, l, c, 9, 3, 3)
    k_s, d_s, j_s = U.kdj(h, l, c, 9)
    return ({"k": fin.get("k"), "d": fin.get("d"), "j": fin.get("j")},
            {"k": k_s[-1], "d": d_s[-1], "j": j_s[-1]}, ("k", "d", "j"))


def _pair_macd():
    """utils.calc_macd 返回三序列元组，finance 返回 dict —— 取末值比。"""
    from app.agent.tools.finance import analysis_tools as AT
    from app.utils import indicators as U
    _h, _l, c, _v = _data()
    fin = AT._calc_macd(c, 12, 26, 9)
    dif_s, dea_s, _hist = U.calc_macd(c, 12, 26, 9)
    return ({"dif": fin.get("dif"), "dea": fin.get("dea")},
            {"dif": dif_s[-1], "dea": dea_s[-1]}, ("dif", "dea"))


def _pair_rsi():
    """finance 一次算多周期（默认 6/12/24），utils 单周期 —— 取 rsi6 比。"""
    from app.agent.tools.finance import analysis_tools as AT
    from app.utils import indicators as U
    _h, _l, c, _v = _data()
    fin = AT._calc_rsi(c, [6])
    return {"rsi6": fin.get("rsi6")}, {"rsi6": U.rsi(c, 6)}, ("rsi6",)


_PAIRS = {
    "atr": _pair_atr,
    "mfi": _pair_mfi,
    "cmf": _pair_cmf,
    "hist_volatility": _pair_hist_volatility,
    "ma_convergence": _pair_ma_convergence,
    "obv": _pair_obv,
    "boll": _pair_boll,
    "kdj": _pair_kdj,
    "macd": _pair_macd,
    "rsi": _pair_rsi,
}


@pytest.mark.parametrize("name", sorted(_PAIRS))
def test_finance_matches_authoritative_layer(name: str):
    """finance 自包含实现与权威层必须同口径。

    ★ 这条是 P12 回退后**新增**的总闸：实现可以两份（finance 自包含给工具层、
      utils 给回测/策略），但同一只票两条链路给出不同的 MACD/KDJ 就是降智级
      缺陷。任一侧改算法而另一侧没跟上，这里立刻红。
    """
    fin, util, keys = _PAIRS[name]()
    assert not fin.get("error"), f"finance 侧数据不足：{fin.get('error')}"
    assert not (isinstance(util, dict) and util.get("error")), \
        f"权威层侧数据不足：{util.get('error')}"
    for k in keys:
        assert k in fin, f"finance 侧缺少键 {k}（返回 {sorted(fin)}）"
        assert k in util, f"权威层侧缺少键 {k}（返回 {sorted(util)}）"
        va, vb = fin[k], util[k]
        if isinstance(va, bool) or isinstance(vb, bool):
            assert va == vb, f"{name}.{k} 口径不一致：finance={va} 权威层={vb}"
        else:
            assert _r2(va) == _r2(vb), (
                f"{name}.{k} 口径不一致：finance={va} 权威层={vb}")


# ── ③ 真源唯一 ────────────────────────────────────────────────
@pytest.mark.parametrize("fname", _SINGLE_SOURCE_FUNCS)
def test_algorithm_defined_only_once(fname: str):
    """`def bollinger(` 全仓只许出现一次，且必须在权威层。"""
    hits = [
        p for p in BACKEND.rglob("*.py")
        if "node_modules" not in p.parts and ".venv" not in p.parts
        # ★ 用 utf-8-sig：带 BOM 的文件用 utf-8 读会让 ast.parse 抛 SyntaxError，
        #   那会让整份扫描静默跳过，守卫形同虚设（P5 真踩过）。
        and any(
            isinstance(n, ast.FunctionDef) and n.name == fname
            for n in ast.parse(p.read_text(encoding="utf-8-sig")).body
        )
    ]
    assert hits == [UTILS], (
        f"{fname} 应在 {UTILS.relative_to(BACKEND)} 唯一定义，实际出现在 "
        f"{[str(h.relative_to(BACKEND)) for h in hits]}")


def test_bollinger_bandwidth_reuses_single_source():
    """`calc_bollinger_bw` 必须复用 `bollinger`，不得自己再算一遍。"""
    node = _top_func(UTILS, "calc_bollinger_bw")
    src = ast.unparse(node)
    assert "bollinger(" in src, (
        "calc_bollinger_bw 没有复用 bollinger —— 带宽算法出现第二份")


def test_finance_indicator_layer_is_self_contained():
    """finance 指标层保持自包含：不得再被抽成转调壳（P12 裁定）。

    判据：`_calc_atr` 等函数体内必须**自带算法**（出现算法特征串），
    而不是一行 `return _ind.atr(...)`。这条与 P8 旧判据恰好相反 ——
    旧判据要求"必须是转调"，被裁定为降智，已废。
    """
    from app.agent.tools.finance import analysis_tools as AT  # noqa: F401
    for name, marker in (("_calc_atr", "tr_list"), ("_calc_mfi", "pos_mf"),
                         ("_calc_kdj", "rsv")):
        node = _top_func(AT_PATH, name)
        body_src = "\n".join(ast.unparse(s) for s in node.body)
        assert marker in body_src, (
            f"{name} 里找不到算法特征 `{marker}` —— 指标实现必须是自包含的，"
            f"不许再退回一行转调壳")
        assert not body_src.strip().startswith("return _ind."), (
            f"{name} 又变成了一行转调（P12 已回退，不许再抽空）")

    # KDJ 的 m1/m2 平滑参数必须还在（P8 曾删掉，属能力删减）
    params = [a.arg for a in _top_func(AT_PATH, "_calc_kdj").args.args]
    assert "m1" in params and "m2" in params, (
        f"_calc_kdj 的 m1/m2 平滑参数被删了（能力删减），实际参数：{params}")


def test_sma_helper_present():
    """`_sma` 是 finance 的共享辅助（P8 曾当死代码删掉），必须还在。"""
    names = [n.name for n in ast.parse(AT_PATH.read_text(encoding="utf-8")).body
             if isinstance(n, ast.FunctionDef)]
    assert "_sma" in names, "_sma 被删了 —— 它是 finance 指标层的共享实现"
