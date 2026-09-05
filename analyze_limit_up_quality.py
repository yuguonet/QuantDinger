#!/usr/bin/env python3
"""涨停日质量评估 + 三策略前置噪音过滤 (妖股前置分析) v2

定位: 独立回测验证工具 (与 test_dragon.py 同级), 核心算法成熟后迁入
      backend_api_python/app/market_cn/auto/。本工具回答一个问题:
      "一个涨停事件是否值得让 V1追板 / 断板 / 龙回头 三个分支继续 downstream 分析",
      即用统一前置过滤把杂毛在进入后置策略前砍掉。

背景: 三策略 = 一只妖股完整上升周期的三个接力段
  V1追板  — 板内加速 (1板放量→2板低开/平开续放量→3板+缩量一致板, 7板易开板, 出货放巨量)
  断板    — 连板≥2断板后换庄/洗盘续涨, 接力快
  龙回头  — 强股大分歧, 回调3-11天洗盘后再接力, 周期长
  龙回头8特征: 换手≥5% / 流通市值30~300亿 / 前期大涨尤其涨停 / 回调3-11天保热度 /
               MA20/30/60斜率向上 / 关键位有支撑 / 上攻放量 / 非ST

v2 升级点 (2026-09-04):
  1. --source db 全市场分析 (默认), 兼容 --source api 原样本模式
  2. 锚点从"每票第一个涨停日"扩展为全部涨停事件 (一字板默认剔除)
  3. 统一前置过滤 U1~U6 (锚点日可知, 无未来函数) + 三策略分支标签
  4. 样本股召回验证 SAMPLE_CASES (用户指定妖股时段必须存活)
  5. 明细写 tmp/limit_up_quality_result.json

关键设计点:
  - 过滤只用 D0(锚点日)及之前数据; 未来数据仅用于打标签 (has_follow/v1/break/dragon),
    标签是评估尺子, 不是过滤输入
  - 换手率/流通市值用当前 circ_shares 估算历史值 (用户认可口径), volume 单位=股
  - qfq 复权不改 volume, 换手率口径不受复权影响
易错点:
  - DB K线时间归一为 15:00, query end 取次日才能含当天最后一根 (fetch_kline_db 已处理)
  - circ_shares=0 的票 (新股/数据缺失) 换手与市值特征为 None, 过滤按 fail-closed 处理
用法:
  python analyze_limit_up_quality.py --source db --days 300        # 全市场300交易日
  python analyze_limit_up_quality.py --source db --codes 002931,600105 --verbose
  python analyze_limit_up_quality.py --source api                  # 原TEST_CODES样本模式
"""
import json, sys, os, time, argparse
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ================================================================
# 参数 (不硬编码, 集中在此; 阈值来自用户经验, 待回测证据修正)
# ================================================================
QUALITY_PARAMS = {
    # 统一前置过滤 (锚点日 D0 可知, 2026-09-04 全市场18564事件证据修订)
    'u_st_excl': True,          # U1 剔除ST/退市风险
    'u_turnover_min': 3.0,      # U2 换手率% 下限 (5%留给龙回头分支口径)
    'u_float_mv_min': 20.0,     # U3 流通市值下限(亿) (30~300严格口径会误杀600105样本)
    'u_float_mv_max': 500.0,    # U3 流通市值上限(亿)
    'u_heat_ret20_min': 10.0,   # U4 前20日涨幅%下限 (与 prior_lu 或关系, 最佳单条+1.4pp)
    'u_heat_prior_lu_min': 1,   # U4 前20日涨停次数下限 (或关系)
    # ⚠已证伪, 不参与统一过滤 (保留参数位供实验):
    #   放量过滤(u_vol_ratio_min): 反指标 — 缩量一致板更强, 与连板节奏(1板放量→3板+缩量)自洽
    #   MA20斜率(u_ma_slope_min): 首板启动位MA常未拐头, 属龙回头分支信号日条件
    'u_vol_ratio_min': None,
    'u_ma_slope_min': None,
    # 事件与标签
    'lu_threshold_shrink': 0.98,  # 涨停判定容差 (9.8%/19.8%)
    'follow_days': 20,            # 后续行情观察窗口
    'follow_gain': 10.0,          # 突破涨幅% (close >= lu_close*(1+x%))
    'break_window': 10,           # 断板后回封观察窗口
    'dragon_pb_min': 3,           # 龙头回调最短天数
    'dragon_pb_max': 11,
    'dragon_relay_gain': 5.0,     # 回调结束后relay判定涨幅%
    'dragon_relay_window': 10,
}

SAMPLE_CASES = [
    # (code, 起始日, 结束日) — 用户指定妖股时段, None=全历史
    ('002931', '2025-10-13', '2025-12-01'),
    ('002931', '2026-06-02', '2026-07-06'),
    ('600105', '2025-11-24', '2026-01-12'),
    ('600105', '2026-03-27', '2026-06-26'),
    ('000017', '2026-08-19', '2026-08-31'),
    ('603318', None, None),
    ('002081', None, None),
    ('002580', None, None),
]

TEST_CODES = [  # api 模式样本池 500只 (深主板+沪主板为主, 排除蓝筹/ST/科创)
    '603196','002548','600116','600918','603081','603721','002469','600085','002353','605133',
    '000590','002676','000859','002727','605116','000657','600128','600356','603535','002912',
    '002201','002777','000989','600391','600096','603111','603396','002215','002899','002943',
    '002051','001226','000088','600658','002210','002405','600446','603215','002180','002107',
    '605098','002998','600645','002508','600825','002083','605086','600989','002967','000021',
    '002203','603568','603606','603073','600056','603232','001308','002921','002692','603202',
    '003026','600862','600820','000517','000597','603276','000956','000527','600625','002786',
    '603042','603328','600749','603590','002028','002346','001201','600248','600178','002746',
    '603577','000588','600601','002602','000029','600567','600711','000862','002510','002893',
    '002400','002240','603678','600888','600866','001391','002906','605136','600459','600925',
    '002521','000800','002467','002132','600272','002879','002588','600312','002735','600694',
    '002226','002318','002931','603585','600470','002593','603656','002553','002163','600435',
    '603132','600135','002415','000025','600315','603697','002653','000785','603689','000919',
    '603095','000626','600780','603685','600237','002498','001395','600066','603810','002939',
    '002821','000513','603407','002500','603348','600756','000153','002420','600973','600618',
    '600573','600452','600997','002490','002663','001236','001322','600697','600117','603380',
    '002073','603266','003023','600399','600020','600285','600566','600575','001359','002511',
    '600280','002282','600717','603053','002693','000151','000570','603282','002043','002294',
    '000923','600758','002244','002529','002356','002887','002060','600499','002733','600642',
    '002589','002973','002647','000663','000748','000407','600786','600408','600212','603070',
    '002701','603321','600162','002531','002057','603325','002552','002009','600725','600318',
    '000915','600996','002737','600662','001339','603098','600800','001266','000001','002121',
    '002364','603719','600113','002945','600834','603636','603993','600281','002971','002627',
    '002140','002903','600054','000566','603980','603861','603335','002413','600508','002880',
    '600433','600720','603218','603313','002090','002047','600305','603999','600983','002494',
    '600241','002407','600616','002021','600456','603228','603916','001283','603110','603399',
    '603035','600962','603613','003001','600691','002448','605168','603075','002991','603698',
    '603538','600801','600338','002063','002079','600832','002982','600708','002829','603868',
    '000695','000555','603225','002756','600580','000788','600151','002324','600300','600375',
    '603316','002459','600963','001231','600729','002387','002101','000514','002161','600860',
    '600021','002440','600263','603336','600742','002059','600552','002137','002891','002926',
    '000683','000679','603002','603466','600182','603088','003019','600594','603337','600936',
    '002388','002092','600063','000930','600126','003021','605069','600478','600986','605100',
    '002534','600160','600724','001278','002535','603155','603896','603393','603109','603717',
    '000791','603045','002928','600768','000755','000027','002773','002827','000967','002436',
    '002590','600129','603207','600350','000506','002277','603456','000070','600001','002348',
    '600027','002261','000929','001288','002241','600208','600939','600905','603004','000789',
    '600886','002599','600196','603658','600612','002077','002649','603051','002985','603310',
    '000837','002442','002757','002380','600371','000861','600579','000828','002007','605028',
    '002565','002836','600748','002116','603040','603208','000567','002615','603091','600930',
    '003039','000562','000623','603690','603633','002915','600192','002103','000819','603375',
    '000925','000676','603103','002466','600493','600057','000529','603181','603257','600033',
    '603136','002396','002540','002608','002429','603162','600841','600218','000713','600827',
    '002768','001393','600159','600120','002154','000518','002367','603231','002790','603171',
    '603889','600283','002223','600718','002623','002708','000536','605006','603387','002570',
    '002397','603099','000607','002870','002585','002725','603385','002625','603726','600228',
    '001234','002295','600339','603281','603061','002406','002256','002976','600984','603168',
    '603275','600779','603195','600422','000014','002229','002965','600629','002453','002742',
    '600100','002285','002515','000886','003028','002214','600058','002111','600075','603826',
    '600259','003017','002366','000428','603968','002386','000688','003018','603370','000758',
]

# ================================================================
# 数据加载 (db 模式复用 test_dragon 的加载链)
# ================================================================
def _load_test_dragon():
    import test_dragon
    test_dragon._load_env()
    return test_dragon

def load_universe_db(days, codes=None):
    """返回 (bars_by_code, stock_info); stock_info 含 name/industry/circ_shares"""
    td = _load_test_dragon()
    code_list = codes if codes else td.get_all_codes_db()
    info = td.fetch_stock_info_db()
    bars_by_code = {}
    for i, c in enumerate(code_list):
        bars_by_code[c] = td.fetch_kline_db(c, days=days)
        if (i + 1) % 500 == 0:
            print(f"  K线加载进度: {i+1}/{len(code_list)}")
    return bars_by_code, info

def _fetch_tencent_info_batch(codes):
    """腾讯行情API批量获取 stock_info (name/circ_shares/turnover_rate)"""
    import requests, re
    SESSION = requests.Session()
    SESSION.headers.update({"User-Agent": "Mozilla/5.0"})
    info = {}
    # 腾讯支持逗号分隔批量查询, 每批80只
    for batch_start in range(0, len(codes), 80):
        batch = codes[batch_start:batch_start + 80]
        syms = []
        for c in batch:
            c = c.strip()
            if c.startswith(('6','5')): syms.append(f"sh{c}")
            elif c.startswith(('0','3','2')): syms.append(f"sz{c}")
        if not syms: continue
        try:
            resp = SESSION.get(f"https://qt.gtimg.cn/q={','.join(syms)}", timeout=10)
            for line in resp.text.strip().split('\n'):
                m = re.search(r'v_(\w+)="(.+)"', line)
                if not m: continue
                sym, raw = m.group(1), m.group(2)
                p = raw.split('~')
                if len(p) < 45: continue
                code = p[2]
                price = float(p[3]) if p[3] else 0
                circ_mv = float(p[44]) if p[44] else 0
                circ_shares = int(circ_mv * 1e8 / price) if price > 0 else 0
                info[code] = {
                    'name': p[1].replace(' ', ''),
                    'industry': '',
                    'circ_shares': circ_shares,
                    'turnover_rate': float(p[38]) if p[38] else None,
                    'circ_mv': circ_mv,
                }
        except Exception:
            pass
        time.sleep(0.2)
    return info

def load_universe_api(days, codes=None):
    from kline_cache import fetch_kline
    code_list = codes if codes else TEST_CODES
    bars_by_code = {}
    for i, c in enumerate(code_list):
        try:
            bars_by_code[c] = fetch_kline(c, days)
        except Exception:
            bars_by_code[c] = []
        time.sleep(0.05)
    # 从腾讯行情获取 stock_info
    print(f"  获取股票基本信息...")
    stock_info = _fetch_tencent_info_batch(code_list)
    print(f"  成功获取 {len(stock_info)} 只股票信息")
    return bars_by_code, stock_info

# ================================================================
# 工具函数
# ================================================================
def get_board_type(code):
    if code.startswith('688'): return 'kcb'
    if code.startswith('300') or code.startswith('301'): return 'cyb'
    return 'main'

def get_limit_threshold(board):
    return 0.198 if board in ('cyb', 'kcb') else 0.098

def _ma(closes, period):
    if len(closes) < period: return None
    return sum(closes[-period:]) / period

def _sma_at(closes, idx, period):
    if idx + 1 < period: return None
    return sum(closes[idx + 1 - period: idx + 1]) / period

def _is_st(name):
    return bool(name) and ('ST' in name.upper() or '退' in name)

def _is_yizi(b):
    return b['open'] == b['high'] == b['low'] == b['close']

def _forward_streak(bars, lu_idx, th):
    """从锚点(含)向后数连续涨停 → (streak_len, streak_top_idx)"""
    n, i = 1, lu_idx + 1
    while i < len(bars):
        prev = bars[i - 1]['close']
        if prev > 0 and bars[i]['close'] / prev - 1 >= th:
            n += 1; i += 1
        else:
            break
    return n, i - 1

# ================================================================
# 事件特征 + 过滤 + 标签
# ================================================================
def analyze_event(bars, lu_idx, code, name, circ_shares, p=QUALITY_PARAMS, verbose_feats=False):
    """单个涨停事件 (锚点D0=lu_idx) 的特征/过滤/标签。

    过滤特征: 全部 D0 可知; 标签: 用 D0 之后数据 (仅评估用)。
    返回 dict; 数据不足时返回 None。
    """
    board = get_board_type(code)
    th = get_limit_threshold(board) * p['lu_threshold_shrink']
    n = len(bars)
    if lu_idx < 20 or lu_idx + 1 >= n:
        return None
    lu = bars[lu_idx]
    prev_c = bars[lu_idx - 1]['close']
    if prev_c <= 0 or lu['close'] <= 0:
        return None

    f = {'code': code, 'board': board, 'name': name or '',
         'lu_date': lu['time'], 'lu_close': lu['close'], 'lu_idx': lu_idx}

    # ── D0 特征 (过滤输入) ──
    f['is_yizi'] = _is_yizi(lu)
    f['lu_close_ret'] = (lu['close'] / prev_c - 1) * 100
    f['lu_open_gap'] = (lu['open'] / prev_c - 1) * 100
    rng = lu['high'] - lu['low']
    f['lu_amplitude'] = (lu['high'] - lu['low']) / lu['low'] * 100 if lu['low'] > 0 else 0
    f['lu_upper_shadow'] = (lu['high'] - max(lu['open'], lu['close'])) / rng * 100 if rng > 0 else 0
    vol5 = [bars[j]['volume'] for j in range(max(0, lu_idx - 5), lu_idx)]
    avg_vol5 = sum(vol5) / len(vol5) if vol5 else 0
    f['lu_vol_ratio'] = lu['volume'] / avg_vol5 if avg_vol5 > 0 else None

    # 连板位置 (第几板): 从锚点向前数连续涨停
    streak_pos, j = 1, lu_idx
    while j >= 2 and bars[j - 1]['close'] > 0 and bars[j - 2]['close'] > 0 \
            and bars[j - 1]['close'] / bars[j - 2]['close'] - 1 >= th:
        streak_pos += 1; j -= 1
    f['streak_pos'] = streak_pos

    # 前20日热度
    f['ret_20d'] = (lu['close'] / bars[lu_idx - 20]['close'] - 1) * 100 if bars[lu_idx - 20]['close'] > 0 else None
    prior_lu = 0
    for k in range(lu_idx - 20, lu_idx):
        if k >= 1 and bars[k - 1]['close'] > 0 and bars[k]['close'] / bars[k - 1]['close'] - 1 >= th:
            prior_lu += 1
    f['prior_lu_count'] = prior_lu

    # 均线斜率 (5日)
    closes = [b['close'] for b in bars[:lu_idx + 1]]
    ma20_now = _sma_at(closes, lu_idx, 20)
    ma20_prev = _sma_at(closes, lu_idx - 5, 20) if lu_idx >= 5 else None
    f['ma20_slope5'] = (ma20_now / ma20_prev - 1) * 100 if ma20_now and ma20_prev and ma20_prev > 0 else None
    ma60_now = _sma_at(closes, lu_idx, 60)
    ma60_prev = _sma_at(closes, lu_idx - 5, 60) if lu_idx >= 5 else None
    f['ma60_slope5'] = (ma60_now / ma60_prev - 1) * 100 if ma60_now and ma60_prev and ma60_prev > 0 else None

    # 换手率 / 流通市值 (当前股本估算历史)
    if circ_shares and circ_shares > 0:
        f['turnover'] = lu['volume'] / circ_shares * 100
        f['float_mv'] = circ_shares * lu['close'] / 1e8
    else:
        f['turnover'] = None
        f['float_mv'] = None
    f['is_st'] = _is_st(name)

    # ── 统一前置过滤判定 (U5/U6 已证伪不参与, 特征仍保留供分支层使用) ──
    u = {}
    u['U1_non_st'] = (not p['u_st_excl']) or (not f['is_st'])
    u['U2_turnover'] = f['turnover'] is None or f['turnover'] >= p['u_turnover_min']  # None=数据不可用,放行
    u['U3_float_mv'] = f['float_mv'] is None or (p['u_float_mv_min'] <= f['float_mv'] <= p['u_float_mv_max'])  # 同上
    u['U4_heat'] = ((f['ret_20d'] is not None and f['ret_20d'] >= p['u_heat_ret20_min'])
                    or f['prior_lu_count'] >= p['u_heat_prior_lu_min'])
    if p.get('u_ma_slope_min') is not None:
        u['U5_ma_up'] = f['ma20_slope5'] is not None and f['ma20_slope5'] > p['u_ma_slope_min']
    if p.get('u_vol_ratio_min') is not None:
        u['U6_vol'] = f['lu_vol_ratio'] is not None and f['lu_vol_ratio'] >= p['u_vol_ratio_min']
    f['ufilter'] = u
    f['upass'] = all(u.values())

    # ── 标签 (未来数据, 仅评估) ──
    target = lu['close'] * (1 + p['follow_gain'] / 100)
    peak_price, first_break = lu['close'], None
    for d in range(1, p['follow_days'] + 1):
        k = lu_idx + d
        if k >= n: break
        if bars[k]['high'] > peak_price: peak_price = bars[k]['high']
        if first_break is None and bars[k]['close'] >= target: first_break = d
    f['has_follow'] = first_break is not None
    f['first_break_day'] = first_break
    f['peak_return'] = (peak_price / lu['close'] - 1) * 100

    s_len, top_i = _forward_streak(bars, lu_idx, th / p['lu_threshold_shrink'])  # 用严格阈值数板
    f['streak_len_after'] = s_len
    f['v1_label'] = s_len >= 2   # 板后还能走出连板 → V1追板有肉

    top_close = bars[top_i]['close']
    break_label = False
    for k in range(top_i + 1, min(top_i + 1 + p['break_window'], n)):
        if bars[k]['close'] >= top_close:
            break_label = True; break
    f['break_label'] = break_label

    # 龙回头标签: 板后连续回调 3~11 天, 回调结束后 relay_window 内最大收盘 >= 回调末收盘×(1+relay%)
    dragon_label = False
    pb_days = 0
    k = top_i + 1
    while k < n and bars[k]['close'] < top_close and pb_days < p['dragon_pb_max'] + 2:
        pb_days += 1; k += 1
    if p['dragon_pb_min'] <= pb_days <= p['dragon_pb_max'] and k < n:
        pb_end = k - 1
        hi = max((bars[m]['close'] for m in range(k, min(k + p['dragon_relay_window'], n))), default=0)
        dragon_label = hi >= bars[pb_end]['close'] * (1 + p['dragon_relay_gain'] / 100)
    f['dragon_label'] = dragon_label
    f['pb_days_after'] = pb_days

    if verbose_feats:
        pass  # 调试期可扩展
    return f

def analyze_stock_events(bars, code, name, circ_shares, p=QUALITY_PARAMS, keep_yizi=False):
    """一只股票的全部涨停事件"""
    board = get_board_type(code)
    th = get_limit_threshold(board) * p['lu_threshold_shrink']
    out = []
    if not bars or len(bars) < 60:
        return out
    for i in range(20, len(bars) - 1):
        prev_c = bars[i - 1]['close']
        if prev_c <= 0: continue
        if bars[i]['close'] / prev_c - 1 >= th:
            if not keep_yizi and _is_yizi(bars[i]):
                continue
            ev = analyze_event(bars, i, code, name, circ_shares, p)
            if ev: out.append(ev)
    return out

# ================================================================
# 报告
# ================================================================
def _rate(sub):
    n = len(sub)
    if n == 0: return "0事件"
    hf = sum(1 for e in sub if e['has_follow']) / n * 100
    pk = sum(e['peak_return'] for e in sub) / n
    v1 = sum(1 for e in sub if e['v1_label']) / n * 100
    bk = sum(1 for e in sub if e['break_label']) / n * 100
    dg = sum(1 for e in sub if e['dragon_label']) / n * 100
    return f"{n:>6}事件  有后续 {hf:5.1f}%  均峰值 {pk:+5.2f}%  V1 {v1:4.1f}%  断板 {bk:4.1f}%  龙回头 {dg:4.1f}%"

def print_report(events, stock_info, args):
    labeled = lambda e: e['v1_label'] or e['break_label'] or e['dragon_label']
    print()
    print("=" * 84)
    print(f"涨停事件质量报告 ({args.source}模式, 共{len(events)}个事件 / {len({e['code'] for e in events})}只票)")
    print("=" * 84)
    print(f"\n基线(全部事件):            {_rate(events)}")

    # 单条统一过滤
    print(f"\n── 单条统一过滤效果 (剔除率 | 保留集表现) ──")
    ulabel = {
        'U1_non_st': 'U1 非ST/退',
        'U2_turnover': f"U2 换手>={QUALITY_PARAMS['u_turnover_min']}%",
        'U3_float_mv': f"U3 流通市值{QUALITY_PARAMS['u_float_mv_min']:.0f}~{QUALITY_PARAMS['u_float_mv_max']:.0f}亿",
        'U4_heat': f"U4 前期热度(20日涨幅>={QUALITY_PARAMS['u_heat_ret20_min']:.0f}%或有涨停)",
        'U5_ma_up': 'U5 MA20斜率向上 [龙回头分支口径]',
        'U6_vol': 'U6 D0放量1.5x [已证伪,反指标]',
    }
    ukeys = [k for k in ['U1_non_st', 'U2_turnover', 'U3_float_mv', 'U4_heat', 'U5_ma_up', 'U6_vol']
             if k in events[0]['ufilter']]
    for k in ukeys:
        kept = [e for e in events if e['ufilter'][k]]
        cut = 100 - len(kept) / len(events) * 100 if events else 0
        print(f"  {ulabel[k]:<34} 剔除{cut:5.1f}% | {_rate(kept)}")

    # 组合
    print(f"\n── 组合过滤 ──")
    base4 = ['U1_non_st', 'U2_turnover', 'U3_float_mv', 'U4_heat']
    for combo, label in [
        (base4, '统一过滤 U1~U4 (推荐)'),
        (['U2_turnover', 'U4_heat', 'U5_ma_up'], '宽松: U2+U4+U5'),
        (ukeys, '全组合'),
    ]:
        if not all(k in ukeys for k in combo):
            continue
        kept = [e for e in events if all(e['ufilter'][k] for k in combo)]
        cut = 100 - len(kept) / len(events) * 100 if events else 0
        print(f"  {label:<34} 剔除{cut:5.1f}% | {_rate(kept)}")

    # 分支口径对照
    print(f"\n── 分支口径对照 (供各策略 downstream 使用, 非统一过滤) ──")
    for tmin in (3.0, 5.0):
        kept = [e for e in events if e['turnover'] is not None and e['turnover'] >= tmin]
        tag = '龙回头口径' if tmin >= 5 else '统一口径'
        print(f"  [对照] 换手>={tmin:.0f}%({tag})     剔除{100-len(kept)/len(events)*100:5.1f}% | {_rate(kept)}")
    kept = [e for e in events if e['float_mv'] is not None and 30 <= e['float_mv'] <= 300]
    print(f"  [对照] 市值30~300亿(龙回头严格) 剔除{100-len(kept)/len(events)*100:5.1f}% | {_rate(kept)}")
    kept = [e for e in events if e['streak_pos'] == 1 and e['lu_vol_ratio'] is not None and e['lu_vol_ratio'] >= 1.5]
    print(f"  [对照] 仅首板放量1.5x          剔除{100-len(kept)/len(events)*100:5.1f}% | {_rate(kept)}")

    # 标签互斥与覆盖
    print(f"\n── 三分支标签覆盖 (一个事件可命中多分支) ──")
    for lbl, name in [('v1_label', 'V1追板(streak>=2)'), ('break_label', '断板(回封)'), ('dragon_label', '龙回头(回调后接力)')]:
        sub = [e for e in events if e[lbl]]
        print(f"  {name:<24} {_rate(sub)}")
    none_lbl = [e for e in events if not labeled(e)]
    print(f"  {'三分支全不命中(纯杂毛)':<22} {_rate(none_lbl)}")
    kept_lbl = [e for e in none_lbl if e['upass']]
    print(f"  {'└ 其中通过统一过滤(残余噪音)':<22} {_rate(kept_lbl)}")

    # 样本股召回 (口径: 带分支标签的事件必须存活; 窗口级: 每时段至少留1个)
    print(f"\n── 样本股召回验证 (用户指定妖股时段, 带标签事件必须存活) ──")
    total_pass, total_evt, win_ok, win_tot = 0, 0, 0, 0
    lost_windows = []
    for code, d1, d2 in SAMPLE_CASES:
        if args.codes and code not in args.codes:
            continue
        evs = [e for e in events if e['code'] == code
               and (d1 is None or d1 <= e['lu_date'] <= d2)]
        if not evs:
            print(f"  {code}: 窗口[{d1}~{d2}] 无涨停事件 (检查数据覆盖!)")
            continue
        lab = [e for e in evs if labeled(e)]
        pas = [e for e in lab if e['upass']]
        info = stock_info.get(code, {})
        if lab:
            win_tot += 1
            total_evt += len(lab); total_pass += len(pas)
            if pas:
                win_ok += 1
            else:
                lost_windows.append(f"{code}[{d1}~{d2}]")
        print(f"  {code} {info.get('name','')} 窗口[{d1}~{d2}]: {len(evs)}事件(带标签{len(lab)}), 存活{len(pas)}")
        for e in evs:
            marks = ' '.join(k for k in ('v1_label', 'break_label', 'dragon_label') if e[k])
            fail = [k for k, v in e['ufilter'].items() if not v]
            tag = '存活' if e['upass'] else ('被砍:' + ','.join(fail) if fail else '')
            print(f"    {e['lu_date']} 第{e['streak_pos']}板 换手{e['turnover'] and round(e['turnover'],1)}% "
                  f"市值{e['float_mv'] and round(e['float_mv'],0)}亿 20日涨幅{e['ret_20d'] and round(e['ret_20d'],1)}% "
                  f"[{marks or '无标签'}] {tag if labeled(e) else '(杂毛,可砍)'}")
    if total_evt:
        print(f"  ── 带标签事件召回: {total_pass}/{total_evt} = {total_pass/total_evt*100:.1f}%  窗口存活: {win_ok}/{win_tot}"
              + (f"  ⚠丢窗: {';'.join(lost_windows)}" if lost_windows else "  ✅全存活"))

# ================================================================
# 主入口
# ================================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--source', default='db', choices=['db', 'api'])
    ap.add_argument('--days', type=int, default=300)
    ap.add_argument('--codes', default='', help='逗号分隔, 只分析指定股票')
    ap.add_argument('--limit', type=int, default=0, help='限制股票数 (调试)')
    ap.add_argument('--keep-yizi', action='store_true', help='保留一字板事件')
    ap.add_argument('--from-json', default='', help='从已保存的明细JSON直接出报告 (跳过扫描)')
    ap.add_argument('--out', default=os.path.join('tmp', 'limit_up_quality_result.json'))
    args = ap.parse_args()
    codes = [c.strip() for c in args.codes.split(',') if c.strip()]

    if args.from_json:
        with open(args.from_json, encoding='utf-8') as f:
            events = json.load(f)
        stock_info = {}
        for e in events:
            e['lu_date'] = str(e['lu_date'])[:10]
            stock_info.setdefault(e['code'], {'name': e.get('name', ''), 'circ_shares': 0})
        # JSON 里的 ufilter/upass 是扫描时的参数快照, 按 QUALITY_PARAMS 当前值重算
        for e in events:
            u = {}
            u['U1_non_st'] = (not QUALITY_PARAMS['u_st_excl']) or (not e['is_st'])
            u['U2_turnover'] = e['turnover'] is None or e['turnover'] >= QUALITY_PARAMS['u_turnover_min']
            u['U3_float_mv'] = e['float_mv'] is None or \
                (QUALITY_PARAMS['u_float_mv_min'] <= e['float_mv'] <= QUALITY_PARAMS['u_float_mv_max'])
            u['U4_heat'] = ((e['ret_20d'] is not None and e['ret_20d'] >= QUALITY_PARAMS['u_heat_ret20_min'])
                            or e['prior_lu_count'] >= QUALITY_PARAMS['u_heat_prior_lu_min'])
            if QUALITY_PARAMS.get('u_ma_slope_min') is not None:
                u['U5_ma_up'] = e['ma20_slope5'] is not None and e['ma20_slope5'] > QUALITY_PARAMS['u_ma_slope_min']
            if QUALITY_PARAMS.get('u_vol_ratio_min') is not None:
                u['U6_vol'] = e['lu_vol_ratio'] is not None and e['lu_vol_ratio'] >= QUALITY_PARAMS['u_vol_ratio_min']
            e['ufilter'] = u
            e['upass'] = all(u.values())
        print(f"从 {args.from_json} 载入 {len(events)} 事件 (过滤参数已按当前配置重算)")
        print_report(events, stock_info, args)
        return

    print(f"涨停事件质量分析 v2 | source={args.source} days={args.days} codes={'全市场' if not codes else codes}")
    t0 = time.time()
    if args.source == 'db':
        bars_by_code, stock_info = load_universe_db(args.days, codes or None)
    else:
        bars_by_code, stock_info = load_universe_api(args.days, codes or None)
    if args.limit:
        bars_by_code = dict(list(bars_by_code.items())[:args.limit])

    events = []
    no_lu = 0
    for i, (code, bars) in enumerate(bars_by_code.items()):
        info = stock_info.get(code, {})
        evs = analyze_stock_events(bars, code, info.get('name', ''), info.get('circ_shares', 0),
                                   keep_yizi=args.keep_yizi)
        if evs:
            events.extend(evs)
        elif bars and len(bars) >= 60:
            no_lu += 1
        if (i + 1) % 1000 == 0:
            print(f"  分析进度: {i+1}/{len(bars_by_code)}  事件{len(events)}")
    print(f"\n完成: {len(bars_by_code)}只票, {len(events)}个涨停事件, {no_lu}只无涨停, 耗时{time.time()-t0:.0f}s")

    if events:
        print_report(events, stock_info, args)
        os.makedirs(os.path.dirname(args.out) or '.', exist_ok=True)
        with open(args.out, 'w', encoding='utf-8') as f:
            json.dump(events, f, ensure_ascii=False, default=str)
        print(f"\n💾 明细已保存: {args.out} ({len(events)}事件)")

if __name__ == '__main__':
    main()
