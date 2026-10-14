-- ═══════════════════════════════════════════════════════════════════════
-- agent_v6 — 技能标识符 kebab 化（snake_case → kebab-case）
-- ═══════════════════════════════════════════════════════════════════════
-- 背景（2026-10-14）：技能目录名 / SKILL.md name / EXECUTABLE_SKILLS 键统一为
--   连字符，以对齐 Anthropic Agent Skills 规范（name 必须=目录名，且仅 [a-z0-9-]）。
--   存量库里权重行仍带旧的下划线技能名 ⇒ evaluator 按新名聚合时对不上、权重丢失。
-- 处置：把 qd_agent_weights（skill 层的 name、factor 层归属的 skill_name）、以及
--   遗留表 qd_skill_weights / qd_factor_weights 的旧技能名改为连字符。
--   幂等：可重复执行；表不存在则跳过（to_regclass 守卫）。
-- 说明：qd_agent_traces 是历史审计记录，**不回填**（保留当时口径，不改写历史）。
-- ═══════════════════════════════════════════════════════════════════════

DO $$
DECLARE
    m RECORD;
BEGIN
    FOR m IN
        SELECT * FROM (VALUES
            ('market_screener',            'market-screener'),
            ('strategy_debug',             'strategy-debug'),
            ('auto_agent',                 'auto-agent'),
            ('auto_finance-analysis-stock','auto-finance-analysis-stock'),
            ('auto_finance-query-stock',   'auto-finance-query-stock'),
            ('auto_stock-analyze',         'auto-stock-analyze')
        ) AS t(old_name, new_name)
    LOOP
        -- ① 统一权重表（qd_skill_weights/qd_factor_weights 的实际替代者）
        IF to_regclass('public.qd_agent_weights') IS NOT NULL THEN
            UPDATE qd_agent_weights
               SET name = m.new_name
             WHERE layer = 'skill' AND name = m.old_name;

            UPDATE qd_agent_weights
               SET skill_name = m.new_name
             WHERE skill_name = m.old_name;
        END IF;

        -- ② 遗留表（若尚未被 drop_deprecated_agent_tables.sql 移除）
        IF to_regclass('public.qd_skill_weights') IS NOT NULL THEN
            UPDATE qd_skill_weights
               SET skill_name = m.new_name
             WHERE skill_name = m.old_name;
        END IF;

        IF to_regclass('public.qd_factor_weights') IS NOT NULL THEN
            UPDATE qd_factor_weights
               SET skill_name = m.new_name
             WHERE skill_name = m.old_name;
        END IF;
    END LOOP;
END $$;
