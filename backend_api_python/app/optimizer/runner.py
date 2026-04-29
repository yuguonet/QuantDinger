"""
自动策略优化 Runner
主入口脚本 — 串联所有模块

用法:
    cd backend_api_python
    python -m app.optimizer.runner \
        --template ma_crossover \
        --symbol "Crypto:BTC/USDT" \
        --timeframe 4H \
        --start 2025-01-01 \
        --end 2025-12-31 \
        --trials 100 \
        --validate
"""
import argparse
import json
import os
import sys
import time
import traceback
from datetime import datetime
from typing import Dict, Any

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

from app.optimizer.param_space import STRATEGY_TEMPLATES, get_template, list_templates
from app.optimizer.optimizer import StrategyOptimizer
from app.optimizer.walk_forward import WalkForwardValidator

_StrategyCompiler = None
_BacktestService = None


class BacktestObjective:
    """
    回测目标函数封装
    将 params → 编译 → 回测 → metrics 的流程封装为可调用对象
    """

    def __init__(
        self,
        template_key: str,
        symbol: str,
        market: str,
        timeframe: str,
        start_date: datetime = None,
        end_date: datetime = None,
        initial_capital: float = 10000.0,
        commission: float = 0.001,
    ):
        self.template = get_template(template_key)
        self.template_key = template_key
        self.symbol = symbol
        self.market = market
        self.timeframe = timeframe
        self.start_date = start_date
        self.end_date = end_date
        self.initial_capital = initial_capital
        self.commission = commission

        # 延迟初始化（避免 import 时触发）
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
    template = get_template(template_key)

    print(f"\n{'#'*60}")
    print(f"  策略模板: {template['name']}")
    print(f"  描述: {template['description']}")
    print(f"  标的: {symbol}")
    print(f"  时间框架: {timeframe}")
    print(f"  回测区间: {start_date.strftime('%Y-%m-%d')} ~ {end_date.strftime('%Y-%m-%d')}")
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
        "timeframe": timeframe,
        "period": f"{start_date.strftime('%Y-%m-%d')} ~ {end_date.strftime('%Y-%m-%d')}",
        "n_trials": n_trials,
        "score_fn": score_fn,
        "elapsed_seconds": round(elapsed, 1),
        "best": best.to_dict(),
        "validation": validation_result,
        "top_10": [r.to_dict() for r in optimizer.get_top_n(10)],
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n  结果保存: {output_path}")

    return output


def main():
    parser = argparse.ArgumentParser(description="QuantDinger 自动策略优化器")
    parser.add_argument("--template", "-t", type=str, default=None,
                        help=f"策略模板名，可选: {', '.join(list_templates())}")
    parser.add_argument("--all", action="store_true", help="运行所有模板")
    parser.add_argument("--symbol", "-s", type=str, default="Crypto:BTC/USDT", help="交易标的 (格式: Market:Symbol)")
    parser.add_argument("--timeframe", "-tf", type=str, default="4H", help="时间框架 (1m/5m/15m/30m/1H/4H/1D)")
    parser.add_argument("--start", type=str, default="2025-01-01", help="回测开始日期 (YYYY-MM-DD)")
    parser.add_argument("--end", type=str, default="2025-12-31", help="回测结束日期 (YYYY-MM-DD)")
    parser.add_argument("--trials", "-n", type=int, default=100, help="试验次数")
    parser.add_argument("--score", type=str, default="composite",
                        choices=["sharpe", "return_dd_ratio", "composite"], help="评分函数")
    parser.add_argument("--no-validate", action="store_true", help="跳过 Walk-Forward 验证")
    parser.add_argument("--output", "-o", type=str, default="optimizer_output", help="输出目录")

    args = parser.parse_args()

    # 解析日期
    start_date = datetime.strptime(args.start, "%Y-%m-%d")
    end_date = datetime.strptime(args.end, "%Y-%m-%d").replace(hour=23, minute=59, second=59)

    # 解析标的
    parts = args.symbol.split(":")
    if len(parts) == 2:
        market, symbol = parts
    else:
        market, symbol = "Crypto", args.symbol

    # 选择模板
    if args.all:
        templates = list_templates()
    elif args.template:
        if args.template not in STRATEGY_TEMPLATES:
            print(f"❌ 未知模板: {args.template}")
            print(f"可用模板: {', '.join(list_templates())}")
            sys.exit(1)
        templates = [args.template]
    else:
        print("请指定 --template <名称> 或 --all")
        print(f"可用模板: {', '.join(list_templates())}")
        sys.exit(1)

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

        print(f"\n  {'Rank':<5} {'模板':<20} {'Sharpe':<10} {'WinRate':<10} {'MaxDD':<10} {'WF测试':<10} {'结论'}")
        print(f"  {'-'*85}")
        for rank, r in enumerate(ranked, 1):
            best = r["best"]["metrics"]
            wf_score = r.get("validation", {}).get("avg_test_score", "N/A")
            verdict = r.get("validation", {}).get("verdict", "未验证")[:20]
            print(f"  {rank:<5} {r['template']:<20} "
                  f"{best.get('sharpeRatio',0):<10.2f} "
                  f"{best.get('winRate',0):<10.1f} "
                  f"{best.get('maxDrawdown',0):<10.1f} "
                  f"{str(wf_score):<10} "
                  f"{verdict}")

        # 保存汇总
        summary_path = os.path.join(args.output, "_summary.json")
        with open(summary_path, "w", encoding="utf-8") as f:
            json.dump(ranked, f, ensure_ascii=False, indent=2)
        print(f"\n  汇总保存: {summary_path}")

    print(f"\n{'='*60}")
    print(f"  优化完成!")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
