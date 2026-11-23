# P0-A tail_recall 诊断接入说明（契口文档）

> 交付物：`backend_api_python/app/agent/wheels/gbdt_tail/recall_diag.py`（已语法校验）。
> 作用：补上设计 §4.3"昨日 top-K vs 今日实际 → tail_recall 诊断指标（轮子层独立归因）"的最后一环。
> 以下两处接线在既有文件上，按 K1 冲突 B 的先例（additive + env 默认关 + fail-open + 留痕）由你确认后添加。

## 1. scheduler 接线（`app/market_cn/scheduler.py` 两处小改）

仿 `tail_scores_daily` 的先例（K2），加一个 18:40 的诊断任务（须晚于 18:30 的分数落库）：

```python
# ① TASKS 列表末尾追加（与 tail_scores_daily 条目并列）:
    # P0-A tail_recall 诊断回填 (2026-11-23, §4.3 轮子层归因): 18:40 —— 晚于 18:30 分数落库,
    # 标签成熟才回填(fail-open 跳过未成熟日); 默认关闭(QD_TAIL_RECALL_ENABLED=1 才跑)。
    Task("tail_recall_daily", _save_tail_recall_daily, interval=86400, trading_only=False,
         once_per_day=True, trigger_hour=18, trigger_minute=40),

# ② 仿 _save_tail_scores_daily 的包装函数放同文件:
def _save_tail_recall_daily():
    """tail_recall 诊断回填(默认关, fail-open)。"""
    if (os.getenv("QD_TAIL_RECALL_ENABLED", "0") or "0").strip() != "1":
        return
    from app.agent.wheels.gbdt_tail.recall_diag import run_daily
    result = run_daily()
    if "error" in (result or {}):
        print(f"[tail_recall_daily] skipped: {result['error']}", flush=True)
```

手动验证（不走 cron）：

```bash
cd backend_api_python
QD_TAIL_RECALL_ENABLED=1 python -m app.agent.wheels.gbdt_tail.recall_diag
# 预期: {'status': 'ok', 'filled': N}；重复运行 filled=0（幂等，唯一键冲突跳过）
# 然后: SELECT score_date, k, n_tail, n_hit, recall FROM qd_tail_recall ORDER BY score_date DESC;
```

验收建议（沿用 K4 风格）：① 建表幂等；② 对 2026-11-20 打分日回填一行，人工抽查 n_tail 与全市场实际涨停+大涨数一致；③ 重复运行幂等；④ env 关时不建表不写库。

## 2. F3 收敛（`app/data_sources/provider/adjustment.py` 公共访问器，另可选）

`score.py`/`recall_diag.py` 目前依赖 `_CACHE_FILE`/`_to_sina_code` 两个私有符号（J3 在案）。若本轮收敛，在 `adjustment.py` 末尾加两个只读公共别名：

```python
# 公共只读访问器(2026-11-23, J3 收敛): 供 app/agent/wheels 读取 qfq 因子缓存指纹与代码归一,
# 不暴露写入口; provider 重构时保持这两个签名即可。
def factor_cache_fingerprint() -> str:
    """qfq 因子缓存文件指纹 (mtime_ns:size)。"""
    st = Path(_CACHE_FILE).stat()
    return f"{st.st_mtime_ns}:{st.st_size}"

def to_sina_code(instrument) -> str:
    return _to_sina_code(str(instrument))
```

score.py 对应替换（3 处）：

| 原 | 换 |
|---|---|
| `json.loads(Path(adjustment._CACHE_FILE).read_text(...))`（_factor_codes/_all_codes） | `json.loads(Path(adjustment._CACHE_FILE).read_text(...))` 不变，或提 `adjustment.factor_cache_json()`（如需一并收敛） |
| `adjustment._to_sina_code(...)` | `adjustment.to_sina_code(...)` |
| factor_sig 计算处 | `adjustment.factor_cache_fingerprint()` |

不换也不影响运行（J3 已按清单记录）；换则 provider 重构风险清零。

## 3. 与既有链路的边界（不做的事）

- 不进 p_base/Δp/Brier 链（冲突 A 拍板 A1：tail 是尾部 estimand，仅诊断）；
- 不动 learning/、skill、tool 既有文件；
- ST 阈值与生产训练口径一致（非 ST 阈值；H3 修复后此表自然受益，无需改本模块）；
- 诊断表可 DROP 重算（全量回填靠 CLI 逐日补），不依赖增量日志。
