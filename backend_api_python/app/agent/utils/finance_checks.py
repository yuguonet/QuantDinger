# -*- coding: utf-8 -*-
"""
金融口径软检查（方案 A2a，2026-09-26）。

verify_node 三查（grounding/banned/估算）之外的**金融领域四查**，全部 soft：
  1. 前视：输出日期不得晚于锚点（前瞻标签"明日/次日/下个交易日"例外，不算泄漏）
  2. 口径：涉及 K 线/均线时须声明复权口径（前复权/后复权/不复权）—— 未声明 warning
  3. 样本：须声明数据样本量（近 N 日 / 样本 N 条）—— 未声明 warning
  4. 新鲜度：须声明数据截止日 —— 未声明 warning

与 utils/grounding.py 同款设计：
- **单一事实源**：verify_node 与评测集共用本模块，禁止各写一套阈值；
- **保守触发**：缺声明只是 soft warning，不阻断交付（与 fatal 的 grounding/banned 区分）；
- **宁松勿卡**：金融输出缺口径声明是常见的，先记录再迭代，不制造拒收噪声。

易错点：
- 前视查的锚点优先用 state.as_of，其次从 corpus 取最晚日期（数据最新日），
  拿不到锚点则跳过前视查（不臆断今天）；
- 前瞻标签例外窗口取日期前后 14 字符，与 tests/evals/runner.py check_time_leak 同口径。
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import List, Tuple

# 前瞻标签：出现这些词的日期上下文不算时间泄漏（是合理的预测/展望表述）
_FORWARD_LABELS = ("明日", "次日", "下个交易日", "下一个交易日", "下一交易日", "隔日")

# 日期匹配：2026-09-24 / 2026/9/24 / 2026年9月24日
_DATE_RE = re.compile(r"20\d{2}[-/年]\d{1,2}[-/月]\d{1,2}日?")

# 复权口径关键词
_ADJUST_TERMS = ("前复权", "后复权", "不复权", "复权")

# 样本量声明模式
_SAMPLE_RE = re.compile(r"近\d+日|近\d+个交易日|样本\d+|\d+日数据|近\d+天|\d+根K线|\d+条")

# 数据截止日声明模式
_FRESHNESS_RE = re.compile(r"数据截止|截至\d{4}|截止\d{4}|更新至|\d{4}-\d{2}-\d{2}日?数据")


def check_finance_discipline(text: str, anchor: str = "", corpus: str = "") -> Tuple[bool, List[str]]:
    """金融口径四查（soft）。

    Args:
        text: final_answer 输出文本
        anchor: 时间锚点 YYYY-MM-DD；空则从 corpus 推断最晚日期，再不行跳过前视查
        corpus: 工具观测语料（辅助推断锚点）

    Returns:
        (pass, warnings)：pass=True 表示无 warning；warnings 是软提示列表，
        供 verify_node 追加到 soft（不阻断交付）。
    """
    warnings: List[str] = []

    # ── 1. 前视查 ──
    anchor_date = _parse_anchor(anchor, corpus)
    if anchor_date:
        leaks = _check_lookahead(text, anchor_date)
        for leak in leaks:
            warnings.append(f"前视:输出含锚点({anchor_date})之后日期 {leak}（若非前瞻表述请修正）")

    # ── 2. 口径查（复权）──
    involves_kline = any(k in text for k in ("K线", "均线", "MA", "MACD", "KDJ", "RSI", "布林"))
    if involves_kline and not any(t in text for t in _ADJUST_TERMS):
        warnings.append("口径:涉及K线/指标但未声明复权口径（前复权/后复权/不复权）")

    # ── 3. 样本查 ──
    if involves_kline and not _SAMPLE_RE.search(text):
        warnings.append("样本:未声明数据样本量（如'近30日'/'120根K线'）")

    # ── 4. 新鲜度查 ──
    if involves_kline and not _FRESHNESS_RE.search(text):
        warnings.append("新鲜度:未声明数据截止日（如'数据截至2026-09-24'）")

    return (len(warnings) == 0, warnings)


def _parse_anchor(anchor: str, corpus: str) -> date | None:
    """解析时间锚点：优先 anchor 参数，其次 corpus 最晚日期，最后 None。"""
    if anchor:
        try:
            return datetime.strptime(anchor[:10], "%Y-%m-%d").date()
        except ValueError:
            pass
    dates = _DATE_RE.findall(corpus or "")
    if dates:
        parsed = []
        for d in dates:
            try:
                parsed.append(datetime.strptime(_normalize_date(d), "%Y-%m-%d").date())
            except ValueError:
                continue
        if parsed:
            return max(parsed)
    return None


def _check_lookahead(text: str, anchor: date) -> List[str]:
    """检查 text 中是否有晚于 anchor 的日期；前瞻标签上下文例外。"""
    leaks: List[str] = []
    for m in _DATE_RE.finditer(text):
        dstr = m.group(0)
        try:
            d = datetime.strptime(_normalize_date(dstr), "%Y-%m-%d").date()
        except ValueError:
            continue
        if d <= anchor:
            continue
        ctx = text[max(0, m.start() - 14):m.end() + 14]
        if any(fl in ctx for fl in _FORWARD_LABELS):
            continue
        leaks.append(dstr)
    return leaks


def _normalize_date(s: str) -> str:
    """2026/9/24、2026年9月24日 → 2026-09-24。"""
    s = s.replace("年", "-").replace("月", "-").replace("日", "").replace("/", "-")
    parts = s.split("-")
    if len(parts) == 3:
        try:
            return f"{parts[0]}-{int(parts[1]):02d}-{int(parts[2]):02d}"
        except ValueError:
            return s
    return s
