#!/usr/bin/env python3
"""
china-market-tools CLI 入口
用法: cmt [--macro] [--fear-greed] [--policy] [--policy-ai] [--llm PROVIDER]
"""

import argparse
import sys
from datetime import datetime


def main():
    parser = argparse.ArgumentParser(
        prog="cmt",
        description="🇨🇳 中国金融市场分析工具箱",
    )
    parser.add_argument("--macro", action="store_true", help="宏观数据看板")
    parser.add_argument("--fear-greed", action="store_true", help="A股贪恐指数")
    parser.add_argument("--policy", action="store_true", help="政策解读 (关键词)")
    parser.add_argument("--policy-ai", action="store_true", help="政策解读 (AI分析)")
    parser.add_argument("--llm", default="deepseek",
                        choices=["deepseek", "moonshot", "zhipu", "openai"],
                        help="AI政策解读的LLM提供商")
    parser.add_argument("--version", action="store_true", help="显示版本")
    args = parser.parse_args()

    if args.version:
        from . import __version__
        print(f"china-market-tools {__version__}")
        sys.exit(0)

    if not any([args.macro, args.fear_greed, args.policy, args.policy_ai]):
        args.macro = args.fear_greed = args.policy = True  # 默认跑前三个

    print(f"""
{'='*60}
  🇨🇳 中国金融市场分析工具箱
  📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
{'='*60}
    """)

    if args.macro:
        print("\n" + "▶" * 30)
        print("  模块 1: 国内宏观经济数据")
        print("▶" * 30)
        from .macro_analysis import macro_dashboard
        macro_dashboard()

    if args.fear_greed:
        print("\n" + "▶" * 30)
        print("  模块 2: A股贪婪恐惧指数")
        print("▶" * 30)
        from .fear_greed_index import fear_greed_index
        fear_greed_index()

    if args.policy:
        print("\n" + "▶" * 30)
        print("  模块 3: 最新政策解读 (关键词)")
        print("▶" * 30)
        from .policy_analysis import policy_dashboard
        policy_dashboard()

    if args.policy_ai:
        print("\n" + "▶" * 30)
        print(f"  模块 3B: 最新政策解读 (AI: {args.llm})")
        print("▶" * 30)
        from .policy_analysis_ai import policy_dashboard_ai
        policy_dashboard_ai(provider=args.llm)

    print(f"\n{'='*60}")
    print("  ✅ 分析完成!")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
