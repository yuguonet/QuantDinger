#!/usr/bin/env python3
"""bb-screener: BB超卖全市场扫描。复用原 bb_screener.py 的辅助函数。"""
import argparse, json, sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

def run(stock_code: str = "", stock_name: str = "", context: dict = None) -> dict:
    """薄壳入口，调用 BBScreenerSkill 执行全市场扫描。"""
    from app.agent.skills.bb_screener import BBScreenerSkill
    from app.agent.tools import registry as tool_registry
    tool_registry.discover()

    def call_tool_fn(tool_name, **kwargs):
        spec = tool_registry.get(tool_name)
        if not spec: raise ValueError(f"Unknown tool: {tool_name}")
        return spec.fn(**kwargs)

    skill = BBScreenerSkill()
    result = skill.run(call_tool_fn=call_tool_fn)
    if isinstance(result, dict):
        result.setdefault("skill", "bb_screener")
        return result
    return {"skill": "bb_screener", "score": 50, "direction": "neutral",
            "confidence": 0.4, "signal": "BB扫描完成", "factors": [],
            "analysis": str(result)[:500], "status": "ok"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("stock_code", nargs="?", default="")
    parser.add_argument("--name", default="")
    args = parser.parse_args()

    # 复用原 bb_screener.py 的辅助函数
    from app.agent.skills.bb_screener import (
        _get_all_codes, _get_name_map, _fetch_kline, _check_bb_entry, _deep_analyze_one
    )
    from app.agent.tools import registry as tool_registry
    tool_registry.discover()

    def call_tool_fn(tool_name, **kwargs):
        spec = tool_registry.get(tool_name)
        if not spec: raise ValueError(f"Unknown tool: {tool_name}")
        return spec.fn(**kwargs)

    codes = _get_all_codes(filter_st=True)
    name_map = _get_name_map()

    hits = []
    for code in codes:
        try:
            bars = _fetch_kline(code, days=300)
            if not bars: continue
            sig = _check_bb_entry(bars, code)
            if sig:
                sig["name"] = name_map.get(code, "")
                hits.append(sig)
        except Exception:
            continue

    hits.sort(key=lambda x: -x["amplitude"])

    analyzed = []
    for hit in hits[:10]:
        result = _deep_analyze_one(hit["code"], hit, call_tool_fn, [], [], [])
        if result:
            analyzed.append({
                "code": result.code if hasattr(result, "code") else hit["code"],
                "score": result.score if hasattr(result, "score") else 50,
                "direction": result.direction if hasattr(result, "direction") else "neutral",
                "signal": result.signal if hasattr(result, "signal") else "",
            })

    avg_score = sum(a["score"] for a in analyzed) / len(analyzed) if analyzed else 60
    bullish = sum(1 for a in analyzed if a["direction"] == "bullish")

    output = {
        "skill": "bb_screener", "action": "hold", "score": round(avg_score, 1),
        "direction": "bullish" if avg_score >= 55 else ("bearish" if avg_score < 45 else "neutral"),
        "confidence": "medium",
        "signal": f"BB超卖命中{len(hits)}只，{bullish}只看多",
        "candidates": [{"code": h["code"], "name": h.get("name",""), "close": h["close"],
                        "amplitude": h["amplitude"], "rsi": h["rsi"], "board": h["board"]} for h in hits[:20]],
        "analyzed": analyzed,
        "analysis": f"全市场扫描完成，命中{len(hits)}只BB超卖，深入分析{len(analyzed)}只",
        "status": "ok",
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
