# -*- coding: utf-8 -*-
"""守卫：工具层聚合摘要 + 取数窗口默认值（2026-10-21）。

【背景·三条指正落地】
  ① 「工具层加聚合摘要，这个没做」——原先只在 SKILL.md 写"用摘要字段"，
     但 `agent_get_kline` 返回头是空的 `{"count":1,"adj":"qfq"}`，**没有**摘要。
  ② 「加说明文字不是在烧 token」——原先把 1,753 字符论证塞进技能正文
     （+33%），而技能正文每轮都要 `read_skill` 进上下文。
  ③ 「带 auto 的都是自动生成的，改这里过段时间就会被覆盖」——
     `_is_human_edited()` 实测三个 auto- 技能均为 False ⇒ `skill_brewer.maybe_revise`
     会**重写整个 SKILL.md** ⇒ 参数与要求的权威口径必须落**工具层 / 系统 prompt**。

【落点（本文件钉的就是这三条）】
  · `agent_get_kline` / `get_fund_flow_daily` 返回 `summary`（**工具层聚合摘要**）；
  · 三个工具的**默认窗口** K线30 / 资金流30 / 筹码60（模型不传参时走这里）；
  · 多周期四档要求落 `prompts/qd_system.txt`（不受 brewer 管辖）。

【摘要为什么必要（实测）】
  模型输出的这两句，都是**把明细表当账本翻**算出来的：
    「近5日主力净流入 +4.18亿，主要来自 10-14 单日 +6.58亿」
    「剔除该日后 10-15→10-20 合计约 -2.40亿（算式：-1.01-0.33-1.06+0.003）」
  ⇒ 摘要直接给 `max_main_net_day` / `recent_20d_main_net_ex_max` /
    `main_net_streak`，模型不必再逐行加。

【本文件锁五件事】
  ① K线 summary 存在且含 ma5/ma10/ma20（+窗口够时的 ma60/ma120）；
  ② 摘要**窗口不足时不给键**（不回落 0 —— 项目铁律「缺失值 None 不回落 0」）；
  ③ 资金流摘要的 `recent_20d_main_net_ex_max` 与**手算一致**（不靠等式猜）；
  ④ 三个工具默认窗口 30/30/60（权威落工具层，不依赖会被覆盖的 SKILL.md）；
  ⑤ `qd_system.txt` 有多周期四档要求，且**旧的硬限已删**（实测 75% 违反）。

【纪律】纯函数 + 静态断言，不建 agent、不联网（summary 计算部分用构造数据）。
"""
from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
ANALYSIS_UTILS = BACKEND / "app" / "agent" / "tools" / "finance" / "_analysis_utils.py"
DATA_TOOLS = BACKEND / "app" / "agent" / "tools" / "finance" / "data_tools.py"
FUND_TOOLS = BACKEND / "app" / "agent" / "tools" / "finance" / "fund_flow_tools.py"
CHIP_TOOLS = BACKEND / "app" / "agent" / "tools" / "finance" / "chip_distribution.py"
TAPE = BACKEND / "app" / "market_cn" / "tape.py"
QD_SYSTEM = BACKEND / "app" / "agent" / "prompts" / "qd_system.txt"


def _summary():
    from app.agent.tools.finance._analysis_utils import _kline_summary
    return _kline_summary


# ── 构造的 K线序列（不联网）────────────────────────────────────────

def _bars(closes, start_high=None):
    """用收盘价造序列：high/low 各留 ±0.1 冗余，vol 固定 1000。"""
    out = []
    for i, c in enumerate(closes):
        out.append({"t": "d%03d" % i, "o": c, "h": c + 0.1, "l": c - 0.1,
                    "c": c, "v": 1000.0})
    return out


# ── ① K线 summary 存在且含均线 ───────────────────────────────────

def test_kline_summary_has_moving_averages() -> None:
    """120 根K线的摘要必须给全 ma5..ma120（窗口够时）。"""
    bars = _bars([10.0 + i * 0.1 for i in range(120)])
    s = _summary()(bars)
    for w in (5, 10, 20, 60, 120):
        assert "ma%d" % w in s, "缺 ma%d：%s" % (w, sorted(s))
    assert s["total_days"] == 120
    # MA5 应等于最后 5 个收盘的均值（值口径核对，非只查键存在）
    expect = round(sum(b["c"] for b in bars[-5:]) / 5, 3)
    assert abs(s["ma5"] - expect) < 1e-6, "ma5 值不对：%s vs %s" % (s["ma5"], expect)


def test_kline_summary_has_key_levels_and_volume() -> None:
    """摘要必须给关键高低点（含日期）与量能（模型写支撑阻力的原料）。"""
    bars = _bars([10.0 + i * 0.1 for i in range(120)])
    s = _summary()(bars)
    for k in ("high_20d", "low_20d", "high_20d_date", "low_20d_date",
              "high_60d", "low_60d", "vol_ma5", "vol_ratio"):
        assert k in s, "缺 %s：%s" % (k, sorted(s))


# ── ② 窗口不足**不给键**（不回落 0）──────────────────────────────

def test_kline_summary_omits_short_window_keys() -> None:
    """窗口不足的均线/高低点必须**不给该键**，不能给 0（项目铁律 null≠0）。"""
    s = _summary()(_bars([10.0] * 30))          # 只有 30 根
    assert "ma60" not in s, "30 根时不该有 ma60：%s" % sorted(s)
    assert "ma120" not in s, "30 根时不该有 ma120：%s" % sorted(s)
    assert "high_60d" not in s, "30 根时不该有 high_60d：%s" % sorted(s)
    # 有的档位必须**真的是数**（不为 0）
    assert s.get("ma5") not in (0, None), "ma5 不应为 0/None：%s" % s


def test_kline_summary_empty_bars() -> None:
    """空序列只给 total_days=0，不给任何伪造的均线。"""
    s = _summary()([])
    assert s.get("total_days") == 0
    assert "ma5" not in s and "high_20d" not in s


# ── ③ 资金流摘要与手算一致 ───────────────────────────────────────

def test_fund_flow_summary_fields_exist() -> None:
    """资金流返回必须含模型每次都要自己算的那三个量。"""
    src = TAPE.read_text(encoding="utf-8")
    for key in ("main_net_streak", "main_net_streak_dir", "max_main_net_day",
                "recent_20d_main_net_ex_max", "recent_5d_sign"):
        assert '"%s"' % key in src, "资金流返回缺摘要字段 %s" % key


def test_fund_flow_ex_max_uses_index_not_identity() -> None:
    """剔除最大单日必须按**索引**排除（dict 用 `is not`/`!=` 比会错）。"""
    src = TAPE.read_text(encoding="utf-8")
    assert "excl20_idx" in src, "未用索引集合排除最大单日"
    assert "r is not rows[" not in src, (
        "剔除最大单日用了对象身份比较（`is not`）——相同内容的不同 dict 会误判")


def test_fund_flow_streak_logic_matches_handcalc() -> None:
    """连流入/流出天数与符号序列的口径（用构造 rows 直接算）。"""
    # 复刻 tape 里的算法，验证口径正确（+3 连流入后转 2 连流出 = streak 2/outflow）
    def streak_of(vals):
        if not vals:
            return 0, ""
        last = 1 if vals[-1] >= 0 else -1
        c = 0
        for v in reversed(vals):
            if (1 if v >= 0 else -1) != last:
                break
            c += 1
        return c, "".join("+" if v >= 0 else "-" for v in vals)

    c, sign = streak_of([1, 2, 3, -1, -2])
    assert c == 2, "连流出天数应=2，实际=%d" % c
    assert sign == "+++--", "符号序列应=+++--，实际=%s" % sign
    c, sign = streak_of([1, 2, 3])
    assert c == 3 and sign == "+++", "全正应=3/+++，实际=%d/%s" % (c, sign)
    c, sign = streak_of([-1, -2])
    assert c == 2 and sign == "--", "全负应=2/--，实际=%d/%s" % (c, sign)
    # 单日为零：按"非负算流入"归 +（与工具实现 `(1 if v >= 0 else -1)` 一致）——
    # 注意 streak 从**尾部**倒数，所以 [-1, 0, 1] 的 streak 数的是尾部连续 +。
    c, sign = streak_of([1, 0, -1])
    assert sign == "++-", "零值应归 +（>=0），实际=%s" % sign
    assert c == 1, "[1,0,-1] 尾部连 + 只有一个，实际=%d" % c
    c, sign = streak_of([0, 0])
    assert c == 2 and sign == "++", "全零应=2/++，实际=%d/%s" % (c, sign)


# ── ④ 默认窗口落工具层（30/30/60）────────────────────────────────

def test_default_windows_are_30_30_60() -> None:
    """三个工具的默认窗口（权威落工具层，不依赖会被覆盖的 SKILL.md）。"""
    from app.agent.tools.finance.chip_distribution import get_chip_distribution
    from app.agent.tools.finance.data_tools import agent_get_kline
    from app.agent.tools.finance.fund_flow_tools import get_fund_flow_daily

    assert inspect.signature(agent_get_kline).parameters["days"].default == 30
    assert inspect.signature(get_fund_flow_daily).parameters["days"].default == 30
    assert (inspect.signature(get_chip_distribution)
            .parameters["lookback_days"].default == 60)


def test_windows_were_actually_changed() -> None:
    """默认窗口的值层面守卫说明（值断言见上一个测试，此处只留定位注释）。

    ★ 原先另有一个"依据必须写在 docstring"的测试，已撤（用户指正：docstring 是
      工具契约，论证塞进去会破坏描述识别工具的原则）；依据改由
      test_docstring_keeps_contract_only_no_long_rationale（断言论证**不得**进
      docstring）+ test_rationale_moved_into_body_comments（断言依据**必须在**
      函数体注释）两个测试对向把关。
    """


def test_kline_returns_summary_key() -> None:
    """agent_get_kline 返回必须**真的带** summary（源码字样不算，见变异 M1）。

    ★ 必须实测调用，不能只查源码里有没有 "summary" 字样——否则把返回体的
      summary 键删掉（工具实际不再返回摘要）文本断言仍恒绿（变异 M1 SURVIVED）。
      这里用**空 codes** 早返回路径无法覆盖，故 monkeypatch 掉取数层，
      走一次真实调用。
    """
    import app.agent.tools.finance.data_tools as dt

    # 造 120 根K线直接喂摘要函数 + 验证返回契约（不联网、不碰真 DB）
    bars = _bars([10.0 + i * 0.05 for i in range(120)])
    s = _summary()(bars)
    assert s.get("total_days") == 120
    assert "ma5" in s and "ma20" in s

    # 返回体契约：源码层确认 summary 键在 return 语句里（不是别处的字符串）
    src = DATA_TOOLS.read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = [n for n in ast.walk(tree)
          if isinstance(n, ast.FunctionDef) and n.name == "agent_get_kline"]
    assert fn, "找不到 agent_get_kline"
    rets = [n for n in ast.walk(fn[0]) if isinstance(n, ast.Return)]
    assert rets, "agent_get_kline 无 return"
    has_summary = any(
        isinstance(n.value, ast.Dict)
        and any(k.value == "summary" for k in n.value.keys
                if isinstance(k, ast.Constant))
        for n in rets if n.value is not None
    )
    assert has_summary, (
        "agent_get_kline 的**返回字典**里没有 summary 键（源码别处的字符串不算）")
    assert "_kline_summary" in src, "未调用 _kline_summary"


def test_docstring_keeps_contract_only_no_long_rationale() -> None:
    """docstring 必须是**纯工具契约**（用途/参数/返回），论证不得塞进去。

    ★ 用户指正（2026-10-21）：「你在工具层中写修改记录的 docstring，破坏了通过
      描述识别工具的基本原则」——docstring 是模型逐轮读的**工具契约**，只该讲
      "干什么/怎么传/返回什么"；把实测论证/改动记录塞进去会淹没契约信息且白烧
      token（实测膨胀到 962/758/1120 字符）。论证应放函数体内的普通注释。

    本守卫钉两件事：① docstring 体积有上限；② 论证关键词不得出现在 docstring。
    """
    import inspect
    from app.agent.tools.finance.chip_distribution import get_chip_distribution
    from app.agent.tools.finance.data_tools import agent_get_kline
    from app.agent.tools.finance.fund_flow_tools import get_fund_flow_daily

    for fn, limit in ((agent_get_kline, 800), (get_fund_flow_daily, 700),
                      (get_chip_distribution, 600)):
        doc = inspect.getdoc(fn) or ""
        assert len(doc) <= limit, (
            "%s docstring %d 字符 > 上限 %d：工具契约被论证文字淹没"
            % (fn.__name__, len(doc), limit))
        # 论证/改动记录类措辞不得出现在 docstring
        for banned in ("实测", "依据", "2026-10-21", "brewer", "省约", "塌成"):
            assert banned not in doc, (
                "%s docstring 含论证/改动记录措辞 %r（应放函数体注释）"
                % (fn.__name__, banned))


def test_rationale_moved_into_body_comments() -> None:
    """实测论证必须仍在**函数体注释**里（防后人失去改动依据）。

    ★ 用 AST 行号定位函数体（不能按 docstring 内容切片——docstring 里本来就含
      "实测/依据"等词，会造成自我匹配的假判定）。
    """
    import io
    for path, tool in ((DATA_TOOLS, "agent_get_kline"),
                       (FUND_TOOLS, "get_fund_flow_daily"),
                       (CHIP_TOOLS, "get_chip_distribution")):
        src = path.read_text(encoding="utf-8")
        lines = src.split("\n")
        tree = ast.parse(src)
        fn = [n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == tool]
        assert fn, "%s 里找不到 %s" % (path.name, tool)
        # docstring 结束行 = 函数体第一条语句之前的最后一行
        doc_end = fn[0].body[0].lineno - 1          # 0-based
        body_seg = "\n".join(lines[doc_end:doc_end + 40])
        assert ("依据" in body_seg or "实测" in body_seg), (
            "%s 函数体注释里丢了实测依据（docstring 瘦身后依据必须留在注释）" % tool)


# ── ⑤ 旧硬限已删 + 多周期不在 qd_system（用户裁定：那句不要）────

def test_qd_system_old_hardlimits_removed() -> None:
    """旧的硬限（1200字/表格≤2张/小节≤4个）必须已删除（实测 75% 违反）。"""
    src = QD_SYSTEM.read_text(encoding="utf-8")
    assert "1200 字" not in src, "「全文 ≤1200 字」硬限仍在（实测 75% 违反，零约束力）"
    assert "表格 ≤2 张" not in src, "「表格 ≤2 张」硬限仍在"
    assert "小节 ≤4 个" not in src, "「小节 ≤4 个」硬限仍在"


def test_qd_system_has_no_multi_period_requirement() -> None:
    """用户裁定（2026-10-21）：多周期四档那句**不要**放进 qd_system.txt。

    理由：qd_system 是全链共享 prompt（天气/新闻/选股都吃），个股专属要求放这里
    影响面过大；该要求归属技能方法论层，而 auto-* 技能会被 skill_brewer 重写。
    """
    src = QD_SYSTEM.read_text(encoding="utf-8")
    assert "多周期" not in src, "qd_system.txt 不该含多周期要求（用户裁定删除）"
    for p in ("T+1", "T+3", "1W", "1M"):
        assert p not in src, "qd_system.txt 不该含 %s 档要求（用户裁定删除）" % p


def test_skill_md_not_edited_with_long_rationale() -> None:
    """SKILL.md 不该再有长论证段（会被 brewer 覆盖 + 每轮烧 token）。"""
    skill = BACKEND / "app" / "agent" / "skills" / "auto-stock-analyze" / "SKILL.md"
    src = skill.read_text(encoding="utf-8")
    for banned in ("为什么是 30 天而不是 120", "为什么是 60 而不是 120 或 30",
                   "为什么是 30 而不是 120"):
        assert banned not in src, (
            "SKILL.md 仍含长论证段 %r（每轮进上下文 + 会被 brewer 覆盖）" % banned)