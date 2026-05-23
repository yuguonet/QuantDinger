# MEMORY.md

## 用户
- 做A股量化交易，目标小资金快速复利
- 本地环境：Windows + 40核，D:\QuantDinger\
- 项目: https://github.com/yuguonet/QuantDinger

## 2026-05-23 策略迭代

### 核心发现
1. **D1是判决书**: 收阳67.6%胜率 vs 收阴19%胜率，最强通用过滤
2. **龙回头+V1双引擎**: 前后错开不矛盾，龙回头抗噪+V1进攻
3. **bottom_volume是最强信号**: 77.8% +21.19% 盈亏比13.51

### 迭代结果
- 纯V1: 136笔 44.1% +2.34% → 最终版: 35笔 **68.6% +10.32%**
- 龙回头独立: 7笔 **71.4% +6.21%**
- 双引擎合并: 43笔 **67.4% +9.18%**

### 关键文件
- `QuantDinger-main/test_dragon_callback.py` — 最终版(龙回头+V1)
- `QuantDinger-main/MEMORY.md` — 详细记录
- `memory/2026-05-23.md` — 今日工作日志
