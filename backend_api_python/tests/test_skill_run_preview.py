# -*- coding: utf-8 -*-
"""守卫：run_skill 回传主 agent 的预览必须**按整条目裁剪**，永不盲切 JSON（2026-10-21）。

【背景】market-screener 漏斗扩容（search_top_n=100、deep 8→12）后，run() 契约
产出 12 张 deep_analyze 证据卡 ≈ 24K 字符，而 skill_run 旧预览上限 20000 且按
字符盲切：`preview[:20000] + "\n……（结果共 X 字符…）"`。切点落在证据卡中间，
模型拿到的是**半张卡 + 悬空括号的坏 JSON**，只能瞎猜或整单重跑；全量虽在
full_path，但主 agent 不读文件 ⇒ 末尾 1~2 只票的证据事实上丢失。

【修法】根因是「按字符截断」而不是「按结构截断」，故：
  · 预算对齐契约：_PREVIEW_LIMIT 20000 → 32000（推导见 skill_run 常量注释），
    12 卡常态完整送达；
  · 装不下时按**整条目**裁（列表尾部先丢整条，deep_analyze 尾部 = p_up 最低），
    `result` 里条目要么完整、要么整条不在，**永远不会半条**；
  · 裁剪事实走外层信封 preview_note（传输层元信息不混进技能载荷）；
  · 超大字符串是唯一改语义的层级，截断处留可见标记并如实计 strings_cut。

【本文件锁四件事】
  ① 缺省预算装得下 12 卡契约（防止预算再被拍脑袋改小 / 卡体积膨胀）；
  ② 超限裁剪后仍是合法 JSON，且留下的每张卡字段完整、是原顺序的前缀
     （丢的是尾部整条，不是半条、不是乱序）；
  ③ 超大字符串被截时 JSON 仍合法 + 标记可见 + preview_note 如实声明；
  ④ 源码里不再出现字符盲切（防后续改动悄悄还原旧写法）。
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SKILL_RUN = ROOT / "app" / "agent" / "scripts" / "skill_run.py"

# scripts/ 不是包（skill_run 以脚本形态被子进程执行），按文件路径直载。
# 模块顶层只 import 标准库，加载即安全。
_spec = importlib.util.spec_from_file_location("skill_run_under_test", SKILL_RUN)
skill_run = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(skill_run)


def _card(code: str) -> dict:
    """按 deep_analyze 返回口径拼一张代表性证据卡（约 1.2K~2K 字符）。"""
    return {
        "code": code, "name": f"测试{code}", "missing": [],
        "tech": {
            "bars": 120, "close": 12.35, "change_pct": 4.52, "amplitude_pct": 6.8,
            "close_pos_in_day": 72.5, "vol_ratio": 2.1, "rsi14": 61.3,
            "ma5": 11.9, "ma10": 11.62, "ma20": 11.28,
            "dist_ma5_pct": 3.8, "dist_ma10_pct": 6.3, "dist_ma20_pct": 9.5,
            "support": {"nearest": "ma5", "nearest_dist_pct": 3.8,
                        "near_support": False, "ma5": 11.9, "ma10": 11.62,
                        "ma20": 11.28},
            "ma_bull_stack": True, "pos_in_20d_pct": 82.0, "is_20d_high": False,
            "ret_5d_pct": 6.7, "ret_20d_pct": 14.2, "limit_up": False,
            "zt_streak": 0, "prev_zt_streak": 0,
        },
        "patterns": [
            {"pattern": "放量突破", "vol_ratio": 2.1, "breakout_pct": 4.8,
             "range_pct": 7.2, "day": 1},
            {"pattern": "底部放量", "vol_ratio": 2.1, "pos_pct": 82.0, "day": 3},
        ],
        "flow": {"ok": True, "main_net_wan": 3200.0, "net_pct_of_amount": 6.2},
        "rt": {"price": 12.41, "change_pct": 5.03, "previous_close": 11.81,
               "limit_up_price": 13.04, "limit_down_price": 10.66,
               "high": 12.6, "low": 11.75, "time": "2026-10-21 14:55:00",
               "name": f"测试{code}", "limit_up": False, "limit_down": False},
        "stale_bar": True, "rt_risks": [],
        "score": 64.0, "direction": "bullish", "confidence": 1.0,
        "signal": "收盘价位于20日区间82.0%、距MA20 9.5%、量比2.1、RSI61.3；实时12.41(+5.03%)",
        "p_up": 0.64,
        "evidence_dims": ["tech", "prediction", "patterns", "flow"],
    }


def _run_result(n: int = 12) -> dict:
    """按 run() 返回口径拼整包：顶层 market/funnel/report_guide + n 张卡。"""
    return {
        "report_guide": {"STEPS": "run_skill 一次即可交付", "horizon": "主写 T+1/T+3/T+5",
                         "priority": "支撑与弱转强/强转弱 ≫ 涨跌幅",
                         "fields": "tech.support.nearest/near_support 看贴哪条均线",
                         "board_cut": "zt_streak>=3 默认只写 T+1",
                         "words": "选股用 看多/中性/看空/回避",
                         "market": "mood/资金/热点已由 run_skill 覆盖",
                         "funnel": "funnel 是分层漏斗统计，写「数据可信度」时引用",
                         "buyable": "rt.limit_up=true → 只进排除表"},
        "funnel": {"rounds": 1, "pool_raw": 512, "excluded_obj": {"st": 12},
                   "scored": 80, "excluded_score": {"hard": 3},
                   "deep_n": n, "eastmoney_ok": True},
        "strategy": "post_market",
        "market": {"mood": "中性", "mood_score": 58, "zt_count": 52,
                   "dt_count": 11, "broken_rate": 28.5, "fund_flow": 1.2e9},
        "mood_regime": "neutral", "themes": ["人形机器人", "并购重组"],
        "hints": {"p_up_floor_suggest": 0.5},
        "signal": "分析 12 只（候选 80 只）", "score": 61.5,
        "direction": "neutral", "confidence": 0.9,
        "missing": [], "errors": [],
        "analyzed": [_card(f"{600000 + i}") for i in range(n)],
    }


def test_default_budget_holds_full_12_card_contract():
    """① 缺省预算必须装下 12 卡契约：完整送达，零裁剪。"""
    res = _run_result(12)
    preview, note = skill_run._build_preview(res, skill_run._preview_limit())
    assert note == {}, (
        f"12 卡契约被裁了（note={note}）：要么 _PREVIEW_LIMIT 被改小，"
        f"要么证据卡膨胀了——先改 skill_run 常量注释里的推导再调预算。"
    )
    for i in range(12):
        assert f"{600000 + i}" in preview, f"第 {i} 张卡的 code 不在预览里"


def test_trim_drops_whole_tail_items_and_keeps_valid_json():
    """② 超限裁剪：合法 JSON + 整条裁 + 尾部先裁（不是半条、不是乱序）。"""
    res = _run_result(12)
    original_codes = [c["code"] for c in res["analyzed"]]
    preview, note = skill_run._build_preview(res, 8000)   # 人为收紧逼出裁剪
    assert note.get("trimmed") is True
    assert note["dropped_items"].get("result.analyzed"), "应从 analyzed 整条裁"

    obj = json.loads(preview)   # 盲切在这里会 JSONDecodeError
    kept_codes = [c["code"] for c in obj["analyzed"]]
    assert kept_codes == original_codes[:len(kept_codes)], (
        "裁剪必须从列表尾部整条丢（deep_analyze 按 p_up 降序，尾巴最弱），"
        f"实际留下的顺序={kept_codes}"
    )
    # 每张留下的卡字段完整：与模板卡同键（半张卡 = 少字段）
    template_keys = set(_card("000000"))
    for c in obj["analyzed"]:
        assert set(c) == template_keys, f"{c.get('code')} 卡片字段不完整（被半切）"
    assert note["dropped_items"]["result.analyzed"] == 12 - len(kept_codes)
    assert len(preview) <= 8000 + 200   # 200 = preview_note 相关序列化余量


def test_scalar_cut_keeps_json_valid_and_visible():
    """③ 超大字符串截断：JSON 合法 + 标记可见 + preview_note 如实计数。"""
    res = {"text": "x" * 50000, "n": 1}
    preview, note = skill_run._build_preview(res, 5000)
    obj = json.loads(preview)
    assert skill_run._STR_CUT_MARK in obj["text"], "截断处必须留可见标记"
    assert note["strings_cut"] >= 1 and note["trimmed"] is True


def test_preview_limit_env_override_and_fallback(monkeypatch):
    """④ env 覆盖：QD_SKILL_PREVIEW_LIMIT 生效；垃圾值回落模块缺省。"""
    monkeypatch.setenv("QD_SKILL_PREVIEW_LIMIT", "1234")
    assert skill_run._preview_limit() == 1234
    monkeypatch.setenv("QD_SKILL_PREVIEW_LIMIT", "not-a-number")
    assert skill_run._preview_limit() == skill_run._PREVIEW_LIMIT
    monkeypatch.setenv("QD_SKILL_PREVIEW_LIMIT", "-5")
    assert skill_run._preview_limit() == skill_run._PREVIEW_LIMIT


def test_no_blind_char_cut_left_in_source():
    """④ 源码级防回退：字符盲切不得复活，主流程必须走 _build_preview。"""
    src = SKILL_RUN.read_text(encoding="utf-8")
    assert "preview[:_PREVIEW_LIMIT]" not in src, "字符盲切（旧根因）回来了"
    assert "_build_preview(result" in src, "main() 必须经 _build_preview 生成预览"
