#!/usr/bin/env python3
"""market-screener: 全市场短线选股。复用原 market_screener.py 的辅助函数。"""
import argparse, json, sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

def run(stock_code: str = "", stock_name: str = "", context: dict = None) -> dict:
    """薄壳入口，返回 dict。"""
    from app.agent.tools import registry as tool_registry
    tool_registry.discover()

    def call_tool_fn(tool_name, **kwargs):
        spec = tool_registry.get(tool_name)
        if not spec: raise ValueError(f"Unknown tool: {tool_name}")
        return spec.fn(**kwargs)

    from datetime import date
    from app.agent.skills.market_screener import _select_strategy, _run_intraday, _run_eod, _run_post_market

    strategy = _select_strategy()
    today = date.today().isoformat()

    if strategy == "intraday":
        report = _run_intraday("market_screener", today, call_tool_fn, [], [], [])
    elif strategy == "eod":
        report = _run_eod("market_screener", call_tool_fn, [], [], [])
    else:
        report = _run_post_market("market_screener", today, call_tool_fn, [], [], [])

    if report is None:
        return {"skill": "market_screener", "status": "failed", "error": "策略执行失败", "score": 0, "direction": "neutral", "confidence": 0, "factors": []}
    if hasattr(report, "to_dict"):
        d = report.to_dict()
        d.setdefault("skill", "market_screener")
        return d
    if isinstance(report, dict):
        report.setdefault("skill", "market_screener")
        return report
    return {"skill": "market_screener", "score": getattr(report, "score", 50),
            "direction": getattr(report, "direction", "neutral"),
            "signal": getattr(report, "signal", ""),
            "analysis": str(getattr(report, "analysis", ""))[:2000],
            "status": getattr(report, "status", "ok"), "confidence": 0.5, "factors": []}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("stock_code", nargs="?", default="")
    parser.add_argument("--name", default="")
    args = parser.parse_args()

    # 复用原 market_screener.py 的核心函数
    from app.agent.skills.market_screener import _select_strategy, _run_intraday, _run_eod, _run_post_market
    from app.agent.tools import registry as tool_registry
    tool_registry.discover()

    def call_tool_fn(tool_name, **kwargs):
        spec = tool_registry.get(tool_name)
        if not spec: raise ValueError(f"Unknown tool: {tool_name}")
        return spec.fn(**kwargs)

    from datetime import date
    strategy = _select_strategy()
    today = date.today().isoformat()

    if strategy == "intraday":
        report = _run_intraday("market_screener", today, call_tool_fn, [], [], [])
    elif strategy == "eod":
        report = _run_eod("market_screener", call_tool_fn, [], [], [])
    else:
        report = _run_post_market("market_screener", today, call_tool_fn, [], [], [])

    if report is None:
        output = {"skill": "market_screener", "status": "failed", "error": "策略执行失败"}
    elif hasattr(report, "to_dict"):
        output = report.to_dict()
    elif isinstance(report, dict):
        output = report
    else:
        output = {
            "skill": "market_screener", "score": getattr(report, "score", 50),
            "direction": getattr(report, "direction", "neutral"),
            "signal": getattr(report, "signal", ""),
            "analysis": str(getattr(report, "analysis", ""))[:2000],
            "status": getattr(report, "status", "ok"),
        }

    print(json.dumps(output, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
