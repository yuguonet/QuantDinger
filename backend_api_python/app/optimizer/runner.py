"""
自动策略优化 Runner
主入口脚本 — 串联所有模块

用法:
    cd backend_api_python

    # A 股优化（默认）
    python -m app.optimizer.runner \
        --template ma_crossover \
        --symbol "A_SHARE:000001.SZ" \
        --timeframe 1D \
        --start 2024-01-01 \
        --end 2025-12-31 \
        --trials 100 \
        --validate

    # 所有模板（含 A 股扩展）
    python -m app.optimizer.runner --all --symbol "A_SHARE:600000.SH"

    # 仅原始模板
    python -m app.optimizer.runner --all --set original

    # 仅 A 股模板
    python -m app.optimizer.runner --all --set ashare

    # 加密市场（向后兼容）
    python -m app.optimizer.runner --template ma_crossover --symbol "Crypto:BTC/USDT" --market crypto
"""
import argparse
import json
import os
import sys
import time
import traceback
from datetime import datetime
from typing import Dict, Any, List

# 确保项目根目录在 path 中
_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

# 直接导入具体模块，避免触发 app/services/__init__.py 的重量级导入
from importlib import import_module as _im

def _lazy_import():
    """延迟导入回测相关模块，只在实际运行时才加载"""
    global _StrategyCompiler, _BacktestService
    if '_StrategyCompiler' not in globals() or globals().get('_StrategyCompiler') is None:
        _sc_mod = _im('app.services.strategy_compiler')
        _StrategyCompiler = _sc_mod.StrategyCompiler
        _bt_mod = _im('app.services.backtest')
        _BacktestService = _bt_mod.BacktestService

# 导入策略模板（原始 + A 股扩展）
from app.optimizer.param_space import STRATEGY_TEMPLATES, get_template, list_templates
from app.optimizer.strategy_templates_ashare import ASHARE_STRATEGY_TEMPLATES
from app.optimizer.ashare_adapter import (
    parse_market_symbol, normalize_symbol,
    get_ashare_commission, get_ashare_initial_capital,
    AShareRules, AShareBacktestAdapter,
)
from app.optimizer.optimizer import StrategyOptimizer
from app.optimizer.walk_forward import WalkForwardValidator

_StrategyCompiler = None
_BacktestService = None


# ============================================================
# 统一模板注册表
# ============================================================

ALL_TEMPLATES: Dict[str, Dict[str, Any]] = {}
ALL_TEMPLATES.update(STRATEGY_TEMPLATES)
ALL_TEMPLATES.update(ASHARE_STRATEGY_TEMPLATES)


def get_all_template_keys() -> List[str]:
    """返回全部模板 key"""
    return list(ALL_TEMPLATES.keys())


def get_original_template_keys() -> List[str]:
    """返回原始 7 个模板 key"""
    return list(STRATEGY_TEMPLATES.keys())


def get_ashare_template_keys() -> List[str]:
    """返回 A 股扩展模板 key"""
    return list(ASHARE_STRATEGY_TEMPLATES.keys())


def get_template_unified(key: str) -> dict:
    """从统一注册表获取模板"""
    if key not in ALL_TEMPLATES:
        raise ValueError(
            f"未知模板: {key}\n"
            f"可用模板: {', '.join(get_all_template_keys())}"
        )
    return ALL_TEMPLATES[key]


# ============================================================
# A 股适配
# ============================================================

def _is_ashare_market(market: str) -> bool:
    """判断是否为 A 股市场"""
    return market.upper() in ("A_SHARE", "ASHARE", "A", "CN", "CHINA", "CNSTOCK")


def _get_ashare_commission() -> float:
    """A 股佣金费率（含印花税，双边）"""
    return get_ashare_commission()


def _get_ashare_initial_capital() -> float:
    """A 股默认初始资金"""
    return get_ashare_initial_capital()


# ============================================================
# BacktestObjective
# ============================================================

class BacktestObjective:
    """
    回测目标函数封装
    将 params → 编译 → 回测 → metrics 的流程封装为可调用对象

    A 股模式下自动应用 T+1、涨跌停、佣金等约束
    """

    def __init__(
        self,
        template_key: str,
        symbol: str,
        market: str,
        timeframe: str,
        start_date: datetime = None,
        end_date: datetime = None,
        initial_capital: float = None,
        commission: float = None,
    ):
        self.template = get_template_unified(template_key)
        self.template_key = template_key
        self.symbol = symbol
        self.market = market
        self.timeframe = timeframe
        self.start_date = start_date
        self.end_date = end_date

        # A 股自动适配参数
        self.is_ashare = _is_ashare_market(market)
        if initial_capital is not None:
            self.initial_capital = initial_capital
        elif self.is_ashare:
            self.initial_capital = _get_ashare_initial_capital()
        else:
            self.initial_capital = 10000.0

        if commission is not None:
            self.commission = commission
        elif self.is_ashare:
            self.commission = _get_ashare_commission()
        else:
            self.commission = 0.001

        # A 股适配器
        self._ashare_adapter = None
        if self.is_ashare:
            self._ashare_adapter = AShareBacktestAdapter()

        # 延迟初始化
        self._compiler = None
        self._backtest = None

    def _ensure_services(self):
        global _StrategyCompiler, _BacktestService
        if _StrategyCompiler is None:
            _lazy_import()
        self._compiler = _StrategyCompiler()
        self._backtest = _BacktestService()

    def __call__(self, params: dict, start_date: datetime = None, end_date: datetime = None) -> dict:
        """
        执行单次回测

        Args:
            params: 策略参数
            start_date: 可选，覆盖默认日期（Walk-Forward 用）
            end_date: 可选，覆盖默认日期

        Returns:
            metrics dict
        """
        self._ensure_services()

        sd = start_date or self.start_date
        ed = end_date or self.end_date
        if sd is None or ed is None:
            raise ValueError("start_date and end_date are required")

        # 1. 参数 → 策略配置
        config = self.template["build_config"](params)

        # A 股模式：注入 A 股特有规则到策略配置
        if self.is_ashare:
            config = self._inject_ashare_rules(config)

        # 2. 编译为代码
        try:
            code = self._compiler.compile(config)
        except Exception as e:
            raise RuntimeError(f"编译失败: {e}")

        # 3. 回测
        result = self._backtest.run(
            indicator_code=code,
            market=self.market,
            symbol=self.symbol,
            timeframe=self.timeframe,
            start_date=sd,
            end_date=ed,
            initial_capital=self.initial_capital,
            commission=self.commission,
        )

        # A 股模式：后处理（T+1、涨跌停等约束）
        if self.is_ashare:
            result = self._apply_ashare_constraints(result)

        return {
            "sharpeRatio": result.get("sharpeRatio", 0),
            "totalReturn": result.get("totalReturn", 0),
            "winRate": result.get("winRate", 0),
            "maxDrawdown": result.get("maxDrawdown", 0),
            "profitFactor": result.get("profitFactor", 0),
            "totalTrades": result.get("totalTrades", 0),
            "avgProfit": result.get("avgProfit", 0),
            "avgLoss": result.get("avgLoss", 0),
        }

    def _inject_ashare_rules(self, config: dict) -> dict:
        """向策略配置注入 A 股特有规则"""
        # 在 risk_management 中加入涨跌停和 T+1 限制
        rm = config.get("risk_management", {})
        rm["ashare_rules"] = {
            "t_plus_1": True,
            "price_limit": True,
            "min_lot": 100,
        }
        config["risk_management"] = rm
        return config

    def _apply_ashare_constraints(self, result: dict) -> dict:
        """对回测结果应用 A 股约束修正"""
        # A 股涨跌停会导致部分交易无法成交，需要过滤
        # T+1 限制会减少日内交易次数
        # 佣金已包含印花税
        # 这里对结果做合理性修正
        total_trades = result.get("totalTrades", 0)

        # 如果交易次数异常多（可能是日内反复交易），按 T+1 限制折半
        if total_trades > 0 and self.timeframe in ("1m", "5m", "15m", "30m", "1H"):
            # A 股 T+1，日内策略实际交易次数约为信号次数的 50%
            result["totalTrades"] = max(1, total_trades // 2)
            result["t1_adjusted"] = True

        return result


# ============================================================
# 单模板优化
# ============================================================

def run_single_template(
    template_key: str,
    symbol: str,
    market: str,
    timeframe: str,
    start_date: datetime,
    end_date: datetime,
    n_trials: int = 100,
    score_fn: str = "composite",
    do_validate: bool = True,
    output_dir: str = "optimizer_output",
) -> Dict[str, Any]:
    """对单个策略模板运行完整优化流程"""
    template = get_template_unified(template_key)

    # A 股默认参数调整
    is_ashare = _is_ashare_market(market)
    if is_ashare:
        initial_capital = _get_ashare_initial_capital()
        commission = _get_ashare_commission()
    else:
        initial_capital = 10000.0
        commission = 0.001

    print(f"\n{'#'*60}")
    print(f"  策略模板: {template['name']}")
    print(f"  描述: {template['description']}")
    print(f"  标的: {symbol}")
    print(f"  市场: {'A 股' if is_ashare else market}")
    print(f"  时间框架: {timeframe}")
    print(f"  回测区间: {start_date.strftime('%Y-%m-%d')} ~ {end_date.strftime('%Y-%m-%d')}")
    print(f"  初始资金: {initial_capital:,.0f}")
    print(f"  佣金费率: {commission:.4f}")
    print(f"  评分函数: {score_fn}")
    print(f"{'#'*60}")

    # 1. 构建回测目标函数
    objective = BacktestObjective(
        template_key=template_key,
        symbol=symbol,
        market=market,
        timeframe=timeframe,
        start_date=start_date,
        end_date=end_date,
        initial_capital=initial_capital,
        commission=commission,
    )

    # 2. 创建优化器
    optimizer = StrategyOptimizer(
        template_key=template_key,
        objective_fn=objective,
        n_trials=n_trials,
        score_fn=score_fn,
        mode="auto",
    )

    # 3. 运行优化
    t0 = time.time()
    best = optimizer.run()
    elapsed = time.time() - t0

    if best is None:
        print(f"\n  ❌ 没有找到有效策略")
        return None

    print(f"\n  优化耗时: {elapsed:.1f}s")

    # 4. Walk-Forward 验证
    validation_result = None
    if do_validate:
        print(f"\n{'='*60}")
        print(f"  Walk-Forward 验证 (最优参数)")
        print(f"{'='*60}")

        validator = WalkForwardValidator(n_splits=5, train_ratio=0.7)
        validation_result = validator.validate(
            objective_fn=objective,
            best_params=best.params,
            start_date=start_date,
            end_date=end_date,
            score_fn=score_fn,
        )

        print(f"\n  训练集平均得分: {validation_result['avg_train_score']}")
        print(f"  测试集平均得分: {validation_result['avg_test_score']}")
        print(f"  过拟合比率:     {validation_result['overfitting_ratio']}")
        print(f"  一致性:         {validation_result['consistency']}")
        print(f"\n  结论: {validation_result['verdict']}")

    # 5. 保存结果
    os.makedirs(output_dir, exist_ok=True)
    safe_symbol = symbol.replace("/", "_").replace(":", "_")
    output_path = os.path.join(output_dir, f"{template_key}_{timeframe}_{safe_symbol}.json")

    output = {
        "template": template_key,
        "template_name": template["name"],
        "symbol": symbol,
        "market": "A_SHARE" if is_ashare else market,
        "timeframe": timeframe,
        "period": f"{start_date.strftime('%Y-%m-%d')} ~ {end_date.strftime('%Y-%m-%d')}",
        "n_trials": n_trials,
        "score_fn": score_fn,
        "initial_capital": initial_capital,
        "commission": commission,
        "elapsed_seconds": round(elapsed, 1),
        "best": best.to_dict(),
        "validation": validation_result,
        "top_10": [r.to_dict() for r in optimizer.get_top_n(10)],
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n  结果保存: {output_path}")

    return output


# ============================================================
# CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="QuantDinger 自动策略优化器（支持 A 股 + 加密市场）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # A 股 — 单个模板
  python -m app.optimizer.runner -t ma_crossover -s "A_SHARE:000001.SZ" -tf 1D

  # A 股 — 所有模板对比
  python -m app.optimizer.runner --all -s "A_SHARE:600000.SH" -tf 1D

  # A 股 — 仅 A 股扩展模板
  python -m app.optimizer.runner --all --set ashare -s "A_SHARE:300001.SZ"

  # 加密市场（向后兼容）
  python -m app.optimizer.runner -t ma_crossover -s "Crypto:BTC/USDT" -tf 4H

可用模板:
  原始 (7): """ + ", ".join(get_original_template_keys()) + """
  A股 (10): """ + ", ".join(get_ashare_template_keys()),
    )

    parser.add_argument("--template", "-t", type=str, default=None,
                        help="策略模板名")
    parser.add_argument("--all", action="store_true", help="运行所有模板")
    parser.add_argument("--set", type=str, default="all",
                        choices=["all", "original", "ashare"],
                        help="模板集合: all=全部, original=原始7个, ashare=A股10个")
    parser.add_argument("--symbol", "-s", type=str, default="A_SHARE:000001.SZ",
                        help="交易标的 (格式: Market:Symbol)")
    parser.add_argument("--timeframe", "-tf", type=str, default="1D",
                        help="时间框架 (1m/5m/15m/30m/1H/4H/1D)")
    parser.add_argument("--start", type=str, default="2024-01-01",
                        help="回测开始日期 (YYYY-MM-DD)")
    parser.add_argument("--end", type=str, default="2025-12-31",
                        help="回测结束日期 (YYYY-MM-DD)")
    parser.add_argument("--trials", "-n", type=int, default=100, help="试验次数")
    parser.add_argument("--score", type=str, default="composite",
                        choices=["sharpe", "return_dd_ratio", "composite"], help="评分函数")
    parser.add_argument("--no-validate", action="store_true", help="跳过 Walk-Forward 验证")
    parser.add_argument("--output", "-o", type=str, default="optimizer_output", help="输出目录")
    parser.add_argument("--list", "-l", action="store_true", help="列出所有可用模板")

    args = parser.parse_args()

    # 列出模板
    if args.list:
        print(f"\n  原始模板 ({len(get_original_template_keys())}):")
        for k in get_original_template_keys():
            tpl = ALL_TEMPLATES[k]
            print(f"    - {k:<25} {tpl['name']}")
        print(f"\n  A 股扩展模板 ({len(get_ashare_template_keys())}):")
        for k in get_ashare_template_keys():
            tpl = ALL_TEMPLATES[k]
            print(f"    - {k:<25} {tpl['name']}")
        print(f"\n  总计: {len(get_all_template_keys())} 个模板")
        return

    # 解析日期
    start_date = datetime.strptime(args.start, "%Y-%m-%d")
    end_date = datetime.strptime(args.end, "%Y-%m-%d").replace(hour=23, minute=59, second=59)

    # 解析标的 — 使用 ashare_adapter 统一解析，确保 market 为 DataSourceFactory 识别的格式
    market, symbol = parse_market_symbol(args.symbol)
    is_ashare = _is_ashare_market(market)

    # A 股自动调整默认时间框架
    if is_ashare and args.timeframe in ("4H", "1H"):
        print(f"  ⚠️  A 股日线数据更稳定，建议使用 --timeframe 1D")
        print(f"     当前: {args.timeframe}，继续使用...")

    # 选择模板
    if args.all:
        if args.set == "original":
            templates = get_original_template_keys()
        elif args.set == "ashare":
            templates = get_ashare_template_keys()
        else:
            templates = get_all_template_keys()
    elif args.template:
        if args.template not in ALL_TEMPLATES:
            print(f"❌ 未知模板: {args.template}")
            print(f"可用模板: {', '.join(get_all_template_keys())}")
            sys.exit(1)
        templates = [args.template]
    else:
        print("请指定 --template <名称> 或 --all")
        print(f"可用模板: {', '.join(get_all_template_keys())}")
        sys.exit(1)

    # 打印运行信息
    print(f"\n{'='*60}")
    print(f"  QuantDinger 策略优化器")
    print(f"  市场: {'🇨🇳 A 股' if is_ashare else market}")
    print(f"  标的: {symbol}")
    print(f"  模板数: {len(templates)}")
    print(f"  模板: {', '.join(templates)}")
    print(f"{'='*60}")

    # 运行
    all_results = []
    for tpl in templates:
        try:
            result = run_single_template(
                template_key=tpl,
                symbol=symbol,
                market=market,
                timeframe=args.timeframe,
                start_date=start_date,
                end_date=end_date,
                n_trials=args.trials,
                score_fn=args.score,
                do_validate=not args.no_validate,
                output_dir=args.output,
            )
            if result:
                all_results.append(result)
        except Exception as e:
            print(f"\n  ❌ 模板 {tpl} 失败: {e}")
            traceback.print_exc()

    # 多模板汇总
    if len(all_results) > 1:
        print(f"\n{'='*60}")
        print(f"  全部模板汇总 (按测试集得分排序)")
        print(f"{'='*60}")

        ranked = sorted(
            all_results,
            key=lambda r: r.get("validation", {}).get("avg_test_score", r["best"]["score"]),
            reverse=True,
        )

        print(f"\n  {'Rank':<5} {'模板':<25} {'Sharpe':<10} {'WinRate':<10} {'MaxDD':<10} {'WF测试':<10} {'结论'}")
        print(f"  {'-'*90}")
        for rank, r in enumerate(ranked, 1):
            best = r["best"]["metrics"]
            wf_score = r.get("validation", {}).get("avg_test_score", "N/A")
            verdict = r.get("validation", {}).get("verdict", "未验证")[:20]
            print(f"  {rank:<5} {r['template']:<25} "
                  f"{best.get('sharpeRatio',0):<10.2f} "
                  f"{best.get('winRate',0):<10.1f} "
                  f"{best.get('maxDrawdown',0):<10.1f} "
                  f"{str(wf_score):<10} "
                  f"{verdict}")

        # 保存汇总
        os.makedirs(args.output, exist_ok=True)
        summary_path = os.path.join(args.output, "_summary.json")
        summary_output = {
            "market": "A_SHARE" if is_ashare else market,
            "symbol": symbol,
            "timeframe": args.timeframe,
            "period": f"{args.start} ~ {args.end}",
            "templates_run": len(all_results),
            "ranked": ranked,
        }
        with open(summary_path, "w", encoding="utf-8") as f:
            json.dump(summary_output, f, ensure_ascii=False, indent=2)
        print(f"\n  汇总保存: {summary_path}")

    print(f"\n{'='*60}")
    print(f"  优化完成!")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
