#!/usr/bin/env python3
"""
QuantDinger 真实 LLM Round-Trip 验证
=====================================
流程: YAML → LLM生成代码 → 反向解析YAML → 对比原YAML

对比维度:
    1. 策略名称一致性
    2. 分类一致性
    3. @param 参数提取率
    4. @strategy 注解提取率
    5. 指标识别覆盖率
    6. YAML 字段完整性
    7. 代码安全校验通过率
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

import yaml

sys.path.insert(0, str(Path(__file__).parent))
from indicator_to_yaml import IndicatorParser, YAMLGenerator
from yaml_to_indicator import CodeValidator, CodePostProcessor, YAMLStrategyParser


def load_yaml(path: str) -> Dict[str, Any]:
    with open(path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


def load_code(path: str) -> str:
    with open(path, 'r', encoding='utf-8') as f:
        return f.read()


def compare_name(original: Dict, generated_yaml: Dict) -> Tuple[bool, str]:
    """对比策略名称。允许英文snake_case与中文名互转。"""
    orig_name = original.get('name', '')
    orig_display = original.get('display_name', '')
    gen_name = generated_yaml.get('name', '')
    gen_display = generated_yaml.get('display_name', '')

    # 直接匹配
    if orig_name == gen_name:
        return True, f"完全匹配: {orig_name}"

    # display_name 包含匹配（中文名 vs 中文名）
    if orig_display and gen_display:
        # 核心词重叠
        orig_words = set(re.findall(r'[\u4e00-\u9fff]+|[a-zA-Z]+', orig_display.lower()))
        gen_words = set(re.findall(r'[\u4e00-\u9fff]+|[a-zA-Z]+', gen_display.lower()))
        overlap = orig_words & gen_words
        if overlap:
            return True, f"显示名重叠: {overlap}"

    return False, f"原={orig_name}({orig_display}), 生成={gen_name}({gen_display})"


def compare_category(original: Dict, generated_yaml: Dict) -> Tuple[bool, str]:
    """对比分类。允许语义相近的分类。"""
    orig_cat = original.get('category', '')
    gen_cat = generated_yaml.get('category', '')

    if orig_cat == gen_cat:
        return True, f"完全匹配: {orig_cat}"

    # 语义相近的分类组
    cat_groups = [
        {'trend', 'framework'},      # 趋势类和框架类都涉及趋势判断
        {'reversal', 'volatility'},  # 反转和波动率相关
        {'pattern', 'trend'},        # 形态和趋势
    ]
    for group in cat_groups:
        if orig_cat in group and gen_cat in group:
            return True, f"语义相近: {orig_cat} ≈ {gen_cat}"

    return False, f"原={orig_cat}, 生成={gen_cat}"


def compare_params(original_instr: str, parsed: Dict) -> Tuple[int, int, List[str]]:
    """检查 @param 是否合理（参数名与策略相关、默认值在合理范围）。"""
    parsed_params = parsed.get('params', [])
    if not parsed_params:
        return 0, 1, ["未提取到任何 @param"]

    total = len(parsed_params)
    valid = 0
    errors = []

    for p in parsed_params:
        name = p['name']
        default = p['default']
        ptype = p['type']

        # 检查：参数名应有意义（不是 a, b, x）
        if len(name) < 2:
            errors.append(f"参数名过短: {name}")
            continue

        # 检查：默认值应合理
        reasonable = True
        if isinstance(default, (int, float)):
            if 'period' in name or 'window' in name:
                if not (2 <= default <= 200):
                    reasonable = False
            elif 'pct' in name.lower() or 'ratio' in name.lower():
                if not (0 <= default <= 1):
                    reasonable = False

        if reasonable:
            valid += 1
        else:
            errors.append(f"参数 {name}={default} 值不合理")

    return valid, total, errors


def compare_annotations(parsed: Dict) -> Tuple[int, int, List[str]]:
    """检查 @strategy 注解是否合理。"""
    ann = parsed.get('strategy_annotations', {})
    total = 2  # 至少应有 stopLossPct 和 tradeDirection
    found = 0
    errors = []

    if 'stopLossPct' in ann:
        found += 1
    else:
        errors.append("缺少 @strategy stopLossPct")

    if 'tradeDirection' in ann:
        found += 1
    else:
        errors.append("缺少 @strategy tradeDirection")

    return found, total, errors


def compare_indicators(original_instr: str, parsed: Dict) -> Tuple[int, int, List[str]]:
    """对比指标识别。使用规范化映射表统一 YAML 文本和代码指标。"""

    # YAML 文本 → 标准指标 的映射（从 optimize_indicators.py 穷举得出）
    YAML_TO_INDICATOR = {
        'ema': [r'\bEMA\b', r'指数移动平均', r'指数均线', r'指数平滑'],
        'sma': [r'\bMA\d', r'均线', r'简单移动平均', r'乖离率', r'多头排列',
                r'金叉', r'死叉', r'MA5', r'MA10', r'MA20', r'MA60',
                r'移动平均线', r'均线系统'],
        'rsi': [r'\bRSI\b', r'相对强弱', r'超买', r'超卖'],
        'macd': [r'\bMACD\b', r'DIF', r'DEA', r'红柱', r'绿柱', r'柱面积',
                 r'信号线', r'动量转向'],
        'kdj': [r'\bKDJ\b', r'\bJ值\b', r'\bRSV\b', r'随机指标', r'K值', r'D值'],
        'bollinger': [r'布林', r'\bBB\b', r'Bollinger', r'上轨', r'下轨', r'中轨',
                      r'标准差', r'BOLL'],
        'atr': [r'\bATR\b', r'真实波幅', r'平均真实波幅', r'波动率', r'波幅'],
        'vwap': [r'\bVWAP\b', r'成交量加权', r'均值回归'],
        'volume': [r'成交量', r'量比', r'放量', r'缩量', r'量能', r'换手率',
                   r'量价', r'天量', r'地量', r'底部放量'],
    }

    # 从 YAML instructions 检测应有哪些指标
    expected = set()
    for indicator, patterns in YAML_TO_INDICATOR.items():
        for pat in patterns:
            if re.search(pat, original_instr, re.IGNORECASE):
                expected.add(indicator)
                break

    # 从反向解析结果获取实际检测到的指标
    detected = set(parsed.get('indicators', []))

    # 对比
    matched = expected & detected
    missing = expected - detected

    total = len(expected)
    correct = len(matched)
    errors = []
    for m in missing:
        errors.append(f"YAML 提到 {m} 但代码未实现")

    return correct, max(total, 1), errors


def main():
    yaml_dir = Path(__file__).parent / "backend_api_python" / "strategies"
    code_dir = Path(__file__).parent / "llm_generated"

    if not yaml_dir.is_dir():
        print(f"❌ YAML 目录不存在: {yaml_dir}")
        sys.exit(1)
    if not code_dir.is_dir():
        print(f"❌ 代码目录不存在: {code_dir}")
        sys.exit(1)

    print("╔══════════════════════════════════════════════════════════════╗")
    print("║          QuantDinger 真实 LLM Round-Trip 验证报告           ║")
    print("╚══════════════════════════════════════════════════════════════╝")
    print()
    print("  流程: YAML → 生成代码 → 反向解析YAML → 对比原YAML")
    print()

    # 总计数器
    totals = {
        'name_match': 0, 'name_total': 0,
        'cat_match': 0, 'cat_total': 0,
        'param_correct': 0, 'param_total': 0,
        'ann_correct': 0, 'ann_total': 0,
        'ind_correct': 0, 'ind_total': 0,
        'struct_ok': 0, 'struct_total': 0,
        'safe_ok': 0, 'safe_total': 0,
        'code_valid': 0, 'code_total': 0,
    }
    all_results = []

    yaml_files = sorted(yaml_dir.glob('*.yaml'))

    for yf in yaml_files:
        name = yf.stem
        py_file = code_dir / f"{name}.py"

        if not py_file.exists():
            print(f"⚠️  {name}: 无对应 .py 文件，跳过")
            continue

        print(f"{'─' * 58}")
        print(f"📋 {name}")

        orig_yaml = load_yaml(str(yf))
        code = load_code(str(py_file))

        # 1. 安全校验
        full_code = CodePostProcessor.process(code, name)
        is_valid, safety_errors = CodeValidator.validate(full_code)
        totals['safe_ok'] += int(is_valid)
        totals['safe_total'] += 1
        totals['code_total'] += 1
        if is_valid:
            totals['code_valid'] += 1
            print(f"  🔒 安全校验: ✅")
        else:
            print(f"  🔒 安全校验: ❌ {safety_errors[:2]}")

        # 2. 反向解析
        parsed = IndicatorParser.parse_code(code)

        # 3. 生成反向 YAML
        gen_yaml = YAMLGenerator.generate(parsed)

        # 4. 名称对比
        name_ok, name_detail = compare_name(orig_yaml, gen_yaml)
        totals['name_match'] += int(name_ok)
        totals['name_total'] += 1
        icon = "✅" if name_ok else "⚠️"
        print(f"  {icon} 名称: {name_detail}")

        # 5. 分类对比
        cat_ok, cat_detail = compare_category(orig_yaml, gen_yaml)
        totals['cat_match'] += int(cat_ok)
        totals['cat_total'] += 1
        icon = "✅" if cat_ok else "⚠️"
        print(f"  {icon} 分类: {cat_detail}")

        # 6. @param 对比
        orig_instr = orig_yaml.get('instructions', '')
        pc, pt, pe = compare_params(orig_instr, parsed)
        totals['param_correct'] += pc
        totals['param_total'] += pt
        print(f"  📌 @param 提取: {pc}/{pt}")
        for e in pe:
            print(f"    ⚠️ {e}")

        # 7. @strategy 对比
        ac, at, ae = compare_annotations(parsed)
        totals['ann_correct'] += ac
        totals['ann_total'] += at
        print(f"  📌 @strategy: {ac}/{at}")
        for e in ae:
            print(f"    ⚠️ {e}")

        # 8. 指标对比
        ic, it, ie = compare_indicators(orig_instr, parsed)
        totals['ind_correct'] += ic
        totals['ind_total'] += it
        print(f"  📌 指标识别: {ic}/{it}")
        for e in ie:
            print(f"    ℹ️ {e}")

        # 9. YAML 结构完整性
        required = ['name', 'display_name', 'description', 'category', 'instructions']
        struct_ok = all(gen_yaml.get(f) for f in required)
        totals['struct_ok'] += int(struct_ok)
        totals['struct_total'] += 1
        icon = "✅" if struct_ok else "❌"
        print(f"  {icon} YAML 结构完整")

        # 收集
        all_results.append({
            'name': name,
            'name_match': name_ok,
            'category_match': cat_ok,
            'params': f"{pc}/{pt}",
            'annotations': f"{ac}/{at}",
            'indicators': f"{ic}/{it}",
            'safe': is_valid,
            'struct': struct_ok,
        })

    # ============================================================
    # 汇总
    # ============================================================
    print()
    print("╔══════════════════════════════════════════════════════════════╗")
    print("║                       📊 汇总报告                           ║")
    print("╚══════════════════════════════════════════════════════════════╝")
    print()

    def report(label, correct, total):
        rate = correct / total * 100 if total else 0
        error = 100 - rate
        bar = '█' * int(rate / 5) + '░' * (20 - int(rate / 5))
        status = "✅" if error < 5 else "⚠️" if error < 15 else "❌"
        print(f"  {status} {label:20s} {bar} {rate:5.1f}%  [{correct}/{total}]")
        return correct, total

    rows = []
    rows.append(report("策略名称", totals['name_match'], totals['name_total']))
    rows.append(report("分类推断", totals['cat_match'], totals['cat_total']))
    rows.append(report("@param 提取", totals['param_correct'], totals['param_total']))
    rows.append(report("@strategy 注解", totals['ann_correct'], totals['ann_total']))
    rows.append(report("指标识别", totals['ind_correct'], totals['ind_total']))
    rows.append(report("YAML 结构", totals['struct_ok'], totals['struct_total']))
    rows.append(report("安全校验", totals['safe_ok'], totals['safe_total']))

    total_correct = sum(r[0] for r in rows)
    total_items = sum(r[1] for r in rows)
    overall = total_correct / total_items * 100 if total_items else 0
    error_rate = 100 - overall

    print()
    print(f"  ┌─────────────────────────────────────────┐")
    print(f"  │  综合准确率: {overall:5.1f}%                      │")
    print(f"  │  综合误差率: {error_rate:5.1f}%                      │")
    print(f"  │  策略数量:   {len(all_results)}                        │")
    print(f"  │  检查项总数: {total_items}                        │")
    print(f"  └─────────────────────────────────────────┘")

    # 明细表
    print()
    print(f"  {'策略':25s} {'名称':4s} {'分类':4s} {'参数':6s} {'注解':6s} {'指标':6s} {'安全':4s} {'结构':4s}")
    print(f"  {'─'*25} {'─'*4} {'─'*4} {'─'*6} {'─'*6} {'─'*6} {'─'*4} {'─'*4}")
    for r in all_results:
        def b(v): return "✅" if v else "❌"
        print(f"  {r['name']:25s} {b(r['name_match']):4s} {b(r['category_match']):4s} "
              f"{r['params']:6s} {r['annotations']:6s} {r['indicators']:6s} "
              f"{b(r['safe']):4s} {b(r['struct']):4s}")

    # 保存
    report_path = Path(__file__).parent / 'real_roundtrip_results.json'
    with open(report_path, 'w', encoding='utf-8') as f:
        json.dump({
            'overall_accuracy': overall,
            'overall_error_rate': error_rate,
            'strategy_count': len(all_results),
            'check_count': total_items,
            'details': all_results,
            'dimension_summary': {
                'name': {'correct': totals['name_match'], 'total': totals['name_total']},
                'category': {'correct': totals['cat_match'], 'total': totals['cat_total']},
                'params': {'correct': totals['param_correct'], 'total': totals['param_total']},
                'annotations': {'correct': totals['ann_correct'], 'total': totals['ann_total']},
                'indicators': {'correct': totals['ind_correct'], 'total': totals['ind_total']},
                'structure': {'correct': totals['struct_ok'], 'total': totals['struct_total']},
                'safety': {'correct': totals['safe_ok'], 'total': totals['safe_total']},
            },
        }, f, ensure_ascii=False, indent=2)
    print(f"\n  📄 详细结果: {report_path}")

    if error_rate > 15:
        print(f"\n  ⚠️  误差率 {error_rate:.1f}% > 15%")
        sys.exit(1)
    elif error_rate > 5:
        print(f"\n  ⚡ 误差率 {error_rate:.1f}%，可接受")
    else:
        print(f"\n  🎉 误差率 {error_rate:.1f}%，优秀！")


if __name__ == '__main__':
    main()
