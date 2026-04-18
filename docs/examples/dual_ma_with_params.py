# ============================================================
# 双均线策略（文档同步版）
# Dual Moving Average Strategy (Doc-Aligned Example)
# ============================================================
#
# 适用场景:
# 1. 在 Indicator IDE 中快速验证均线交叉逻辑
# 2. 演示 `# @param` + `# @strategy` 的标准写法
# 3. 演示与平台回测面板更一致的默认风控配置
#
# 注意:
# - 杠杆请在产品面板中设置，不要写进源码
# - 这里的 `# @strategy` 是默认值，不是额外的数据列
#
# ============================================================

my_indicator_name = "双均线交叉策略"
my_indicator_description = "使用短期/长期均线金叉与死叉生成买卖信号，并交给平台默认风控处理。"

# === 参数声明（供前端、AI 调参与代码质量检查识别） ===
# @param sma_short int 14 短期均线周期
# @param sma_long int 28 长期均线周期

# === 平台默认策略配置 ===
# @strategy stopLossPct 0.02
# @strategy takeProfitPct 0.05
# @strategy entryPct 0.25
# @strategy trailingEnabled false
# @strategy tradeDirection both

df = df.copy()

# === 从 params 读取参数，而不是只声明不用 ===
sma_short_period = int(params.get('sma_short', 14))
sma_long_period = int(params.get('sma_long', 28))

# === 计算均线 ===
sma_short = df["close"].rolling(sma_short_period).mean()
sma_long = df["close"].rolling(sma_long_period).mean()

# === 生成边缘触发信号 ===
raw_buy = (sma_short > sma_long) & (sma_short.shift(1) <= sma_long.shift(1))
raw_sell = (sma_short < sma_long) & (sma_short.shift(1) >= sma_long.shift(1))

df["buy"] = (raw_buy.fillna(False) & (~raw_buy.shift(1).fillna(False))).astype(bool)
df["sell"] = (raw_sell.fillna(False) & (~raw_sell.shift(1).fillna(False))).astype(bool)

# === 买卖标记点（用于 K 线图显示） ===
buy_marks = [df["low"].iloc[i] * 0.995 if df["buy"].iloc[i] else None for i in range(len(df))]
sell_marks = [df["high"].iloc[i] * 1.005 if df["sell"].iloc[i] else None for i in range(len(df))]

# === 图表输出 ===
output = {
    "name": my_indicator_name,
    "plots": [
        {"name": f"SMA{sma_short_period}", "data": sma_short.fillna(0).tolist(), "color": "#FF9800", "overlay": True},
        {"name": f"SMA{sma_long_period}", "data": sma_long.fillna(0).tolist(), "color": "#3F51B5", "overlay": True}
    ],
    "signals": [
        {"type": "buy", "text": "B", "data": buy_marks, "color": "#00E676"},
        {"type": "sell", "text": "S", "data": sell_marks, "color": "#FF5252"}
    ]
}
