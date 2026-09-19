# -*- coding: utf-8 -*-

"""

用户反馈闭环 — 检测负面反馈，惩罚上一轮分析。



从 agent-old/nodes.py 提取，独立模块。

"""

from __future__ import annotations



import re



from log import logger



_SEVERE = [

    "完全不对", "大错特错", "错得离谱", "离谱", "反了", "完全错",

    "一塌糊涂", "乱七八糟", "瞎扯", "胡说", "垃圾", "废了", "没用", "一点用没有",

]

_MILD = [

    "不对", "不正确", "不好", "不行", "不准", "不太对",

    "有问题", "有误", "错了", "不太行", "不靠谱",

]



# session_id → 最近一次 flush 的 root_id（由 finalize_node 写入）

_session_last_root: dict[str, int] = {}


def record_session_root(session_id: str, root_id: int) -> None:

    """finalize_node flush 后调用，记录 session → root_id 映射。"""

    _session_last_root[session_id] = root_id


# 复合词白名单：含反馈字的名词/形容词，出现在消息里不代表用户在反馈本轮结果。
# 例："垃圾股怎么筛"里的"垃圾"、"这走势有点不对劲"里的"不对"（审计 P1-8）。
_COMPOUND_NOUNS = [
    "垃圾股", "垃圾股吧", "垃圾时间", "垃圾场", "垃圾箱", "垃圾桶",
    "不对劲", "不对齐", "不行情", "不靠谱儿",
]

# 带问号的消息按提问处理，跳过 mild 检测：

# "为什么昨天数据不对？"是请求解释，不是对上一轮结果的反馈（审计 P1-8）。

# severe 词仍检测——"完全不对"即使出现在问句里也强烈示意出了问题，漏报代价更高。

_QUESTION_MARKS = ("?", "？")


def _strip_compounds(msg: str) -> str:
    """剔除复合词，避免其内部反馈字被独立命中。"""
    for w in _COMPOUND_NOUNS:
        msg = msg.replace(w, "□")
    return msg


def detect_feedback_severity(message: str) -> str | None:
    """检测消息中的负面反馈严重程度。

    Returns:
        "severe" / "mild" / None
    """
    if not message:
        return None
    msg = message.strip()
    cleaned = _strip_compounds(msg)
    for pat in _SEVERE:
        if pat in cleaned:
            return "severe"
    # 问句不做 mild 判定（请求解释 ≠ 反馈）
    if any(q in msg for q in _QUESTION_MARKS):
        return None
    for pat in _MILD:
        if pat in cleaned:
            return "mild"
    return None


def check_negative_feedback(user_input: str, session_id: str = "default") -> None:

    """检测负面反馈并惩罚上一轮分析。



    在 TaskAgent.chat() 入口调用。检测到负面反馈时：

    - 惩罚次数 < 3: mark_root_wrong（标记 correct=False）

    - 惩罚次数 >= 3: delete_tree（删除整棵 trace 树）

    """

    severity = detect_feedback_severity(user_input)

    if not severity:

        return



    # 通过 session_id 找到上一轮的 root_id

    root_id = _session_last_root.get(session_id)

    if not root_id:

        logger.debug("[Feedback] session=%s 无历史 trace，跳过", session_id)

        return



    try:

        from chain import store as chain_store



        # 获取 trace 信息

        from app.utils.db import get_db_connection

        with get_db_connection() as conn:

            cur = conn.cursor()

            cur.execute(

                "SELECT id, stock_code, name FROM qd_traces WHERE id = %s",

                (root_id,),

            )

            row = cur.fetchone()

            if not row:

                return



            stock_code = row["stock_code"]

            chain_name = row["name"]



            # [AUDIT-MASK:C3|2026-09-19] 双口径取舍：按 stock 计数时同链换标的惩罚记忆清零；
            # 按 chain 计数时跨标的连坐。已知取舍，待确认统一口径。
            if stock_code:

                count = chain_store.get_penalty_count(stock_code)

            else:

                count = chain_store.get_penalty_count_by_chain(chain_name)



            if count >= 3:

                chain_store.delete_tree(root_id)

            else:

                chain_store.mark_root_wrong(root_id)



            logger.info("[Feedback] %s: root_id=%d stock=%s chain=%s penalty=%d",

                        severity, root_id, stock_code, chain_name, count)



    except Exception as e:

        logger.warning("[Feedback] 检测失败: %s", e)



# ── 正面反馈（2026-09-15）：用户认可上一轮编排 → 奖励链路 ──
# 与负面闭环对称：正面认可固化 correct=True + human_reviewed（跳过 T+N 自动校准），
# 使 win_rate 提前计入下轮 update_weights 的 skill/tool 权重，并提高编排缓存命中率。
_POSITIVE = [
    '很好', '非常好', '不错', '准确', '到位', '有用', '有帮助', '厉害', '完美',
    '专业', '靠谱', '满意', '赞', '好分析', '分析得很好', '分析不错', '就是这样',
    '答对了', '说得好', 'nice', 'good', 'great', 'perfect', 'thx', '感谢',
]

# 复合词防误伤：含正面词但实际在提问/贬损的形态
_POSITIVE_COMPOUND_SKIP = ['怎么样', '怎么办', '呢', '吗', '还行吧', '也就', '一般']


def detect_positive_feedback(message: str) -> bool:
    """检测正面认可（无问句、无转折、命中正面词表）。"""
    msg = message.strip()
    if not msg or len(msg) > 40:          # 长消息大概率是新任务而非认可
        return False
    if any(ch in msg for ch in _QUESTION_MARKS):
        return False
    if any(w in msg for w in _POSITIVE_COMPOUND_SKIP):
        return False
    msg = _strip_compounds(msg)
    return any(w in msg for w in _POSITIVE)


def check_positive_feedback(user_input: str, session_id: str = 'default') -> None:
    """检测正面认可并奖励上一轮编排（与 check_negative_feedback 对称）。"""
    if not detect_positive_feedback(user_input):
        return
    root_id = _session_last_root.get(session_id)
    if not root_id:
        logger.debug('[Feedback] session=%s 无历史 trace，正面认可跳过', session_id)
        return
    try:
        from chain import store as chain_store
        chain_store.mark_root_good(root_id)
        logger.info('[Feedback] 正面认可 → root_id=%d 已奖励（correct 固化 + 权重受益）', root_id)
    except Exception as e:
        logger.warning('[Feedback] 正面认可处理失败（不影响主流程）: %s', e)