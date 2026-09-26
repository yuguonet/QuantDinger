-- qd_agent_weights.layer 扩宽到 VARCHAR(20)（A1b 校准层）
-- 原 VARCHAR(10) 存不下 'calibration'(11字符)；扩宽不缩窄，不破坏现有数据。
-- layer 取值（冻结语义，禁止中途改码值）：
--   'skill'       技能层权重
--   'factor'      因子层权重
--   'tool'        工具层权重
--   'chain'       链路层权重（P3）
--   'calibration' 评分校准层（A1b，score→P(方向正确) 的映射参数）
ALTER TABLE qd_agent_weights ALTER COLUMN layer TYPE VARCHAR(20);
