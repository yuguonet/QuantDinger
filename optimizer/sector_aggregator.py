"""
板块聚合模块 — 同概念/同行业股票横向聚合，扩样本验证策略

═══════════════════════════════════════════════════════════════════════
  核心问题
═══════════════════════════════════════════════════════════════════════

  单只股票日线策略交易数太少（5 笔/3 年），WF 根本没法验证。

  解决方案：同概念 20 只股票跑同一个策略 → 5笔×20只 = 100 笔，
  样本量够了，统计检验才有意义。

═══════════════════════════════════════════════════════════════════════
  工作流程
═══════════════════════════════════════════════════════════════════════

  1. 按概念/行业分组：从 stock_basic_info 拉取分组
  2. 组内跑策略：同一个策略模板 + 参数，跑组内所有股票
  3. 汇聚交易：把所有股票的 trades 合并到一个池子里
  4. 统计验证：对汇聚后的交易池做 WF / 统计检验
  5. 输出结论：这个策略在"锂电池概念"上是否成立

═══════════════════════════════════════════════════════════════════════
  使用方式
═══════════════════════════════════════════════════════════════════════

  from optimizer.sector_aggregator import SectorAggregator

  sa = SectorAggregator()

  # 查看有哪些概念
  concepts = sa.list_concepts()  # ["锂电池", "新能源", "白酒", ...]

  # 获取某概念下的股票
  stocks = sa.get_concept_stocks("锂电池")  # ["002594.SZ", "300750.SZ", ...]

  # 按概念聚合回测（核心）
  result = sa.run_concept_backtest(
      concept="锂电池",
      template_key="kdj_crossover",
      start_date="2023-01-01",
      end_date="2025-12-31",
      n_trials=50,
  )
  # result = {
  #     "concept": "锂电池",
  #     "n_stocks": 25,
  #     "total_trades": 127,
  #     "win_rate": 0.58,
  #     "avg_alpha": 0.003,
  #     "sharpe": 1.2,
  #     "per_stock": [...],
  #     "pooled_trades": [...],
  # }

  # 按行业聚合
  result = sa.run_industry_backtest(industry="白酒", ...)

═══════════════════════════════════════════════════════════════════════
  与 optimizer/runner 的关系
═══════════════════════════════════════════════════════════════════════

  runner.py 是单只股票跑完整优化流程。
  本模块是把 runner 的能力扩展到"一组股票"，然后汇聚结果。

  不替代 runner，而是调用 runner 的底层能力。
"""
from __future__ import annotations

import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

# 确保路径
_optimizer_dir = os.path.dirname(os.path.abspath(__file__))
_project_root = os.path.dirname(_optimizer_dir)
_backend_root = os.path.join(_project_root, "backend_api_python")
if _backend_root not in sys.path:
    sys.path.insert(0, _backend_root)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


def _load_env():
    """加载 .env"""
    try:
        from dotenv import load_dotenv
        for env_path in [
            os.path.join(_backend_root, '.env'),
            os.path.join(_project_root, '.env'),
        ]:
            if os.path.isfile(env_path):
                load_dotenv(env_path, override=False)
                break
    except Exception:
        pass


def _get_basic_db():
    """获取 stock_basic_info 数据库"""
    _load_env()
    from app.utils.basicinfo_db import get_stock_basic_db
    return get_stock_basic_db()


# ================================================================
#  概念/行业分组
# ================================================================

class SectorAggregator:
    """
    板块聚合器。

    按概念或行业把股票分组，然后横向跑策略、汇聚交易、统计验证。
    """

    def __init__(self):
        self._db = None

    def _ensure_db(self):
        if self._db is None:
            self._db = _get_basic_db()

    # ── 概念/行业查询 ──────────────────────────────────────

    def list_concepts(self) -> List[str]:
        """列出所有概念标签"""
        self._ensure_db()
        return self._db.get_all_concepts()

    def list_industries(self) -> List[str]:
        """列出所有行业"""
        self._ensure_db()
        return self._db.get_all_industries()

    def get_concept_stocks(self, concept: str, active_only: bool = True) -> List[str]:
        """
        获取某概念下的股票代码列表。

        Args:
            concept: 概念关键词，如 "锂电池"
            active_only: 只返回 active 状态的股票

        Returns:
            ["002594.SZ", "300750.SZ", ...]
        """
        self._ensure_db()
        stocks = self._db.get_stocks_by_concept(concept)
        # get_stocks_by_concept 返回 dict 列表，提取 symbol
        result = []
        for s in stocks:
            if isinstance(s, dict):
                sym = s.get("symbol", "")
                if active_only and s.get("status") != "active":
                    continue
            else:
                sym = str(s)
            if sym:
                result.append(sym)
        return result

    def get_industry_stocks(self, industry: str, active_only: bool = True) -> List[str]:
        """获取某行业下的股票代码列表"""
        self._ensure_db()
        stocks = self._db.get_stocks_by_industry(industry)
        result = []
        for s in stocks:
            if isinstance(s, dict):
                sym = s.get("symbol", "")
                if active_only and s.get("status") != "active":
                    continue
            else:
                sym = str(s)
            if sym:
                result.append(sym)
        return result

    def get_stock_concepts(self, symbol: str) -> List[str]:
        """获取某只股票的所有概念标签"""
        self._ensure_db()
        # get_stock 可能接受 symbol 字符串
        stock = self._db.get_stock(symbol)
        if isinstance(stock, dict) and stock.get("concepts"):
            return [c.strip() for c in stock["concepts"].split(",") if c.strip()]
        return []

    def get_stock_industry(self, symbol: str) -> str:
        """获取某只股票的行业"""
        self._ensure_db()
        stock = self._db.get_stock(symbol)
        if isinstance(stock, dict):
            return stock.get("industry", "")
        return ""

    # ── 概念间重叠分析 ──────────────────────────────────────

    def concept_overlap(self, concept_a: str, concept_b: str) -> Dict[str, Any]:
        """
        分析两个概念之间的股票重叠度。

        用于选择不重叠的概念做独立样本，避免同一只股票被重复计数。
        """
        stocks_a = set(self.get_concept_stocks(concept_a))
        stocks_b = set(self.get_concept_stocks(concept_b))
        overlap = stocks_a & stocks_b

        return {
            "concept_a": concept_a,
            "concept_b": concept_b,
            "stocks_a": len(stocks_a),
            "stocks_b": len(stocks_b),
            "overlap": len(overlap),
            "jaccard": len(overlap) / len(stocks_a | stocks_b) if stocks_a | stocks_b else 0,
            "overlap_stocks": sorted(overlap),
        }

    def find_independent_concepts(
        self, min_stocks: int = 10, max_overlap_ratio: float = 0.3
    ) -> List[str]:
        """
        找出一组互不重叠（或低重叠）的概念，用于独立验证。

        贪心算法：按股票数从大到小依次选取，与已选概念重叠度低于阈值的才保留。
        """
        all_concepts = self.list_concepts()
        # 按股票数排序
        concept_sizes = []
        for c in all_concepts:
            stocks = self.get_concept_stocks(c)
            if len(stocks) >= min_stocks:
                concept_sizes.append((c, len(stocks), set(stocks)))
        concept_sizes.sort(key=lambda x: -x[1])

        selected = []
        selected_stocks = set()

        for name, size, stocks in concept_sizes:
            # 与已选概念的重叠
            overlap_with_selected = stocks & selected_stocks
            overlap_ratio = len(overlap_with_selected) / size if size > 0 else 1.0
            if overlap_ratio <= max_overlap_ratio:
                selected.append(name)
                selected_stocks.update(stocks)

        return selected

    # ── 核心：板块聚合回测 ──────────────────────────────────

    def run_concept_backtest(
        self,
        concept: str,
        template_key: str,
        start_date: str = "2023-01-01",
        end_date: str = "2025-12-31",
        n_trials: int = 50,
        score_fn: str = "composite",
        market: str = "CNStock",
        timeframe: str = "1D",
        max_stocks: int = 30,
        jobs: int = 1,
        market_filter: bool = False,
    ) -> Dict[str, Any]:
        """
        对某个概念下的所有股票跑同一个策略，汇聚交易结果。

        Args:
            concept: 概念名称
            template_key: 策略模板名
            start_date/end_date: 回测区间
            n_trials: 每只股票的优化轮数
            score_fn: 评分函数
            market/timeframe: 市场和时间框架
            max_stocks: 最多跑多少只（概念下股票太多时截断）
            jobs: 并行进程数

        Returns:
            {
                "concept": str,
                "template": str,
                "n_stocks": int,
                "n_stocks_traded": int,
                "total_trades": int,
                "win_rate": float,
                "avg_profit_per_trade": float,
                "total_return": float,
                "sharpe": float,
                "avg_alpha": float,
                "per_stock_results": [...],
                "pooled_trades": [...],
            }
        """
        stocks = self.get_concept_stocks(concept)
        if not stocks:
            return {"error": f"概念 '{concept}' 下无股票"}

        if len(stocks) > max_stocks:
            # 优先选有更多数据的股票（简单截断）
            stocks = stocks[:max_stocks]

        return self._run_group_backtest(
            group_name=concept,
            group_type="concept",
            stocks=stocks,
            template_key=template_key,
            start_date=start_date,
            end_date=end_date,
            n_trials=n_trials,
            score_fn=score_fn,
            market=market,
            timeframe=timeframe,
            jobs=jobs,
            market_filter=market_filter,
        )

    def run_industry_backtest(
        self,
        industry: str,
        template_key: str,
        start_date: str = "2023-01-01",
        end_date: str = "2025-12-31",
        n_trials: int = 50,
        score_fn: str = "composite",
        market: str = "CNStock",
        timeframe: str = "1D",
        max_stocks: int = 30,
        jobs: int = 1,
        market_filter: bool = False,
    ) -> Dict[str, Any]:
        """对某个行业下的所有股票跑同一个策略，汇聚交易结果。"""
        stocks = self.get_industry_stocks(industry)
        if not stocks:
            return {"error": f"行业 '{industry}' 下无股票"}

        if len(stocks) > max_stocks:
            stocks = stocks[:max_stocks]

        return self._run_group_backtest(
            group_name=industry,
            group_type="industry",
            stocks=stocks,
            template_key=template_key,
            start_date=start_date,
            end_date=end_date,
            n_trials=n_trials,
            score_fn=score_fn,
            market=market,
            timeframe=timeframe,
            jobs=jobs,
            market_filter=market_filter,
        )

    def _run_group_backtest(
        self,
        group_name: str,
        group_type: str,
        stocks: List[str],
        template_key: str,
        start_date: str,
        end_date: str,
        n_trials: int,
        score_fn: str,
        market: str,
        timeframe: str,
        jobs: int,
        market_filter: bool = False,
    ) -> Dict[str, Any]:
        """
        内部方法：对一组股票跑策略并汇聚结果。
        market_filter: 启用大盘过滤，剔除大盘下跌趋势期间开仓的交易
        """
        from optimizer.runner import (
            get_template_unified, BacktestObjective,
            _is_ashare_market, _get_ashare_initial_capital, _get_ashare_commission,
            parse_market_symbol, _list_local_symbols,
        )
        import pandas as pd
        from app.data_sources.factory import DataSourceFactory
        from app.services.backtest import BacktestService

        # ── Monkey-patch: 用正确的日期范围取数据，绕过 runner 的 limit*1.5 反算 ──
        _original_fetch = BacktestService._fetch_kline_data

        def _direct_fetch(self, market, symbol, timeframe, start_date, end_date):
            """直接用 start_date/end_date 查 db_market，不做 limit 反算"""
            from optimizer.data_warehouse.storage import _get_writer
            writer = _get_writer()
            db_symbol = symbol.split(".")[0] if "." in symbol else symbol
            kline_data = writer.query(
                market, db_symbol, timeframe,
                start_time=start_date, end_time=end_date, limit=0,
            )
            if not kline_data:
                return pd.DataFrame()
            df = pd.DataFrame(kline_data)
            if "time" in df.columns:
                df["time"] = pd.to_datetime(df["time"])
                df = df.set_index("time")  # 关键：设置 time 为索引，与原始 _fetch_kline_data 保持一致
            return df

        # 过滤出本地有数据的股票
        # db_market 和 BacktestObjective 都用不带后缀的 symbol（如 "000009"）
        local_symbols = set(_list_local_symbols(market, timeframe))
        if not local_symbols:
            return {"error": f"本地数据仓库中没有 {market}/{timeframe} 的数据，请先下载"}

        filtered = []
        for s in stocks:
            if ":" in s:
                _, sym = parse_market_symbol(s)
            else:
                sym = s
            # 统一去掉后缀
            code = sym.split(".")[0] if "." in sym else sym
            if code in local_symbols:
                filtered.append(code)

        if not filtered:
            return {
                "error": f"概念 '{group_name}' 下 {len(stocks)} 只股票在本地数据仓库中均无数据",
                "hint": f"本地有数据的股票: {len(local_symbols)} 只",
            }

        skipped = len(stocks) - len(filtered)
        stocks = filtered

        sd = datetime.strptime(start_date, "%Y-%m-%d")
        ed = datetime.strptime(end_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59)

        template = get_template_unified(template_key)
        is_ashare = _is_ashare_market(market)
        initial_capital = _get_ashare_initial_capital() if is_ashare else 10000.0
        commission = _get_ashare_commission() if is_ashare else 0.001

        # 确定默认参数（从 params 参数空间取中点）
        raw_params = template.get("params") or template.get("param_space") or {}
        default_params = {}
        for k, v in raw_params.items():
            if isinstance(v, dict):
                if "choices" in v:
                    default_params[k] = v["choices"][0]
                elif "low" in v and "high" in v:
                    default_params[k] = (v["low"] + v["high"]) // 2 if isinstance(v["low"], int) else (v["low"] + v["high"]) / 2
                else:
                    default_params[k] = v
            else:
                default_params[k] = v

        print(f"\n{'='*60}")
        print(f"  板块聚合回测: [{group_type}] {group_name}")
        print(f"  策略: {template['name']} ({template_key})")
        print(f"  股票数: {len(stocks)} (原 {len(stocks)+skipped} 只, {skipped} 只本地无数据跳过)")
        print(f"  回测区间: {start_date} ~ {end_date}")
        print(f"  默认参数: {default_params}")
        print(f"{'='*60}")

        per_stock_results = []
        all_trades = []
        errors = []

        # 应用 patch：让 BacktestService 用正确的日期范围取数据
        BacktestService._fetch_kline_data = _direct_fetch

        for idx, symbol in enumerate(stocks):
            print(f"\r  [{idx+1}/{len(stocks)}] {symbol}...", end="", flush=True)

            try:
                objective = BacktestObjective(
                    template_key=template_key,
                    symbol=symbol,
                    market=market,
                    timeframe=timeframe,
                    start_date=sd,
                    end_date=ed,
                    initial_capital=initial_capital,
                    commission=commission,
                )

                metrics = objective(default_params)
                trades = metrics.get("trades", [])

                per_stock_results.append({
                    "symbol": symbol,
                    "total_trades": metrics.get("totalTrades", 0),
                    "win_rate": metrics.get("winRate", 0),
                    "total_return": metrics.get("totalReturn", 0),
                    "sharpe": metrics.get("sharpeRatio", 0),
                    "max_drawdown": metrics.get("maxDrawdown", 0),
                    "profit_factor": metrics.get("profitFactor", 0),
                })

                for trade in trades:
                    trade_copy = dict(trade)
                    trade_copy["_symbol"] = symbol
                    all_trades.append(trade_copy)

            except Exception as e:
                errors.append({"symbol": symbol, "error": str(e)})
                per_stock_results.append({
                    "symbol": symbol,
                    "total_trades": 0,
                    "error": str(e),
                })

        print(f"\n  完成: {len(stocks)} 只股票, {len(errors)} 个错误")

        # 还原 patch
        BacktestService._fetch_kline_data = _original_fetch

        # ── 大盘噪音过滤 ──
        if market_filter and all_trades:
            from optimizer.market_sentiment import MarketBenchmark
            mb = MarketBenchmark()

            # 按股票分组，识别需要剔除的开仓交易
            # trades 是按顺序排列的 open/close 事件对
            filtered_trades = []
            removed_count = 0
            regime_stats = {"up": 0, "flat": 0, "down": 0}
            # 按 _symbol 分组
            trades_by_symbol = {}
            for t in all_trades:
                sym = t.get("_symbol", "")
                trades_by_symbol.setdefault(sym, []).append(t)

            for sym, sym_trades in trades_by_symbol.items():
                # 构造带后缀的代码（用于市场判断）
                code = sym.split(".")[0] if "." in sym else sym
                if code.startswith("6"):
                    sym_with_suffix = code + ".SH"
                else:
                    sym_with_suffix = code + ".SZ"

                # 找出大盘下跌期间的开仓时间
                bad_opens = set()
                for t in sym_trades:
                    if t.get("type", "").startswith("open_"):
                        trade_date = t["time"][:10]  # 'YYYY-MM-DD'
                        regime = mb.get_regime(sym_with_suffix, trade_date)
                        regime_stats[regime["trend"]] += 1
                        if regime["trend"] == "down":
                            bad_opens.add(id(t))
                            removed_count += 1

                if bad_opens:
                    # 配对删除：开仓被删 → 对应的平仓也要删
                    pending_close = None
                    for t in sym_trades:
                        if t.get("type", "").startswith("open_"):
                            if id(t) in bad_opens:
                                pending_close = True  # 标记下一个平仓要删
                                continue
                            else:
                                pending_close = False
                        elif t.get("type", "").startswith("close_"):
                            if pending_close:
                                removed_count += 1
                                continue  # 跳过对应的平仓
                            pending_close = None
                        filtered_trades.append(t)
                else:
                    filtered_trades.extend(sym_trades)

            if removed_count > 0:
                print(f"\n  🛡️ 大盘过滤: 剔除 {removed_count} 笔交易（大盘下跌趋势期间开仓）")
                print(f"     大盘状态分布: up={regime_stats['up']}, flat={regime_stats['flat']}, down={regime_stats['down']}")
                all_trades = filtered_trades

                # 重新计算 per_stock_results
                for r in per_stock_results:
                    sym = r["symbol"]
                    sym_filtered = [t for t in all_trades if t.get("_symbol") == sym]
                    opens = [t for t in sym_filtered if t.get("type", "").startswith("open_")]
                    closes = [t for t in sym_filtered if t.get("type", "").startswith("close_")]
                    r["total_trades"] = len(opens)
                    if opens and closes:
                        profits = [t.get("profit", 0) for t in closes]
                        wins = sum(1 for p in profits if p > 0)
                        r["win_rate"] = round(wins / len(profits) * 100, 2) if profits else 0
                    else:
                        r["win_rate"] = 0
            else:
                print(f"\n  🛡️ 大盘过滤: 无交易被剔除")
                print(f"     大盘状态分布: up={regime_stats['up']}, flat={regime_stats['flat']}, down={regime_stats['down']}")

        # ── 汇聚统计 ──
        if errors:
            print(f"\n  ❌ 错误详情 (前 5 个):")
            for e in errors[:5]:
                print(f"    {e['symbol']}: {e['error']}")

        traded_stocks = [r for r in per_stock_results if r.get("total_trades", 0) > 0]
        total_trades = sum(r.get("total_trades", 0) for r in per_stock_results)

        pooled_stats = {}
        if total_trades > 0:
            # 汇聚胜率
            total_wins = 0
            total_profit = 0.0
            total_loss = 0.0
            all_returns = []

            for r in traded_stocks:
                n = r["total_trades"]
                wr = r.get("win_rate", 0) / 100.0
                total_wins += round(n * wr)
                # 估算盈亏（从 total_return 和 profit_factor 推算）
                tr = r.get("total_return", 0)
                pf = r.get("profit_factor", 1)
                if tr > 0 and pf > 1:
                    total_profit += tr * pf / (pf - 1) * initial_capital / 100
                    total_loss += tr / (pf - 1) * initial_capital / 100
                all_returns.append(tr)

            pooled_win_rate = total_wins / total_trades if total_trades > 0 else 0
            avg_return = float(np.mean(all_returns)) if all_returns else 0
            avg_sharpe = float(np.mean([r.get("sharpe", 0) for r in traded_stocks])) if traded_stocks else 0

            pooled_stats = {
                "total_trades": total_trades,
                "n_stocks_traded": len(traded_stocks),
                "pooled_win_rate": round(pooled_win_rate * 100, 2),
                "avg_return_per_stock": round(avg_return, 2),
                "avg_sharpe": round(avg_sharpe, 3),
                "median_trades_per_stock": int(np.median([r["total_trades"] for r in traded_stocks])) if traded_stocks else 0,
            }
        else:
            pooled_stats = {
                "total_trades": 0,
                "n_stocks_traded": 0,
            }

        return {
            "group_name": group_name,
            "group_type": group_type,
            "template": template_key,
            "n_stocks": len(stocks),
            "n_errors": len(errors),
            "start_date": start_date,
            "end_date": end_date,
            **pooled_stats,
            "per_stock_results": per_stock_results,
            "errors": errors,
        }

    # ── 批量概念验证 ──────────────────────────────────────

    def run_multi_concept_backtest(
        self,
        concepts: List[str],
        template_key: str,
        **kwargs,
    ) -> Dict[str, Any]:
        """
        对多个概念分别跑聚合回测，对比结果。

        用于判断策略在哪些概念上有效。
        """
        results = {}
        for concept in concepts:
            print(f"\n{'#'*60}")
            print(f"  概念: {concept}")
            print(f"{'#'*60}")
            results[concept] = self.run_concept_backtest(
                concept=concept,
                template_key=template_key,
                **kwargs,
            )

        # 汇总对比
        summary = []
        for concept, r in results.items():
            if "error" in r:
                continue
            summary.append({
                "concept": concept,
                "n_stocks": r["n_stocks"],
                "total_trades": r.get("total_trades", 0),
                "pooled_win_rate": r.get("pooled_win_rate", 0),
                "avg_return": r.get("avg_return_per_stock", 0),
                "avg_sharpe": r.get("avg_sharpe", 0),
            })

        # 按 total_trades 排序（样本量优先）
        summary.sort(key=lambda x: -x["total_trades"])

        return {
            "per_concept": results,
            "summary": summary,
        }


# ================================================================
#  CLI
# ================================================================

def main():
    import argparse

    parser = argparse.ArgumentParser(description="板块聚合回测")
    parser.add_argument("--concept", "-c", type=str, help="概念名称")
    parser.add_argument("--industry", "-i", type=str, help="行业名称")
    parser.add_argument("--template", "-t", type=str, default="kdj_crossover", help="策略模板")
    parser.add_argument("--start", type=str, default="2023-01-01")
    parser.add_argument("--end", type=str, default="2025-12-31")
    parser.add_argument("--list-concepts", action="store_true", help="列出所有概念")
    parser.add_argument("--list-industries", action="store_true", help="列出所有行业")
    parser.add_argument("--top", type=int, default=20, help="列出前 N 个概念")
    parser.add_argument("--max-stocks", type=int, default=30)
    parser.add_argument("--trials", type=int, default=50)
    parser.add_argument("--market-filter", "-mf", action="store_true", help="启用大盘过滤：剔除大盘下跌趋势期间开仓的交易")

    args = parser.parse_args()

    sa = SectorAggregator()

    if args.list_concepts:
        concepts = sa.list_concepts()
        print(f"\n📊 共 {len(concepts)} 个概念:")
        for c in concepts[:args.top]:
            stocks = sa.get_concept_stocks(c)
            print(f"   {c}: {len(stocks)} 只")
        return

    if args.list_industries:
        industries = sa.list_industries()
        print(f"\n📊 共 {len(industries)} 个行业:")
        for ind in industries[:args.top]:
            stocks = sa.get_industry_stocks(ind)
            print(f"   {ind}: {len(stocks)} 只")
        return

    if args.concept:
        result = sa.run_concept_backtest(
            concept=args.concept,
            template_key=args.template,
            start_date=args.start,
            end_date=args.end,
            n_trials=args.trials,
            max_stocks=args.max_stocks,
            market_filter=args.market_filter,
        )
        print(f"\n📊 汇总:")
        print(json.dumps({k: v for k, v in result.items()
                          if k not in ("per_stock_results", "errors")},
                         indent=2, ensure_ascii=False))
        return

    if args.industry:
        result = sa.run_industry_backtest(
            industry=args.industry,
            template_key=args.template,
            start_date=args.start,
            end_date=args.end,
            n_trials=args.trials,
            max_stocks=args.max_stocks,
            market_filter=args.market_filter,
        )
        print(f"\n📊 汇总:")
        print(json.dumps({k: v for k, v in result.items()
                          if k not in ("per_stock_results", "errors")},
                         indent=2, ensure_ascii=False))
        return

    parser.print_help()


if __name__ == "__main__":
    main()
