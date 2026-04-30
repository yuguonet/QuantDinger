"""
自动策略优化 Runner
主入口脚本 — 串联所有模块

用法（在项目根目录运行）:
    # 查看本地仓库
    python -m optimizer.runner --list-local

    # 跑单只股票
    python -m optimizer.runner -t ma_crossover -s "000001.SZ" -tf 1D

    # 跑本地仓库全部 A 股（多进程）
    python -m optimizer.runner --all -m CNStock -tf 1D --all-local -j 4

stock_list.txt 格式（同 downloader）:
    000001.SZ
    600000.SH
    300750.SZ
    # 井号开头是注释

输出目录与 data_warehouse 对齐:
    optimizer_output/
      CNStock/
        daily/
          000001.SZ_ma_crossover.json
          600000.SH_ma_crossover.json
        weekly/
          000001.SZ_rsi_oversold.json
      Crypto/
        4h/
          BTC_USDT_ma_crossover.json
      _summary.json
"""
import argparse
import json
import multiprocessing
import os
import sys
import time
import traceback
from datetime import datetime
from typing import Dict, Any, List

# 确保 backend_api_python 在 path 中（app 模块在那里）
_optimizer_dir = os.path.dirname(os.path.abspath(__file__))
_project_root = os.path.dirname(_optimizer_dir)
_backend_root = os.path.join(_project_root, "backend_api_python")
if _backend_root not in sys.path:
    sys.path.insert(0, _backend_root)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

# ── Monkey-patch: 让 DataSourceFactory.get_kline 优先读本地 data_warehouse ──
def _patch_datasource_warehouse():
    """在 BacktestService 加载前，注入本地仓库读取逻辑"""
    from app.data_sources.factory import DataSourceFactory
    from optimizer.data_warehouse.storage import read_local

    _orig_get_kline = DataSourceFactory.get_kline.__func__

    def _get_kline_with_warehouse(cls, market, symbol, timeframe, limit, before_time=None, after_time=None):
        try:
            data = read_local(
                market=market, timeframe=timeframe, symbol=symbol,
                limit=limit, before_time=before_time, after_time=after_time,
            )
            if data and len(data) >= 10:
                print(f"  [本地仓库] 命中 {symbol} {timeframe}: {len(data)} 条")
                return data
        except Exception:
            pass
        return _orig_get_kline(cls, market, symbol, timeframe, limit, before_time=before_time, after_time=after_time)

    DataSourceFactory.get_kline = classmethod(_get_kline_with_warehouse)

_patch_datasource_warehouse()

# 直接导入具体模块，避免触发 app/services/__init__.py 的重量级导入
from importlib import import_module as _im

def _lazy_import():
    """延迟导入回测相关模块，只在实际运行时才加载"""
    global _StrategyCompiler, _BacktestService
    if '_StrategyCompiler' not in globals() or globals().get('_StrategyCompiler') is None:
        _sc_mod = _im('optimizer.strategy_compiler')
        _StrategyCompiler = _sc_mod.StrategyCompiler
        _bt_mod = _im('app.services.backtest')
        _BacktestService = _bt_mod.BacktestService

# 导入策略模板（原始 + A 股扩展）
from optimizer.param_space import STRATEGY_TEMPLATES, get_template, list_templates
from optimizer.strategy_templates_ashare import ASHARE_STRATEGY_TEMPLATES
from optimizer.strategy_templates_llm import LLM_STRATEGY_TEMPLATES
from optimizer.ashare_adapter import (
    parse_market_symbol, normalize_symbol,
    get_ashare_commission, get_ashare_initial_capital,
    AShareRules, AShareBacktestAdapter,
)
from optimizer.strategy_optimizer import StrategyOptimizer
from optimizer.walk_forward import WalkForwardValidator
from optimizer.data_warehouse.storage import _TF_DIR_MAP

_StrategyCompiler = None
_BacktestService = None


# ============================================================
# 统一模板注册表
# ============================================================

ALL_TEMPLATES: Dict[str, Dict[str, Any]] = {}
ALL_TEMPLATES.update(STRATEGY_TEMPLATES)
ALL_TEMPLATES.update(ASHARE_STRATEGY_TEMPLATES)
ALL_TEMPLATES.update(LLM_STRATEGY_TEMPLATES)

# 加载 LLM 批量生成的模板
try:
    from optimizer.strategies_generated import GENERATED_TEMPLATES
    ALL_TEMPLATES.update(GENERATED_TEMPLATES)
except ImportError:
    pass


def get_all_template_keys() -> List[str]:
    """返回全部模板 key"""
    return list(ALL_TEMPLATES.keys())


def get_original_template_keys() -> List[str]:
    """返回原始 7 个模板 key"""
    return list(STRATEGY_TEMPLATES.keys())


def get_ashare_template_keys() -> List[str]:
    """返回 A 股扩展模板 key"""
    return list(ASHARE_STRATEGY_TEMPLATES.keys())


def get_llm_template_keys() -> List[str]:
    """返回 LLM 生成的模板 key"""
    return list(LLM_STRATEGY_TEMPLATES.keys())


def get_template_unified(key: str) -> dict:
    """从统一注册表获取模板"""
    if key in ALL_TEMPLATES:
        return ALL_TEMPLATES[key]
    # fallback: 动态加载（多进程 worker 用）
    from optimizer.param_space import get_template
    return get_template(key)



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
        total_trades = result.get("totalTrades", 0)
        if total_trades > 0 and self.timeframe in ("1m", "5m", "15m", "30m", "1H"):
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
    output_dir: str = None,
) -> Dict[str, Any]:
    """对单个策略模板运行完整优化流程"""
    if output_dir is None:
        output_dir = os.path.join(_optimizer_dir, "optimizer_output")
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

    # 5. 保存结果 — 目录结构与 data_warehouse 对齐: {market}/{tf_dir}/{symbol}_{template}.json
    os.makedirs(output_dir, exist_ok=True)
    tf_dir = _TF_DIR_MAP.get(timeframe, timeframe.lower())
    market_dir = os.path.join(output_dir, market, tf_dir)
    os.makedirs(market_dir, exist_ok=True)
    safe_symbol = symbol.replace("/", "_").replace(":", "_")
    output_path = os.path.join(market_dir, f"{safe_symbol}_{template_key}.json")

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
# 多进程 Worker
# ============================================================

def _worker_init():
    """每个子进程初始化：确保 monkey-patch 和模块路径就绪"""
    import optimizer.strategy_templates_llm   # 确保 LLM 模板注册
    import optimizer.strategy_templates_ashare # 确保 A 股模板注册



def _worker_run_one(args: tuple) -> Dict[str, Any]:
    """
    子进程入口：运行单个 (stock, template) 组合。
    参数打包为 tuple 以便 multiprocessing.Pool.starmap 使用。
    """
    (template_key, symbol, market, timeframe,
     start_str, end_str, n_trials, score_fn, do_validate, output_dir, seed) = args

    # 每个 worker 用不同随机种子，避免并行试验采样重复
    import random
    import numpy as np
    random.seed(seed)
    np.random.seed(seed)

    start_date = datetime.strptime(start_str, "%Y-%m-%d")
    end_date = datetime.strptime(end_str, "%Y-%m-%d").replace(hour=23, minute=59, second=59)

    try:
        result = run_single_template(
            template_key=template_key,
            symbol=symbol,
            market=market,
            timeframe=timeframe,
            start_date=start_date,
            end_date=end_date,
            n_trials=n_trials,
            score_fn=score_fn,
            do_validate=do_validate,
            output_dir=output_dir,
        )
        if result:
            result["_symbol_raw"] = f"{market}:{symbol}"
            result["_market"] = market
        return result
    except Exception as e:
        return {"_error": str(e), "_symbol_raw": f"{market}:{symbol}", "_template": template_key}


# ============================================================
# CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="QuantDinger 自动策略优化器（支持 A 股 + 加密市场）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例（在项目根目录运行）:
  # 查看本地仓库有哪些股票
  python -m optimizer.runner --list-local

  # 跑本地仓库里全部 A 股日线（自动发现）
  python -m optimizer.runner --all -m CNStock -tf 1D --all-local

  # 跑单只股票
  python -m optimizer.runner -t ma_crossover -s "000001.SZ" -tf 1D

  # 跑多只股票（逗号分隔）
  python -m optimizer.runner --all -s "000001.SZ,600000.SH,300750.SZ" -tf 1D

  # 从文件读取（每行一个代码，同 downloader 格式）
  python -m optimizer.runner --all --symbols-file stock_list.txt -tf 1D

  # 多进程并行（4 核同时跑）
  python -m optimizer.runner --all -m CNStock -tf 1D --all-local -j 4

  # 随机抽 100 只股票（防止单一化，seed 可复现）
  python -m optimizer.runner --all -m CNStock -tf 1D --random-sample 100 --seed 42

  # 每次随机不同样本
  python -m optimizer.runner --all -m CNStock -tf 1D --random-sample 100

  # 加密市场
  python -m optimizer.runner -t ma_crossover -m Crypto -s "BTC/USDT" -tf 4H

stock_list.txt 格式（同 downloader）:
  000001.SZ
  600000.SH
  300750.SZ
  # 井号开头是注释

可用模板:
  原始 (7): """ + ", ".join(get_original_template_keys()) + """
  A股 (10): """ + ", ".join(get_ashare_template_keys()) + """
  LLM (5): """ + ", ".join(get_llm_template_keys()),
    )

    parser.add_argument("--template", "-t", type=str, default=None,
                        help="策略模板名")
    parser.add_argument("--all", action="store_true", help="运行所有模板")
    parser.add_argument("--set", type=str, default="all",
                        choices=["all", "original", "ashare", "llm"],
                        help="模板集合: all=全部, original=原始7个, ashare=A股10个, llm=LLM生成5个")
    parser.add_argument("--market", "-m", type=str, default="CNStock",
                        help="市场类型 (CNStock, Crypto, ...)")
    parser.add_argument("--symbol", "-s", type=str, default=None,
                        help="交易标的，多个用逗号分隔 (如 000001.SZ,600000.SH)")
    parser.add_argument("--symbols-file", type=str, default=None,
                        help="股票列表文件（每行一个代码，同 downloader 格式）")
    parser.add_argument("--all-local", action="store_true",
                        help="自动扫描本地仓库中该市场+时间框架下的全部股票")
    parser.add_argument("--random-sample", type=int, default=0, metavar="N",
                        help="从本地仓库随机抽取 N 只股票（防止单一化，每次运行样本不同）")
    parser.add_argument("--seed", type=int, default=None, metavar="S",
                        help="随机种子（配合 --random-sample 使用，固定抽样结果可复现）")
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
    parser.add_argument("--jobs", "-j", type=int, default=1,
                        help="并行进程数 (默认 1，建议 CPU 核心数)")
    parser.add_argument("--output", "-o", type=str,
                        default=os.path.join(_optimizer_dir, "optimizer_output"),
                        help="输出目录")
    parser.add_argument("--list", "-l", action="store_true", help="列出所有可用模板")
    parser.add_argument("--list-local", action="store_true",
                        help="列出本地仓库中已有数据的股票")

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
        print(f"\n  LLM 生成模板 ({len(get_llm_template_keys())}):")
        for k in get_llm_template_keys():
            tpl = ALL_TEMPLATES[k]
            print(f"    - {k:<25} {tpl['name']}")
        print(f"\n  总计: {len(get_all_template_keys())} 个模板")
        return

    # 列出本地仓库数据
    if args.list_local:
        from optimizer.data_warehouse.storage import list_local, get_stats
        stats = get_stats()
        print(f"\n📊 本地数据仓库")
        print(f"   路径: {stats['root']}")
        if not stats['exists']:
            print(f"   ❌ 仓库不存在")
            return
        print(f"   股票总数: {stats['stocks']}")
        print(f"   数据总行数: {stats['total_rows']:,}")
        for mkt, count in stats.get('markets', {}).items():
            print(f"   - {mkt}: {count} 只")
        symbols = list_local(args.market, args.timeframe)
        if symbols:
            print(f"\n   {args.market} / {args.timeframe}: {len(symbols)} 只")
            for s in symbols:
                print(f"     {s}")
        else:
            print(f"\n   {args.market} / {args.timeframe}: 无数据")
        return

    # 解析日期
    start_date = datetime.strptime(args.start, "%Y-%m-%d")
    end_date = datetime.strptime(args.end, "%Y-%m-%d").replace(hour=23, minute=59, second=59)

    # 解析多个标的 — 四种来源（优先级: --symbol > --symbols-file > --random-sample > --all-local）
    symbols_raw = []
    if args.symbol:
        symbols_raw = [s.strip() for s in args.symbol.split(",") if s.strip()]
    elif args.symbols_file:
        if not os.path.isfile(args.symbols_file):
            print(f"❌ 股票列表文件不存在: {args.symbols_file}")
            sys.exit(1)
        with open(args.symbols_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    symbols_raw.append(line)
        if not symbols_raw:
            print(f"❌ 股票列表文件为空: {args.symbols_file}")
            sys.exit(1)
    elif args.random_sample > 0:
        import random as _random
        from optimizer.data_warehouse.storage import list_local
        all_local = list_local(args.market, args.timeframe)
        if not all_local:
            print(f"❌ 本地仓库中没有 {args.market}/{args.timeframe} 的数据")
            sys.exit(1)
        n = min(args.random_sample, len(all_local))
        if args.seed is not None:
            _random.seed(args.seed)
        symbols_raw = _random.sample(all_local, n)
        print(f"  🎲 从本地仓库 {len(all_local)} 只股票中随机抽取 {n} 只"
              f"{f' (seed={args.seed})' if args.seed is not None else ''}")
    elif args.all_local:
        from optimizer.data_warehouse.storage import list_local
        symbols_raw = list_local(args.market, args.timeframe)
        if not symbols_raw:
            print(f"❌ 本地仓库中没有 {args.market}/{args.timeframe} 的数据")
            print(f"   先用 downloader 下载: python -m optimizer.data_warehouse.downloader -m CNStock -tf 1D")
            sys.exit(1)
        print(f"  📦 从本地仓库发现 {len(symbols_raw)} 只股票 ({args.market}/{args.timeframe})")
    else:
        # 默认：扫描本地仓库，没有则 fallback 到 000001.SZ
        from optimizer.data_warehouse.storage import list_local
        local = list_local(args.market, args.timeframe)
        if local:
            symbols_raw = local
            print(f"  📦 自动发现本地仓库 {len(symbols_raw)} 只股票 ({args.market}/{args.timeframe})")
        else:
            symbols_raw = ["000001.SZ"]
            print(f"  ℹ️  本地仓库无数据，使用默认标的 000001.SZ")

    # 参数组合校验
    if args.seed is not None and args.random_sample <= 0:
        print(f"⚠️  --seed 需要配合 --random-sample 使用，当前将被忽略")

    # 选择模板
    if args.all:
        if args.set == "original":
            templates = get_original_template_keys()
        elif args.set == "ashare":
            templates = get_ashare_template_keys()
        elif args.set == "llm":
            templates = get_llm_template_keys()
        else:
            templates = get_all_template_keys()
    elif args.template:
        if args.set != "all":
            print(f"⚠️  --set {args.set} 在指定 --template 时将被忽略，使用 --all 才生效")
        if args.template not in ALL_TEMPLATES:
            print(f"❌ 未知模板: {args.template}")
            print(f"可用模板: {', '.join(get_all_template_keys())}")
            sys.exit(1)
        templates = [args.template]
    else:
        print("请指定 --template <名称> 或 --all")
        print(f"可用模板: {', '.join(get_all_template_keys())}")
        sys.exit(1)

    # 解析标的：统一走 parse_market_symbol，兼容新旧格式
    #   "000001.SZ"          + --market CNStock → ("CNStock", "000001.SZ")
    #   "A_SHARE:000001.SZ"  （含冒号）         → ("CNStock", "000001.SZ")
    if not symbols_raw:
        print(f"❌ 没有可用的股票标的，请检查参数")
        sys.exit(1)

    resolved = []
    for s in symbols_raw:
        if ":" in s:
            mkt, sym = parse_market_symbol(s)
        else:
            mkt = args.market
            sym = normalize_symbol(s) if _is_ashare_market(mkt) else s
        resolved.append((mkt, sym))

    first_market = resolved[0][0]
    is_ashare = _is_ashare_market(first_market)

    # 打印运行信息
    print(f"\n{'='*60}")
    print(f"  QuantDinger 策略优化器")
    print(f"  市场: {'🇨🇳 A 股' if is_ashare else first_market}")
    print(f"  标的数: {len(resolved)}")
    for _m, _s in resolved:
        print(f"    - {_s} ({_m})")
    print(f"  模板数: {len(templates)}")
    print(f"  模板: {', '.join(templates)}")
    print(f"  并行进程: {args.jobs}")
    total_jobs = len(resolved) * len(templates)
    print(f"  总任务数: {total_jobs} ({len(resolved)} 只股票 × {len(templates)} 个模板)")
    print(f"{'='*60}")

    # 构建任务列表
    tasks = []
    seed_base = 42
    for i, (market, symbol) in enumerate(resolved):
        for j, tpl in enumerate(templates):
            seed = seed_base + i * 1000 + j
            tasks.append((
                tpl, symbol, market, args.timeframe,
                args.start, args.end, args.trials, args.score,
                not args.no_validate, args.output, seed,
            ))

    # 执行：单进程 or 多进程
    all_results = []
    t_total = time.time()

    if args.jobs <= 1:
        # 单进程（原逻辑，保留实时输出）
        for task in tasks:
            result = _worker_run_one(task)
            if result and "_error" not in result:
                all_results.append(result)
            elif result and "_error" in result:
                print(f"\n  ❌ {result['_symbol_raw']} / {result['_template']} 失败: {result['_error']}")
    else:
        # 多进程
        print(f"\n  🚀 启动 {args.jobs} 个进程并行优化...")
        # 子进程需要 unbuffered 输出，否则 print 会攒到结束才显示
        os.environ["PYTHONUNBUFFERED"] = "1"
        with multiprocessing.Pool(
            processes=args.jobs,
            initializer=_worker_init,
        ) as pool:
            results = pool.map(_worker_run_one, tasks)

        for result in results:
            if result and "_error" not in result:
                all_results.append(result)
            elif result and "_error" in result:
                print(f"\n  ❌ {result['_symbol_raw']} / {result['_template']} 失败: {result['_error']}")

    elapsed_total = time.time() - t_total

    # ── 汇总报告 ──
    stock_summaries = {}
    for r in all_results:
        sym = r.get("symbol", "?")
        wf_score = (r.get("validation") or {}).get("avg_test_score", r["best"]["score"])
        if sym not in stock_summaries or wf_score > stock_summaries[sym]["_best_wf_score"]:
            r["_best_wf_score"] = wf_score
            stock_summaries[sym] = r

    # ── 汇总报告 ──
    print(f"\n{'='*60}")
    print(f"  📊 全部运行完成 — 汇总报告")
    print(f"  总耗时: {elapsed_total:.1f}s (进程数: {args.jobs})")
    print(f"{'='*60}")

    # 1) 每只股票的最佳策略
    if stock_summaries:
        print(f"\n  每只股票的最佳策略:")
        print(f"  {'股票':<15} {'最佳模板':<25} {'Sharpe':<10} {'WinRate':<10} {'MaxDD':<10} {'WF测试':<10}")
        print(f"  {'-'*80}")
        for sym, r in sorted(stock_summaries.items()):
            best_m = r["best"]["metrics"]
            wf_s = r.get("_best_wf_score", "N/A")
            print(f"  {sym:<15} {r['template']:<25} "
                  f"{best_m.get('sharpeRatio',0):<10.2f} "
                  f"{best_m.get('winRate',0):<10.1f} "
                  f"{best_m.get('maxDrawdown',0):<10.1f} "
                  f"{str(wf_s):<10}")

    # 2) 所有结果按得分排序
    if len(all_results) > 1:
        ranked = sorted(
            all_results,
            key=lambda r: (r.get("validation") or {}).get(
                "avg_test_score", r["best"]["score"]
            ),
            reverse=True,
        )
        print(f"\n  全部结果排名 (按测试集得分):")
        print(f"  {'Rank':<5} {'股票':<15} {'模板':<25} {'Sharpe':<10} {'WinRate':<10} {'MaxDD':<10} {'WF测试':<10}")
        print(f"  {'-'*95}")
        for rank, r in enumerate(ranked, 1):
            best_m = r["best"]["metrics"]
            wf_score = (r.get("validation") or {}).get("avg_test_score", "N/A")
            print(f"  {rank:<5} {r.get('_symbol_raw','?'):<15} {r['template']:<25} "
                  f"{best_m.get('sharpeRatio',0):<10.2f} "
                  f"{best_m.get('winRate',0):<10.1f} "
                  f"{best_m.get('maxDrawdown',0):<10.1f} "
                  f"{str(wf_score):<10}")

    # 保存汇总
    os.makedirs(args.output, exist_ok=True)
    summary_path = os.path.join(args.output, "_summary.json")
    summary_output = {
        "symbols": [f"{m}:{s}" for m, s in resolved],
        "timeframe": args.timeframe,
        "period": f"{args.start} ~ {args.end}",
        "templates": templates,
        "total_runs": len(all_results),
        "stock_best": {
            sym: {
                "template": r["template"],
                "best_score": r["best"]["score"],
                "metrics": r["best"]["metrics"],
                "wf_test_score": (r.get("validation") or {}).get("avg_test_score"),
            }
            for sym, r in stock_summaries.items()
        },
        "all_ranked": [
            {
                "symbol": r.get("_symbol_raw"),
                "template": r["template"],
                "best_score": r["best"]["score"],
                "metrics": r["best"]["metrics"],
                "wf_test_score": (r.get("validation") or {}).get("avg_test_score"),
            }
            for r in sorted(all_results, key=lambda r: r["best"]["score"], reverse=True)
        ],
    }
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary_output, f, ensure_ascii=False, indent=2)
    print(f"\n  汇总保存: {summary_path}")

    print(f"\n{'='*60}")
    print(f"  优化完成!")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
