"""
Phase 2: 数据驱动的 LLM 策略发现

从 Phase 1 回测结果中提取有效模式，生成数据驱动的 LLM prompt，
自动产生新策略模板并编译回测。

用法:
    # 1. 分析 Phase 1 结果，生成模式摘要
    python -m optimizer.phase2_strategy_discovery analyze --output phase2_patterns.json

    # 2. 基于模式生成 LLM prompt
    python -m optimizer.phase2_strategy_discovery prompts --patterns phase2_patterns.json

    # 3. 全流程：分析 → 生成 prompt → 调用 LLM → 编译 → 回测
    python -m optimizer.phase2_strategy_discovery run --all-local -m CNStock \
        --start 2023-05-01 --end 2026-03-31 --trials 50 -j 35
"""
import argparse
import json
import os
import sys
from collections import defaultdict
from typing import Dict, List, Any, Optional


# ============================================================
# Phase 1 模式提取
# ============================================================

def load_all_results(output_dir: str) -> List[Dict]:
    """加载 optimizer_output/ 下所有回测结果"""
    results = []
    if not os.path.isdir(output_dir):
        print(f"  ⚠️ 目录不存在: {output_dir}")
        return results

    for fname in sorted(os.listdir(output_dir)):
        if not fname.endswith(".json") or fname.startswith("_"):
            continue
        fpath = os.path.join(output_dir, fname)
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                data = json.load(f)
            if "template" in data and "best" in data:
                results.append(data)
        except Exception as e:
            pass
    return results


def extract_indicator_patterns(results: List[Dict]) -> Dict[str, Any]:
    """
    从回测结果中提取指标组合模式
    
    分析维度：
    1. 哪些指标组合产生高 Sharpe
    2. 哪些 entry_rules 条件最有效
    3. 参数范围与表现的关系
    4. 股票特征与策略匹配
    """
    patterns = {
        "template_performance": {},      # 模板级别表现
        "indicator_combos": {},          # 指标组合效果
        "effective_conditions": {},      # 有效条件模式
        "param_ranges": {},              # 最优参数范围
        "stock_clusters": {},            # 股票聚类
    }

    # 按模板分组
    by_template = defaultdict(list)
    for r in results:
        by_template[r["template"]].append(r)

    for tpl, tpl_results in by_template.items():
        sharpes = [r["best"]["metrics"]["sharpeRatio"] for r in tpl_results]
        returns = [r["best"]["metrics"]["totalReturn"] for r in tpl_results]
        win_rates = [r["best"]["metrics"]["winRate"] for r in tpl_results]
        trades = [r["best"]["metrics"]["totalTrades"] for r in tpl_results]
        scores = [r["best"]["score"] for r in tpl_results]

        n = len(tpl_results)
        positive = sum(1 for s in scores if s > 0)

        patterns["template_performance"][tpl] = {
            "count": n,
            "positive_rate": positive / n if n else 0,
            "avg_sharpe": sum(sharpes) / n if n else 0,
            "avg_return": sum(returns) / n if n else 0,
            "avg_win_rate": sum(win_rates) / n if n else 0,
            "avg_trades": sum(trades) / n if n else 0,
            "avg_score": sum(scores) / n if n else 0,
            "median_sharpe": sorted(sharpes)[n // 2] if n else 0,
        }

        # 提取最优参数范围
        top_20_pct = sorted(tpl_results, key=lambda r: r["best"]["score"], reverse=True)[:max(1, n // 5)]
        if top_20_pct and top_20_pct[0]["best"].get("params"):
            param_keys = top_20_pct[0]["best"]["params"].keys()
            param_ranges = {}
            for pk in param_keys:
                vals = [r["best"]["params"].get(pk) for r in top_20_pct if pk in r["best"].get("params", {})]
                if vals and all(isinstance(v, (int, float)) for v in vals):
                    param_ranges[pk] = {
                        "min": min(vals),
                        "max": max(vals),
                        "mean": sum(vals) / len(vals),
                        "best": top_20_pct[0]["best"]["params"][pk],
                    }
            patterns["param_ranges"][tpl] = param_ranges

    return patterns


def generate_llm_prompts(patterns: Dict) -> List[Dict[str, str]]:
    """
    基于模式分析生成 LLM 策略发现 prompt
    
    策略思路：
    1. 组合成功指标（RSI + VWAP + Volume 三重确认）
    2. 反向策略（做空/止盈逻辑）
    3. 多时间框架确认
    4. 新指标引入（OBV、ADX、CCI 等）
    5. 自适应参数（基于波动率调整阈值）
    """
    prompts = []

    # 获取成功模板的信息
    successful = {k: v for k, v in patterns.get("template_performance", {}).items()
                  if v.get("positive_rate", 0) > 0.8 and v.get("avg_sharpe", 0) > 0.3}
    failed = {k: v for k, v in patterns.get("template_performance", {}).items()
              if v.get("positive_rate", 0) < 0.5 or v.get("avg_sharpe", 0) < 0}

    success_info = ""
    if successful:
        success_info = "\n\n## 已验证有效的策略模板（表现优秀）\n"
        for tpl, stats in successful.items():
            success_info += (f"- **{tpl}**: 正得分率={stats['positive_rate']:.0%}, "
                           f"平均Sharpe={stats['avg_sharpe']:.3f}, "
                           f"平均胜率={stats['avg_win_rate']:.1%}, "
                           f"平均交易次数={stats['avg_trades']:.0f}\n")

    fail_info = ""
    if failed:
        fail_info = "\n\n## 已验证失败的策略模板（避免类似设计）\n"
        for tpl, stats in failed.items():
            fail_info += (f"- **{tpl}**: 正得分率={stats['positive_rate']:.0%}, "
                         f"平均Sharpe={stats['avg_sharpe']:.3f}\n")

    # Prompt 1: 指标组合创新
    prompts.append({
        "name": "indicator_combo_innovation",
        "description": "基于成功指标的创新组合",
        "prompt": f"""你是量化策略研究员。基于以下 A 股回测数据，设计一个全新的 IndicatorStrategy 策略模板。

{success_info}
{fail_info}

## 设计要求
1. 使用以下已验证有效的指标组合：RSI、VWAP、Volume、Bollinger、EMA
2. 可以引入新指标：OBV（能量潮）、ADX（趋势强度）、CCI（商品通道）、MFI（资金流）
3. 核心思路：多指标共振确认，至少 3 个指标同时满足条件才入场
4. 考虑 A 股特点：T+1、涨跌停、量价关系重要
5. 必须包含止损（ATR 动态止损优先）
6. 目标：正得分率 > 90%，平均 Sharpe > 0.5，平均交易次数 > 10

## 输出格式
生成一个完整的 Python 策略模板，包含：
1. `_build_xxx_config(p: dict) -> dict` 函数
2. 参数空间定义（使用 _p_int, _p_float, _p_choice）
3. 参数约束条件
4. STRATEGY_TEMPLATE_KEY 和 STRATEGY_TEMPLATE_DICT 变量
"""
    })

    # Prompt 2: 自适应波动率策略
    prompts.append({
        "name": "adaptive_volatility",
        "description": "基于波动率自适应的策略",
        "prompt": f"""你是量化策略研究员。设计一个基于波动率自适应的 A 股 IndicatorStrategy 策略。

{success_info}

## 核心思路
市场波动率是均值回归的——高波动后往往低波动，低波动后往往高波动。利用这个特性：
1. 用 ATR 或布林带宽度衡量波动率
2. 低波动收缩期 → 准备入场（等待突破）
3. 高波动扩张期 → 趋势跟踪或止盈
4. 参数阈值随波动率动态调整（而非固定值）

## A 股特点
- T+1 制度，日内信号需隔日出场
- 涨跌停限制（主板 10%，创业板/科创板 20%）
- 量价关系比美股更重要
- 均线系统（5/10/20/60/120/250 日）是基础

## 输出格式
完整的 Python 策略模板代码，包含 _build_xxx_config、参数空间、约束条件。
"""
    })

    # Prompt 3: 量价背离 + 趋势确认
    prompts.append({
        "name": "volume_price_trend",
        "description": "量价背离与趋势确认策略",
        "prompt": f"""你是量化策略研究员。设计一个基于量价关系与趋势确认的 A 股策略。

{success_info}
{fail_info}

## 核心思路
A 股的量价关系是最重要的信号来源：
1. **量价齐升**：健康上涨趋势
2. **量价背离**：价格创新高但量萎缩 → 趋势可能反转
3. **放量突破**：关键阻力位放量突破 → 强势信号
4. **缩量回调**：上涨趋势中缩量回调 → 买入机会

结合趋势指标（EMA 多头排列、ADX 趋势强度）确认方向。

## 设计要求
1. 使用 RSI、Volume MA、EMA、ADX 组合
2. 入场条件：趋势确认 + 量价配合 + 动量不超买
3. 出场：ATR 动态止损 + 移动止盈
4. 目标交易次数 > 15（增加统计可靠性）

## 输出格式
完整的 Python 策略模板代码。
"""
    })

    # Prompt 4: 均线系统 + KDJ 动量
    prompts.append({
        "name": "ma_kdj_momentum",
        "description": "均线系统与 KDJ 动量策略",
        "prompt": f"""你是量化策略研究员。设计一个基于均线系统 + KDJ 动量的 A 股策略。

{success_info}

## 核心思路
A 股技术分析的经典组合：
1. 均线多头排列（5>10>20>60）确认趋势方向
2. KDJ 金叉确认短期动量
3. 回调到均线支撑位 + KDJ 超卖回升 = 最佳买点
4. 用成交量确认突破有效性

## 设计要点
- 快速均线：EMA(5/10)
- 慢速均线：EMA(20/60)
- KDJ 参数：(9,3,3) 为基础，可优化
- 入场：价格在均线支撑 + KDJ 金叉 + RSI 不超买
- 出场：KDJ 死叉 或 跌破关键均线 或 ATR 止损

## 输出格式
完整的 Python 策略模板代码。
"""
    })

    # Prompt 5: 布林带 + MACD + 成交量三重确认
    prompts.append({
        "name": "bollinger_macd_volume",
        "description": "布林带+MACD+成交量三重确认策略",
        "prompt": f"""你是量化策略研究员。设计一个布林带 + MACD + 成交量三重确认的 A 股策略。

{success_info}
{fail_info}

## 核心思路
三重过滤减少假信号：
1. **布林带过滤**：价格触及下轨或中轨支撑 → 潜在买点
2. **MACD 确认**：MACD 金叉或柱状图转正 → 动量确认
3. **成交量确认**：放量突破或缩量回调 → 资金确认

## 进阶设计
- 布林带宽度作为波动率指标（收缩后突破更有效）
- MACD 零轴上方金叉比下方金叉更可靠
- 成交量相对 MA 的比值作为强度指标
- ATR 动态止损

## 输出格式
完整的 Python 策略模板代码。
"""
    })

    return prompts


# ============================================================
# LLM 调用（复用 backend_api_python 的 LLMService）
# ============================================================

def _init_backend_path():
    """将 backend_api_python 加入 sys.path"""
    backend_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend_api_python")
    if backend_path not in sys.path:
        sys.path.insert(0, backend_path)


def call_llm_via_backend(prompt: str, system: str = "") -> str:
    """通过 backend_api_python 的 LLMService 调用 LLM"""
    _init_backend_path()
    try:
        from app.services.llm import LLMService
        llm = LLMService()

        # 检查是否有配置的 API Key
        api_key = llm.get_api_key()
        if not api_key:
            print("    ⚠️ 未配置 LLM API Key，请在 backend_api_python 中设置 DEEPSEEK_API_KEY 等")
            return ""

        # 构建消息
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        # 调用
        response = llm.call_llm_api(
            messages=messages,
            temperature=0.3,
            max_tokens=4000,
        )

        # call_llm_api 直接返回字符串
        if isinstance(response, str):
            return response
        return str(response) if response else ""

    except Exception as e:
        print(f"    ❌ LLM 调用失败: {e}")
        # 回退到原始 call_llm
        from optimizer.llm_strategy_generator import call_llm as _orig_call
        return _orig_call(prompt, system)


# ============================================================
# Phase 2 执行引擎
# ============================================================

def run_phase2(output_dir: str = "optimizer_output",
               market: str = "CNStock",
               symbols: str = None,
               all_local: bool = False,
               timeframe: str = "1D",
               start: str = "2023-05-01",
               end: str = "2026-03-31",
               trials: int = 50,
               jobs: int = 1,
               score: str = "composite"):
    """
    Phase 2 全流程
    1. 分析 Phase 1 结果
    2. 生成 LLM prompt
    3. 调用 LLM 生成策略
    4. 编译并回测
    """
    from optimizer.llm_strategy_generator import (
        extract_python_code, validate_strategy_code,
        execute_and_extract, format_template_code
    )

    print("=" * 60)
    print("  Phase 2: 数据驱动的 LLM 策略发现")
    print("=" * 60)

    # Step 1: 分析 Phase 1 结果
    print("\n📊 Step 1: 分析 Phase 1 回测结果...")
    results = load_all_results(output_dir)
    if not results:
        print("  ⚠️ 未找到回测结果，使用 README 中的已知模式")
        patterns = _get_known_patterns()
    else:
        patterns = extract_indicator_patterns(results)
        print(f"  分析了 {len(results)} 条回测记录")

    # Step 2: 生成 LLM prompt
    print("\n📝 Step 2: 生成数据驱动的 LLM prompt...")
    prompts = generate_llm_prompts(patterns)
    print(f"  生成了 {len(prompts)} 个策略发现 prompt")

    # Step 3: 调用 LLM 生成策略
    print("\n🤖 Step 3: 调用 LLM 生成策略模板...")
    all_templates = {}
    for i, p_info in enumerate(prompts, 1):
        print(f"\n  [{i}/{len(prompts)}] {p_info['name']}: {p_info['description']}")

        from optimizer.llm_strategy_generator import build_generation_prompt, SYSTEM_PROMPT
        prompt = build_generation_prompt(p_info["prompt"])
        raw = call_llm_via_backend(prompt, SYSTEM_PROMPT)

        if not raw:
            print(f"    ⚠️ LLM 无响应，跳过")
            continue

        code = extract_python_code(raw)
        valid, msg = validate_strategy_code(code)

        if not valid:
            print(f"    ❌ {msg}")
            # 尝试修复
            print(f"    🔧 尝试修复...")
            fix_prompt = f"修复以下代码错误：\n{msg}\n\n代码：\n{code}"
            fixed = call_llm_via_backend(fix_prompt, SYSTEM_PROMPT)
            if fixed:
                code = extract_python_code(fixed)
                valid, msg = validate_strategy_code(code)
                if not valid:
                    print(f"    ❌ 修复失败")
                    continue

        templates = execute_and_extract(code)
        if templates:
            all_templates.update(templates)
            print(f"    ✅ 生成: {list(templates.keys())}")
        else:
            print(f"    ❌ 无法提取模板")

    if not all_templates:
        print("\n❌ 没有成功生成任何策略模板")
        return

    # Step 4: 保存生成的模板
    gen_path = "optimizer/strategies_generated.py"
    print(f"\n💾 Step 4: 保存生成的模板到 {gen_path}...")
    lines = ['"""LLM Phase 2 生成的策略模板"""\n\n']
    lines.append("def _p_int(low, high, step=1): return {'type': 'int', 'low': low, 'high': high, 'step': step}\n")
    lines.append("def _p_float(low, high, step=0.001): return {'type': 'float', 'low': low, 'high': high, 'step': step}\n")
    lines.append("def _p_choice(choices): return {'type': 'choice', 'choices': choices}\n\n")

    for key, template in all_templates.items():
        lines.append(format_template_code(key, template))

    lines.append(f"\nGENERATED_TEMPLATES = {json.dumps({k: {kk: vv for kk, vv in v.items() if kk != 'build_config'} for k, v in all_templates.items()}, indent=2, ensure_ascii=False)}\n")

    with open(gen_path, "w", encoding="utf-8") as f:
        f.writelines(lines)
    print(f"  保存了 {len(all_templates)} 个模板")

    # Step 5: 回测
    print(f"\n🚀 Step 5: 回测生成的策略...")
    template_names = ",".join(all_templates.keys())

    cmd_parts = [
        "python -m optimizer.runner",
        f"-t {template_names}",
        f"-m {market}",
        f"-tf {timeframe}",
        f"--start {start}",
        f"--end {end}",
        f"--trials {trials}",
        f"--score {score}",
        f"-j {jobs}",
    ]

    if all_local:
        cmd_parts.append("--all-local")
    elif symbols:
        cmd_parts.append(f'-s "{symbols}"')

    cmd = " ".join(cmd_parts)
    print(f"  命令: {cmd}")
    print(f"\n  请在你的 Windows 环境中执行以上命令")

    return all_templates


def _get_known_patterns() -> Dict:
    """从 README 中提取的已知模式（当没有回测结果文件时使用）"""
    return {
        "template_performance": {
            "triple_rsi_momentum": {
                "count": 200, "positive_rate": 1.0,
                "avg_sharpe": 0.630, "avg_win_rate": 0.576,
                "avg_trades": 20, "avg_score": 3.421,
            },
            "vwap_bollinger_squeeze": {
                "count": 200, "positive_rate": 1.0,
                "avg_sharpe": 1.101, "avg_win_rate": 0.515,
                "avg_trades": 15, "avg_score": 3.334,
            },
            "rsi_volume_divergence": {
                "count": 200, "positive_rate": 0.99,
                "avg_sharpe": 0.719, "avg_win_rate": 0.450,
                "avg_trades": 18, "avg_score": 2.874,
            },
            "vwap_volume_confirm": {
                "count": 200, "positive_rate": 0.915,
                "avg_sharpe": 0.838, "avg_win_rate": 0.522,
                "avg_trades": 12, "avg_score": 2.197,
            },
            "macd_vol_divergence": {
                "count": 200, "positive_rate": 0.325,
                "avg_sharpe": -0.309, "avg_win_rate": 0.297,
                "avg_trades": 8, "avg_score": -5.912,
            },
            "limitup_continuation": {
                "count": 1260, "positive_rate": 0.55,
                "avg_sharpe": -0.260, "avg_win_rate": 0.328,
                "avg_trades": 5.9, "avg_score": 1.55,
                "wf_passed": 0, "wf_tested": 16,
            },
        },
        "key_findings": [
            "RSI + Volume + 趋势指标三重确认最可靠",
            "VWAP 在 A 股日内/短线策略中效果显著",
            "交易次数 > 10 的策略 WF 更可能通过",
            "涨停追涨策略在日线上泛化能力差",
            "macd_vol_divergence 的 histogram_negative 条件在日线上太稀有",
        ],
    }


# ============================================================
# CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="Phase 2: 数据驱动的 LLM 策略发现")
    sub = parser.add_subparsers(dest="command")

    # analyze
    ana = sub.add_parser("analyze", help="分析 Phase 1 结果")
    ana.add_argument("--output", "-o", default="phase2_patterns.json")
    ana.add_argument("--input-dir", default="optimizer_output")

    # prompts
    pro = sub.add_parser("prompts", help="生成 LLM prompt")
    pro.add_argument("--patterns", default="phase2_patterns.json")
    pro.add_argument("--output", "-o", default="phase2_prompts.json")

    # run
    run = sub.add_parser("run", help="全流程执行")
    run.add_argument("-m", "--market", default="CNStock")
    run.add_argument("-s", "--symbol", default=None)
    run.add_argument("--all-local", action="store_true")
    run.add_argument("-tf", "--timeframe", default="1D")
    run.add_argument("--start", default="2023-05-01")
    run.add_argument("--end", default="2026-03-31")
    run.add_argument("-n", "--trials", type=int, default=50)
    run.add_argument("-j", "--jobs", type=int, default=1)
    run.add_argument("--score", default="composite")
    run.add_argument("--output-dir", default="optimizer_output")

    args = parser.parse_args()

    if args.command == "analyze":
        results = load_all_results(args.input_dir)
        if results:
            patterns = extract_indicator_patterns(results)
        else:
            print("  ⚠️ 未找到回测结果，使用已知模式")
            patterns = _get_known_patterns()
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(patterns, f, indent=2, ensure_ascii=False)
        print(f"  ✅ 保存到: {args.output}")

    elif args.command == "prompts":
        if os.path.exists(args.patterns):
            with open(args.patterns, "r") as f:
                patterns = json.load(f)
        else:
            patterns = _get_known_patterns()
        prompts = generate_llm_prompts(patterns)
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(prompts, f, indent=2, ensure_ascii=False)
        print(f"  ✅ 保存了 {len(prompts)} 个 prompt 到: {args.output}")

    elif args.command == "run":
        run_phase2(
            output_dir=args.output_dir,
            market=args.market,
            symbols=args.symbol,
            all_local=args.all_local,
            timeframe=args.timeframe,
            start=args.start,
            end=args.end,
            trials=args.trials,
            jobs=args.jobs,
            score=args.score,
        )

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
