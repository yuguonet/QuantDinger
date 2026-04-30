#!/usr/bin/env python3
"""
QuantDinger 全量策略验证
========================
对 backend_api_python/strategies/ 目录下所有 YAML 策略文件进行完整校验。

校验维度：
    1. YAML 结构完整性（7 个必填字段）
    2. 内容质量（instructions 长度、category 合法性、core_rules 格式）
    3. 安全规范检查（指令中是否包含危险关键词）
    4. 指标覆盖度（是否覆盖 QuantDinger 支持的指标）
    5. 参数合理性（required_tools 合法性）
    6. 交叉引用检查（name 与文件名一致性）
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

import yaml

sys.path.insert(0, str(Path(__file__).parent))
from yaml_to_indicator import QuantDingerStandards


# ============================================================
# 校验规则
# ============================================================

REQUIRED_FIELDS = ['name', 'display_name', 'description', 'category', 'core_rules', 'required_tools', 'instructions']

VALID_CATEGORIES = {'trend', 'reversal', 'framework', 'pattern', 'volume', 'volatility', 'unknown'}

VALID_CORE_RULES = set(range(1, 10))  # 规则编号 1-9

VALID_TOOLS = {
    'get_daily_history', 'get_realtime_quote', 'analyze_trend',
    'analyze_volume', 'get_stock_info', 'calculate_indicator',
    'backtest_strategy', 'get_market_sentiment',
    'get_sector_rankings', 'search_stock_news', 'get_financial_data',
    'get_holder_info', 'get_block_trade', 'get_margin_data',
}

# instructions 中应包含的关键结构（支持等价表述）
EXPECTED_SECTIONS = {
    '信号判定': ['信号判定', '信号标准', '信号逻辑', '入场信号', '触发条件', '核心逻辑',
               '前提条件', '判断标准', '买卖点判定', '筛选条件', '技术指标',
               '评估标准', '反转判定标准', '判定标准', '选股条件',
               '信号判定标准', '主信号', '强信号'],
    '买入条件': ['买入条件', '买入信号', '建仓条件', '建仓信号', '做多条件', '入场条件',
               '开仓条件', '买点', '入场', '建仓', '反弹信号', '核心逻辑',
               '价格确认', '确认条件', '确认因素', '前提条件', '筛选',
               '量能异动', '价格企稳', '板块领涨', '评估标准', '持续下跌确认',
               '买入/加仓信号', '买入区', '贪婪时买入', '底背驰',
               '买入/观望/减仓', '买入倾向'],
    '卖出条件': ['卖出条件', '卖出信号', '减仓条件', '减仓信号', '平仓条件', '出场条件',
               '卖点', '出场', '减仓', '止盈', '风险过滤', '级别与仓位',
               '评估标准', '相对强度', '换手率', '卖出/减仓信号', '减仓区',
               '顶背驰', '卖出倾向', '减仓倾向', '买入/观望/减仓',
               '情绪顶部特征'],
    '止损': ['止损', '风险控制', '风控', '止损位', '止损线', '风险提示', '风险排查',
            '风险过滤', '仓位管理', '风险', '风险可控', '止损参考'],
}

# instructions 中不应出现的危险内容
DANGEROUS_KEYWORDS = [
    'eval(', 'exec(', 'os.system', '__import__',
    'subprocess', 'shell', 'rm -rf', 'DROP TABLE',
]

# QuantDinger 支持的指标关键词
SUPPORTED_INDICATOR_KEYWORDS = {
    'ema', 'sma', 'ma', 'rsi', 'macd', 'kdj', 'bollinger', '布林',
    'atr', 'supertrend', 'volume', '成交量', 'donchian', 'vwap',
    '均线', '金叉', '死叉', '超买', '超卖', '背离', '突破',
    '量比', '换手率', '乖离率', '龙头', '板块', '轮动', '情绪',
    '中枢', '笔', '线段', '波浪', '浪型', '放量', '缩量',
    '支撑', '阻力', '趋势', '震荡', '反转', '形态',
}


# ============================================================
# 校验器
# ============================================================

class StrategyValidator:
    """策略文件校验器。"""

    def __init__(self):
        self.results: List[Dict[str, Any]] = []
        self.stats = {
            'total': 0, 'pass': 0, 'warn': 0, 'fail': 0,
            'checks_total': 0, 'checks_pass': 0, 'checks_warn': 0, 'checks_fail': 0,
        }

    def validate_all(self, dir_path: str) -> List[Dict[str, Any]]:
        """校验目录下所有 YAML 文件。"""
        path = Path(dir_path)
        if not path.is_dir():
            print(f"❌ 目录不存在: {dir_path}")
            return []

        yaml_files = sorted(path.glob('*.yaml'))
        print(f"📂 找到 {len(yaml_files)} 个策略文件")
        print()

        for yf in yaml_files:
            result = self.validate_file(str(yf))
            self.results.append(result)

        self._print_summary()
        return self.results

    def validate_file(self, yaml_path: str) -> Dict[str, Any]:
        """校验单个文件。"""
        path = Path(yaml_path)
        self.stats['total'] += 1
        checks = []

        def check(name, ok, level='pass', detail=''):
            nonlocal checks
            status = '✅' if level == 'pass' else ('⚠️' if level == 'warn' else '❌')
            checks.append({'name': name, 'ok': ok, 'level': level, 'detail': detail})
            self.stats['checks_total'] += 1
            if level == 'pass':
                self.stats['checks_pass'] += 1
            elif level == 'warn':
                self.stats['checks_warn'] += 1
            else:
                self.stats['checks_fail'] += 1

        # 读取
        try:
            with open(yaml_path, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f)
        except Exception as e:
            check('YAML 解析', False, 'fail', str(e))
            return {'file': path.name, 'status': 'fail', 'checks': checks}

        if not isinstance(data, dict):
            check('YAML 格式', False, 'fail', '顶层不是字典')
            return {'file': path.name, 'status': 'fail', 'checks': checks}

        # 1. 必填字段
        for field in REQUIRED_FIELDS:
            present = field in data and data[field]
            check(f'字段 {field}', present, 'pass' if present else 'fail',
                  '' if present else '缺失或为空')

        # 2. name 与文件名一致性
        file_stem = path.stem
        yaml_name = data.get('name', '')
        name_match = yaml_name == file_stem
        check('name 与文件名一致', name_match,
              'pass' if name_match else 'warn',
              f'文件名={file_stem}, name={yaml_name}')

        # 3. category 合法性
        category = data.get('category', '')
        cat_valid = category in VALID_CATEGORIES
        check('category 合法', cat_valid,
              'pass' if cat_valid else 'warn',
              f'category={category}')

        # 4. core_rules 格式
        core_rules = data.get('core_rules', [])
        rules_valid = (isinstance(core_rules, list) and
                       all(isinstance(r, int) and r in VALID_CORE_RULES for r in core_rules))
        check('core_rules 格式', rules_valid,
              'pass' if rules_valid else 'warn',
              f'core_rules={core_rules}')

        # 5. required_tools 合法性
        tools = data.get('required_tools', [])
        unknown_tools = [t for t in tools if t not in VALID_TOOLS]
        tools_ok = len(unknown_tools) == 0
        check('required_tools 合法', tools_ok,
              'pass' if tools_ok else 'warn',
              f'未知工具: {unknown_tools}' if unknown_tools else '')

        # 6. display_name 非空且含中文
        dn = data.get('display_name', '')
        has_chinese = bool(re.search(r'[\u4e00-\u9fff]', dn))
        check('display_name 含中文', has_chinese,
              'pass' if has_chinese else 'warn',
              f'display_name={dn}')

        # 7. description 长度
        desc = data.get('description', '')
        desc_len = len(desc)
        desc_ok = desc_len >= 10
        check('description 足够长', desc_ok,
              'pass' if desc_ok else 'warn',
              f'长度={desc_len}')

        # 8. instructions 长度
        instr = data.get('instructions', '')
        instr_len = len(instr)
        instr_ok = instr_len >= 50
        check('instructions 足够详细', instr_ok,
              'pass' if instr_ok else 'fail',
              f'长度={instr_len}')

        # 9. instructions 结构性检查（支持等价表述）
        for section, aliases in EXPECTED_SECTIONS.items():
            has_section = any(alias in instr for alias in aliases)
            matched = [a for a in aliases if a in instr]
            check(f'instructions 含"{section}"', has_section,
                  'pass' if has_section else 'warn',
                  f'匹配: {matched[0]}' if matched else f'缺少 {section}（尝试: {aliases[:3]}...）')

        # 10. 安全检查
        for kw in DANGEROUS_KEYWORDS:
            safe = kw.lower() not in instr.lower()
            check(f'安全: 无"{kw}"', safe,
                  'pass' if safe else 'fail',
                  '' if safe else f'发现危险关键词: {kw}')

        # 11. 指标覆盖度
        instr_lower = instr.lower()
        found_indicators = [kw for kw in SUPPORTED_INDICATOR_KEYWORDS if kw in instr_lower]
        has_indicators = len(found_indicators) > 0
        check('instructions 含指标关键词', has_indicators,
              'pass' if has_indicators else 'warn',
              f'识别到: {found_indicators[:5]}' if found_indicators else '未识别到指标关键词')

        # 12. 评分调整合理性（如果有的话）
        if 'sentiment_score' in instr_lower or '评分' in instr:
            # 检查评分值是否合理（-20 ~ +20）
            score_matches = re.findall(r'[+-]?\d+', instr.split('评分')[-1][:100] if '评分' in instr else '')
            unreasonable = [s for s in score_matches if abs(int(s)) > 30]
            check('评分调整合理', len(unreasonable) == 0,
                  'pass' if len(unreasonable) == 0 else 'warn',
                  f'异常评分值: {unreasonable}' if unreasonable else '')

        # 汇总
        has_fail = any(c['level'] == 'fail' for c in checks)
        has_warn = any(c['level'] == 'warn' for c in checks)
        if has_fail:
            status = 'fail'
            self.stats['fail'] += 1
        elif has_warn:
            status = 'warn'
            self.stats['warn'] += 1
        else:
            status = 'pass'
            self.stats['pass'] += 1

        # 打印结果
        icon = {'pass': '✅', 'warn': '⚠️', 'fail': '❌'}[status]
        fail_count = sum(1 for c in checks if c['level'] == 'fail')
        warn_count = sum(1 for c in checks if c['level'] == 'warn')
        pass_count = sum(1 for c in checks if c['level'] == 'pass')

        print(f"{icon} {path.name:35s}  {pass_count}✅ {warn_count}⚠️ {fail_count}❌")

        # 打印非 pass 的项
        for c in checks:
            if c['level'] != 'pass':
                marker = '⚠️' if c['level'] == 'warn' else '❌'
                print(f"   {marker} {c['name']}: {c['detail']}")

        return {'file': path.name, 'status': status, 'checks': checks,
                'display_name': dn, 'category': category}

    def _print_summary(self):
        """打印汇总。"""
        print()
        print("╔══════════════════════════════════════════════════════════╗")
        print("║              📊 全量策略验证汇总                         ║")
        print("╚══════════════════════════════════════════════════════════╝")
        print()

        # 文件级统计
        print(f"  文件总数:  {self.stats['total']}")
        print(f"  完全通过:  {self.stats['pass']} ✅")
        print(f"  有警告:    {self.stats['warn']} ⚠️")
        print(f"  有错误:    {self.stats['fail']} ❌")
        print()

        # 检查项统计
        total_checks = self.stats['checks_total']
        pass_checks = self.stats['checks_pass']
        warn_checks = self.stats['checks_warn']
        fail_checks = self.stats['checks_fail']

        pass_rate = pass_checks / total_checks * 100 if total_checks else 0
        error_rate = (fail_checks + warn_checks * 0.5) / total_checks * 100 if total_checks else 0

        bar = '█' * int(pass_rate / 5) + '░' * (20 - int(pass_rate / 5))
        print(f"  检查项总数: {total_checks}")
        print(f"  通过:       {pass_checks}  警告: {warn_checks}  错误: {fail_checks}")
        print(f"  通过率:     {bar} {pass_rate:.1f}%")
        print(f"  综合误差率: {error_rate:.1f}%")
        print()

        # 按策略展示详情表
        print(f"  {'策略文件':35s} {'状态':6s} {'类别':10s} {'显示名'}")
        print(f"  {'─'*35} {'─'*6} {'─'*10} {'─'*20}")
        for r in self.results:
            icon = {'pass': '✅', 'warn': '⚠️', 'fail': '❌'}[r['status']]
            print(f"  {r['file']:35s} {icon:6s} {r.get('category','?'):10s} {r.get('display_name','?')}")

        # 保存详细报告
        report_path = Path(__file__).parent / 'strategy_validation_report.json'
        with open(report_path, 'w', encoding='utf-8') as f:
            json.dump({
                'summary': {
                    'total_files': self.stats['total'],
                    'pass': self.stats['pass'],
                    'warn': self.stats['warn'],
                    'fail': self.stats['fail'],
                    'total_checks': total_checks,
                    'pass_checks': pass_checks,
                    'warn_checks': warn_checks,
                    'fail_checks': fail_checks,
                    'pass_rate': pass_rate,
                    'error_rate': error_rate,
                },
                'details': [
                    {
                        'file': r['file'],
                        'status': r['status'],
                        'display_name': r.get('display_name', ''),
                        'category': r.get('category', ''),
                        'issues': [c for c in r.get('checks', []) if c['level'] != 'pass'],
                    }
                    for r in self.results
                ],
            }, f, ensure_ascii=False, indent=2)
        print(f"\n  📄 详细报告: {report_path}")

        if error_rate > 15:
            print(f"\n  ⚠️  综合误差率 {error_rate:.1f}% > 15%，需优化")
            return 1
        elif error_rate > 5:
            print(f"\n  ⚡ 综合误差率 {error_rate:.1f}%，可接受")
            return 0
        else:
            print(f"\n  🎉 综合误差率 {error_rate:.1f}%，优秀！")
            return 0


# ============================================================
# 入口
# ============================================================

def main():
    strategies_dir = Path(__file__).parent / "backend_api_python" / "strategies"
    if not strategies_dir.is_dir():
        print(f"❌ 目录不存在: {strategies_dir}")
        sys.exit(1)

    validator = StrategyValidator()
    results = validator.validate_all(str(strategies_dir))

    # 退出码
    has_fail = any(r['status'] == 'fail' for r in results)
    sys.exit(1 if has_fail else 0)


if __name__ == '__main__':
    main()
