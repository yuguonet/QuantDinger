# -*- coding: utf-8 -*-
"""tools/finance/auto_bt_tools.py — agent × 双回测桥的工具面（backtest_bridge_plan.md
Task 5 / Task 8，§5.7，2026-10-15）。

只放「个股/小集合」桥工具；全盘量级走技能 skills/auto-backtest（能力层 60s 扛不住，
走 §5.3 子进程+产物落盘）。**不塞进 backtest_tools.py** —— 那是指标/脚本策略链
（BacktestService → qd_backtest_runs），与本桥（内存 trades / svc 纯计算、零写共享表）
两套口径，混文件必把工具预选送错链（§5.7 与现网两条回测链的关系表）。
双泳道：agent_backtest_strategy=auto 泳道（run_all 官方口径）；
agent_backtest_svc=svc 泳道（指标/脚本策略直构 snapshot，Task 8）。

命名红线：公开函数名避开 capabilities/scanner.WRITE_PREFIXES（run_/apply_ 等），
"agent_backtest_strategy"/"agent_backtest_svc" 由此得名（§5.6 坑表「run_ 前缀硬拒」）。

易错点：
  - 本工具是 agent 上下文内的同步调用（分钟级）——预算钳制（QD_BT_*）保证不超时；
    scope=all 永远不要从这里走；
  - auto 载体分发/入参规整**不在此处**：统一走桥 parse_carrier 单源（批次C质量审查 I4，
    工具面/技能面共用；这里只留「空 codes 指路技能」场景化提示）；
  - 失败返回**裸 {"error": ...}**（finance 域约定，_fn_adapter._to_output 只对单键
    error dict 置 success=False；带 "ok" 键会把失败误标成功）；BridgeBusy 必须先于
    BridgeError 接（前者是后者的子类，顺序颠倒丢「回测桥忙」前缀）；
  - 本文件禁止出现 svc 引擎的名字（import services.backtest）——svc 引擎只允许在
    backtest_svc_cli 子进程内被消费，守卫 test_tool_file_separate_from_backtest_tools
    钉住。
"""
from __future__ import annotations

import json
from datetime import datetime

from app.agent.chain.backtest_bridge import (
    BridgeBusy,
    BridgeError,
    parse_carrier,
    run_backtest_job,
    run_svc_backtest_job,
)


def agent_backtest_strategy(
    strategy_key: str = "",
    proposal_json: str = "",
    strategy_py: str = "",
    strategy_yaml: str = "",
    codes: str = "",
    days: int = 120,
) -> dict:
    """对 auto 折叠策略跑一次回测（独立桥：生产树只读、零写共享表、删作业目录即回滚）。

    载体三选一：
      ①/② proposal_json —— 对现有策略 <strategy>.yaml 的最小改动提案，最小 schema：
        {"strategy":"break","edits":[{"kind":"param","target":"x","before":1,"after":2}]}
        （真源 app/market_cn/auto/tools/proposal.py；改选择集须附
        evidence.stability/pool_check，由 proposal.validate 强制）；
      ③ strategy_py + strategy_yaml 成对 —— 全新策略（strategy_key 必填，agentbt_ 前缀；
        strategy_yaml 是 JSON 字符串）；
      existing —— 只传 strategy_key 且不传前两者 = 回测现有策略零改动（对照组）。
    全盘回测（scope=all）不在此工具：请走 auto-backtest 技能。

    Args:
        strategy_key: 现有策略 key（如 break/g56/dragon_callback）或新策略 key
        proposal_json: proposal JSON 字符串（①/②档，schema 见上）
        strategy_py: 新策略 .py 全文（③档，@register + 折叠契约）
        strategy_yaml: 新策略 yaml 的 JSON 字符串（③档）
        codes: 逗号/空格分隔股票代码（必填，≤ QD_BT_MAX_CODES 个）
        days: 回看自然日数（默认 120，上限 QD_BT_MAX_DAYS）

    Returns:
        成功：桥 summary（ok=True/job_id/stats{n,winrate,pl_ratio,...}/report_path（全量逐笔，
        agent 自取）/elapsed_seconds/scope/days/codes_n/codes_ok/overlay_digest/git_sha/
        effective_params/summary_path——口径单源 core.backtest._summary）；
        失败：裸 {"error": str}（含 stderr 尾部；畸形入参经 parse_carrier 归一）。
    """
    if not codes.strip():
        return {"error": "codes 不能为空；全盘回测请走 auto-backtest 技能"}
    try:
        carrier, code_list, days_int = parse_carrier(
            proposal_json=proposal_json, strategy_py=strategy_py,
            strategy_yaml=strategy_yaml, strategy_key=strategy_key,
            codes=codes, days=days)
        return run_backtest_job(carrier=carrier, scope="codes", codes=code_list,
                                days=days_int, strategy_key=strategy_key)
    except BridgeBusy as e:
        return {"error": f"回测桥忙: {e}"}
    except BridgeError as e:
        return {"error": str(e)}


def agent_backtest_svc(
    code: str = "",
    run_type: str = "strategy_indicator",
    strategy_id: int = 0,
    symbol: str = "",
    market: str = "CNStock",
    start_date: str = "",
    end_date: str = "",
    timeframe: str = "1D",
    commission: float = 0.0,
    slippage: float = 0.0,
    leverage: int = 1,
    trade_direction: str = "long",
    strategy_config_json: str = "",
    initial_capital: float = 0.0,
    user_id: int = 1,
) -> dict:
    """指标/脚本策略回测（svc 泳道独立桥：子进程纯计算、零写共享表、删作业目录即回滚）。

    载体二选一（互斥）：
      code —— agent 自拟策略：strategy_indicator（指标 DSL）或 strategy_script
       （含 on_bar 的 Python 脚本），直构 snapshot 回测，无需 DB 注册；
      strategy_id —— 对照组：回测 DB 已注册策略（子进程内走 resolver，与 run_backtest 同语义）。

    口径与问责：commission/slippage 默认 0 = 零费率毛收益；A 股 T+1/涨跌停假设须传
    strategy_config_json（如 {"ashare_rules": {"t_plus_1": true, "limit_pct": 0.10}}）；
    结果 cost_model 字段是实际口径，引用结论必须带口径。code 载体是 agent 自由代码，
    带前视/理想化风险，结论按「探索性参考」表述，不与 auto 泳道结论同权重进判定闭环。

    Args:
        code: 指标 DSL 或策略脚本全文（与 strategy_id 互斥）
        run_type: strategy_indicator（默认）| strategy_script
        strategy_id: DB 已注册策略 ID（与 code 互斥）
        symbol: 标的（如 600519 / BTC/USDT，必填；加密货币必须配 market="Crypto"）
        market: CNStock（默认）| Crypto | Forex | USStock | HKStock（完整集见 app/data_sources/factory.py）
        start_date / end_date: YYYY-MM-DD（上限 QD_BT_MAX_DAYS，默认 365 天）
        timeframe: K 线周期，默认 1D
        commission / slippage: 单边费率/滑点（比例），默认 0（毛收益口径）
        leverage: 杠杆倍数，默认 1
        trade_direction: long（默认）| short
        strategy_config_json: 执行假设 JSON（如 ashare_rules），默认空
        initial_capital: 初始本金，默认 0=引擎默认 10000；仅 code 载体生效（对照组由 DB 配置决定）
        user_id: 用户 ID（对照组按此过滤策略归属），默认 1

    Returns:
        成功：桥 summary（ok=True/lane="svc"/job_id/summary_metrics{totalReturn,maxDrawdown,
        winRate,...}（标量；trades/equityCurve 留 report.json，明细取 report_path）/
        cost_model{market,commission,slippage,trade_direction,source}/snapshot_digest/
        report_path/summary_path）；失败：裸 {"error": str}（含 stderr 尾部或校验原因）。
    """
    if bool(code) == bool(strategy_id):
        return {"error": "code 与 strategy_id 必须二选一（互斥载体）"}
    if not symbol.strip():
        return {"error": "symbol 必填（如 600519 或 BTC/USDT）"}
    if run_type not in ("strategy_indicator", "strategy_script"):
        return {"error": f"run_type 必须是 strategy_indicator|strategy_script: {run_type!r}"}
    strategy_config = None
    if strategy_config_json:
        try:
            strategy_config = json.loads(strategy_config_json)
        except (TypeError, ValueError):
            return {"error": f"strategy_config_json 不是合法 JSON: "
                             f"{str(strategy_config_json)[:200]!r}"}
        if not isinstance(strategy_config, dict):
            return {"error": f"strategy_config_json 必须是 JSON object: "
                             f"{str(strategy_config_json)[:200]!r}"}
    for d in (start_date, end_date):
        try:
            datetime.strptime(str(d), "%Y-%m-%d")
        except (TypeError, ValueError):
            return {"error": f"日期格式错误，请使用 YYYY-MM-DD: {d!r}"}
    try:
        # ⑩（P2）：carrier 构造（含 int(strategy_id)）必须在 try 内——此前在 try 外，
        # strategy_id='abc' 会以裸 ValueError 逃逸出工具（LLM 侧可见 traceback）。
        # ⑰-svc（P1）：initial_capital 归一同族——0=不指定（None ⇒ 不写 snapshot 键
        # ⇒ 引擎默认 10000），负数裸 error（不许静默钳制成 0/默认）。
        cap = float(initial_capital)
        if cap < 0:
            return {"error": "initial_capital 必须是正数（0 = 不指定，用引擎默认 10000）"}
        carrier = ({"kind": "svc_code", "run_type": run_type, "code": code} if code
                   else {"kind": "svc_strategy", "strategy_id": int(strategy_id)})
        return run_svc_backtest_job(
            carrier=carrier, symbol=symbol.strip(), market=str(market),
            start_date=str(start_date), end_date=str(end_date), timeframe=str(timeframe),
            commission=float(commission), slippage=float(slippage),
            leverage=int(leverage), trade_direction=str(trade_direction),
            strategy_config=strategy_config,
            initial_capital=cap if cap > 0 else None, user_id=int(user_id))
    except BridgeBusy as e:
        return {"error": f"回测桥忙: {e}"}
    except BridgeError as e:
        return {"error": str(e)}
    except (TypeError, ValueError) as e:
        # 局部数值强转归一（批次E质量审查 I1 + P2-⑩ 补缺）：int(strategy_id) 与
        # float(commission)/int(leverage)/int(user_id) 全部在 try 内。必须接在
        # BridgeBusy/BridgeError 之后——二者是 ValueError 子类，放前面会把桥的
        # 校验报文吞成通用文案。
        return {"error": f"strategy_id/commission/slippage/leverage/user_id 必须是数字: {e}"}
