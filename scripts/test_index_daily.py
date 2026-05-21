"""
测试 akshare 拉取 A 股主要指数日线数据
本地运行: python scripts/test_index_daily.py
"""
import akshare as ak
import pandas as pd

# 5 大指数
INDICES = {
    "上证指数":  "sh000001",
    "深证成指":  "sz399001",
    "创业板指":  "sz399006",
    "科创50":   "sh000688",
    "北证50":   "bj899050",
}

print("=" * 60)
print("  akshare 指数日线拉取测试")
print("=" * 60)

for name, code in INDICES.items():
    print(f"\n📊 {name} ({code})")
    print("-" * 40)
    try:
        df = ak.stock_zh_index_daily(symbol=code)
        print(f"  ✅ 成功! {len(df)} 行")
        print(f"  列: {list(df.columns)}")
        print(f"  日期范围: {df['date'].iloc[0]} ~ {df['date'].iloc[-1]}")
        print(f"  最新 3 行:")
        print(df.tail(3).to_string(index=False))
    except Exception as e:
        print(f"  ❌ 失败: {e}")

print("\n" + "=" * 60)
print("  测试完成")
print("=" * 60)
