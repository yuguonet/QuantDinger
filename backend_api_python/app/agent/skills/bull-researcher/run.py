#!/usr/bin/env python3
"""bull-researcher: 调用工具获取数据，输出 JSON 供 agent 构建多头论据。"""
import argparse, json, sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("stock_code")
    parser.add_argument("--name", default="")
    args = parser.parse_args()

    from app.agent.tools.analysis_tools import get_realtime_quote, analyze_trend, get_volume_analysis, get_indicator_snapshot
    from app.agent.tools.intel_tools import search_comprehensive_intel

    results = {}
    for name, fn in [
        ("realtime_quote", lambda: get_realtime_quote(args.stock_code)),
        ("trend", lambda: analyze_trend(args.stock_code)),
        ("volume", lambda: get_volume_analysis(args.stock_code)),
        ("indicator", lambda: get_indicator_snapshot(args.stock_code)),
        ("intel", lambda: search_comprehensive_intel(args.stock_code, query=f"{args.name or args.stock_code} 利好 增长")),
    ]:
        try:
            results[name] = fn()
        except Exception as e:
            results[name] = {"error": str(e)}

    print(json.dumps({"skill": "bull_researcher", "stock_code": args.stock_code, "stock_name": args.name, "data": results}, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
