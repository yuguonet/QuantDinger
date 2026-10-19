# -*- coding: utf-8 -*-
"""DB 光标占位符转换守卫（2026-10-19，IndexError 三连事故修复）。

事故回溯（logs/app.log 10-15/10-16/10-19 三次盘后评估全部命中）：

  ``PostgresCursor._convert_placeholders`` 旧实现是 ``query.replace('?', '%s')``
  无条件全局替换——SQL **字符串字面量内部**的 ``?`` 也被替换，平白多出占位符，
  psycopg2 抛 ``IndexError: tuple index out of range``。实证受害者：evaluator
  ``update_weights`` 第⑦步 chain 权重查询内嵌的链名归一正则
  ``'\\+?\\d{4,6}'``，量词 ``?`` 被改成 ``%s`` ⇒ 占位符 1→3 而参数只有
  ``(since,)`` 一个。连带损伤比错误本身更重：该查询在单事务末尾抛错 ⇒
  ``conn.commit()`` 执行不到 ⇒ 连接池 putconn 自动 rollback ⇒ 同事务里
  skill 注册/清理、tool 权重更新全部静默丢弃（日志显示"成功"实则作废），
  chain 层权重从未写入（stats 恒 "chain 0"）。

本文件锁三条不变量：

  ① ``_convert_placeholders`` 只替换**引号字面量之外**的 ``?``（单引号字面量
     + ``''`` 转义、双引号标识符都不动）；routes/*.py 大量遗留 SQLite 风格
     ``?`` 占位符仍依赖该转换，故不能删除、只能收窄。
  ② evaluator chain 查询模板经 PostgresCursor 全链路后占位符数与参数数
     一致（不抛 IndexError）；模板里的日期参数必须写成 ``%%s``（防呆：
     有人把 ``%%s`` "简化" 回 ``%s`` 会重新引入 3 占位符 bug）。
  ③ 无 id 列表（qd_skill_feedback，PK=(skill_id, version)）的 upsert 必须
     自带 RETURNING，抑制兼容层自动追加的 ``RETURNING id``（与 evaluator
     qd_agent_weights 各 INSERT 同款约定）。

另附源码禁令：evaluator/skill_brewer 不得再出现硬编码
``package_prefix="tools"``（2026-10-19 同批修复的工具扫描前缀问题）。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from app.utils.db_postgres import PostgresCursor

BACKEND = Path(__file__).resolve().parents[1]
EVALUATOR = BACKEND / "app" / "agent" / "chain" / "evaluator.py"
SKILL_EVENTS = BACKEND / "app" / "agent" / "chain" / "skill_events.py"
SKILL_BREWER = BACKEND / "app" / "agent" / "chain" / "skill_brewer.py"


# ── 假裸光标：模拟 psycopg2 的占位符校验语义 ──────────────────
# psycopg2 在 execute 时按占位符逐个取 vars[i]；占位符多于参数即
# IndexError: tuple index out of range（本次事故的复现判据）。

class _FakeRawCursor:
    def __init__(self, insert_row=None):
        self.queries = []          # [(最终 SQL, args)]
        self.fetchone_calls = 0
        self._insert_row = dict(insert_row or {})

    def execute(self, query, args=None):
        n_ph = len(re.findall(r"(?<!%)%s", query))
        if args is not None and n_ph > len(args):
            raise IndexError("tuple index out of range")
        self.queries.append((query, args))

    def fetchone(self):
        self.fetchone_calls += 1
        return dict(self._insert_row)

    def fetchall(self):
        return []


def _sql_constants(path: Path, marker: str):
    """取源码中所有含 marker 的 SQL 字符串常量（AST，非全文匹配）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if marker in node.value:
                out.append(node.value)
    return out


# ── ① _convert_placeholders：引号感知 ─────────────────────────

class TestConvertPlaceholders:
    def test_legacy_question_mark_still_converted(self):
        """遗留 SQLite 风格 ? 占位符必须照旧转换（routes/*.py 依赖它）。"""
        sql = "SELECT id FROM qd_users WHERE id = ? AND user_id = ?"
        assert PostgresCursor._convert_placeholders(sql) == (
            "SELECT id FROM qd_users WHERE id = %s AND user_id = %s")

    def test_question_mark_inside_single_quoted_literal_preserved(self):
        """单引号字面量内的 ? 是数据（本次事故根：正则量词 \\+?）。"""
        sql = "SELECT regexp_replace(t.name, '\\+?\\d{4,6}', '', 'g')"
        got = PostgresCursor._convert_placeholders(sql)
        assert "'\\+?\\d{4,6}'" in got
        assert "%s" not in got

    def test_mixed_literal_and_placeholder(self):
        """字面量外照转、字面量内不动，一次命中两种形态。"""
        sql = "SELECT * FROM t WHERE name = 'a?b' AND id = ?"
        got = PostgresCursor._convert_placeholders(sql)
        assert got == "SELECT * FROM t WHERE name = 'a?b' AND id = %s"

    def test_escaped_quote_inside_literal(self):
        """'' 转义引号不闭串，其后内容仍在字面量内。"""
        sql = "SELECT 'it''s ? ok' , ?"
        got = PostgresCursor._convert_placeholders(sql)
        assert got == "SELECT 'it''s ? ok' , %s"

    def test_question_mark_inside_double_quoted_identifier_preserved(self):
        """双引号标识符内的 ? 是标识符一部分，不是占位符。"""
        sql = 'SELECT "we?ird" FROM t WHERE id = ?'
        got = PostgresCursor._convert_placeholders(sql)
        assert got == 'SELECT "we?ird" FROM t WHERE id = %s'

    def test_idempotent_on_pg_style_sql(self):
        """已是 %s 风格的 SQL 不受影响（不重复造占位符）。"""
        sql = "SELECT %s FROM t WHERE a = %s AND b = 'x?y'"
        assert PostgresCursor._convert_placeholders(sql) == sql


# ── ② evaluator chain 查询全链路（镜像 update_weights 第⑦步）──

def _chain_sql() -> str:
    """复刻 evaluator.py update_weights 第⑦步的 chain 权重查询。

    模板与 evaluator.py 中该 execute 一致（日期参数用 %%s 转义）；
    链名归一表达式取真实的 store._sql_chain_norm，避免测试另造一份语义。
    若 evaluator 模板变更，请同步本函数与 test_chain_template_escapes_date。
    """
    from app.agent.chain.store import _sql_chain_norm
    nk = _sql_chain_norm("t.name")
    return """
                SELECT %s AS chain_name,
                       COUNT(*) AS n,
                       AVG(CASE WHEN t.correct THEN 1.0 ELSE 0.0 END) AS win_rate
                FROM qd_agent_traces t
                WHERE t.layer = 'chain'
                  AND t.correct IS NOT NULL
                  AND t.exec_date >= %%s
                  AND position('unknown' in t.name) = 0
                GROUP BY %s
            """ % (nk, nk)


class TestChainQueryThroughCursor:
    def test_no_index_error_and_single_placeholder(self):
        """经 PostgresCursor 全链路：1 占位符配 1 参数，不再 IndexError。"""
        import datetime
        raw = _FakeRawCursor()
        cur = PostgresCursor(raw)
        cur.execute(_chain_sql(), (datetime.date(2026, 7, 21),))
        sql, args = raw.queries[-1]
        assert len(re.findall(r"(?<!%)%s", sql)) == 1
        assert len(args) == 1
        # 正则量词 ? 必须完好无损地到达裸光标
        assert "'\\+?\\d{4,6}'" in sql

    def test_chain_template_escapes_date_placeholder(self):
        """防呆：evaluator 源码里的日期参数必须是 %%s。

        若被"简化"回 %s，Python % 格式化阶段就会多消耗一个 _nk，SQL 结构
        直接错位（且历史版本正是因此 3 占位符对 1 参数）。
        """
        cands = _sql_constants(EVALUATOR, "AS chain_name")
        assert cands, "evaluator 中未找到 chain 权重查询模板"
        assert any("%%s" in c for c in cands), (
            "chain 查询模板的日期参数应为 %%s（转义写法），防 % 格式化错位")


# ── ③ 无 id 列表的 upsert 必须自带 RETURNING ──────────────────

class TestSkillEventsUpsertReturning:
    @staticmethod
    def _strip_line_comments(sql: str) -> str:
        """剥掉 -- 行注释后再做 RETURNING 断言（注释里提到字样不算数）。"""
        return "\n".join(
            line.split("--", 1)[0] for line in sql.splitlines())

    def test_source_has_explicit_returning(self):
        """skill_events 写 qd_skill_feedback 的 INSERT 必须自带 RETURNING。"""
        cands = _sql_constants(SKILL_EVENTS, "INSERT INTO qd_skill_feedback")
        assert cands, "skill_events 中未找到 qd_skill_feedback INSERT"
        assert all("RETURNING" in c for c in cands), (
            "qd_skill_feedback 无 id 列，必须自带 RETURNING 抑制兼容层"
            "自动追加的 RETURNING id（2026-10-19 UndefinedColumn 事故）")

    def test_upsert_not_auto_appended_returning_id(self):
        """ON CONFLICT DO UPDATE + 自带 RETURNING ⇒ 不再被追加 RETURNING id。"""
        tmpl = [c for c in _sql_constants(SKILL_EVENTS,
                                          "INSERT INTO qd_skill_feedback")][0]
        raw = _FakeRawCursor(insert_row={"skill_id": "auto-agent"})
        cur = PostgresCursor(raw)
        cur.execute(tmpl, tuple(["s"] * 10))
        sql, _ = raw.queries[-1]
        body = self._strip_line_comments(sql)
        assert "RETURNING skill_id" in body
        assert not re.search(r"RETURNING\s+id\b", body), (
            "兼容层不应再给无 id 列的表追加 RETURNING id")

    def test_simple_insert_still_gets_returning_id(self):
        """反向不变量：简单 INSERT...VALUES 仍自动追加 RETURNING id。

        兼容层的本来目的（填 lastrowid）不能被这次修复矫枉过正掉。
        """
        raw = _FakeRawCursor(insert_row={"id": 42})
        cur = PostgresCursor(raw)
        cur.execute("INSERT INTO t (a, b) VALUES (%s, %s)", ("x", "y"))
        sql, args = raw.queries[-1]
        assert sql.rstrip().endswith("RETURNING id")
        assert args == ("x", "y")
        assert raw.fetchone_calls == 1

    def test_insert_on_conflict_do_nothing_not_appended(self):
        """ON CONFLICT DO NOTHING 仍不追加（_is_simple_insert 原语义）。"""
        raw = _FakeRawCursor()
        cur = PostgresCursor(raw)
        cur.execute(
            "INSERT INTO t (a) VALUES (%s) ON CONFLICT (a) DO NOTHING", ("x",))
        sql, _ = raw.queries[-1]
        assert "RETURNING" not in sql


# ── 源码禁令：工具扫描前缀不得再硬编码 "tools" ─────────────────

class TestNoHardcodedToolsPrefix:
    @staticmethod
    def _hardcoded_tools_prefix_calls(path: Path):
        """AST 找调用：任何 package_prefix="tools" 的关键字实参。

        刻意不用全文匹配——被禁字样常写在修复注释/docstring 里，全文匹配
        会恒红（2026-10-19 首版即踩此坑）。
        """
        tree = ast.parse(path.read_text(encoding="utf-8"))
        hits = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if (kw.arg == "package_prefix"
                        and isinstance(kw.value, ast.Constant)
                        and kw.value.value == "tools"):
                    hits.append(node.lineno)
        return hits

    @pytest.mark.parametrize("path", [EVALUATOR, SKILL_BREWER])
    def test_no_package_prefix_tools(self, path):
        """evaluator/skill_brewer 不得再出现 package_prefix="tools"。

        2026-10-19：这三处自建扫描（sys.path 时代写法）导致 24 个工具模块 +
        2 个元工具全部导入失败。现统一走 ToolProvider.get_or_build()。
        """
        assert not self._hardcoded_tools_prefix_calls(path), (
            f"{path.name} 存在硬编码 package_prefix=\"tools\" 的扫描调用，"
            "应改走 ToolProvider.get_or_build()")

    def test_brewer_and_evaluator_use_get_or_build(self):
        """两处扫描必须走 get_or_build()（前缀从 __module__ 推导）。"""
        for path in (EVALUATOR, SKILL_BREWER):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            calls = [
                n.func.attr for n in ast.walk(tree)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            ]
            assert "get_or_build" in calls, (
                f"{path.name} 应经 ToolProvider.get_or_build() 取工具面")
