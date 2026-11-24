# 涨跌停规则

| 板块 | 涨跌停幅度 | 代码特征 |
|------|-----------|----------|
| 主板/中小板 | ±10% | 60xxxx / 00xxxx |
| ST / 风险警示 | **跟随所在板**（主板 ±10%、创业/科创 ±20%） | 名称含 ST |
| 创业板 | ±20% | 300xxx / 301xxx |
| 科创板 | ±20% | 688xxx |
| 北交所 | ±30% | 8xxxxx / 4xxxxx / 92xxxx |
| **新股上市首日 / 前 5 日** | **无限制**（`get_limit_pct`→0） | 名称 N/C 开头 |

## 口径

- `get_limit_pct()`：**按板块定幅度，ST 不单独 ±5%**；N/C 新股返回 `0.0`（无涨跌幅）。
  仅作 rt 缺失时的近似，**有 `rt.limit_*_price` 时以 rt 为准**。
- 个股是否封板以 **`rt.limit_up`**（现价 vs 涨停价）为准；`stale_bar=true` 时
  `tech.limit_up` 已按 rt 修正。封板 = **当日买不进**，在 `rt_risks` 里标
  「涨停封板-当日买不进」——**是否排除由你决定**，代码不自动淘汰。

### `rt.limit_band` 字段

`apply_rt_overlay` 会把 quote 价解析后写入：

| 取值 | 含义 | `limit_up_price` / `limit_down_price` | `limit_up` |
| --- | --- | --- | --- |
| `price` | 有有效涨跌停价 | >0 的价格 | 现价是否触涨停 |
| `no_limit` | quote 明确哨兵 ≤0（新股无限制等） | `None` | **False**（禁止回退公式） |
| `missing` | 源无此字段 | `None` | 才允许 `get_limit_pct` 近似 |

## 过滤规则在哪

**本文件不存放过滤/排序/淘汰规则**（`pre_screen`/`profile_candidates` 层不做取舍）。
`run()` 一键路径 = **单链路保底轨**（截断 35 只恒入 deep，2026-11-23 起；旧版 25），
被非封板 `hard` 拦掉几只见 `funnel` 统计键——那是客观障碍计数，不是「代码重新筛选」。
现行取舍规则见 `strategy-rules.md` §二（选人链路）。
