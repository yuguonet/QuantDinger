# -*- coding: utf-8 -*-
"""scripts/backtest_svc_cli.py — svc 泳道 CLI（agent 回测桥的子进程消费者，Task 8）。

只做一件事：读 snapshot.json → 纯计算回测入口 → report.json 落盘。
重依赖（svc 引擎等）全部在 `_load_engines()` 函数内惰性 import —— 本模块可被
单元测试安全 import（顶层零重依赖），替身注入也以 `_load_engines` 为唯一接缝。

铁律（改进 L；守卫为 AST 调用级——改进 N——所以这里可以正常写明禁令）：只调
`BacktestService.run_strategy_snapshot`（纯计算、不落库），**绝不调用
`BacktestService.persist_run` / `ensure_storage_schema`**（落库与 DDL 均不在
run_strategy_snapshot 调用链上）⇒ qd_backtest_runs/trades/equity_points 零写；
运行时只依赖 app.services.*，绝不 import 桥/能力层/工具面（守卫测试
test_svc_cli_only_talks_to_services 钉住）。

关键设计点：
  - 失败语义（改进 H）：可判定的业务失败（空结果 / 引擎返回 {"error": ...}）走
    stderr 文本 + 退出码 2，桥捕获后诚实落盘 ok=False；未预期异常以 traceback
    退出码 1，桥取 stderr 尾部 2000 字符——两条路都可追溯；
  - svc_code 直构 snapshot 是**纯 dict**，键集 = services/backtest.run_strategy_snapshot
    的 snapshot.get 读取面（code/market/symbol/timeframe/initial_capital/commission/
    slippage/leverage/trade_direction/strategy_config/indicator_params/indicator_id/
    user_id/run_type，F12）——桥侧已校验，这里只兜形状合法性（缺 code 拒绝）；
  - end_date 补到 23:59:59：日线取数按时间戳过滤，不含当日会丢最后一天 K 线。

易错点：
  - 必须以 `python -m` 且 cwd=backend 根被调起（模块解析 + .env 契约，同 backtest_cli）；
  - svc_strategy 分支要连 DB（StrategyService/Resolver），仅对照组用；svc_code 分支
    零 DB——两条分支的依赖差异由 _build_snapshot 内惰性 import 天然隔离。

用法（桥以 `python -m` 子进程调用，cwd=backend 根）：
  python -m app.agent.scripts.backtest_svc_cli \
      --snapshot-file <job>/snapshot.json --start-date 2025-01-01 \
      --end-date 2025-06-30 --out <job>/report.json

snapshot.json 两种形态（桥侧构造，改进 J）：
  svc_code 直构：{"kind": "svc_code", "snapshot": {...纯 dict（键集见上）...}}
  svc_strategy 对照：{"kind": "svc_strategy", "strategy_id": N, "symbol": "600519",
    "timeframe": "1D", "user_id": 1} → 子进程内走 resolver（与既有工具同语义）
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path


def _load_engines():
    """函数内 import（不在模块顶层）：测试可整体替身，且 CLI import 零副作用。"""
    from app.services.backtest import BacktestService
    from app.services.strategy import StrategyService
    from app.services.strategy_snapshot import StrategySnapshotResolver
    return BacktestService, StrategyService, StrategySnapshotResolver


def _build_snapshot(spec: dict) -> dict:
    """snapshot.json → snapshot dict。svc_code 直构（纯 dict，F12）；svc_strategy 走 resolver。"""
    kind = spec.get("kind")
    if kind == "svc_code":
        snap = spec.get("snapshot")
        if not isinstance(snap, dict) or not str(snap.get("code") or "").strip():
            raise ValueError("svc_code 载体缺少 snapshot.code")
        return snap
    if kind == "svc_strategy":
        _, StrategySvc, Resolver = _load_engines()
        strategy_id = int(spec["strategy_id"])
        user_id = int(spec.get("user_id") or 1)
        strategy = StrategySvc().get_strategy(strategy_id, user_id=user_id)
        if not strategy:
            raise ValueError(f"策略 {strategy_id} 不存在")
        override = {}
        if spec.get("symbol"):
            override["symbol"] = spec["symbol"]
        if spec.get("timeframe"):
            override["timeframe"] = spec["timeframe"]
        snap = Resolver(user_id=user_id).resolve(strategy, override)
        snap["user_id"] = user_id
        return snap
    raise ValueError(f"未知载体 kind: {kind!r}")


def main() -> int:
    p = argparse.ArgumentParser(description="svc 泳道回测 CLI（纯计算，零落库）")
    p.add_argument("--snapshot-file", required=True)
    p.add_argument("--start-date", required=True, help="YYYY-MM-DD")
    p.add_argument("--end-date", required=True, help="YYYY-MM-DD")
    p.add_argument("--out", required=True, help="report.json 输出路径")
    args = p.parse_args()

    spec = json.loads(Path(args.snapshot_file).read_text(encoding="utf-8"))
    snapshot = _build_snapshot(spec)
    start = datetime.strptime(args.start_date, "%Y-%m-%d")
    end = datetime.strptime(args.end_date, "%Y-%m-%d").replace(
        hour=23, minute=59, second=59)
    BacktestService, _, _ = _load_engines()
    result = BacktestService().run_strategy_snapshot(
        snapshot, start_date=start, end_date=end)
    if not result:
        print("回测返回空结果", file=sys.stderr)
        return 2
    if isinstance(result, dict) and result.get("error"):
        # script 类策略的失败路径直接返回 {"error": ...}（与既有工具的判定一致）
        print(str(result["error"]), file=sys.stderr)
        return 2
    Path(args.out).write_text(
        json.dumps(result, ensure_ascii=False, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
