"""store.py (原 dragon_store.py) - 自动策略组存储层

职责:
  1. qd_dragon_signals 事实表 (状态机全量+历史) 的建表与 CRUD
  2. qd_watchlist 迁移 (strategy_state/strategy_detail 列 + UNIQUE 约束放宽)
  3. sync_watchlist_group(): 活跃信号 → qd_watchlist '自动策略组' 的全量对账
     (引擎独占读写删, 失效票删行, 历史留在 signals 表)

设计要点:
  - signals 表是唯一事实源; qd_watchlist 策略组行只是活跃信号的"投影"
  - 全部幂等: 重复执行不产生脏数据
  - 单用户部署: 写 user_id=1 (DRAGON_USER_ID), 所有用户可见同一策略组
  - 策略元数据 (key/标签/胜率/名额/状态机) 2026-09-10 拆至 registry.py,
    本模块 re-export 全部名字, `from app.market_cn.auto import store as ds; ds.S_HOLDING` 等旧用法不变。
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta

from app.utils.logger import get_logger

logger = get_logger(__name__)

# ---- 策略注册表 (元数据单一事实源在 registry.py, 此处 re-export) ----
from app.market_cn.auto.registry import (  # noqa: F401  (re-export, 对外 API 不变)
    ACTIVE_GROUP_STATES,
    DRAGON_GROUP_NAME,
    DRAGON_MARKET,
    DRAGON_STRATEGY,
    DRAGON_USER_ID,
    S_BUY_TODAY,
    S_CLOSED,
    S_EXPIRED,
    S_EXIT_TODAY,
    S_HOLDING,
    S_WATCH_PENDING,
    enabled_keys,
    state_label,
    strategy_winrate,
    strategy_keys,
    strategy_labels,
)

_SIGNALS_TABLE = "qd_dragon_signals"
_WATCHLIST_TABLE = "qd_watchlist"


# ================================================================
# 建表与迁移 (幂等)
# ================================================================

def ensure_tables():
    """建 qd_dragon_signals + qd_watchlist 迁移 (加列/放宽UNIQUE约束)。可重复调用。"""
    from app.utils.db import get_db_connection

    with get_db_connection() as db:
        cur = db.cursor()
        # ── 1. signals 事实表 ──
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {_SIGNALS_TABLE} (
                id            SERIAL PRIMARY KEY,
                trade_date    DATE NOT NULL,
                strategy      VARCHAR(30) NOT NULL DEFAULT '{DRAGON_STRATEGY}',
                code          VARCHAR(16) NOT NULL,
                name          VARCHAR(64) DEFAULT '',
                board         VARCHAR(16) DEFAULT '',
                entry_style   VARCHAR(8) DEFAULT 'a',
                score         INTEGER DEFAULT 0,
                state         VARCHAR(20) NOT NULL,
                signal_date   DATE,
                signal_price  NUMERIC,
                lu_date       DATE,
                pullback_days INTEGER,
                confirm_date  DATE,
                d1_chg        NUMERIC,
                d1_vol_r      NUMERIC,
                entry_date    DATE,
                entry_price   NUMERIC,
                stop_price    NUMERIC,
                exit_reason   VARCHAR(80) DEFAULT '',
                exit_date     DATE,
                exit_price    NUMERIC,
                extra         JSONB DEFAULT '{{}}',
                created_at    TIMESTAMP DEFAULT NOW(),
                updated_at    TIMESTAMP DEFAULT NOW(),
                UNIQUE(trade_date, strategy, code, entry_style)
            )
        """)
        cur.execute(f"CREATE INDEX IF NOT EXISTS idx_qdds_state ON {_SIGNALS_TABLE}(state)")
        cur.execute(f"CREATE INDEX IF NOT EXISTS idx_qdds_date ON {_SIGNALS_TABLE}(trade_date)")
        # 旧版唯一键 (未含 strategy) → 升级 (名称无关, 按定义判定)
        cur.execute("""
            SELECT conname FROM pg_constraint
            WHERE conrelid = 'qd_dragon_signals'::regclass AND contype = 'u'
              AND pg_get_constraintdef(oid) NOT ILIKE '%strategy%'
        """)
        for r in cur.fetchall():
            oldname = r["conname"] if isinstance(r, dict) else r[0]
            cur.execute(f"ALTER TABLE {_SIGNALS_TABLE} DROP CONSTRAINT {oldname}")
        cur.execute("""
            SELECT 1 FROM pg_constraint WHERE conname = 'qd_dragon_signals_ukey'
        """)
        if not cur.fetchone():
            cur.execute(f"""
                ALTER TABLE {_SIGNALS_TABLE}
                ADD CONSTRAINT qd_dragon_signals_ukey UNIQUE (trade_date, strategy, code, entry_style)
            """)

        # ── 2. qd_watchlist 加列 ──
        cur.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'qd_watchlist'
        """)
        existing = {r["column_name"] if isinstance(r, dict) else r[0] for r in cur.fetchall()}
        if "strategy_state" not in existing:
            cur.execute("ALTER TABLE qd_watchlist ADD COLUMN strategy_state VARCHAR(20)")
        if "strategy_detail" not in existing:
            cur.execute("ALTER TABLE qd_watchlist ADD COLUMN strategy_detail JSONB")
        if "sort_order" not in existing:
            cur.execute("ALTER TABLE qd_watchlist ADD COLUMN sort_order INTEGER DEFAULT 0")
        # 组名统一为 自动策略组 (旧名迁移)
        cur.execute("UPDATE qd_watchlist SET group_name = %s WHERE group_name = %s",
                    (DRAGON_GROUP_NAME, "龙回头Pro"))

        # ── 3. UNIQUE 约束放宽: (user_id, market, symbol) → (+ group_name) ──
        # 名称无关判定: 只要存在覆盖 4 列的 UNIQUE 约束即视为已迁移
        # (约束名可能是 PG 自动生成的 qd_watchlist_user_id_market_symbol_group_name_key,
        #  硬编码名字会误判并撞上其它表上的同名索引 → DuplicateTable)
        cur.execute(f"""
            SELECT conname FROM pg_constraint
            WHERE conrelid = '{_WATCHLIST_TABLE}'::regclass AND contype = 'u'
              AND pg_get_constraintdef(oid) ILIKE 'UNIQUE (user_id, market, symbol, group_name)%'
        """)
        has_new = bool(cur.fetchall())
        if not has_new:
            try:
                cur.execute("ALTER TABLE qd_watchlist DROP CONSTRAINT IF EXISTS qd_watchlist_user_id_market_symbol_key")
                cur.execute("ALTER TABLE qd_watchlist ADD CONSTRAINT qd_watchlist_ukey "
                            "UNIQUE (user_id, market, symbol, group_name)")
            except Exception as e:
                # 重名冲突等环境差异: 若目标列组合的约束已由其它方式满足则忽略, 否则抛出
                cur.execute(f"""
                    SELECT 1 FROM pg_constraint
                    WHERE conrelid = '{_WATCHLIST_TABLE}'::regclass AND contype = 'u'
                      AND pg_get_constraintdef(oid) ILIKE 'UNIQUE (user_id, market, symbol, group_name)%'
                """)
                if not cur.fetchone():
                    raise
                logger.info("[dragon_store] UNIQUE 约束已存在(重名跳过): %s", e)

        # ── 4. 历史残留清理 ──
        cur.execute(f"DELETE FROM {_SIGNALS_TABLE} WHERE strategy = 'dragon2'")

        db.commit()
        cur.close()
    logger.info("[dragon_store] ensure_tables 完成")


# ================================================================
# signals 表 CRUD
# ================================================================

def _row_to_dict(r):
    d = dict(r)
    for k in ("trade_date", "signal_date", "lu_date", "confirm_date", "entry_date", "exit_date"):
        if d.get(k) is not None and hasattr(d[k], "isoformat"):
            d[k] = d[k].isoformat()
    if d.get("extra") and isinstance(d["extra"], str):
        try:
            d["extra"] = json.loads(d["extra"])
        except Exception:
            pass
    return d


def signal_row(strategy_key, sig, name=""):
    """Signal → qd_dragon_signals 行 dict (扫描器通用转换, 替代各策略手写补字段)。

    口径与旧 dragon_scan 后处理逐字段等价:
      entry_style = 策略类属性 entry_style (dragon=a/v1=v1/break=brk/relay3=r3)
      score       = sig.score (策略构造时已按旧口径设好; 0 值保留 —— dragon 历史口径恒0)
      signal_price= sig.price (0 → None; break 不定价)
      lu_date/pullback_days 来自 extra; extra 整包落库 (策略自保证 clean, None 剔除,
      顶层已映射键 board/lu_date/pullback_days 不重复进子字典) → qd_dragon_signals.extra JSON
    """
    ex = sig.extra or {}
    from app.market_cn.auto.core.market import get_board_name
    row = {
        "strategy": strategy_key,
        "code": sig.code,
        "name": name,
        "board": ex.get("board") or get_board_name(sig.code),
        "style": getattr(_strategy_meta(strategy_key), "entry_style", "a"),
        "score": int(sig.score or 0),
        "signal_date": sig.time,
        "signal_price": float(sig.price) if sig.price else None,
        "lu_date": ex.get("lu_date"),
        "pullback_days": ex.get("pullback_days"),
    }
    # 方案A (2026-09-18): 拔插式 —— 不再有全局白名单, Signal.extra 整包落库。
    # 约定: 策略仅把应落库的字段放进 extra (不塞内部调试量)。
    _top = {"board", "lu_date", "pullback_days"}   # 已在上面映射为顶层列, 不重复
    row["extra"] = {k: v for k, v in ex.items()
                    if v is not None and k not in _top}
    return row


def _strategy_meta(key):
    try:
        from app.market_cn.auto import strategies as _reg
        return _reg.get_strategy(key)
    except Exception:
        return None


def upsert_scan_signals(trade_date: str, rows: list, purge_buy_today: tuple = (), max_retries: int = 5):
    """扫描结果写入 (幂等): rows 为各策略今日信号列表, 行内带 strategy 键。

    扫描是 watch_pending 状态的权威来源: 先清空该 trade_date 的旧 watch_pending
    (防止参数/数据变化后残留幽灵信号), 再插入本轮结果。
    行内可选 state/entry_date/entry_price/stop_price 覆盖默认值
    (knife_catch 等盘中即买策略: state=buy_today, 14:56 已入场)。
    purge_buy_today: 额外清理这些策略今日 state=buy_today 的旧行
      (tail_oversold 滚动预览/终审专用: 14:50~14:56 每分钟重判, 上一轮命中本轮落选的
       股票须删行, 否则残留误导用户; 仅清 buy_today 态, 不碰已转移的 holding 等)。

    瞬态冲突重试 (2026-09-18 事故修复②): 并发 DELETE+INSERT (调度重启补跑触发
    deadlock_detected / 序列化失败) 会整事务回滚丢信号 — 此处捕获 40P01/40001 后
    退避重试, 保证最终写入 (操作幂等, 重试安全)。
    """
    from app.utils.db import get_db_connection
    last_err = None
    for _attempt in range(1, max_retries + 1):
        try:
            with get_db_connection() as db:
                cur = db.cursor()
                cur.execute(
                    f"DELETE FROM {_SIGNALS_TABLE} WHERE trade_date = %s AND state = %s",
                    (trade_date, S_WATCH_PENDING),
                )
                purged = cur.rowcount
                if purge_buy_today:
                    cur.execute(
                        f"DELETE FROM {_SIGNALS_TABLE} "
                        f"WHERE trade_date = %s AND state = %s AND strategy = ANY(%s)",
                        (trade_date, S_BUY_TODAY, list(purge_buy_today)),
                    )
                    purged += cur.rowcount
                n = 0
                for s in rows:
                    # 方案A (2026-09-18): extra 已由 signal_row 整包构造, 直接取 (剔除 None)
                    extra = {k: v for k, v in (s.get("extra") or {}).items()
                             if v is not None}
                    state = s.get("state") or S_WATCH_PENDING
                    cur.execute(f"""
                        INSERT INTO {_SIGNALS_TABLE}
                            (trade_date, strategy, code, name, board, entry_style, score, state,
                             signal_date, signal_price, lu_date, pullback_days, extra,
                             entry_date, entry_price, stop_price, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                %s, %s, %s, NOW())
                        ON CONFLICT (trade_date, strategy, code, entry_style) DO UPDATE SET
                            name = EXCLUDED.name, score = EXCLUDED.score, state = EXCLUDED.state,
                            signal_date = EXCLUDED.signal_date, signal_price = EXCLUDED.signal_price,
                            lu_date = EXCLUDED.lu_date, pullback_days = EXCLUDED.pullback_days,
                            extra = EXCLUDED.extra, updated_at = NOW()
                    """, (
                        trade_date, s.get("strategy", DRAGON_STRATEGY), s["code"], s.get("name", ""), s.get("board", ""),
                        s.get("style", "a"), int(s.get("score", 0)), state,
                        s.get("signal_date"), s.get("signal_price"),
                        s.get("lu_date"), s.get("pullback_days"),
                        json.dumps(extra, ensure_ascii=False, default=str),
                        s.get("entry_date"), s.get("entry_price"), s.get("stop_price"),
                    ))
                    n += 1
                db.commit()
                cur.close()
            return {"written": n, "purged": purged}
        except Exception as _e:
            _pg = getattr(_e, "pgcode", None)
            _transient = _pg in ("40P01", "40001")
            last_err = _e
            if _transient and _attempt < max_retries:
                logger.warning(
                    "[upsert_scan_signals] 瞬态冲突(pgcode=%s) 第%d/%d次重试 trade_date=%s: %s",
                    _pg, _attempt, max_retries, trade_date, _e)
                time.sleep(0.2 * _attempt)
                continue
            logger.error("[upsert_scan_signals] 失败(attempt %d): %s", _attempt, _e)
            raise
    raise last_err


def set_state(sig_id, state, detail=None, confirm_date=None, d1_chg=None, d1_vol_r=None,
              entry_date=None, entry_price=None, exit_reason=None, exit_date=None, exit_price=None):
    """状态转移 (单条)。"""
    from app.utils.db import get_db_connection
    with get_db_connection() as db:
        cur = db.cursor()
        _set_state(cur, sig_id, state, detail=detail, confirm_date=confirm_date,
                   d1_chg=d1_chg, d1_vol_r=d1_vol_r, entry_date=entry_date,
                   entry_price=entry_price, exit_reason=exit_reason,
                   exit_date=exit_date, exit_price=exit_price)
        db.commit()
        cur.close()


def purge_stale_detail(keys, keep_state, keep_entry_date):
    """清除 extra 中过期的"瞬时标记"键, 保留 state=keep_state 且 entry_date=keep_entry_date 的行。

    背景: extra 的写入是增量合并 (`extra = extra || %s`, 见 _set_state), **只能加不能减**。
    像 pre_confirm/pre_ts 这类只在"当日买入窗口"有意义的标记 —— 设计口径见
    docs/龙回头自动化设计方案.md:92/154 (14:25 加"预"角标 → 15:00 正式确认覆盖) ——
    超过窗口若不显式删除, 标记会永久残留: 显示层会把它当"当前预判", 持仓行被渲染成"预持"。

    幂等: 时机/次数无关, 已清理过的行不再匹配; 亦可用于自愈历史脏数据。
    返回受影响行数。
    """
    if not keys:
        return 0
    from app.utils.db import get_db_connection
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            f"UPDATE {_SIGNALS_TABLE} SET extra = extra - %s::text[] "
            "WHERE jsonb_exists_any(extra, %s::text[]) "
            "AND (state IS DISTINCT FROM %s OR entry_date IS DISTINCT FROM %s::date)",
            (list(keys), list(keys), keep_state, keep_entry_date))
        n = cur.rowcount
        db.commit()
        cur.close()
    return n


def update_stop_price(sig_id, stop_price):
    """补记止损价 (buy_today 时按 board 规则计算)。"""
    from app.utils.db import get_db_connection
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(f"UPDATE {_SIGNALS_TABLE} SET stop_price = %s, updated_at = NOW() WHERE id = %s",
                    (stop_price, sig_id))
        db.commit()
        cur.close()


def _set_state(cur, sig_id, state, detail=None, confirm_date=None, d1_chg=None, d1_vol_r=None,
               entry_date=None, entry_price=None, exit_reason=None, exit_date=None, exit_price=None):
    sets = ["state = %s", "updated_at = NOW()"]
    vals = [state]
    for col, v in (("confirm_date", confirm_date), ("d1_chg", d1_chg), ("d1_vol_r", d1_vol_r),
                   ("entry_date", entry_date), ("entry_price", entry_price),
                   ("exit_reason", exit_reason), ("exit_date", exit_date), ("exit_price", exit_price)):
        if v is not None:
            sets.append(f"{col} = %s")
            vals.append(v)
    if detail is not None:
        sets.append("extra = extra || %s")
        vals.append(json.dumps(detail, ensure_ascii=False, default=str))
    vals.append(sig_id)
    cur.execute(f"UPDATE {_SIGNALS_TABLE} SET {', '.join(sets)} WHERE id = %s", vals)


def list_signals(states=None, trade_date=None, days=20, only_active=False,
                 strategies=None, enabled_only=False):
    """查询信号 (signals 表)。states: 状态过滤; trade_date: 指定信号日; days: 最近N日。

    Args:
        enabled_only: True=只显示 enabled=true 策略的行 (展示层用)。
            ★ 但**停用策略的已入场行 (entry_date 非空) 仍保留可见** —— 否则用户
            会遗忘手上还有票要卖, 是实盘资金事故 (见 startup.py 模块 docstring 硬约束)。
            即: 停用 = 不再提示新买入, 但不隐藏已有持仓/卖出提示。
            ⚠ monitor 推进状态机**不能**带此过滤 (它要接着推进已入场行), 故默认 False。

    Returns:
        list[dict]: 信号行（含 trade_date/strategy/code/name/state/score 及 entry/exit 系列字段）。
    """
    from app.utils.db import get_db_connection
    with get_db_connection() as db:
        cur = db.cursor()
        sql = f"SELECT * FROM {_SIGNALS_TABLE} WHERE strategy = ANY(%s)"
        vals = [list(strategies or strategy_keys())]
        if states:
            sql += " AND state = ANY(%s)"
            vals.append(list(states))
        if trade_date:
            sql += " AND trade_date = %s"
            vals.append(trade_date)
        elif days:
            sql += " AND trade_date >= (CURRENT_DATE - %s::int)"
            vals.append(days)
        if only_active:
            sql += " AND state = ANY(%s)"
            vals.append(list(ACTIVE_GROUP_STATES))
        if enabled_only:
            sql += " AND (strategy = ANY(%s) OR entry_date IS NOT NULL)"
            vals.append(list(enabled_keys()))
        sql += " ORDER BY trade_date DESC, score DESC"
        cur.execute(sql, vals)
        rows = [_row_to_dict(r) for r in cur.fetchall()]
        cur.close()
    return rows


def get_active_signals():
    """组内活跃信号 (买入/持仓/卖出)。

    Returns:
        list[dict]: 同 list_signals；仅 买入/持仓/卖出 活跃状态、最近 30 日。
    """
    return list_signals(states=ACTIVE_GROUP_STATES, days=30, enabled_only=True)


def get_watch_pending(trade_date=None, days=5):
    """观察池 (watch_pending) —— 只含 enabled=true 策略。

    观察池是"待买入候选", 停用策略不该再提名新股; 其未入场行也不显示。

    Returns:
        list[dict]: 同 list_signals；仅观察池(watch_pending)状态。
    """
    return list_signals(states=(S_WATCH_PENDING,), trade_date=trade_date, days=days,
                        enabled_only=True)


def get_signal_by_code(code, trade_date=None):
    """取某票当前活跃信号 (买入/持仓/卖出) 最新一条。

    Returns:
        dict | None: 该股最新一条活跃信号行；无则 None。
    """
    rows = list_signals(states=ACTIVE_GROUP_STATES, trade_date=trade_date, days=30)
    for r in rows:
        if r["code"] == code:
            return r
    return None


def get_markers(code, days=60):
    """买卖点标记 (K线图 overlay 用): 信号点/买点/卖点。

    Returns:
        list[dict]: [{time, side, price, label}]；side ∈ signal/buy/sell。
    """
    from app.utils.db import get_db_connection
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(f"""
            SELECT trade_date, strategy, code, name, entry_style, score, state,
                   signal_date, signal_price, entry_date, entry_price,
                   exit_date, exit_price, exit_reason, confirm_date, d1_chg, d1_vol_r
            FROM {_SIGNALS_TABLE}
            WHERE strategy = ANY(%s) AND code = %s AND trade_date >= (CURRENT_DATE - %s::int)
            ORDER BY trade_date
        """, (list(strategy_keys()), code, days))
        rows = [_row_to_dict(r) for r in cur.fetchall()]
        cur.close()

    markers = []
    _labels = strategy_labels()
    for r in rows:
        sname = _labels.get(r.get("strategy"), r.get("strategy", ""))
        if r.get("signal_date") and r.get("signal_price"):
            markers.append({"time": r["signal_date"], "side": "signal",
                            "price": float(r["signal_price"]),
                            "label": f"{sname}信号({r['entry_style']},score{r['score']})"})
        if r.get("entry_date") and r.get("entry_price") and \
                r["state"] in (S_BUY_TODAY, S_HOLDING, S_EXIT_TODAY, S_CLOSED):
            markers.append({"time": r["entry_date"], "side": "buy",
                            "price": float(r["entry_price"]), "label": f"买入·{sname}"})
        if r.get("exit_date") and r.get("exit_price") and \
                r["state"] in (S_EXIT_TODAY, S_CLOSED):
            markers.append({"time": r["exit_date"], "side": "sell",
                            "price": float(r["exit_price"]),
                            "label": f"卖出·{sname}({r.get('exit_reason') or ''})"})
    return markers


# ================================================================
# qd_watchlist 策略组投影同步
# ================================================================

def _display_detail(s):
    """signals 行 → qd_watchlist.strategy_detail (前端 popover 表格明细)。v 字段用于变更检测。

    注意: 不渲染 entry_style —— 它只是 qd_dragon_signals 的唯一键成分与 K 线 marker 文案来源
    (见 signals_markers), 前端 popover 已于 §9.3 缩减中删除"形态"行。
    """
    strat = s.get("strategy") or DRAGON_STRATEGY
    return {
        "v": f"{s['state']}|{s.get('entry_price')}|{s.get('exit_reason') or ''}|{s.get('score')}",
        "strategy": strat,
        "strategy_label": strategy_labels().get(strat, strat),
        "winrate": strategy_winrate(strat),
        "state_label": state_label(s["state"]),
        "score": s.get("score"),
        "lu_date": s.get("lu_date"),
        "pullback_days": s.get("pullback_days"),
        "signal_date": s.get("signal_date"),
        "signal_price": _f(s.get("signal_price")),
        "entry_date": s.get("entry_date"),
        "entry_price": _f(s.get("entry_price")),
        "stop_price": _f(s.get("stop_price")),
        "confirm_date": s.get("confirm_date"),
        "d1_chg": _f(s.get("d1_chg")),
        "d1_vol_r": _f(s.get("d1_vol_r")),
        "pre_confirm": (s.get("extra") or {}).get("pre_confirm"),
        "turnover_anchor": _f((s.get("extra") or {}).get("turnover_anchor")),
        "turnover_sig": _f((s.get("extra") or {}).get("turnover_sig")),
        "turnover_anchor_total": _f((s.get("extra") or {}).get("turnover_anchor_total")),
        "float_mcap_yi": _f((s.get("extra") or {}).get("float_mcap_yi")),
        "ma60_slope": _f((s.get("extra") or {}).get("ma60_slope")),
        "ma_bull": (s.get("extra") or {}).get("ma_bull"),
        "entry_gate": (s.get("extra") or {}).get("entry_gate"),
        "entry_pctb": _f((s.get("extra") or {}).get("entry_pctb")),
        "entry_bd": _f((s.get("extra") or {}).get("entry_bd")),
        "board_height": (s.get("extra") or {}).get("board_height"),
        "lu_vol_ratio": _f((s.get("extra") or {}).get("lu_vol_ratio")),
        "rsi": _f((s.get("extra") or {}).get("rsi")),
        "exit_reason": s.get("exit_reason") or "",
        "exit_date": s.get("exit_date"),
        "exit_price": _f(s.get("exit_price")),
    }


def _f(v):
    try:
        return round(float(v), 3) if v is not None else None
    except (TypeError, ValueError):
        return None


def sync_watchlist_group(active_rows):
    """活跃信号 → qd_watchlist '自动策略组' 全量对账 (引擎独占读写删)。

    active_rows: signals 行列表 (state ∈ ACTIVE_GROUP_STATES)。
    每轮调用: 缺失→INSERT / 状态变→UPDATE / 多余→DELETE。幂等。
    """
    from app.utils.db import get_db_connection
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            "SELECT id, symbol, strategy_state, strategy_detail FROM qd_watchlist "
            "WHERE user_id = %s AND market = %s AND group_name = %s",
            (DRAGON_USER_ID, DRAGON_MARKET, DRAGON_GROUP_NAME),
        )
        current = {}
        for r in cur.fetchall():
            d = dict(r)
            cur_detail = d.get("strategy_detail")
            if isinstance(cur_detail, str):
                try:
                    cur_detail = json.loads(cur_detail)
                except Exception:
                    cur_detail = {}
            current[d["symbol"]] = {"id": d["id"], "state": d.get("strategy_state"),
                                    "detail": cur_detail or {}}

        target = {s["code"]: s for s in active_rows}

        inserted = updated = deleted = 0

        # ── UPSERT 目标集 ──
        for code, s in target.items():
            detail = _display_detail(s)
            if code in current:
                row = current[code]
                if row["state"] != s["state"] or (row["detail"] or {}).get("v") != detail.get("v"):
                    cur.execute(
                        "UPDATE qd_watchlist SET strategy_state = %s, strategy_detail = %s, "
                        "name = %s, updated_at = NOW() "
                        "WHERE user_id = %s AND market = %s AND symbol = %s AND group_name = %s",
                        (s["state"], json.dumps(detail, ensure_ascii=False, default=str),
                         s.get("name") or code, DRAGON_USER_ID, DRAGON_MARKET, code,
                         DRAGON_GROUP_NAME),
                    )
                    updated += 1
            else:
                cur.execute(
                    "INSERT INTO qd_watchlist "
                    "(user_id, market, symbol, name, group_name, strategy_state, strategy_detail, "
                    " created_at, updated_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, NOW(), NOW()) "
                    "ON CONFLICT (user_id, market, symbol, group_name) DO UPDATE SET "
                    "strategy_state = EXCLUDED.strategy_state, "
                    "strategy_detail = EXCLUDED.strategy_detail, name = EXCLUDED.name, "
                    "updated_at = NOW()",
                    (DRAGON_USER_ID, DRAGON_MARKET, code, s.get("name") or code,
                     DRAGON_GROUP_NAME, s["state"],
                     json.dumps(detail, ensure_ascii=False, default=str)),
                )
                inserted += 1

        # ── DELETE 组内多余 (已失效/已平仓/已执行卖出) ──
        for code, row in current.items():
            if code not in target:
                cur.execute("DELETE FROM qd_watchlist WHERE id = %s", (row["id"],))
                deleted += 1

        db.commit()
        cur.close()
    logger.info("[dragon_store] 组同步: 目标%d 插入%d 更新%d 删除%d",
                len(target), inserted, updated, deleted)

    # ── 上级答案 → label 唯一写接口 submit() (grade=3, auto) ──
    # 这是**唯一允许的跨层边** (auto → submit, 写)。label 侧零反向依赖:
    # auto 不得回读 label 的数据作策略输入 (方案 §5.5 禁令 4)。
    # 失败只记日志: 标签是展示层, 不得影响组对账与信号链。
    submitted = 0
    try:
        submitted = _submit_labels_to_label_layer(active_rows)
    except Exception:
        logger.error("[dragon_store] label 提交失败 (不影响组同步)")
        import traceback as _tb
        logger.error(_tb.format_exc())

    return {"target": len(target), "inserted": inserted, "updated": updated,
            "deleted": deleted, "label_submitted": submitted}


# ================================================================
# 上级答案 → label 扩展段 (auto 侧唯一调用点)
# ================================================================

#: auto 自己的答案有效期 (交易日)。**由提交方决定** —— label 侧不加默认、不设上限 (方案 §3.3)。
#: 2 个交易日: 信号状态每交易日刷新, 停更 2 日即认为上级链路异常, 由 system 接管。
LABEL_TTL_TRADING_DAYS = 2

#: 策略明细表列清单 (方案 §9.3.5)。**"状态"刻意不做列**: 每票只有一行, 状态已由行上
#: 竖排 tag 承载; 列内无法表达"预判", 会与 tag 星级形成两个信息源。
LABEL_TABLE_COLUMNS = (
    ("strategy", "策略"),
    ("winrate", "历史胜率"),
    ("score", "评分"),
    ("anchor", "锚点日"),
    ("turnover", "换手(锚)"),
    ("mcap", "流通市值"),
    ("ma60", "MA60斜率"),
    ("entry", "买入"),
    ("stop", "止损"),
    ("d1", "D1确认"),
    ("exit", "出场"),
)


def _label_row(s) -> dict:
    """signals 行 → 策略明细表的一行（列口径见 LABEL_TABLE_COLUMNS）。"""
    d = _display_detail(s)
    lu = d.get("lu_date")
    anchor = ""
    if lu:
        anchor = f"{lu}{(' 回调%d天' % d['pullback_days']) if d.get('pullback_days') else ''}"
    turnover = ""
    if d.get("turnover_anchor") is not None:
        turnover = f"{d['turnover_anchor']}%" + (
            f" / 信{d['turnover_sig']}%" if d.get("turnover_sig") is not None else "")
    ma60 = ""
    if d.get("ma60_slope") is not None:
        ma60 = f"{d['ma60_slope']}%" + (" 多头排列" if d.get("ma_bull") else "")
    entry = ""
    if d.get("entry_date"):
        entry = f"{d['entry_date']} @ {d.get('entry_price', '')}"
    d1 = ""
    if d.get("d1_chg") is not None:
        d1 = f"{'+' if d['d1_chg'] > 0 else ''}{d['d1_chg']}%" + (
            f" 量比{d['d1_vol_r']}" if d.get("d1_vol_r") is not None else "")
    exit_txt = ""
    if d.get("exit_reason"):
        exit_txt = str(d["exit_reason"]) + (
            f" ({d['exit_date']} @ {d.get('exit_price', '')})" if d.get("exit_date") else "")
    return {
        "strategy": d.get("strategy_label") or d.get("strategy") or "",
        "winrate": d.get("winrate"),
        "score": d.get("score"),
        "anchor": anchor,
        "turnover": turnover,
        "mcap": f"{d['float_mcap_yi']}亿" if d.get("float_mcap_yi") is not None else "",
        "ma60": ma60,
        "entry": entry,
        "stop": d.get("stop_price"),
        "d1": d1,
        "exit": exit_txt,
    }


def _label_payload(s) -> dict:
    """构造 4 段 payload：评分 + 扩展段(策略明细表) + 评分口径说明。

    supports/resistances 留空 —— auto 的答案不含筹码关键位; 读路径有**段级回填**，
    空段不会抹掉 system 已有的支撑位/压力位答案。
    """
    d = _display_detail(s)
    state_rows = [{"label": "状态",
                   "value": d.get("state_label") or s.get("state") or ""}]
    pre = d.get("pre_confirm")
    if pre:                                  # 无预判 ⇒ **不产出该行**（"无"是展示文案, 不该由数据层造）
        state_rows.append({"label": "预判", "value": pre})
    return {
        "score": d.get("score"),
        "score_version": None,          # auto 的评分口径归 auto, 本批未定义 ⇒ 不声明
        "supports": [],
        "resistances": [],
        "extras": [
            {"type": "table", "title": "策略明细",
             "columns": [{"key": k, "label": v} for k, v in LABEL_TABLE_COLUMNS],
             "rows": [_label_row(s)]},
            {"type": "fields", "title": "策略状态", "rows": state_rows},
        ],
    }


def _submit_labels_to_label_layer(active_rows) -> int:
    """把活跃信号的上级答案经 label 唯一写接口落库 (grade=3)。"""
    from app.watchlist import submit
    n = 0
    for s in active_rows:
        code = s.get("code")
        if not code:
            continue
        try:
            submit("auto", DRAGON_MARKET, str(code), _label_payload(s),
                   ttl_days=LABEL_TTL_TRADING_DAYS)
            n += 1
        except Exception as e:
            logger.warning("[dragon_store] label 提交失败 %s: %s", code, e)
    if n:
        logger.info("[dragon_store] label 提交(auto/grade3): %d 条 (ttl=%d 交易日)",
                    n, LABEL_TTL_TRADING_DAYS)
    return n


def cleanup_cutoff(days=15):
    """cleanup_old 的物理删除边界 (YYYY-MM-DD)。

    ⚠ 是**日历日**且带 1.6 放大系数 (交易日→日历日): days=15 ⇒ 边界为 24 个日历日前。
    rebuild 用它区分「真漏发」(边界内该有却没有) 与「已被清理」(边界外, 补了也会被再删)。
    单一事实源在此, 禁止各处重写公式。
    """
    return (datetime.now() - timedelta(days=int(days * 1.6))).strftime("%Y-%m-%d")


def cleanup_old(days=15):
    """历史清理: signals 表保留最近约 N 个交易日 (holding 保留至自然终态)。三策略统一清理。"""
    from app.utils.db import get_db_connection
    cutoff = cleanup_cutoff(days)
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            f"DELETE FROM {_SIGNALS_TABLE} WHERE strategy = ANY(%s) AND trade_date < %s "
            "AND state = ANY(%s)",
            (list(strategy_keys()), cutoff, [S_WATCH_PENDING, S_BUY_TODAY, S_EXIT_TODAY, S_CLOSED, S_EXPIRED]),
        )
        n = cur.rowcount
        db.commit()
        cur.close()
    return {"deleted": n}
