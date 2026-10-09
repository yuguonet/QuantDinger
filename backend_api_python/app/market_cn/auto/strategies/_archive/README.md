# strategies/_archive/ — 已退役策略归档

本目录存放**已退役**的策略实现，保留以便回溯，不参与运行时。

## 为什么放在子目录

`strategies/__init__.py::autodiscover()` 用 `pkgutil.iter_modules([pkg_dir])` 遍历本包，
**不递归子目录** ⇒ 本目录下的模块不会被自动注册，也不会被 `doctor` / `scan` / 展示层
当作现存策略。目录本身出现在 `os.listdir` 结果里，但不以 `.py` 结尾，被门禁过滤。

## 归档清单

| 策略 | 文件 | 退役日期 | 原因 |
|---|---|---|---|
| `lead_chase`（领涨追击） | `lead_chase.py` | 2026-10-09 | 停用遗留：**无 yaml 宏**（违反 §2 单一事实源），且执行通道未就绪（早盘单点策略与本项目下午滚动扫描批次不兼容）。研究依据（v7 重设计的全部实证）见 `docs/策略研究依据归档.md#lead_chase`。 |
| `relay3`（3板接力） | `relay3.py` / `relay3.yaml` | 2026-10-09 | 用户 2026-09-23 裁定停用（入场逻辑缺陷：3板次日追入无法成交/期望为 0）；且**无折叠契约**（`init_state`/`step`/`evaluate` 缺失 ⇒ 进不了回测/调试路径）。研究依据见 `docs/策略研究依据归档.md#relay3`。 |
| `v1`（V1 追板） | `v1.py` / `v1.yaml` | 2026-10-09 | 停用遗留：**无折叠契约**（非完整宏）。研究依据（含前视史）见 `docs/策略研究依据归档.md#v1`。 |

> 决策出处：`docs/目标态偏离核对_20261009.md` 的 **P6-6 / P6-7**（退役 vs 补齐宏/契约 → 用户 2026-10-09 裁定退役）。
> 退役惯例与 `registry.py` 的历史沿用：`dragon_callback_legacy` / `break_v2` / `dragon_v2` /
> `triple_resonance` / `t_hilo` 也曾以「归档 `_archive/` + 同步删 fallback」方式退役。

## 恢复方法（若日后重启）

1. 把对应文件移回上级目录 `strategies/`；
2. 在 `registry.py::_STRATEGIES_FALLBACK` 补回 key（可选，仅兜底用）；
3. 若策略**无 yaml**，需补 `<key>.yaml` 宏（`meta.enabled` + `gates` + `signal.fields`）；
4. `relay3` / `v1` 还需补**折叠契约**（`init_state` / `step` / `evaluate`）方能进回测/调试；
5. 同步 `tests/present/test_strategy_files_are_documents.py::BASELINE` 与
   `app/market_cn/auto/sampler.py::STAGE_RANK`。
