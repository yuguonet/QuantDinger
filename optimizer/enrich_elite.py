"""
给 elite_strategies_adaptive_vol.json 补充行业、地域、股票名称信息。

用法（在项目根目录执行）：
    python -m optimizer.enrich_elite

依赖：无额外依赖，使用东方财富公开API
"""

import json
import os
import urllib.request
import time


def get_stock_info(code):
    """从东方财富获取股票基本信息（行业、地域、名称）"""
    if code.startswith('6'):
        em_code = f'SH{code}'
    else:
        em_code = f'SZ{code}'

    url = f'https://emweb.securities.eastmoney.com/PC_HSF10/CompanySurvey/PageAjax?code={em_code}'
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0',
        'Referer': 'https://emweb.securities.eastmoney.com/'
    })
    try:
        resp = urllib.request.urlopen(req, timeout=8)
        data = json.loads(resp.read().decode('utf-8'))
        info = data.get('jbzl', [{}])[0]
        return {
            'name': info.get('SECURITY_NAME_ABBR', ''),
            'em_industry': info.get('EM2016', ''),
            'em_industry_l1': (info.get('EM2016', '') or '').split('-')[0],
            'csrc_industry': info.get('INDUSTRYCSRC1', ''),
            'csrc_industry_l1': (info.get('INDUSTRYCSRC1', '') or '').split('-')[0],
            'province': info.get('PROVINCE', ''),
            'reg_capital': info.get('REG_CAPITAL'),
            'emp_num': info.get('EMP_NUM'),
        }
    except Exception as e:
        return {'error': str(e)}


def get_board(code):
    if code.startswith('688'):
        return '科创板'
    elif code.startswith('300') or code.startswith('301'):
        return '创业板'
    elif code.startswith('002'):
        return '中小板'
    elif code.startswith('000') or code.startswith('001'):
        return '深主板'
    elif code.startswith('600') or code.startswith('601') or code.startswith('603') or code.startswith('605'):
        return '沪主板'
    return '其他'


def enrich():
    elite_path = os.path.join(os.path.dirname(__file__), 'elite_strategies_adaptive_vol.json')
    if not os.path.exists(elite_path):
        print(f'错误: 找不到 {elite_path}')
        print('请先运行 python -m optimizer.extract_elite_stocks')
        return

    with open(elite_path, 'r', encoding='utf-8') as f:
        elite = json.load(f)

    stocks = elite.get('stocks', {})
    print(f'加载 {len(stocks)} 只双优股票，开始获取行业数据...')

    enriched = 0
    failed = 0

    for i, (code, info) in enumerate(stocks.items()):
        if (i + 1) % 20 == 0:
            print(f'  进度 {i+1}/{len(stocks)} ...')

        # 获取行业信息
        stock_info = get_stock_info(code)
        time.sleep(0.3)

        if 'error' in stock_info:
            failed += 1
            info['board'] = get_board(code)
            continue

        enriched += 1
        info['name'] = stock_info['name']
        info['board'] = get_board(code)
        info['em_industry'] = stock_info['em_industry']
        info['em_industry_l1'] = stock_info['em_industry_l1']
        info['csrc_industry'] = stock_info['csrc_industry']
        info['csrc_industry_l1'] = stock_info['csrc_industry_l1']
        info['province'] = stock_info['province']
        info['reg_capital'] = stock_info['reg_capital']
        info['emp_num'] = stock_info['emp_num']

    # 统计
    from collections import Counter
    boards = Counter(v.get('board', '未知') for v in stocks.values())
    industries = Counter(v.get('em_industry_l1', '未知') for v in stocks.values() if v.get('em_industry_l1'))
    provinces = Counter(v.get('province', '未知') for v in stocks.values() if v.get('province'))

    elite['industry_analysis'] = {
        'description': '双优股票行业/板块/地域分布',
        'enriched_at': time.strftime('%Y-%m-%d %H:%M'),
        'enriched_count': enriched,
        'failed_count': failed,
        'board_distribution': dict(boards.most_common()),
        'industry_distribution': dict(industries.most_common()),
        'province_distribution': dict(provinces.most_common()),
    }

    with open(elite_path, 'w', encoding='utf-8') as f:
        json.dump(elite, f, ensure_ascii=False, indent=2)

    print(f'\n完成: 成功 {enriched}, 失败 {failed}')
    print(f'已更新: {elite_path}')

    # 打印摘要
    print(f'\n=== 板块分布 ===')
    for b, cnt in boards.most_common():
        print(f'  {b}: {cnt}')

    print(f'\n=== 行业分布（前10）===')
    for ind, cnt in industries.most_common(10):
        print(f'  {ind}: {cnt}')

    print(f'\n=== 地域分布（前10）===')
    for p, cnt in provinces.most_common(10):
        print(f'  {p}: {cnt}')


if __name__ == '__main__':
    enrich()
