#!/usr/bin/env python3
"""backtest-agent: 策略回测分析。"""
import argparse, json, sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

def algo_analyze(stock_code, stock_name, tool_results, call_tool_fn=None):
    factors = []
    best_score = 50.0
    best_strategy = None

    strategies = tool_results.get("list_strategies", {})
    strat_list = strategies.get("strategies", []) if isinstance(strategies, dict) else strategies

    if not strat_list:
        return {"skill": "backtest_agent", "action": "hold", "score": 50, "direction": "neutral",
                "signal": "无用户策略", "confidence": "low", "factors": [], "analysis": "无策略可回测", "status": "ok"}

    for strat in strat_list[:3]:
        strat_id = strat.get("id")
        strat_name = strat.get("name", f"策略{strat_id}")
        if not strat_id: continue

        bt_result = None
        if call_tool_fn:
            try: bt_result = call_tool_fn("run_backtest", strategy_id=strat_id, stock_code=stock_code)
            except Exception: pass

        if isinstance(bt_result, dict) and "error" not in bt_result:
            win_rate = bt_result.get("win_rate", 0)
            profit_loss_ratio = bt_result.get("profit_loss_ratio", 0)
            max_drawdown = bt_result.get("max_drawdown", 0)

            if win_rate >= 60 and profit_loss_ratio >= 2: score = 75
            elif win_rate >= 50 and profit_loss_ratio >= 1.5: score = 60
            elif win_rate < 40 or max_drawdown > 30: score = 30
            else: score = 50

            if score > best_score: best_score = score; best_strategy = strat_name
            factors.append({"name": f"回测:{strat_name}", "value": f"胜率{win_rate:.0%} 盈亏比{profit_loss_ratio:.1f} 回撤{max_drawdown:.0%}", "score": score})

    if not factors:
        return {"skill": "backtest_agent", "action": "hold", "score": 50, "direction": "neutral",
                "signal": "回测未产生结果", "confidence": "low", "factors": [], "analysis": "无数据", "status": "ok"}

    direction = "bullish" if best_score >= 60 else ("bearish" if best_score <= 40 else "neutral")
    return {
        "skill": "backtest_agent", "action": "hold", "score": best_score,
        "direction": direction, "confidence": "medium",
        "signal": f"最佳策略:{best_strategy}" if best_strategy else "回测完成",
        "factors": factors, "analysis": f"回测{len(factors)}个策略，最佳:{best_strategy}", "status": "ok",
    }

def run(stock_code: str, stock_name: str = "", context: dict = None) -> dict:
    """薄壳入口：调用工具 + 算法分析，返回 dict。"""
    from app.agent.tools.trading_tools import list_strategies
    from app.agent.tools.backtest_tools import run_backtest

    results = {}
    try: results["list_strategies"] = list_strategies()
    except Exception as e: results["list_strategies"] = {"error": str(e)}

    def call_tool_fn(name, **kwargs):
        if name == "run_backtest": return run_backtest(**kwargs)
        raise ValueError(f"Unknown tool: {name}")

    return algo_analyze(stock_code, stock_name, results, call_tool_fn=call_tool_fn)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("stock_code")
    parser.add_argument("--name", default="")
    args = parser.parse_args()

    from app.agent.tools.trading_tools import list_strategies
    from app.agent.tools.backtest_tools import run_backtest

    results = {}
    try: results["list_strategies"] = list_strategies()
    except Exception as e: results["list_strategies"] = {"error": str(e)}

    def call_tool_fn(name, **kwargs):
        if name == "run_backtest": return run_backtest(**kwargs)
        raise ValueError(f"Unknown tool: {name}")

    print(json.dumps(algo_analyze(args.stock_code, args.name, results, call_tool_fn=call_tool_fn), ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
