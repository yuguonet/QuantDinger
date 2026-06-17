#!/usr/bin/env python3
"""intelligence-agent: 调用情报工具，输出 JSON。"""
import argparse, json, sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("stock_code")
    parser.add_argument("--name", default="")
    args = parser.parse_args()

    from app.agent.tools.intel_tools import search_comprehensive_intel, get_eastmoney_stock_news, get_global_finance_news, get_consensus_eps
    from app.agent.tools.quote_tools import get_realtime_quote

    results = {}
    for name, fn in [
        ("realtime_quote", lambda: get_realtime_quote(args.stock_code)),
        ("comprehensive_intel", lambda: search_comprehensive_intel(args.stock_code)),
        ("stock_news", lambda: get_eastmoney_stock_news(args.stock_code)),
        ("global_news", lambda: get_global_finance_news()),
        ("consensus_eps", lambda: get_consensus_eps(args.stock_code)),
    ]:
        try:
            results[name] = fn()
        except Exception as e:
            results[name] = {"error": str(e)}

    print(json.dumps({"skill": "intelligence_agent", "stock_code": args.stock_code, "stock_name": args.name, "data": results}, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
