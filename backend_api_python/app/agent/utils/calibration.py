# -*- coding: utf-8 -*-
"""
评分校准（方案 A1b，2026-09-26）：把工具输出的启发式 score(0-100) 映射到
P(方向正确=hit_rate) —— 用 sklearn IsotonicRegression 拟合 qd_agent_traces 历史 (score, correct)。

单一事实源：technical_analysis / bull_bear_research 的 hit_rate 字段一律从本模块取，
禁止各工具自己算概率（项目红线：不各写一套阈值）。

存储：qd_agent_weights(layer='calibration')，复用现有表结构，不加新列
  name        = skill_name（如 'technical_analysis'）
  skill_name  = score 桶字符串（如 '0'/'10'/.../'100'，主键第三列）
  weight      = 该桶的方向正确率 hit_rate
  win_rate    = 同 weight（冗余便于查询）
  sample_count= 该桶实际样本数

冷启动：总样本 < _MIN_SAMPLES 或无记录 → get 返回 None → 调用方标 calibrated=false / hit_rate=n/a。
依赖：sklearn.isotonic（惰性导入，缺失时 fit 返回 status='sklearn_missing'，不抛异常）。
"""
from __future__ import annotations

import logging
from typing import Dict, Optional

logger = logging.getLogger(__name__)

_CALIBRATION_LAYER = "calibration"
_MIN_SAMPLES = 10          # 总样本低于此数不输出校准（避免过拟合）
_BUCKET_STEP = 10          # score 分桶步长（0,10,...,100）


def _get_db():
    """惰性取 DB 连接（兼容 app.utils.db 与 utils.db 两种导入路径）。"""
    try:
        from app.utils.db import get_db_connection
        return get_db_connection
    except ImportError:
        from utils.db import get_db_connection
        return get_db_connection


def get(skill_name: str) -> Optional[Dict[int, float]]:
    """取某 skill 的 score桶→hit_rate 映射；无数据/样本不足返回 None。"""
    try:
        get_db_connection = _get_db()
    except ImportError:
        return None

    mapping: Dict[int, float] = {}
    total_samples = 0
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT skill_name, weight, sample_count FROM qd_agent_weights
                WHERE layer = %s AND name = %s AND sample_count >= 1
            """, (_CALIBRATION_LAYER, skill_name))
            for score_str, hit_rate, cnt in cur.fetchall():
                try:
                    score = int(float(score_str))
                except (ValueError, TypeError):
                    continue
                mapping[score] = float(hit_rate)
                total_samples += int(cnt or 0)
            cur.close()
    except Exception as e:
        logger.debug("[calibration] get(%s) failed: %s", skill_name, e)
        return None
    if not mapping or total_samples < _MIN_SAMPLES:
        return None
    return mapping


def apply(skill_name: str, score: float) -> Optional[float]:
    """把 score(0-100) 映射到 hit_rate；无校准返回 None。

    取 <= score 的最大已校准桶的 hit_rate（分段常数映射）。
    """
    mapping = get(skill_name)
    if not mapping:
        return None
    buckets = sorted(mapping.keys())
    hit_rate = None
    for b in buckets:
        if score >= b:
            hit_rate = mapping[b]
        else:
            break
    return hit_rate


def fit_score_map(skill_name: str) -> Dict:
    """从 qd_agent_traces 取 (score, correct) 拟合 isotonic，分桶存 qd_agent_weights。

    返回 {skill, samples, buckets, status}：
      status='ok'            成功
      status='insufficient'  样本不足
      status='sklearn_missing' sklearn 未安装
      status='db_error'/'write_error'  数据库异常
    """
    try:
        from sklearn.isotonic import IsotonicRegression
    except ImportError:
        return {"skill": skill_name, "samples": 0, "buckets": 0, "status": "sklearn_missing"}

    try:
        get_db_connection = _get_db()
    except ImportError:
        return {"skill": skill_name, "samples": 0, "buckets": 0, "status": "db_error",
                "error": "db module not found"}

    rows = []
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT score, correct FROM qd_agent_traces
                WHERE skill_name = %s AND score IS NOT NULL AND correct IS NOT NULL
                  AND score >= 0 AND score <= 100
            """, (skill_name,))
            rows = cur.fetchall()
            cur.close()
    except Exception as e:
        logger.warning("[calibration] fit 取数失败 %s: %s", skill_name, e)
        return {"skill": skill_name, "samples": 0, "buckets": 0,
                "status": "db_error", "error": str(e)}

    if len(rows) < _MIN_SAMPLES:
        return {"skill": skill_name, "samples": len(rows), "buckets": 0,
                "status": "insufficient"}

    scores = [float(r[0]) for r in rows]
    corrects = [1.0 if r[1] else 0.0 for r in rows]

    # IsotonicRegression 保证 score→hit_rate 单调不降（金融评分的基本假设）
    iso = IsotonicRegression(out_of_bounds="clip")
    iso.fit(scores, corrects)

    # 按 _BUCKET_STEP 分桶，取每桶实际样本数
    bucket_counts: Dict[int, int] = {}
    for s in scores:
        b = int(s // _BUCKET_STEP) * _BUCKET_STEP
        bucket_counts[b] = bucket_counts.get(b, 0) + 1

    # 在标准分桶点上用 isotonic 预测（保证单调性），只存有实际样本的桶
    bucket_scores = list(range(0, 101, _BUCKET_STEP))
    predicted = iso.predict(bucket_scores)

    inserted = 0
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            # 先删旧映射（全量替换，非增量）
            cur.execute("DELETE FROM qd_agent_weights WHERE layer = %s AND name = %s",
                        (_CALIBRATION_LAYER, skill_name))
            for b, pred_hr in zip(bucket_scores, predicted):
                cnt = bucket_counts.get(b, 0)
                if cnt < 1:
                    continue
                hr = round(float(pred_hr), 4)
                cur.execute("""
                    INSERT INTO qd_agent_weights
                        (layer, name, skill_name, weight, win_rate, sample_count, last_updated)
                    VALUES (%s, %s, %s, %s, %s, %s, NOW())
                """, (_CALIBRATION_LAYER, skill_name, str(b), hr, hr, cnt))
                inserted += 1
            conn.commit()
            cur.close()
    except Exception as e:
        logger.warning("[calibration] fit 写库失败 %s: %s", skill_name, e)
        return {"skill": skill_name, "samples": len(rows), "buckets": 0,
                "status": "write_error", "error": str(e)}

    return {"skill": skill_name, "samples": len(rows), "buckets": inserted, "status": "ok"}
