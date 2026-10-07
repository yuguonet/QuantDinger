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

import hashlib
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
# 日期 / 窗口 的单一事实源 (A7, 2026-10-05)
# ================================================================
# ★★ 本项目存在两套管"今天"的时钟, 此前**没有任何一处统合**:
#     ① Python 侧 `datetime.now()` —— 进程本地时区 (北京时间)
#     ② SQL 侧 `CURRENT_DATE`     —— DB 会话时区 (`db_postgres.py:144` 设
#        `options="-c timezone=UTC"`) ⇒ 北京 00:00~07:59 拿到的是**前一天**
#    二者混用的直接后果 (`list_signals` 的日期窗口 vs `monitor._today()`):
#      · 北京 00:00~08:00 之间, SQL 窗口比 Python 侧多给一天 ⇒ 边界行的
#        进出窗口时点不一致, 且随时间漂移 —— 属"看日历才复现"的幽灵 bug。
#      · `updated_at` 由 `NOW()` 写入 ⇒ 存的是 **UTC 挂钟** (实测 id=402 的
#        `updated_at=2026-09-30 07:01` 对应北京时间 15:01)。任何 `str(...)[:10]`
#        把它当本地日期用的地方都在受害。
#
#     统一口径: **所有 SQL 日期窗口都接收 Python 传入的本地日期**, 不再依赖
#     `CURRENT_DATE`。改动点只有下面一个函数 + 两处 SQL, 不触碰其它模块的读法。

def today_str() -> str:
    """进程本地"今天" (YYYY-MM-DD) —— **SQL 日期窗口的唯一来源**。

    与 `monitor._today()` 同源 (monitor 直接委托本函数), 保证「monitor 判 today」
    与「store 按 today 开窗」拿到同一个日期。
    """
    return datetime.now().strftime("%Y-%m-%d")


#: monitor 每 tick 拉取的候选集回看窗口 (日历日)。
#: 必须与 `get_active_signals` 的可见窗口 **同口径** —— 否则展示层看得到、
#: monitor 却扫不到的行会静默滞留 (A5, 2026-10-05)。
#: 取值的硬下界 = `cleanup_old` 的清理边界 24 日历日 (`cleanup_cutoff(15)`,
#: days×1.6) ⇒ 30 天然覆盖它: 任何还没被物理删除的行都逃不出这个窗口。
VISIBLE_WINDOW_DAYS = 30


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
            SELECT 1 FROM pg_constraint
            WHERE conname = 'qd_dragon_signals_ukey'
              AND conrelid = 'qd_dragon_signals'::regclass
        """)
        if not cur.fetchone():
            cur.execute(f"""
                ALTER TABLE {_SIGNALS_TABLE}
                ADD CONSTRAINT qd_dragon_signals_ukey UNIQUE (trade_date, strategy, code, entry_style)
            """)
        # 2026-09-29 审计修复: 历史 CREATE TABLE 内联 UNIQUE 与命名 ukey 同列组重复
        # (每个新部署多一条重复约束+索引)。保留命名 ukey, 其余同列组 UNIQUE 清除。
        cur.execute("""
            SELECT conname FROM pg_constraint
            WHERE conrelid = 'qd_dragon_signals'::regclass AND contype = 'u'
              AND conname <> 'qd_dragon_signals_ukey'
              AND pg_get_constraintdef(oid) ILIKE '%trade_date, strategy, code, entry_style%'
        """)
        for r in cur.fetchall():
            dup = r["conname"] if isinstance(r, dict) else r[0]
            cur.execute(f"ALTER TABLE {_SIGNALS_TABLE} DROP CONSTRAINT {dup}")

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
      style (落库列 entry_style) = 策略类属性 entry_style (dragon=a/v1=v1/break=brk/relay3=r3)
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


def upsert_scan_signals(trade_date: str, rows: list, purge_buy_today: tuple = (), max_retries: int = 5,
                        strategies: tuple = ()):
    """扫描结果写入 (幂等): rows 为各策略今日信号列表, 行内带 strategy 键。

    扫描是 watch_pending 状态的权威来源: 先清该 trade_date 的旧 watch_pending
    (防止参数/数据变化后残留幽灵信号), 再插入本轮结果。
    行内可选 state/entry_date/entry_price/stop_price 覆盖默认值
    (knife_catch 等盘中即买策略: state=buy_today, 14:56 已入场)。
    purge_buy_today: 额外清理这些策略今日 state=buy_today 的旧行
      (tail_oversold 滚动预览/终审专用: 14:50~14:56 每分钟重判, 上一轮命中本轮落选的
       股票须删行, 否则残留误导用户; 仅清 buy_today 态, 不碰已转移的 holding 等)。
    strategies: 本批扫描的策略范围 (修 M12)。前置 DELETE 只清这些策略的
      watch_pending——不带范围时会把同 trade_date 其它策略的盘后信号整批删掉
      且不补回 (盘后扫描先写、之后任何 _scan_cycle 预览轮都会丢信号)。空 tuple
      时从 rows 推断; rows 也为空 → DELETE 0 行 (安全方向: 宁多留不误删)。

    Returns:
        {"written": 本次写入行数, "purged": 清理行数,
         "superseded": **跨日被取代的旧观察票行数** (A11, 2026-10-05)}

    ⚠️ 2026-09-28 修 A1 (资金事故红线): ON CONFLICT 的 state/extra 加 CASE 守卫
    ——monitor 已推进的行 (entry_date 非空: buy_today/holding/exit) 不得被补扫
    的新信号行回滚 state 或冲掉 extra (t_legs_today/pre_confirm 等运行时字段)。
    D+1 白天重启后端自动补扫 (_target_date=上一交易日) 最易触发: 行被回滚
    watch_pending 后过窗无人推进 → 次日 stale 扫成 expired「隔日未处理」→
    已买入持仓不再提示卖出。

    瞬态冲突重试 (2026-09-18 事故修复②): 并发 DELETE+INSERT (调度重启补跑触发
    deadlock_detected / 序列化失败) 会整事务回滚丢信号 — 此处捕获 40P01/40001 后
    退避重试, 保证最终写入 (操作幂等, 重试安全)。
    """
    from app.utils.db import get_db_connection
    # M12: DELETE 策略范围 (显式传入优先, 否则从 rows 推断; 都空 → 删 0 行)
    scope = sorted({str(s) for s in strategies if s}) or \
        sorted({str(r.get("strategy") or DRAGON_STRATEGY) for r in rows})
    last_err = None
    for _attempt in range(1, max_retries + 1):
        try:
            with get_db_connection() as db:
                cur = db.cursor()
                if scope:
                    cur.execute(
                        f"DELETE FROM {_SIGNALS_TABLE} WHERE trade_date = %s AND state = %s "
                        f"AND strategy = ANY(%s)",
                        (trade_date, S_WATCH_PENDING, scope),
                    )
                    purged = cur.rowcount
                else:
                    purged = 0   # 2026-09-29 审计修复: scope 空时 cur.rowcount 取未执行游标的未定义值
                if purge_buy_today:
                    # 2026-09-26: 只清「预览未定价」行; 已写 entry_price 的 buy_today
                    # = 已在盘中某分钟成交 (14:50 起滚动买入), **不得**被后续轮次冲掉。
                    cur.execute(
                        f"DELETE FROM {_SIGNALS_TABLE} "
                        f"WHERE trade_date = %s AND state = %s AND strategy = ANY(%s) "
                        f"AND entry_price IS NULL",
                        (trade_date, S_BUY_TODAY, list(purge_buy_today)),
                    )
                    purged += cur.rowcount

                # ── A11 (2026-10-05): 跨日幽灵观察票 —— 旧提名被新提名取代 ──
                # 上面的 DELETE 只清 `trade_date = 本次` 的行, 而唯一键也含 trade_date
                # ⇒ 同一 (strategy, code, entry_style) 在**更早交易日**的 watch_pending
                #   行会原样留下。补扫 target_date ≠ 原 trade_date 时 (D+1 白天重启补
                #   跑最常见) 就变成同时挂着两条同名观察票 —— 前端重复、且旧行由于
                #   trade_date 早会被 A5 的 stale 逻辑判过期, 中间这段窗口它们就是幽灵。
                #
                # ★ 用法pending 语义**: 「同一策略对同一只票的未入场提名」在同一时刻
                #   最多一条 —— 今天重新提名了, 昨天的提名就已被取代, 没有理由继续待
                #   在候选集里等用户看。
                # ⚠ 用 UPDATE 成 expired 而不是 DELETE: signals 是历史事实表, 回测/
                #   统计要看得见这一行存在过; state=expired 既让它脱离 watch_pending
                #   候选集, 又留下可追溯痕迹 (extra.superseded_by 记被哪天取代)。
                # ⚠ 只按本次 rows 里出现的 (strategy, code, entry_style) 精确命中 ——
                #   不带范围的批量 UPDATE 会重演 M12 事故 (误伤同表其它策略的行)。
                superseded = 0
                seen_keys = set()
                for s in rows:
                    k = (s.get("strategy") or DRAGON_STRATEGY,
                         s.get("code"), s.get("style", "a"))
                    if not k[1] or k in seen_keys:
                        continue
                    seen_keys.add(k)
                    cur.execute(f"""
                        UPDATE {_SIGNALS_TABLE}
                           SET state = %s, updated_at = NOW(),
                               extra = COALESCE(extra, '{{}}'::jsonb) || %s::jsonb
                         WHERE state = %s AND trade_date < %s
                           AND strategy = %s AND code = %s AND entry_style = %s
                    """, (
                        S_EXPIRED,
                        json.dumps({"superseded_by": str(trade_date),
                                    "superseded_ts": time.strftime("%Y-%m-%d %H:%M:%S")},
                                   ensure_ascii=False, default=str),
                        S_WATCH_PENDING, trade_date, k[0], k[1], k[2],
                    ))
                    superseded += cur.rowcount

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
                            name = EXCLUDED.name, score = EXCLUDED.score,
                            signal_date = EXCLUDED.signal_date, signal_price = EXCLUDED.signal_price,
                            lu_date = EXCLUDED.lu_date, pullback_days = EXCLUDED.pullback_days,
                            updated_at = NOW(),
                            -- A1 守卫 (2026-09-28, 2026-10-07 补「已 expired 未入场行」):
                            -- 已推进行 (entry_date 非空) **或终态 expired 行** 保留原
                            -- state 与 extra——补扫新信号行不得回滚生命周期状态,
                            -- 不得冲掉 monitor 运行时字段 (t_legs_today/pre_confirm/marked)。
                            -- 2026-10-07: 原判据只有 entry_date 非空 ⇒ **未入场的 expired
                            -- 行不受保护**; expired 是终态 (信号已失效 / 已被清理), 补扫
                            -- 会把它复活成 watch_pending ⇒ 死信号重回活跃池, 被 monitor
                            -- 当新信号处理。
                            -- ⚠ 2026-09-28 核验修正: DO UPDATE 里引用"已存在的行"必须用
                            -- **真表名** (此处 = {_SIGNALS_TABLE}, 由 f-string 展开)。写死短
                            -- 表名前缀会让 Pg 报 missing FROM-clause entry ⇒
                            -- upsert_scan_signals 整条路径抛 UndefinedTable, 全策略信号无法
                            -- 落库 (比修前的覆盖更严重)。表名单一事实源 = 同名常量。
                            state = CASE WHEN {_SIGNALS_TABLE}.entry_date IS NOT NULL
                                              OR {_SIGNALS_TABLE}.state = %s
                                         THEN {_SIGNALS_TABLE}.state ELSE EXCLUDED.state END,
                            extra = CASE WHEN {_SIGNALS_TABLE}.entry_date IS NOT NULL
                                              OR {_SIGNALS_TABLE}.state = %s
                                         THEN {_SIGNALS_TABLE}.extra ELSE EXCLUDED.extra END
                    """, (
                        trade_date, s.get("strategy", DRAGON_STRATEGY), s["code"], s.get("name", ""), s.get("board", ""),
                        s.get("style", "a"), int(s.get("score", 0)), state,
                        s.get("signal_date"), s.get("signal_price"),
                        s.get("lu_date"), s.get("pullback_days"),
                        json.dumps(extra, ensure_ascii=False, default=str),
                        s.get("entry_date"), s.get("entry_price"), s.get("stop_price"),
                        S_EXPIRED, S_EXPIRED,   # ← A1 守卫 state/extra 两处 %s, 按出现顺序
                    ))
                    n += 1
                db.commit()
                cur.close()
            return {"written": n, "purged": purged, "superseded": superseded}
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
              entry_date=None, entry_price=None, exit_reason=None, exit_date=None, exit_price=None,
              expect_state=None, only_unexited=False):
    """状态转移 (单条)。返回受影响行数 —— 0 = 守卫拦截, 写入未发生。

    条件写入守卫 (2026-09-28 修 A3, 见 _set_state):
      expect_state: 旧快照写入方必传 (调用行的快照态), 行已被并发推进则拦截;
      only_unexited: 出场类标记写入必传, 防覆盖已有出场价/出场原因 (资金事实)。
    被拦截时记 WARNING —— 该日志出现即说明存在基于过期快照的写入竞争, 应排查调用方。
    """
    from app.utils.db import get_db_connection
    with get_db_connection() as db:
        cur = db.cursor()
        n = _set_state(cur, sig_id, state, detail=detail, confirm_date=confirm_date,
                       d1_chg=d1_chg, d1_vol_r=d1_vol_r, entry_date=entry_date,
                       entry_price=entry_price, exit_reason=exit_reason,
                       exit_date=exit_date, exit_price=exit_price,
                       expect_state=expect_state, only_unexited=only_unexited)
        db.commit()
        cur.close()
    if n == 0 and (expect_state is not None or only_unexited):
        logger.warning("[store.set_state] 写入被守卫拦截: id=%s 拟写=%s expect_state=%s "
                       "only_unexited=%s (行已被并发推进, 旧快照写入已丢弃)",
                       sig_id, state, expect_state, only_unexited)
    return n


def retire_unfilled(keys=None, ids=None, reason="策略已停用, 未入场信号作废",
                    reason_by_key=None, dry_run=False):
    """作废「未入场」活跃行 —— 停用策略清扫的**唯一实现** (2026-09-26 P0-3 收编)。

    红线 (与 rebuild/startup 既有约定一致, 变更须用户裁定):
      - 只碰 ``state='watch_pending' AND entry_date IS NULL`` 的行;
      - 已入场行 (buy_today/holding/exit_today/closed) 一律不动 ——
        停用策略的已入场行必须继续可见, 否则用户会遗忘卖出 (资金事故)。

    Args:
        keys: 按策略 key 批量清扫 (startup / rebuild plan)
        ids:  按行 id 清扫 (rebuild apply / monitor); 写库前仍复核 state/entry_date
        reason: 默认作废原因文案 (写入 extra.reason)
        reason_by_key: {key: 文案} 覆盖默认 (startup 的 per-strategy 说明)
        dry_run: True=只查不写 (rebuild plan/影子审计)

    Returns:
        list[dict]: 被作废/将被作废的行 ``{id, strategy, code, trade_date}``。
        调用方按 strategy 分组即得 {key: count}。
        ⚠ **None = 失败/未知** (2026-10-07): 空 list 严格表示"确实没有这样的行",
          两者不再混同 —— 详见下面 except 的注释。**调用方必须判 `is None`**。
    """
    if not keys and not ids:
        return []
    from app.utils.db import get_db_connection
    # 2026-09-25 bugfix: sql_sel 曾写 `{_t}` 却 `.format(t=...)` → KeyError '_t',
    # retire 静默失败 (rebuild 应用时 sweep_expired 恒 0)。改 f-string 与 UPDATE 一致。
    sql_sel = (
        f"SELECT id, strategy, code, trade_date FROM {_SIGNALS_TABLE} "
        "WHERE state = %s AND entry_date IS NULL"
    )
    rows = []
    try:
        with get_db_connection() as db:
            cur = db.cursor()
            if keys:
                cur.execute(
                    sql_sel + " AND strategy = ANY(%s)",
                    (S_WATCH_PENDING, list(keys)))
                rows = [dict(r) for r in cur.fetchall()]
            if ids:
                cur.execute(
                    sql_sel + " AND id = ANY(%s)",
                    (S_WATCH_PENDING, list(ids)))
                seen = {r["id"] for r in rows}
                for r in cur.fetchall():
                    d = dict(r)
                    if d["id"] not in seen:
                        rows.append(d)
            if dry_run or not rows:
                cur.close()
                return rows
            for r in rows:
                why = (reason_by_key or {}).get(r.get("strategy")) or reason
                cur.execute(
                    f"UPDATE {_SIGNALS_TABLE} SET state = %s, updated_at = NOW(), "
                    "extra = extra || %s::jsonb "
                    "WHERE id = %s AND state = %s AND entry_date IS NULL",
                    (S_EXPIRED,
                     json.dumps({"reason": why}, ensure_ascii=False),
                     r["id"], S_WATCH_PENDING))
            db.commit()
            cur.close()
    except Exception as e:
        # 2026-10-07 (P2): 原实现吞异常后 `return []` —— **失败与"确实没有未入场行"
        #   不可区分**, 而它处在一条会把错误放大的链上:
        #     ① rebuild dry_run 拿到 [] ⇒ plan 认为"无需清扫" ⇒ apply 完全不执行,
        #        行继续挂在 watch_pending (下一步 diff 仍报 missing/ghost, 越差越大);
        #     ② 调用方的 `len(rows)` 把失败记成 **0 行**, 审计报告看不出任何异常。
        #   ⇒ 改成 ERROR + 返回 **None** (刻意不满足 Sequence ⇒ 漏改的调用方会当场
        #      TypeError 而不是静默地假装 0 行)。调用方见 monitor/startup/rebuild。
        logger.error("[store.retire_unfilled] 作废失败(返回 None 表示未知, 非「无此行」): %s", e)
        return None
    return rows


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
               entry_date=None, entry_price=None, exit_reason=None, exit_date=None, exit_price=None,
               expect_state=None, only_unexited=False):
    """构建并执行状态 UPDATE, 返回受影响行数。

    A3 条件写入守卫 (2026-09-28):
      - expect_state: WHERE 附加 `AND state = %s` —— 调用方持有的快照态与库内当前态
        一致才写。monitor 的 run_monitor 每 tick 开头批量取行快照, step2/3/4 共用同一份;
        step2 盘中可把 buy_today/holding 打成 exit_today, 若 step3/4 仍按快照无条件回写,
        会把同 tick 刚做出的出场回滚 (出场价/原因丢失后该行永不回归出场态)。
      - only_unexited: WHERE 附加 `AND COALESCE(exit_reason,'')=''` —— 出场标记
        (盘中止损/live 出场/收盘重放) 一经写入即资金事实, 后续写入不得覆盖。
    无守卫的调用 (调度路径的状态推进, 先按 id 查后写) 不受影响; 返回 0 供调用方感知拦截。
    """
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
    conds = ["id = %s"]
    vals.append(sig_id)
    if expect_state is not None:
        conds.append("state = %s")
        vals.append(expect_state)
    if only_unexited:
        conds.append("COALESCE(exit_reason, '') = ''")
    cur.execute(f"UPDATE {_SIGNALS_TABLE} SET {', '.join(sets)} WHERE {' AND '.join(conds)}", vals)
    return cur.rowcount


def list_signals(states=None, trade_date=None, days=20, only_active=False,
                 strategies=None, enabled_only=False, today=None):
    """查询信号 (signals 表)。states: 状态过滤; trade_date: 指定信号日; days: 最近N日。

    Args:
        enabled_only: True=只显示 enabled=true 策略的行 (展示层用)。
            ★ 但**停用策略的已入场行 (entry_date 非空) 仍保留可见** —— 否则用户
            会遗忘手上还有票要卖, 是实盘资金事故 (见 startup.py 模块 docstring 硬约束)。
            即: 停用 = 不再提示新买入, 但不隐藏已有持仓/卖出提示。
            ⚠ monitor 推进状态机**不能**带此过滤 (它要接着推进已入场行), 故默认 False。
        today: 窗口基准日 (YYYY-MM-DD), 默认 `today_str()` = 进程本地日期。
            ★ 显式传日是为了**同一个 tick 内多处查询看到同一条日期线** —— 跨零点
            的 monitor tick 里, 分别调 today_str() 可能拿到两个不同日期。

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
            # 2026-09-29 审计修复 (P1): 已入场行 (entry_date 非空) 不受日期窗口约束 ——
            # days 过滤的是 trade_date (D0 信号日), 持仓超窗口的行会被 monitor
            # (days=8: 止损/出场判定停摆) 和展示层 (days=30: 组行被 sync 删除)
            # 静默丢出视野, 与"已入场行一律可见"资金红线冲突。窗口仅约束未入场行。
            # A7 (2026-10-05): 基准日改由 **Python 传入**, 不再用 SQL `CURRENT_DATE`
            # —— 后者是 DB 会话时区 (UTC), 在北京 00:00~08:00 会拿到前一天 ⇒
            #   同一时刻, monitor 的 `today`(本地) 与本窗口(UTC) 不在同一天。
            sql += (" AND (trade_date >= (%s::date - %s::int) "
                    "OR entry_date IS NOT NULL)")
            vals.append(today or today_str())
            vals.append(int(days))
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
    # A5 (2026-10-05): 天数取 `VISIBLE_WINDOW_DAYS` 而非硬编码 30 —— 展示层窗口必须
    # 与 monitor 候选集同源, 否则"展示看得到、monitor 扫不到"的行会无声滞留。
    return list_signals(states=ACTIVE_GROUP_STATES, days=VISIBLE_WINDOW_DAYS,
                        enabled_only=True)


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
    rows = list_signals(states=ACTIVE_GROUP_STATES, trade_date=trade_date,
                        days=VISIBLE_WINDOW_DAYS)
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
            WHERE strategy = ANY(%s) AND code = %s AND trade_date >= (%s::date - %s::int)
            ORDER BY trade_date
        """, (list(strategy_keys()), code, today_str(), int(days)))
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

    v = **全字段**稳定哈希 (A6, 2026-10-05) —— 见函数尾部注释; 勿再改成字段拼接。

    注意: 不渲染 entry_style —— 它只是 qd_dragon_signals 的唯一键成分与 K 线 marker 文案来源
    (见 signals_markers), 前端 popover 已于 §9.3 缩减中删除"形态"行。
    """
    strat = s.get("strategy") or DRAGON_STRATEGY
    d = {
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
    # ── A6 (2026-10-05): v = 全字段稳定哈希 ──
    # 原实现 `v = f"{state}|{entry_price}|{exit_reason}|{score}"` 只覆盖 4 个字段,
    # 而本函数输出 32 个。sync_watchlist_group 的 UPDATE 判据是
    # `row["state"] != s["state"] or detail["v"] != 新v` ⇒ 其余 20 个会变的字段
    # (entry_date/stop_price/confirm_date/d1_chg/pre_confirm/exit_date/exit_price/
    #  turnover_*/ma60_slope/entry_gate/...) 变了**不触发刷新**。
    # 已证实后果: ① purge_stale_detail 清掉 pre_confirm 后投影不刷新, 前端把买入当天
    # 的「预」角标当当前预判 (store.py:437 注释自己预言过这个坑);
    # ② update_stop_price 只改 signals ⇒ 前端一直显示旧止损价 (资金相关字段)。
    # 现改为对全部字段做稳定哈希 (剔除 v 自身), 任一字段变化即刷新。
    d["v"] = hashlib.sha256(
        json.dumps(d, sort_keys=True, ensure_ascii=False, default=str)
        .encode("utf-8")).hexdigest()[:16]
    return d


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
            cur_detail = cur_detail or {}
            # 2026-10-07: 折叠键改 (code, strategy) —— 原只按 symbol 折叠, 而同一只票可被
            #   多个策略同时选中 ⇒ 折成一条后「变更检测」是在拿 A 策略的旧 detail 比
            #   B 策略的新 detail, 恒不等 ⇒ 每轮无谓 UPDATE, 且展示内容随遍历顺序漂移。
            #   strategy 只能从 strategy_detail JSON 取 (表无 strategy 列, 见 init.sql)。
            current[(d["symbol"], cur_detail.get("strategy") or DRAGON_STRATEGY)] = {
                "id": d["id"], "state": d.get("strategy_state"), "detail": cur_detail}

        target = {(s["code"], s.get("strategy") or DRAGON_STRATEGY): s for s in active_rows}

        inserted = updated = deleted = 0

        # ── UPSERT 目标集 ──
        for key, s in target.items():
            code = s["code"]
            detail = _display_detail(s)
            if key in current:
                row = current[key]
                if row["state"] != s["state"] or (row["detail"] or {}).get("v") != detail.get("v"):
                    # 按 id 定位, 不再拼 (user_id, market, symbol, group_name): 后者在同票
                    # 多策略时会打到别的策略正占用的那一行 (两策略共享物理一行)。
                    cur.execute(
                        "UPDATE qd_watchlist SET strategy_state = %s, strategy_detail = %s, "
                        "name = %s, updated_at = NOW() "
                        "WHERE id = %s",
                        (s["state"], json.dumps(detail, ensure_ascii=False, default=str),
                         s.get("name") or code, row["id"]),
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
        # ⚠ 删除粒度仍是 **code**, 不跟着降成 (code, strategy): 表唯一键是
        #   (user_id, market, symbol, group_name), 不含 strategy ⇒ 同票多策略**共享物理
        #   一行**。若按 (code, strategy) 差集删, 该行当前的 strategy 与 target 里任一
        #   策略不符时就会被删 —— 而它正是上面刚 UPSERT 过的那一行 (current 是 UPSERT
        #   之前的快照) ⇒ 票会直接从自选组消失。
        target_codes = {c for c, _ in target}
        for key, row in current.items():
            if key[0] not in target_codes:
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


_CONFIRM_LEVEL_TXT = {"strong": "强", "ok": "中", "weak": "弱"}


def _confirm_pre_text(state, d):
    """「预判」展示文案 —— 与前端 `strategyPreLevel` 同口径（双层防御）。

    返回 None 表示**不产出该行**（"没有预判"不该由数据层造一行"无"）：
      · 行不在 buy_today：holding/exit_today 上的 pre_confirm 是增量合并残留的脏数据，
        且与 docs/龙回头自动化设计方案.md:92「14:25 加角标 → 15:00 正式确认覆盖」口径不符
        （前端曾因此把持仓行渲染成"预持"）。
      · 无标记。
    档位只认 strong/ok/weak（经 core.display_meta.confirm_level_of 归一）；
    非三档只在老数据/回滚场景出现，退化为"已预判"，**不把策略内部 token 漏到 UI**。
    """
    if state != S_BUY_TODAY:
        return None
    pc = d.get("pre_confirm")
    if not pc:
        return None
    return _CONFIRM_LEVEL_TXT.get(pc) or "已预判"


def _label_payload(s) -> dict:
    """构造 4 段 payload：评分 + 扩展段(策略明细表) + 评分口径说明。

    supports/resistances 留空 —— auto 的答案不含筹码关键位; 读路径有**段级回填**，
    空段不会抹掉 system 已有的支撑位/压力位答案。
    """
    d = _display_detail(s)
    state_rows = [{"label": "状态",
                   "value": d.get("state_label") or s.get("state") or ""}]
    pre = _confirm_pre_text(s.get("state"), d)
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
    """历史清理: signals 表保留约 N 个交易日 (holding 保留至自然终态)。三策略统一清理。

    2026-09-29 审计修复 (P2): 未平仓的已入场行 (entry_date 非空且 exit_date 空)
    一律不删 —— buy_today 可是"14:56 已入场"(knife/tail), exit_today 是"待执行
    卖出"; monitor 长期停摆时这些行旧于 cutoff 会被物理删除, 持仓库/UI 双消失,
    越过"绝不能让客户忘卖"红线。已平仓行 (exit_date 非空) 照常按期清理。
    """
    from app.utils.db import get_db_connection
    cutoff = cleanup_cutoff(days)
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            f"DELETE FROM {_SIGNALS_TABLE} WHERE strategy = ANY(%s) AND trade_date < %s "
            "AND state = ANY(%s) "
            "AND (entry_date IS NULL OR exit_date IS NOT NULL)",
            (list(strategy_keys()), cutoff, [S_WATCH_PENDING, S_BUY_TODAY, S_EXIT_TODAY, S_CLOSED, S_EXPIRED]),
        )
        n = cur.rowcount
        db.commit()
        cur.close()
    return {"deleted": n}
