# -*- coding: utf-8 -*-
"""auto_bt_tools 单测：命名红线（WRITE_PREFIXES）+ 载体分发 + 预算前置校验。

Task 5/6（backtest_bridge_plan.md）+ 批次C质量审查修正（2026-10-15）：
  - 失败形态按 finance 域约定断言**裸 {"error": ...}**（_fn_adapter._to_output 只对
    单键 error dict 置 success=False；双键 {"ok": False, "error"} 会把失败误标成功）；
  - 载体规整统一走桥 parse_carrier 单源，此前逃逸路径（畸形 proposal_json /
    非 object / days='abc' / 半③档静默降级 existing）在此钉死为可归一 error。

易错点：
  - 技能面用例经 spec_from_file_location 按路径加载 run.py（目录名带连字符，不可包
    导入，照 market-screener 先例）；monkeypatch 必须在加载**之前**打在桥模块上 ——
    run.py 的 `from ... import run_backtest_job` 在 exec_module 时才绑定；
  - autouse 钉 QD_BT_ROOT：技能 run() 起手 gc_jobs() 会扫桥产物根，不钉的话单测
    可能搬动开发机真实作业目录（GC 只动超 TTL 的目录，但测试不该有这个副作用）；
  - 全部用例 fake 子进程/桥函数，零 DB 零网络（文件头部契约）。
"""
import importlib.util
import json
from pathlib import Path

import pytest

from app.agent.capabilities.scanner import is_write_name
from app.agent.tools.finance import auto_bt_tools as T

_SKILL_RUN = Path(__file__).resolve().parents[1] / "app/agent/skills/auto-backtest/run.py"


@pytest.fixture(autouse=True)
def _tmp_bt_root(tmp_path, monkeypatch):
    """桥产物根指向 tmp（技能 gc_jobs / 桥落盘都不碰真实 data/backtest_bridge）。"""
    monkeypatch.setenv("QD_BT_ROOT", str(tmp_path / "bt"))


def _load_skill_run():
    spec = importlib.util.spec_from_file_location("auto_bt_skill_run", _SKILL_RUN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ================================================================
# Task 5：工具面
# ================================================================

def test_tool_name_avoids_write_prefixes():
    assert not is_write_name("agent_backtest_strategy")   # §5.6 坑表：run_ 前缀硬拒


def test_empty_codes_rejected(monkeypatch):
    captured = {}

    def fake_run(**kw):
        captured.update(kw)
        return {"ok": True}

    monkeypatch.setattr(T, "run_backtest_job", fake_run)
    out = T.agent_backtest_strategy(strategy_key="break", codes="")
    assert set(out) == {"error"} and "技能" in out["error"]   # M1：裸 {"error"} 形态
    assert not captured


def test_bad_codes_rejected():
    out = T.agent_backtest_strategy(strategy_key="break", codes="000001,ABCD")
    assert set(out) == {"error"} and "数字" in out["error"]


def test_fullwidth_codes_rejected():
    """⑪（P2）：全角数字 isdigit=True 会穿透到 CLI 取数层才炸——工具面就必须拒。"""
    out = T.agent_backtest_strategy(strategy_key="break", codes="０００００１")
    assert set(out) == {"error"} and "数字" in out["error"]


def test_proposal_carrier_dispatch(monkeypatch):
    captured = {}

    def fake_run(**kw):
        captured.update(kw)
        return {"ok": True}

    monkeypatch.setattr(T, "run_backtest_job", fake_run)
    proposal = {"strategy": "break", "edits": [{"kind": "param",
                                                "target": "x", "before": 1, "after": 2}]}
    out = T.agent_backtest_strategy(proposal_json=json.dumps(proposal),
                                    codes="000001 600000", days=30)
    assert out["ok"] is True
    assert captured["carrier"]["kind"] == "proposal"
    assert captured["codes"] == ["000001", "600000"]
    assert captured["days"] == 30


def test_new_carrier_requires_key():
    out = T.agent_backtest_strategy(strategy_py="x", strategy_yaml='{"a": 1}',
                                    codes="000001")
    assert set(out) == {"error"} and "strategy_key" in out["error"]


def test_existing_carrier_via_key_only(monkeypatch):
    """只传 strategy_key = existing 对照组（零改动回测现有策略）。"""
    captured = {}

    def fake_run(**kw):
        captured.update(kw)
        return {"ok": True}

    monkeypatch.setattr(T, "run_backtest_job", fake_run)
    out = T.agent_backtest_strategy(strategy_key="break", codes="000001")
    assert out["ok"] is True
    assert captured["carrier"] == {"kind": "existing", "key": "break"}


def test_missing_carrier_rejected():
    out = T.agent_backtest_strategy(codes="000001")
    assert set(out) == {"error"} and "载体缺失" in out["error"]


# ---- 批次C质量审查：此前逃逸路径钉死（I1/I2/I3/C1 + 归一序）----

def test_malformed_proposal_json_normalized():
    """I1：畸形 proposal_json 必须归一为 {"error"}，不许 JSONDecodeError 裸逃逸。"""
    out = T.agent_backtest_strategy(proposal_json="{not-json", codes="000001")
    assert set(out) == {"error"} and "proposal_json" in out["error"]


def test_non_object_proposal_json_normalized():
    """I3：合法 JSON 但非 object（如字符串字面量）必须归一为 error。"""
    out = T.agent_backtest_strategy(proposal_json='"just-a-string"', codes="000001")
    assert set(out) == {"error"} and "JSON object" in out["error"]


def test_bad_days_normalized():
    """I2：days='abc' 走桥 _to_positive_int → BridgeError，不许裸 ValueError 逃逸。"""
    out = T.agent_backtest_strategy(strategy_key="break", codes="000001", days="abc")
    assert set(out) == {"error"} and "days" in out["error"]


def test_half_new_carrier_rejected_not_existing(monkeypatch):
    """C1：只给 strategy_py 不给 strategy_yaml 必须 error，严禁静默降级成 existing。"""
    captured = {}

    def fake_run(**kw):
        captured.update(kw)
        return {"ok": True}

    monkeypatch.setattr(T, "run_backtest_job", fake_run)
    out = T.agent_backtest_strategy(strategy_py="x", codes="000001")
    assert set(out) == {"error"} and "strategy_yaml" in out["error"]
    assert not captured


def test_bridge_busy_normalized_before_bridge_error(monkeypatch):
    """BridgeBusy 是 BridgeError 子类：except 顺序颠倒会丢「回测桥忙」前缀，钉住归一序。"""
    def fake_run(**kw):
        raise T.BridgeBusy("已有 1 个作业在跑")

    monkeypatch.setattr(T, "run_backtest_job", fake_run)
    out = T.agent_backtest_strategy(strategy_key="break", codes="000001")
    assert set(out) == {"error"} and "回测桥忙" in out["error"]


# ================================================================
# Task 6：技能面 skills/auto-backtest/
# ================================================================

def test_skill_registered_in_whitelist():
    from app.agent.scripts.skill_run import EXECUTABLE_SKILLS
    assert EXECUTABLE_SKILLS.get("auto-backtest") == \
        ("skills.auto-backtest.run", ("run",), "run")


def test_skill_run_dispatches_scope_all(monkeypatch):
    captured = {}

    def fake_run(**kw):
        captured.update(kw)
        return {"ok": True, "job_id": "j1", "stats": {"n": 120},
                "strategy_key": "break", "scope": "all", "days": 180,
                "report_path": "/tmp/r.json", "summary_path": "/tmp/s.json",
                "elapsed_seconds": 12.0}

    monkeypatch.setattr("app.agent.chain.backtest_bridge.run_backtest_job", fake_run)
    mod = _load_skill_run()
    # Step 1.5 核对结论：skill_run.py 调用形态是 fn(**kwargs)（market-screener 同款），
    # 非计划预填的 run(args: dict) —— 测试同步为关键字实参
    out = mod.run(scope="all", strategy_key="break", days=180)
    assert out["ok"] is True
    assert captured["scope"] == "all" and captured["days"] == 180
    # M4：preview 弱断言 → 必含 strategy_key 与 report_path（O(摘要) 的实质承诺）
    assert "break" in out["preview"] and "/tmp/r.json" in out["preview"]
    assert len(out["preview"]) <= 20000     # F7 预览上限口径


def test_skill_run_rejects_missing_carrier():
    mod = _load_skill_run()
    out = mod.run(scope="all")
    assert set(out) == {"error"}


def test_skill_non_object_proposal_normalized():
    """技能面同享 parse_carrier 单源：非 object proposal 归一为 error，不抛异常。"""
    mod = _load_skill_run()
    out = mod.run(scope="all", proposal_json='"just-a-string"')
    assert set(out) == {"error"} and "JSON object" in out["error"]


def test_skill_half_new_carrier_rejected(monkeypatch):
    """技能面半③档（只给 py 不给 yaml）：必须 error，不许静默路由成 existing（C1）。"""
    calls = []

    def fake_run(**kw):
        calls.append(kw)
        return {"ok": True}

    monkeypatch.setattr("app.agent.chain.backtest_bridge.run_backtest_job", fake_run)
    mod = _load_skill_run()
    out = mod.run(scope="all", strategy_py="print(1)")
    assert set(out) == {"error"} and "strategy_yaml" in out["error"]
    assert not calls


# ================================================================
# Task 8：svc 泳道工具 agent_backtest_svc（指标/脚本策略链桥）
# 失败形态同 finance 域约定：裸 {"error": ...}（单键判据，见文件头）
# ================================================================

def test_svc_tool_name_avoids_write_prefixes():
    assert not is_write_name("agent_backtest_svc")


def test_svc_tool_code_carrier(monkeypatch):
    captured = {}

    def fake_svc(**kw):
        captured.update(kw)
        return {"ok": True}

    monkeypatch.setattr(T, "run_svc_backtest_job", fake_svc)
    out = T.agent_backtest_svc(code="def on_bar(ctx): pass",
                               run_type="strategy_script", symbol="600519",
                               start_date="2025-01-01", end_date="2025-03-31")
    assert out["ok"] is True
    assert captured["carrier"] == {"kind": "svc_code", "run_type": "strategy_script",
                                   "code": "def on_bar(ctx): pass"}
    assert captured["symbol"] == "600519"


def test_svc_tool_strategy_carrier(monkeypatch):
    captured = {}

    def fake_svc(**kw):
        captured.update(kw)
        return {"ok": True}

    monkeypatch.setattr(T, "run_svc_backtest_job", fake_svc)
    out = T.agent_backtest_svc(strategy_id=7, symbol="600519",
                               start_date="2025-01-01", end_date="2025-03-31")
    assert out["ok"] is True
    assert captured["carrier"] == {"kind": "svc_strategy", "strategy_id": 7}


def test_svc_tool_both_carriers_rejected():
    out = T.agent_backtest_svc(code="x", strategy_id=7, symbol="600519",
                               start_date="2025-01-01", end_date="2025-03-31")
    assert set(out) == {"error"} and "互斥" in out["error"]


def test_svc_tool_date_format_rejected():
    out = T.agent_backtest_svc(code="x", symbol="600519",
                               start_date="20250101", end_date="2025-03-31")
    assert set(out) == {"error"} and "YYYY-MM-DD" in out["error"]


def test_svc_tool_non_numeric_normalized():
    """I1：数值强转失败（commission='abc'）必须归一为裸 {"error"}，不许 ValueError 裸逃逸。"""
    out = T.agent_backtest_svc(code="x", symbol="600519",
                               start_date="2025-01-01", end_date="2025-03-31",
                               commission="abc")
    assert set(out) == {"error"} and "必须是数字" in out["error"]


def test_svc_tool_non_int_strategy_id_normalized():
    """⑩（P2）：int(strategy_id) 曾在 try 外——strategy_id='abc' 裸抛 ValueError
    逃逸出工具（LLM 侧可见 traceback）。carrier 构造挪进 try 后必须归一为裸 error。"""
    out = T.agent_backtest_svc(strategy_id="abc", symbol="600519",
                               start_date="2025-01-01", end_date="2025-03-31")
    assert set(out) == {"error"} and "必须是数字" in out["error"]


def test_svc_tool_params_passthrough(monkeypatch):
    """批次E Minor 5/6：user_id 与执行假设（commission/slippage/leverage/
    trade_direction/strategy_config）的工具→桥透传断言（captured 对照）。"""
    captured = {}

    def fake_svc(**kw):
        captured.update(kw)
        return {"ok": True}

    monkeypatch.setattr(T, "run_svc_backtest_job", fake_svc)
    out = T.agent_backtest_svc(
        code="x", symbol="600519", start_date="2025-01-01", end_date="2025-03-31",
        commission=0.0003, slippage=0.001, leverage=2, trade_direction="short",
        strategy_config_json='{"ashare_rules": {"t_plus_1": true}}', user_id=5)
    assert out["ok"] is True
    assert captured["user_id"] == 5
    assert captured["commission"] == 0.0003
    assert captured["slippage"] == 0.001
    assert captured["leverage"] == 2
    assert captured["trade_direction"] == "short"
    assert captured["strategy_config"] == {"ashare_rules": {"t_plus_1": True}}


def test_svc_tool_initial_capital_passthrough(monkeypatch):
    """⑰-svc（P1）：initial_capital 工具→桥透传；0=不指定（None ⇒ 不写 snapshot
    键 ⇒ 引擎默认 10000）。LLM 侧传字符串也必须走数值归一，不许裸逃逸。"""
    captured = {}

    def fake_svc(**kw):
        captured.update(kw)
        return {"ok": True}

    monkeypatch.setattr(T, "run_svc_backtest_job", fake_svc)
    out = T.agent_backtest_svc(code="x", symbol="600519",
                               start_date="2025-01-01", end_date="2025-03-31",
                               initial_capital=50000)
    assert out["ok"] is True and captured["initial_capital"] == 50000
    T.agent_backtest_svc(code="x", symbol="600519",
                         start_date="2025-01-01", end_date="2025-03-31",
                         initial_capital="25000.5")                 # 字符串也归一
    assert captured["initial_capital"] == 25000.5
    T.agent_backtest_svc(code="x", symbol="600519",
                         start_date="2025-01-01", end_date="2025-03-31")
    assert captured["initial_capital"] is None                     # 0=不指定


def test_svc_tool_initial_capital_negative_rejected():
    out = T.agent_backtest_svc(code="x", symbol="600519",
                               start_date="2025-01-01", end_date="2025-03-31",
                               initial_capital=-5)
    assert set(out) == {"error"} and "initial_capital" in out["error"]
