"""dragon_store.py - 龙回头Pro 存储层

职责:
  1. qd_dragon_signals 事实表 (状态机全量+历史) 的建表与 CRUD
  2. qd_watchlist 迁移 (strategy_state/strategy_detail 列 + UNIQUE 约束放宽)
  3. sync_watchlist_group(): 活跃信号 → qd_watchlist '龙回头Pro' 组的全量对账
     (引擎独占读写删, 失效票删行, 历史留在 signals 表)

设计要点:
  - signals 表是唯一事实源; qd_watchlist 策略组行只是活跃信号的"投影"
  - 全部幂等: 重复执行不产生脏数据
  - 单用户部署: 写 user_id=1 (DRAGON_USER_ID), 所有用户可见同一策略组
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from app.utils.logger import get_logger

logger = get_logger(__name__)

# 组名与用户 (固定名: 自动策略组, 三策略共用: 龙回头Pro/V1/断板)
DRAGON_GROUP_NAME = "自动策略组"
DRAGON_USER_ID = 1
DRAGON_MARKET = "CNStock"
DRAGON_STRATEGY = "dragon2"
STRATEGIES = ("dragon2", "v1", "break", "relay3")
STRATEGY_LABELS = {"dragon2": "龙回头Pro", "v1": "V1", "break": "断板", "relay3": "3板接力"}
# 历史回测胜率 (300日全市场): 策略组排序用; relay3 = 3板+MA多头 长窗口回测 (2026-09-06)
STRATEGY_WINRATE = {"v1": 76.5, "break": 62.7, "dragon2": 70.4, "relay3": 53.4}

# 状态机 (signals.state)
S_WATCH_PENDING = "watch_pending"    # D0信号成立, 待D1确认 (默认不入组)
S_BUY_TODAY = "buy_today"            # D1 9:26 gap判定通过, 今日开盘买入 (label 买入·深绿)
S_HOLDING = "holding"                # 已买入持有中 (15:00强/中确认后; label 持仓·蓝)
S_EXIT_TODAY = "exit_today"          # 触发出场 (label 卖出·红; 次日开盘执行)
S_CLOSED = "closed"                  # 已平仓 (组内删行, 留历史)
S_EXPIRED = "expired"                # 失效: 弱确认/开盘gap放弃 (组内删行, 留历史)

# 同步进 qd_watchlist 策略组的状态 (观察票入组: 灰色"观察中"置底展示, 09-04 用户要求提前可见)
ACTIVE_GROUP_STATES = (S_WATCH_PENDING, S_BUY_TODAY, S_HOLDING, S_EXIT_TODAY)
# 每策略每日买入名额 (09-04 用户要求: 每策略每天≈5笔, 质量排名末位淘汰; relay3 信号稀少 n≈0.7/日, 名额2)
DAILY_LIMIT_PER_STRATEGY = {"dragon2": 5, "v1": 5, "break": 5, "relay3": 2}
# label 文案 (前端映射兜底, 前端也有映射)
STATE_LABELS = {S_WATCH_PENDING: "观察中", S_BUY_TODAY: "买入", S_HOLDING: "持仓",
                S_EXIT_TODAY: "卖出", S_CLOSED: "已平仓", S_EXPIRED: "已失效"}

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
        # 组名统一为 自动策略组 (旧名 龙回头Pro 迁移)
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


def upsert_scan_signals(trade_date: str, rows: list):
    """盘后扫描结果写入 (幂等): rows 为各策略 (dragon2/v1/break) 的今日信号列表, 行内带 strategy 键。

    扫描是 watch_pending 状态的权威来源: 先清空该 trade_date 的旧 watch_pending
    (防止参数/数据变化后残留幽灵信号), 再插入本轮结果。
    """
    from app.utils.db import get_db_connection
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            f"DELETE FROM {_SIGNALS_TABLE} WHERE trade_date = %s AND state = %s",
            (trade_date, S_WATCH_PENDING),
        )
        purged = cur.rowcount
        n = 0
        for s in rows:
            extra = {k: s.get(k) for k in
                     ("turnover_anchor", "turnover_sig", "turnover_anchor_total", "turnover_sig_total",
                      "float_mcap_yi", "ma60_slope",
                      "ma_bull", "support_ma", "support_anchor_open", "pullback_drawdown",
                      "anchor_type", "anchor_vol_r", "sig_vol", "ret_20d", "d_1_change",
                      "streak_len", "break_chg", "break_gap", "break_vol_r", "confirm_chg",
                      "pre20_gain", "board_height", "lu_vol_ratio", "rsi") if s.get(k) is not None}
            cur.execute(f"""
                INSERT INTO {_SIGNALS_TABLE}
                    (trade_date, strategy, code, name, board, entry_style, score, state,
                     signal_date, signal_price, lu_date, pullback_days, extra, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (trade_date, strategy, code, entry_style) DO UPDATE SET
                    name = EXCLUDED.name, score = EXCLUDED.score, state = EXCLUDED.state,
                    signal_date = EXCLUDED.signal_date, signal_price = EXCLUDED.signal_price,
                    lu_date = EXCLUDED.lu_date, pullback_days = EXCLUDED.pullback_days,
                    extra = EXCLUDED.extra, updated_at = NOW()
            """, (
                trade_date, s.get("strategy", DRAGON_STRATEGY), s["code"], s.get("name", ""), s.get("board", ""),
                s.get("style", "a"), int(s.get("score", 0)), S_WATCH_PENDING,
                s.get("signal_date"), s.get("signal_price"),
                s.get("lu_date"), s.get("pullback_days"),
                json.dumps(extra, ensure_ascii=False, default=str),
            ))
            n += 1
        db.commit()
        cur.close()
    return {"written": n, "purged": purged}


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


def list_signals(states=None, trade_date=None, days=20, only_active=False, strategies=None):
    """查询信号 (signals 表)。states: 状态过滤; trade_date: 指定信号日; days: 最近N日。"""
    from app.utils.db import get_db_connection
    with get_db_connection() as db:
        cur = db.cursor()
        sql = f"SELECT * FROM {_SIGNALS_TABLE} WHERE strategy = ANY(%s)"
        vals = [list(strategies or STRATEGIES)]
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
        sql += " ORDER BY trade_date DESC, score DESC"
        cur.execute(sql, vals)
        rows = [_row_to_dict(r) for r in cur.fetchall()]
        cur.close()
    return rows


def get_active_signals():
    """组内活跃信号 (买入/持仓/卖出)。"""
    return list_signals(states=ACTIVE_GROUP_STATES, days=30)


def get_watch_pending(trade_date=None, days=5):
    """观察池 (watch_pending)。"""
    return list_signals(states=(S_WATCH_PENDING,), trade_date=trade_date, days=days)


def get_signal_by_code(code, trade_date=None):
    """取某票当前活跃信号 (买入/持仓/卖出) 最新一条。"""
    rows = list_signals(states=ACTIVE_GROUP_STATES, trade_date=trade_date, days=30)
    for r in rows:
        if r["code"] == code:
            return r
    return None


def get_markers(code, days=60):
    """买卖点标记 (K线图 overlay 用): 信号点/买点/卖点。"""
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
        """, (list(STRATEGIES), code, days))
        rows = [_row_to_dict(r) for r in cur.fetchall()]
        cur.close()

    markers = []
    for r in rows:
        sname = STRATEGY_LABELS.get(r.get("strategy"), r.get("strategy", ""))
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
    """signals 行 → qd_watchlist.strategy_detail (前端 popover 表格明细)。v 字段用于变更检测。"""
    strat = s.get("strategy") or "dragon2"
    return {
        "v": f"{s['state']}|{s.get('entry_price')}|{s.get('exit_reason') or ''}|{s.get('score')}",
        "strategy": strat,
        "strategy_label": STRATEGY_LABELS.get(strat, strat),
        "winrate": STRATEGY_WINRATE.get(strat),
        "state_label": STATE_LABELS.get(s["state"], s["state"]),
        "entry_style": "(a)缩量企稳" if s.get("entry_style") == "a" else ("(b)放量启动" if s.get("entry_style") == "b" else (s.get("entry_style") or "")),
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
    """活跃信号 → qd_watchlist '龙回头Pro' 组全量对账 (引擎独占读写删)。

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
    return {"target": len(target), "inserted": inserted, "updated": updated, "deleted": deleted}


def cleanup_old(days=15):
    """历史清理: signals 表保留最近约 N 个交易日 (holding 保留至自然终态)。三策略统一清理。"""
    from app.utils.db import get_db_connection
    cutoff = (datetime.now() - timedelta(days=int(days * 1.6))).strftime("%Y-%m-%d")
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            f"DELETE FROM {_SIGNALS_TABLE} WHERE strategy = ANY(%s) AND trade_date < %s "
            "AND state = ANY(%s)",
            (list(STRATEGIES), cutoff, [S_WATCH_PENDING, S_BUY_TODAY, S_EXIT_TODAY, S_CLOSED, S_EXPIRED]),
        )
        n = cur.rowcount
        db.commit()
        cur.close()
    return {"deleted": n}
