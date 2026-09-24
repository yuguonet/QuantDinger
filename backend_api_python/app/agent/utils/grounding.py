"""
数字溯源（grounding）判定 ——「结论数字必须来自工具输出」的引擎级单一事实源。

用途（2026-09-24 提智阶段 0.10，审计 A1 复活）：
- 现役消费方：`agents/task_agent.py::_check_final_answer`（final_answer 拒收闸门）；
- 规划消费方（提智方案 E1/E3）：verify_node 三查的「落地性」判定、评测集 L1 幻觉判定
  ——必须与本模块同实现，禁止各写一份阈值（单一事实源）。

背景：旧实现把两处 `raise ValueError` 写在 try 内、被 `except Exception: logger.debug`
吞掉，函数恒返回 True（数字溯源形同虚设，告警与行为脱节，审计 A1）。2026-09-24 复活：
判定与 raise 移到吞不掉的位置，语料改 `executor.state` 全量（审计 A1b：memory 的
observations 已被 _truncate_observations 截到 400 字符，拿它当语料会把真数值误判成编造）。

设计要点：
- 保守触发（宁松勿卡）：少量无法溯源的数值多为推理衍生值（百分比/评分），全拦会误伤；
- 语料含 print 输出（"print 过的数值也算可溯源"，见拒收提示语）。

【2026-09-24 提智评审 C2：corpus 口径收窄】
语料**只认"工具/数据来源槽位"**，不再无差别吞 `executor.state` 全量：
- 白名单槽位：`_r_*`（工具原始载荷，`_wrap_stage_guard` 登记）、`_print_outputs`
  （模型 print 回显，视为留证）、`_qd_stats` 摘要；以及**显式标来源的模型变量**
  （变量名匹配 `_SOURCE_VAR_RE`，如 `quote_df`/`raw_kline`/`_data_*`）。
- 其余模型自造变量（如 `x = 28.5`、`score = 92`）**不进语料**：否则模型先写
  `x=28.5` 再在结论引用 `28.5`，会被判"可溯源"→ 门自我放过幻觉，验收失效。
- 调用方若已自行构造窄语料（评测集/测试），直接传字符串进 `check_grounding` 即可，
  不经本函数据；本函数仅服务"从 state 采语料"的现役 final_answer 闸门。

易错点：
- 数值匹配是**原样子串**匹配：语料里是 `12.50` 而结论写 `12.5` 不命中、千分位/百分号
  变体不归一——衍生值与格式变体靠阈值容忍；提智方案 E2 的 match_value 变体白名单
  落地时在本模块扩展（`_NUM_RE`/匹配函数），勿在调用方各自补丁；
- 改阈值/触发条件前先看 check_grounding 两条规则的注释（0.3 / 0.5 各有出处）。
"""

import re
from typing import Tuple

_NUM_RE = re.compile(r"\d+(?:\.\d+)?")

# 执行器 state 的内部键（与 infra/guided_executor.py::_NON_VAR_STATE_KEYS 同源，
# 但**刻意保留 `_print_outputs`**——print 过的数值算可溯源）。
_INTERNAL_STATE_KEYS = frozenset({"__name__", "__builtins__", "_qd_stats"})

# C2（2026-09-24）：语料白名单槽位。
# 只认「工具返回值槽位」与「print 留证」，不认模型自造变量。
#   · `_r_<工具名>` —— `_wrap_stage_guard` 登记的工具原始载荷（核心来源）
#   · `_print_outputs` —— 模型 print 回显（视为留证）
#   · `_qd_stats` —— 执行器统计摘要
#   · 模型变量里的「疑似数据源命名」——按命名登记表（_SOURCE_VAR_RE）放行
_TOOL_SLOT_PREFIX = "_r_"
_PRINT_SLOT = "_print_outputs"
_STATS_SLOT = "_qd_stats"

# 模型变量名「疑似数据源」登记表（C2）：命中则视为工具/数据来源槽位。
# 设计取舍：宁可少放宽（漏吞几个真数据变量→保守拒收可重写），不可放宽成
# 「任意变量都算来源」（那就退回 C2 要修的问题）。命名约定见 code_agent.yaml。
_SOURCE_VAR_RE = re.compile(
    r"^(?:.*_)?(?:df|data|raw|quote|quotes|kline|bars|flow|fund|sector|news|"
    r"snapshot|result|results|resp|payload|rows|records|mkt|market|ind|indicators|"
    r"fin|fundamentals)(?:_\d+)?$",
    re.I,
)

# 语料体积上限：防极端大变量（DataFrame repr）拖慢判定；按 state 迭代顺序截断。
_CORPUS_MAX_CHARS = 200_000

# E3（2026-09-24）：越界性/营销式确定性承诺单语表——**单一事实源**
# 消费方：nodes.py::make_verify_node 三查「越界性」、tests/evals/runner.py L1 禁用表述。
BANNED_PHRASES = (
    "保证收益", "稳赚", "必然涨停", "百分百", "无风险套利", "包赚", "必涨无疑",
    "保证盈利", "稳簿不赔",
)


def check_banned_phrases(text: str) -> list:
    """E3：返回命中禁用表述（营销/确定性承诺），供 verify 越界性与评测 L1 共用。"""
    return [w for w in BANNED_PHRASES if w in (text or "")]


def _to_float(s: str) -> float:
    try:
        return float(s)
    except Exception:
        return 0.0


def _is_source_slot(key: str) -> bool:
    """C2：该 state 键是否属「工具/数据来源槽位」（可进溯源语料）。"""
    if key == _PRINT_SLOT or key == _STATS_SLOT:
        return True
    if key.startswith(_TOOL_SLOT_PREFIX):
        return True
    return bool(_SOURCE_VAR_RE.match(key))


def collect_grounding_corpus(state: dict, *, strict: bool = True) -> str:
    """从 executor.state 收集溯源语料——**仅白名单来源槽位**（提智评审 C2）。

    收录（strict=True，默认）：
      · `_r_<工具名>` —— 工具原始载荷（`_wrap_stage_guard` 登记，核心来源）
      · `_print_outputs` —— 模型 print 回显（留证）
      · `_qd_stats` —— 执行器统计摘要
      · 模型变量名命中 `_SOURCE_VAR_RE`（疑似数据源命名，如 `quote_df`/`raw_kline`）

    排除：其余模型自造变量（标量/中间计算结果）——否则模型先写 `x=28.5` 再引用
    `28.5`，会被判「可溯源」→ 门自我放过幻觉（C2 的原始病灶）。

    strict=False：保留旧的「state 全量」行为，仅供排查/对照；生产禁用。
    state 非 dict（未初始化/异常路径）时返回 ""，由调用方决定是否退回 observations。
    """
    if not isinstance(state, dict):
        return ""
    parts: list = []
    total = 0
    for key, val in state.items():
        if key in _INTERNAL_STATE_KEYS:
            continue
        if strict and not _is_source_slot(key):
            continue
        try:
            chunk = f"{key}={val!r}"
        except Exception:
            chunk = f"{key}=<unreprable>"
        parts.append(chunk)
        total += len(chunk)
        if total >= _CORPUS_MAX_CHARS:
            break
    return "\n".join(parts)


# ═══════════════════════════════════════════════════════════════════════════
# E2（2026-09-24 提智先手）：数值变体归一 + 容差匹配（**单一事实源**）
# ═══════════════════════════════════════════════════════════════════════════
# 为什么：原样字符串匹配（`12.50` ≠ `12.5`）误杀严重（千分位/百分号/亿·万/
# 复权同值不同号/四舍五入），把**真数据**判成编造 → 触发重写死循环（见 §7.19）。
# 设计取舍（评审：「**误杀比漏杀更烦人**」）：宁可宽松放行，也不冤枉真数字。
# 变体登记表化：新增变体只改本模块，调用方（闸门/verify/评测）禁各自补丁。
_UNIT_SCALE = {"\u4ebf": 1e8, "\u4e07": 1e4, "\u5343": 1e3}  # 亿/万/千

# 匹配容差：相对 0.5% 或绝对 0.5pp（百分比类），取较宽者。
_MATCH_TOL_REL = 0.005
_MATCH_TOL_ABS = 0.5

_NUM_FULL_RE = re.compile(
    r"([+-]?)(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*([\u4ebf\u4e07\u5343])?\s*(%?)"
)


def extract_numbers(text: str) -> list:
    """抽取文本中的数值并归一（去千分位、化亿/万、百分号保留为原值）。"""
    out: list = []
    for m in _NUM_FULL_RE.finditer(text or ""):
        sign, raw, unit, pct = m.group(1), m.group(2), m.group(3), m.group(4)
        try:
            v = float(raw.replace(",", ""))
        except Exception:
            continue
        if unit:
            v *= _UNIT_SCALE.get(unit, 1.0)
        if sign == "-":
            v = -v
        out.append(v)
    return out


def numbers_match(a: float, b: float, *, tol_rel: float = _MATCH_TOL_REL,
                  tol_abs: float = _MATCH_TOL_ABS) -> bool:
    """两数是否匹配（含变体：±容差、正负号无关『复权同值不同号』、百分号 /100）。"""
    for x, y in ((a, b), (abs(a), abs(b)), (a / 100.0, b), (a, b / 100.0)):
        if abs(x - y) <= tol_abs + tol_rel * max(abs(x), abs(y)):
            return True
    return False


def _corpus_values(corpus: str) -> list:
    return extract_numbers(corpus)


def value_in_corpus(value, corpus: str, *, corpus_text: str = None,
                    corpus_vals: list = None) -> bool:
    """单个数值是否可在语料溯源（先原样子串，再变体归一匹配）。

    供 verify_node 三查「落地性」/评测集 L1/claims 校验共用，勿另写一份。
    """
    if value is None:
        return True
    s = str(value).strip()
    if not s:
        return True
    text = corpus_text if corpus_text is not None else (corpus or "")
    if s in text:            # 原有：原样子串命中（最快、零误伤）
        return True
    try:
        v = float(s.replace(",", "").replace("%", ""))
    except Exception:
        return True          # 非数值（文本字段）不在溯源范围
    vals = corpus_vals if corpus_vals is not None else _corpus_values(text)
    return any(numbers_match(v, c) for c in vals)


def _grounded_count(nums: list, corpus: str) -> int:
    """按变体归一计可溯源数（check_grounding 用；比原样子串宽松）。"""
    cvals = _corpus_values(corpus)
    n = 0
    for tok in nums:
        try:
            v = float(tok.replace(",", ""))
        except Exception:
            continue
        if tok in corpus or any(numbers_match(v, c) for c in cvals):
            n += 1
    return n


def check_claims(claims, corpus: str) -> Tuple[bool, list]:
    """E2：校验 claims[] 中每个 claim 的 value 能否溯源（single source）。

    claims: list[dict]，每项应含 {text, value, provenance{tool,field,row}, verified_by_exec}。
    返回 (ok, 未溯源 claim 列表)——ok=False 表示存在凭空数值，须改走 missing_data。
    """
    ungrounded: list = []
    cvals = _corpus_values(corpus)
    for c in (claims or []):
        if not isinstance(c, dict):
            continue
        v = c.get("value")
        if c.get("verified_by_exec"):
            continue
        if not value_in_corpus(v, corpus, corpus_vals=cvals):
            ungrounded.append(c)
    return (not ungrounded), ungrounded


def check_grounding(text: str, corpus: str) -> Tuple[bool, str]:
    """核对 text 中数值是否可在 corpus 溯源。返回 (ok, 拒收指导)。

    规则①（总量级，2026-09-16）：孤立数值 ≥4 个且可溯源率 <30% → 拒收。
    规则②（价格/金额类加严，2026-09-19）：含小数点且 ≥1 的数值 ≥4 个且
          可溯源率 <50% → 拒收（这类数字几乎只可能来自工具数据）。
    两规则独立判定、任一命中即拒收；均保守（数值少于 4 不触发）。
    E2（2026-09-24）：可溯源计数改「变体归一」`_grounded_count`（千分位/百分号/亿·万/
    正负号无关/±容差），降低对**真数据**的误杀。
    """
    if not text or not corpus:
        return True, ""
    # 忽略 0/1/2 这类噪音数值（两位有效字符以下）
    nums = [n for n in _NUM_RE.findall(text) if len(n.lstrip("0.")) >= 2]
    if len(nums) < 4:
        return True, ""

    grounded = _grounded_count(nums, corpus)
    if grounded / len(nums) < 0.3:
        ungrounded = [n for n in nums
                      if not (n in corpus or value_in_corpus(n, corpus))][:10]
        guide = (
            f"数字溯源失败（{len(nums)} 个数值仅 {grounded} 个可溯源）。"
            f"以下数值在工具输出中找不到，疑似编造：{ungrounded}。"
            "修复方法（三选一）：① 删除这些数值，只保留可溯源的数据；"
            "② 在数值后标注来源或'估算'，如'约12.5元（估算）'；"
            "③ 改为引用前面步骤的变量而非写死数字。"
            "重写 final_answer 时逐条核对每个数值。"
        )
        return False, guide

    decimal_nums = [n for n in nums if "." in n and _to_float(n) >= 1.0]
    if len(decimal_nums) >= 4:
        dec_grounded = _grounded_count(decimal_nums, corpus)
        if dec_grounded / len(decimal_nums) < 0.5:
            dec_un = [n for n in decimal_nums
                      if not (n in corpus or value_in_corpus(n, corpus))][:10]
            guide = (
                f"数字溯源失败（价格/金额类：{len(decimal_nums)} 个小数数值仅 {dec_grounded} 个可溯源）。"
                f"以下价格/金额类小数在工具输出中找不到（疑似编造）：{dec_un}。"
                "修复方法：① 用之前步骤从工具取到的**变量**（如 latest['c']）代替写死的数字；"
                "② 无法变量化的，在数值后标注'（估算）'或'（工具未返回）'；"
                "③ 删除非必要数值。注意：print 过的数值也算可溯源，重写前可 print 复核。"
            )
            return False, guide
    return True, ""
