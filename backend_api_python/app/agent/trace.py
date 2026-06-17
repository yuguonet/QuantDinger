# -*- coding: utf-8 -*-
"""
Trace — EvalNode 树 + DB 持久化 + 权重查询 + DecisionCard 格式化。

替代旧 chain/store.py + chain/schema.py，适配 nanobot。
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# 1. EvalNode — 执行树节点
# ═══════════════════════════════════════════════════════════════

@dataclass
class EvalNode:
    layer: str  # "chain" / "skill" / "tool"
    name: str
    exec_date: str = ""
    stock_code: str = ""
    stock_name: str = ""
    score: Optional[float] = None
    direction: str = ""
    action: str = ""
    signal: str = ""
    confidence: Optional[float] = None
    timeframe: str = ""
    analysis: str = ""
    factors: List[Dict] = field(default_factory=list)
    input_params: Dict = field(default_factory=dict)
    output_summary: Any = None
    tools_called: List[str] = field(default_factory=list)
    missing_data: List[str] = field(default_factory=list)
    status: str = "ok"
    error: str = ""
    elapsed_ms: float = 0
    data_source: str = ""
    children: List["EvalNode"] = field(default_factory=list)

    def add_child(self, child: "EvalNode"):
        self.children.append(child)


# ═══════════════════════════════════════════════════════════════
# 2. JSON 提取（Agent 输出 → 结构化数据）
# ═══════════════════════════════════════════════════════════════

def extract_agent_json(answer: str) -> Dict[str, Any]:
    """从 Agent 输出中提取 JSON。优先 ```json 块，降级裸 JSON。"""
    patterns = [
        r'```json\s*\n?(.*?)\n?\s*```',
        r'(\{[^{}]*"action"[^{}]*"score"[^{}]*\})',
    ]
    for pat in patterns:
        m = re.search(pat, answer, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(1).strip())
                if isinstance(data, dict) and "action" in data:
                    return data
            except (json.JSONDecodeError, TypeError):
                continue
    return {}


def build_eval_tree(answer: str, session_id: str, user_query: str,
                    tools_used: List[str], elapsed_ms: float,
                    model: str = "") -> EvalNode:
    """从 Agent 输出构建 EvalNode 根节点。"""
    data = extract_agent_json(answer)

    # fallback: 正则提取
    if not data:
        data = _regex_extract(answer)

    return EvalNode(
        layer="chain",
        name="agent",
        exec_date=date.today().isoformat(),
        stock_code=data.get("stock_code", ""),
        stock_name=data.get("stock_name", ""),
        score=_clamp(data.get("score"), 0, 100),
        direction=data.get("direction", "neutral"),
        action=data.get("action", "hold"),
        signal=data.get("signal", ""),
        confidence=_parse_confidence(data.get("confidence")),
        timeframe=data.get("timeframe", ""),
        analysis=str(data.get("analysis", answer[:2000]))[:2000],
        factors=data.get("factors", []),
        input_params={"user_query": user_query},
        tools_called=tools_used,
        elapsed_ms=elapsed_ms,
    )


def _regex_extract(answer: str) -> Dict:
    """正则 fallback 提取。"""
    result = {}
    m = re.search(r'(?:评分|score)[：:\s]*(\d+(?:\.\d+)?)', answer, re.I)
    if m:
        result["score"] = float(m.group(1))
    al = answer.lower()
    if any(k in al for k in ["买入", "buy", "看多"]):
        result["direction"] = "bullish"
        result["action"] = "buy"
    elif any(k in al for k in ["卖出", "sell", "看空"]):
        result["direction"] = "bearish"
        result["action"] = "sell"
    return result


def _clamp(v, lo, hi):
    if v is None:
        return None
    try:
        return max(lo, min(hi, float(v)))
    except (ValueError, TypeError):
        return None


def _parse_confidence(v) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return max(0.0, min(1.0, float(v)))
    return {"high": 0.8, "medium": 0.5, "low": 0.3}.get(str(v).lower(), 0.5)


# ═══════════════════════════════════════════════════════════════
# 3. DB 操作
# ═══════════════════════════════════════════════════════════════

def _get_db():
    from app.utils.db import get_db_connection
    return get_db_connection()


def init_tables():
    """创建表（幂等）。"""
    with _get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS qd_traces (
                id SERIAL PRIMARY KEY,
                parent_id INTEGER REFERENCES qd_traces(id),
                root_id INTEGER REFERENCES qd_traces(id),
                layer VARCHAR(10) NOT NULL,
                step_order INTEGER DEFAULT 0,
                exec_date DATE NOT NULL,
                stock_code VARCHAR(10),
                stock_name VARCHAR(50),
                name VARCHAR(100) NOT NULL,
                score REAL,
                direction VARCHAR(20),
                action VARCHAR(10),
                signal TEXT,
                confidence REAL,
                timeframe VARCHAR(10),
                analysis TEXT,
                factors JSONB,
                input_params JSONB,
                output_summary JSONB,
                tools_called TEXT[],
                missing_data TEXT[],
                status VARCHAR(20) DEFAULT 'ok',
                error TEXT,
                elapsed_ms REAL DEFAULT 0,
                data_source VARCHAR(50),
                exit_date DATE,
                exit_reason VARCHAR(20),
                pnl_pct REAL,
                hold_days INTEGER,
                correct BOOLEAN,
                calibration REAL DEFAULT 1.0,
                session_id VARCHAR(100),
                user_query TEXT,
                model VARCHAR(100),
                total_tokens INTEGER,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS qd_skill_weights (
                skill_name VARCHAR(100) PRIMARY KEY,
                weight REAL DEFAULT 1.0,
                win_rate REAL,
                avg_pnl_pct REAL,
                avg_hold_days REAL,
                return_per_day REAL,
                profit_loss_ratio REAL,
                sample_count INTEGER DEFAULT 0,
                decay_half_life INTEGER DEFAULT 30,
                last_updated TIMESTAMPTZ
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS qd_factor_weights (
                id SERIAL PRIMARY KEY,
                skill_name VARCHAR(100) NOT NULL,
                factor_name VARCHAR(100) NOT NULL,
                weight REAL DEFAULT 1.0,
                win_rate REAL,
                avg_pnl_pct REAL,
                avg_hold_days REAL,
                return_per_day REAL,
                sample_count INTEGER DEFAULT 0,
                decay_half_life INTEGER DEFAULT 30,
                last_updated TIMESTAMPTZ,
                UNIQUE(skill_name, factor_name)
            )
        """)
        # 索引
        for idx_sql in [
            "CREATE INDEX IF NOT EXISTS idx_traces_root ON qd_traces(root_id)",
            "CREATE INDEX IF NOT EXISTS idx_traces_layer ON qd_traces(layer)",
            "CREATE INDEX IF NOT EXISTS idx_traces_stock ON qd_traces(stock_code, exec_date)",
            "CREATE INDEX IF NOT EXISTS idx_traces_pending ON qd_traces(id) WHERE layer='chain' AND exit_date IS NULL",
        ]:
            cur.execute(idx_sql)
        conn.commit()
        cur.close()


def save_tree(root: EvalNode, session_id: str = "", user_query: str = "",
              model: str = "") -> int:
    """将 EvalNode 树存入 qd_traces，返回根节点 ID。"""
    with _get_db() as conn:
        cur = conn.cursor()
        root_id = _insert_node(cur, root, None, session_id, user_query, model)
        conn.commit()
        cur.close()
    return root_id


def _insert_node(cur, node: EvalNode, parent_id: Optional[int],
                 session_id: str, user_query: str, model: str) -> int:
    """递归插入节点。"""
    cur.execute("""
        INSERT INTO qd_traces (
            parent_id, root_id, layer, step_order, exec_date,
            stock_code, stock_name, name, score, direction, action,
            signal, confidence, timeframe, analysis, factors,
            input_params, output_summary, tools_called, missing_data,
            status, error, elapsed_ms, data_source,
            session_id, user_query, model
        ) VALUES (
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s, %s
        ) RETURNING id
    """, (
        parent_id, None, node.layer, 0, node.exec_date or date.today().isoformat(),
        node.stock_code or None, node.stock_name or None, node.name,
        node.score, node.direction, node.action,
        node.signal, node.confidence, node.timeframe,
        node.analysis[:2000] if node.analysis else None,
        json.dumps(node.factors, ensure_ascii=False) if node.factors else None,
        json.dumps(node.input_params, ensure_ascii=False) if node.input_params else None,
        json.dumps(node.output_summary, ensure_ascii=False, default=str) if node.output_summary else None,
        node.tools_called or None, node.missing_data or None,
        node.status, node.error or None, node.elapsed_ms, node.data_source or None,
        session_id or None, user_query or None, model or None,
    ))
    node_id = cur.fetchone()[0]

    # 更新 root_id
    if parent_id is None:
        cur.execute("UPDATE qd_traces SET root_id = %s WHERE id = %s", (node_id, node_id))

    # 递归子节点
    for i, child in enumerate(node.children):
        child.step_order = i
        _insert_node(cur, child, node_id, session_id, user_query, model)

    return node_id


# ═══════════════════════════════════════════════════════════════
# 4. 权重查询（注入 Agent instructions）
# ═══════════════════════════════════════════════════════════════

def get_skill_weights_text() -> str:
    """获取 Skill 权重表，格式化为 Agent instructions 注入文本。"""
    try:
        with _get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT skill_name, weight, win_rate, return_per_day, sample_count
                FROM qd_skill_weights
                WHERE sample_count > 0
                ORDER BY weight DESC
            """)
            rows = cur.fetchall()
            cur.close()
    except Exception:
        return ""

    if not rows:
        return ""

    lines = ["## 技能权重（按历史收益率迭代）", "",
             "| 技能 | 权重 | 单位时间收益率 | 胜率 | 样本数 |",
             "|------|------|-------------|------|--------|"]
    for r in rows:
        w = r["weight"] or 1.0
        rpd = r["return_per_day"] or 0
        wr = r["win_rate"] or 0
        sc = r["sample_count"] or 0
        lines.append(f"| {r['skill_name']} | {w:.2f} | {rpd:+.2f}%/天 | {wr:.0%} | {sc} |")
    return "\n".join(lines)


def get_factor_weights_text(skill_name: str = "") -> str:
    """获取因子权重（可按 Skill 过滤）。"""
    try:
        with _get_db() as conn:
            cur = conn.cursor()
            if skill_name:
                cur.execute("""
                    SELECT factor_name, weight, return_per_day, sample_count
                    FROM qd_factor_weights WHERE skill_name = %s AND sample_count > 0
                    ORDER BY weight DESC
                """, (skill_name,))
            else:
                cur.execute("""
                    SELECT skill_name, factor_name, weight, return_per_day, sample_count
                    FROM qd_factor_weights WHERE sample_count > 0
                    ORDER BY weight DESC LIMIT 30
                """)
            rows = cur.fetchall()
            cur.close()
    except Exception:
        return ""

    if not rows:
        return ""

    lines = ["| 因子 | 权重 | 单位时间收益率 | 样本数 |",
             "|------|------|-------------|--------|"]
    for r in rows:
        prefix = f"{r['skill_name']}:" if "skill_name" in r.keys() and not skill_name else ""
        lines.append(f"| {prefix}{r['factor_name']} | {r['weight']:.2f} | {r['return_per_day']:+.2f}%/天 | {r['sample_count']} |")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# 5. DecisionCard 格式化
# ═══════════════════════════════════════════════════════════════

_ACTION_CN = {"buy": "买入", "sell": "卖出", "hold": "观望", "skip": "跳过"}
_DIR_CN = {"bullish": "看多", "bearish": "看空", "neutral": "中性"}
_CONF_CN = {"high": "高", "medium": "中", "low": "低"}
_TF_CN = {"T+1": "1天", "T+3": "3天", "T+5": "5天", "1W": "1周", "1M": "1月", "3M": "3月", "1Y": "1年"}


def format_decision_card(data: Dict) -> str:
    """将 Agent 输出的 JSON 格式化为标准卡片。"""
    action = _ACTION_CN.get(data.get("action", ""), "观望")
    name = data.get("stock_name", "")
    code = data.get("stock_code", "")
    tf = _TF_CN.get(data.get("timeframe", ""), data.get("timeframe", ""))
    score = data.get("score", 0)
    direction = _DIR_CN.get(data.get("direction", ""), "中性")
    conf = _CONF_CN.get(str(data.get("confidence", "")).lower(), "中")

    lines = [f"**{action}** {name}({code})" if code else f"**{action}**",
             f"维度:{tf} 评分:{score:.0f} 方向:{direction} 置信:{conf}"]

    factors = data.get("factors", [])
    if factors:
        parts = []
        for f in factors:
            s = f"{f['score']:.0f}" if f.get("score") is not None else "—"
            parts.append(f"{f['name']}:{s}")
        lines.append(" | ".join(parts))

    if data.get("signal"):
        lines.append(f"信号: {data['signal']}")

    if data.get("analysis"):
        lines.append(f"\n<details><summary>详细分析</summary>\n\n{data['analysis']}\n</details>")

    return "\n".join(lines)
