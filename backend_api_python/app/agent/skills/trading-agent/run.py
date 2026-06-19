#!/usr/bin/env python3
"""trading-agent: 交易策略状态汇总（只读）。"""
import argparse, json, sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

def algo_analyze(stock_code, stock_name, tool_results, call_tool_fn=None):
    factors = []
    running_count = total_count = 0

    strategies = tool_results.get("list_strategies", {})
    strat_list = strategies.get("strategies", []) if isinstance(strategies, dict) else strategies

    if not strat_list:
        return {"skill": "trading_agent", "action": "hold", "score": 50, "direction": "neutral",
                "signal": "无交易策略", "confidence": "low", "factors": [], "analysis": "无策略", "status": "ok"}

    for strat in strat_list[:5]:
        strat_id = strat.get("id")
        strat_name = strat.get("name", f"策略{strat_id}")
        is_running = strat.get("status") == "running" or strat.get("is_running")
        total_count += 1
        if is_running: running_count += 1

        trades = None
        if call_tool_fn and strat_id:
            try: trades = call_tool_fn("get_strategy_trades", strategy_id=strat_id, limit=5)
            except Exception: pass

        trade_count = len(trades.get("trades", [])) if isinstance(trades, dict) else (len(trades) if isinstance(trades, list) else 0)
        status_str = "运行中" if is_running else "已停止"
        factors.append({"name": f"策略:{strat_name}", "value": f"{status_str}, 最近{trade_count}笔", "score": 60 if is_running else 50})

    score = min(50 + running_count * 5, 80)
    direction = "bullish" if score >= 60 else "neutral"
    return {
        "skill": "trading_agent", "action": "hold", "score": score,
        "direction": direction, "confidence": "medium",
        "signal": f"{running_count}/{total_count} 策略运行中",
        "factors": factors, "analysis": f"{running_count}/{total_count} 策略运行中", "status": "ok",
    }

def run(stock_code: str, stock_name: str = "", context: dict = None) -> dict:
    """薄壳入口：调用工具 + 算法分析，返回 dict。"""
    from app.agent.tools.trading_tools import list_strategies, get_strategy_trades

    results = {}
    try: results["list_strategies"] = list_strategies()
    except Exception as e: results["list_strategies"] = {"error": str(e)}

    def call_tool_fn(name, **kwargs):
        if name == "get_strategy_trades": return get_strategy_trades(**kwargs)
        raise ValueError(f"Unknown tool: {name}")

    return algo_analyze(stock_code, stock_name, results, call_tool_fn=call_tool_fn)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("stock_code")
    parser.add_argument("--name", default="")
    args = parser.parse_args()

    from app.agent.tools.trading_tools import list_strategies, get_strategy_trades

    results = {}
    try: results["list_strategies"] = list_strategies()
    except Exception as e: results["list_strategies"] = {"error": str(e)}

    def call_tool_fn(name, **kwargs):
        if name == "get_strategy_trades": return get_strategy_trades(**kwargs)
        raise ValueError(f"Unknown tool: {name}")

    print(json.dumps(algo_analyze(args.stock_code, args.name, results, call_tool_fn=call_tool_fn), ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
