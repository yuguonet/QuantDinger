#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
验证脚本 — 测试语义路由器的准确性和性能。

用法:
    cd backend_api_python
    python -m app.agent.router.verify

测试内容：
1. 路由准确性（各场景应命中正确路由）
2. 上下文加成（连续同 domain 消息应获得加成）
3. 多用户隔离（不同 session 互不影响）
4. 降级机制（无意义输入应返回未命中）
5. 性能基准（延迟和吞吐量）
"""
from __future__ import annotations

import sys
import os
import time

# 确保项目根目录在 sys.path 中
_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


def test_accuracy():
    """测试路由准确性。"""
    from app.agent.router.core import SemanticIntentRouter
    from app.agent.router.routes import build_default_routes

    print("\n" + "=" * 60)
    print("📊 测试 1: 路由准确性")
    print("=" * 60)

    router = SemanticIntentRouter(routes=build_default_routes())
    print(f"  编码器: {router.encoder.__class__.__name__}")
    print(f"  路由数: {len(router.routes)}")
    print(f"  索引向量数: {len(router.index)}")

    test_cases = [
        # (输入, 期望 domain, 期望 intent)
        ("帮我分析一下贵州茅台", "finance", "stock_analysis"),
        ("600519技术面怎么样", "finance", "stock_analysis"),
        ("今天涨停的有哪些", "finance", "market_scan"),
        ("看看龙虎榜", "finance", "market_scan"),
        ("帮我选几只股票", "finance", "screener"),
        ("筛选低估值蓝筹", "finance", "screener"),
        ("回测一下双均线策略", "finance", "backtest"),
        ("看看主力资金流向", "finance", "fund_flow"),
        ("MACD金叉了吗", "finance", "indicator"),
        ("启动网格策略", "finance", "trading"),
        ("茅台市值多少", "finance", "stock_info"),
        ("什么是MACD金叉", "finance", "concept_explain"),
        ("帮我写个Python脚本", "coding", "code_create"),
        ("修复这个bug", "coding", "code_modify"),
        ("看看项目结构", "coding", "project_scan"),
        ("你好", "chat", "greeting"),
        ("谢谢", "chat", "thanks"),
        ("再见", "chat", "farewell"),
    ]

    correct = 0
    total = len(test_cases)
    for query, expected_domain, expected_intent in test_cases:
        result = router.route(query, session_id="test-accuracy")
        domain_ok = result.domain == expected_domain
        intent_ok = result.intent == expected_intent
        status = "✅" if domain_ok and intent_ok else "❌"
        if domain_ok and intent_ok:
            correct += 1
        score_str = f"{result.confidence:.3f}" if result.matched else "N/A"
        print(f"  {status} [{score_str}] {query[:30]:<30} → {result.domain}/{result.intent}"
              + (f" (期望: {expected_domain}/{expected_intent})" if not (domain_ok and intent_ok) else ""))

    accuracy = correct / total * 100
    print(f"\n  准确率: {correct}/{total} = {accuracy:.1f}%")
    return accuracy


def test_context_boost():
    """测试上下文加成。"""
    from app.agent.router.core import SemanticIntentRouter
    from app.agent.router.routes import build_default_routes

    print("\n" + "=" * 60)
    print("📊 测试 2: 上下文加成")
    print("=" * 60)

    router = SemanticIntentRouter(routes=build_default_routes(), context_boost=0.15)

    # 第一轮：正常路由
    r1 = router.route("帮我分析茅台", session_id="test-ctx")
    print(f"  第1轮: {r1.domain}/{r1.intent} ({r1.confidence:.3f})")

    # 第二轮：带上下文 domain 的路由（应获得加成）
    r2 = router.route("看看走势", session_id="test-ctx", context_domain="finance")
    print(f"  第2轮 (ctx=finance): {r2.domain}/{r2.intent} ({r2.confidence:.3f})")

    # 第三轮：不带上下文（不应获得加成）
    r3 = router.route("看看走势", session_id="test-ctx-no-ctx")
    print(f"  第3轮 (无上下文): {r3.domain}/{r3.intent} ({r3.confidence:.3f})")

    if r2.matched and r3.matched:
        boost = r2.confidence - r3.confidence
        print(f"  上下文加成效果: +{boost:.3f}")
        return boost > 0
    return False


def test_multi_user():
    """测试多用户隔离。"""
    from app.agent.router.context import ContextManager

    print("\n" + "=" * 60)
    print("📊 测试 3: 多用户隔离")
    print("=" * 60)

    ctx = ContextManager()

    # 用户 A 讨论股票
    ctx.record_route("user-a", "finance", "stock_analysis", 0.9, "分析茅台")
    ctx.record_route("user-a", "finance", "stock_analysis", 0.85, "看看走势")

    # 用户 B 讨论代码
    ctx.record_route("user-b", "coding", "code_modify", 0.8, "修复bug")

    # 验证隔离
    domain_a = ctx.get_context_domain("user-a")
    domain_b = ctx.get_context_domain("user-b")

    print(f"  用户A context domain: {domain_a}")
    print(f"  用户B context domain: {domain_b}")

    stats_a = ctx.get_session_stats("user-a")
    stats_b = ctx.get_session_stats("user-b")
    print(f"  用户A turns: {stats_a['turn_count']}")
    print(f"  用户B turns: {stats_b['turn_count']}")

    ok = domain_a == "finance" and domain_b == "coding"
    print(f"  隔离验证: {'✅' if ok else '❌'}")
    return ok


def test_performance():
    """测试性能基准。"""
    from app.agent.router.core import SemanticIntentRouter
    from app.agent.router.routes import build_default_routes

    print("\n" + "=" * 60)
    print("📊 测试 4: 性能基准")
    print("=" * 60)

    router = SemanticIntentRouter(routes=build_default_routes())

    queries = [
        "帮我分析贵州茅台",
        "今天涨停的有哪些",
        "帮我写个脚本",
        "你好",
        "回测双均线策略",
        "看看资金流向",
        "MACD什么状态",
        "启动量化策略",
    ]

    # 预热
    for q in queries:
        router.route(q, session_id="bench")

    # 基准测试
    iterations = 50
    t0 = time.time()
    for _ in range(iterations):
        for q in queries:
            router.route(q, session_id="bench")
    elapsed = time.time() - t0

    total_calls = iterations * len(queries)
    avg_ms = elapsed / total_calls * 1000
    qps = total_calls / elapsed

    print(f"  总调用: {total_calls}")
    print(f"  总耗时: {elapsed:.2f}s")
    print(f"  平均延迟: {avg_ms:.1f}ms/次")
    print(f"  吞吐量: {qps:.0f} queries/sec")
    print(f"  编码器: {router.encoder.__class__.__name__}")

    return avg_ms


def test_fallback():
    """测试降级机制。"""
    from app.agent.router.core import SemanticIntentRouter
    from app.agent.router.routes import build_default_routes

    print("\n" + "=" * 60)
    print("📊 测试 5: 降级机制（无意义输入）")
    print("=" * 60)

    router = SemanticIntentRouter(routes=build_default_routes(), default_threshold=0.45)

    gibberish = [
        "asdfghjkl",
        "123456789",
        "啊啊啊啊啊",
        "......",
        "a",
    ]

    for q in gibberish:
        result = router.route(q, session_id="test-fallback")
        status = "未命中 ✅" if not result.matched else f"误命中 ❌ ({result.domain}/{result.intent})"
        print(f"  '{q}' → {status} (best={result.all_scores})")


def main():
    print("🔍 QuantDinger 语义路由器验证")
    print(f"   Python {sys.version}")

    try:
        test_accuracy()
        test_context_boost()
        test_multi_user()
        test_performance()
        test_fallback()
        print("\n✅ 全部测试完成")
    except ImportError as e:
        print(f"\n❌ 依赖缺失: {e}")
        print("   请安装: pip install sentence-transformers")
        print("   或使用降级模式: INTENT_ROUTER_ENCODER=hash")
    except Exception as e:
        print(f"\n❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
