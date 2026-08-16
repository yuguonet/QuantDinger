"""
D0正样本全维度数据提取
从数据库加载120天K线, 计算筹码分布, 输出JSON
"""
import json, sys, os
from datetime import datetime, timedelta
from collections import defaultdict
import numpy as np

_backend_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backend_api_python")
if _backend_root not in sys.path:
    sys.path.insert(0, _backend_root)

def _load_env():
    try:
        from dotenv import load_dotenv
        for p in [os.path.join(_backend_root, '.env'),
                  os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')]:
            if os.path.isfile(p):
                load_dotenv(p, override=False)
                break
    except Exception:
        pass
_load_env()

_pool_cache = None
def _get_pool():
    global _pool_cache
    if _pool_cache is not None:
        return _pool_cache
    from app.utils.db_market import get_market_db_manager
    _pool_cache = get_market_db_manager()._get_pool("CNStock")
    return _pool_cache

_writer_cache = None
def _get_writer():
    global _writer_cache
    if _writer_cache is not None:
        return _writer_cache
    from app.utils.db_market import get_market_kline_writer
    _writer_cache = get_market_kline_writer()
    return _writer_cache


def load_kline(code, days=300):
    from app.data_sources.provider.adjustment import unadj_to_qfq
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=int(days*1.5))).strftime("%Y-%m-%d")
    try:
        writer = _get_writer()
        data = writer.query("CNStock", code, "1D", start_time=start, end_time=end, limit=0)
        if not data: return []
        bars = [{"time":str(r["time"])[:10],"open":float(r["open"]),"high":float(r["high"]),
                 "low":float(r["low"]),"close":float(r["close"]),"volume":float(r["volume"])} for r in data]
        return unadj_to_qfq(bars, code)
    except: return []


def calc_chip(bars, lookback=120):
    """筹码分布计算 (120天K线)"""
    if len(bars) < 30: return {}
    bars = bars[-lookback:] if len(bars) > lookback else bars
    closes=[b['close'] for b in bars]; highs=[b['high'] for b in bars]
    lows=[b['low'] for b in bars]; volumes=[b['volume'] for b in bars]
    current=closes[-1]
    pmin,pmax=min(lows),max(highs)
    if pmax<=pmin: return {}
    bw=max((pmax-pmin)/80, 0.01)
    buckets=int((pmax-pmin)/bw)+1
    chip=[0.0]*buckets
    n=len(bars)
    for i in range(n):
        lo,hi,cl,vol=lows[i],highs[i],closes[i],volumes[i]
        if hi<=lo or vol<=0: continue
        decay=0.98**(n-1-i)
        lh=max(cl-lo,0.001); rh=max(hi-cl,0.001)
        steps=max(int((hi-lo)/bw)+1,10)
        tw=0; ws=[]
        for j in range(steps+1):
            p=lo+(hi-lo)*j/steps
            dist=((cl-p)/lh if p<=cl else (p-cl)/rh)
            w=max(1-dist,0); ws.append((p,w)); tw+=w
        if tw<=0: continue
        for p,w in ws:
            idx=int((p-pmin)/bw); idx=max(0,min(idx,buckets-1))
            chip[idx]+=vol*decay*w/tw
    total=sum(chip)
    if total<=0: return {}
    prices=[pmin+i*bw for i in range(buckets)]
    avg_cost=sum(prices[i]*chip[i] for i in range(buckets))/total
    profit=sum(chip[i] for i in range(buckets) if prices[i]<=current)/total
    cum=[]; acc=0
    for d in chip: acc+=d; cum.append(acc/total)
    li=0; ui=buckets-1
    for i in range(buckets):
        if cum[i]>=0.05: li=i; break
    for i in range(buckets-1,-1,-1):
        if cum[i]<=0.95: ui=i; break
    c90w=(prices[ui]-prices[li])/avg_cost if avg_cost>0 else 0
    l70=0; u70=buckets-1
    for i in range(buckets):
        if cum[i]>=0.15: l70=i; break
    for i in range(buckets-1,-1,-1):
        if cum[i]<=0.85: u70=i; break
    c70w=(prices[u70]-prices[l70])/avg_cost if avg_cost>0 else 0
    return {'avg_cost':round(avg_cost,2),'profit_ratio':round(profit,4),
            'conc90_width':round(c90w*100,1),'conc70_width':round(c70w*100,1),
            'price_vs_cost':round((current/avg_cost-1)*100,2) if avg_cost>0 else 0}


def calc_ma5_angle(closes, period=5, days=3):
    if len(closes)<period+days: return None
    ma=[sum(closes[i-period+1:i+1])/period for i in range(period-1,len(closes))]
    if len(ma)<days: return None
    recent=ma[-days:]; n=days
    sx=n*(n-1)/2; sy=sum(recent); sxy=sum(i*recent[i] for i in range(n))
    sx2=n*(n-1)*(2*n-1)/6; denom=n*sx2-sx*sx
    slope=(n*sxy-sx*sy)/denom if denom else 0
    return slope/recent[-1]*100 if recent[-1] else None


def calc_rsi(closes, period=14):
    if len(closes)<period+1: return None
    g,l=[],[]
    for i in range(1,len(closes)):
        d=closes[i]-closes[i-1]; g.append(max(d,0)); l.append(max(-d,0))
    ag=sum(g[:period])/period; al=sum(l[:period])/period
    for i in range(period,len(g)):
        ag=(ag*(period-1)+g[i])/period; al=(al*(period-1)+l[i])/period
    return 100-100/(1+ag/al) if al>0 else 100


def calc_kdj_k(closes, highs, lows, period=9):
    if len(closes)<period: return None
    rsvs=[]
    for i in range(period-1,len(closes)):
        hn=max(highs[i-period+1:i+1]); ln=min(lows[i-period+1:i+1]); c=closes[i]
        rsvs.append((c-ln)/(hn-ln)*100 if hn!=ln else 50)
    k=50
    for r in rsvs: k=2/3*k+1/3*r
    return k


def extract_one(item, dragon_intervals, kline_cache):
    code=item['stock_code']
    d0_date=item['d0_date']
    d0_info=item['d0_info']
    window=item['window']
    kb=window.get('klines_before',[])
    kline_d0=window.get('kline_d0',{})
    if len(kb)<20: return None

    closes=[k['close'] for k in kb]; highs=[k['high'] for k in kb]
    lows=[k['low'] for k in kb]; vols=[k['volume'] for k in kb]

    ma5=np.mean(closes[-5:]); ma10=np.mean(closes[-10:]); ma20=np.mean(closes)
    ma_bull=1 if ma5>ma10>ma20 else 0
    angle=calc_ma5_angle(closes,5,3)
    angle_5d=calc_ma5_angle(closes,5,5)
    rsi14=calc_rsi(closes,14); rsi6=calc_rsi(closes,6)
    kdj_k=calc_kdj_k(closes,highs,lows,9)
    h20=max(highs); l20=min(lows)
    pos=(closes[-1]-l20)/(h20-l20)*100 if h20>l20 else 50
    chg5=(closes[-1]/closes[-5]-1)*100; chg10=(closes[-1]/closes[-10]-1)*100; chg20=(closes[-1]/closes[-20]-1)*100
    vol5=np.mean(vols[-5:]); vol10=np.mean(vols[-10:]); vol20=np.mean(vols)
    vol_ratio_5_10=vol5/vol10 if vol10>0 else 1
    vol_ratio_5_20=vol5/vol20 if vol20>0 else 1
    vol_shrink=1 if vol5<vol10 else 0
    d0_chg=kline_d0.get('change_pct',d0_info.get('change_percent',0))
    d0_is_limit=1 if d0_chg>=9.5 else 0
    d0_vol=kline_d0.get('volume',0)
    d0_vol_ratio=d0_vol/vol5 if vol5>0 else 1
    d1=kb[-1]; d1_chg=d1.get('change_pct',0)
    d1_vol_ratio=d1['volume']/vol5 if vol5>0 else 1
    streak=0
    for k in reversed(kb):
        if k.get('change_pct',0)>0: streak+=1
        else: break
    limit_cnt=sum(1 for k in kb if k.get('change_pct',0)>=9.5)

    # 筹码: 从数据库加载120天K线计算
    chip={}
    if code in kline_cache:
        full_bars = kline_cache[code]
    else:
        full_bars = load_kline(code, 300)
        kline_cache[code] = full_bars
    if full_bars:
        # 截取到D0日期(含)
        d0_bars = [b for b in full_bars if b['time'] <= d0_date]
        if len(d0_bars) >= 60:
            chip = calc_chip(d0_bars, lookback=120)

    intervals=dragon_intervals.get(code,{})
    c=str(code)[:3]
    if c.startswith('68'): board='科创板'
    elif c.startswith('30'): board='创业板'
    elif c.startswith('6'): board='沪主板'
    else: board='深主板'

    return {
        'code':code,'name':item['stock_name'],'board':board,'d0_date':d0_date,
        'ma5':round(ma5,3),'ma10':round(ma10,3),'ma20':round(ma20,3),'ma_bull':ma_bull,
        'angle_3d':round(angle,3) if angle else None,
        'angle_5d':round(angle_5d,3) if angle_5d else None,
        'rsi14':round(rsi14,1) if rsi14 else None,
        'rsi6':round(rsi6,1) if rsi6 else None,
        'kdj_k':round(kdj_k,1) if kdj_k else None,
        'position':round(pos,1),
        'chg5':round(chg5,2),'chg10':round(chg10,2),'chg20':round(chg20,2),
        'vol_ratio_5_10':round(vol_ratio_5_10,3),'vol_ratio_5_20':round(vol_ratio_5_20,3),
        'vol_shrink':vol_shrink,
        'd0_chg':round(d0_chg,2),'d0_is_limit':d0_is_limit,
        'd0_vol_ratio':round(d0_vol_ratio,2),
        'd1_chg':round(d1_chg,2),'d1_vol_ratio':round(d1_vol_ratio,2),
        'up_streak':streak,'limit_cnt_20d':limit_cnt,
        'chip_avg_cost':chip.get('avg_cost'),
        'chip_profit_ratio':chip.get('profit_ratio'),
        'chip_conc90_width':chip.get('conc90_width'),
        'chip_conc70_width':chip.get('conc70_width'),
        'chip_price_vs_cost':chip.get('price_vs_cost'),
        'dragon_count':intervals.get('count',0),
        'dragon_median_gap':intervals.get('median_gap',0),
        'dragon_avg_gap':intervals.get('avg_gap',0),
        'net_amount_wan':d0_info.get('net_amount_wan',0),
        'buy_amount_wan':d0_info.get('buy_amount_wan',0),
    }


def main():
    print("加载D0数据...",file=sys.stderr)
    with open('d0_data.json',encoding='utf-8') as f:
        data=json.load(f)['data']
    print(f"  {len(data)}条",file=sys.stderr)

    print("计算周期统计...",file=sys.stderr)
    dragon_intervals={}
    all_recs=defaultdict(list)
    for item in data:
        code=item['stock_code']
        for rec in item.get('all_dragon_records',[]):
            all_recs[code].append(rec['trade_date'])
        if item['d0_date'] not in all_recs[code]:
            all_recs[code].append(item['d0_date'])
    for code,dates in all_recs.items():
        dates=sorted(set(dates))
        if len(dates)>=2:
            gaps=[(datetime.strptime(dates[i],'%Y-%m-%d')-datetime.strptime(dates[i-1],'%Y-%m-%d')).days for i in range(1,len(dates))]
            dragon_intervals[code]={'count':len(dates),'median_gap':float(np.median(gaps)),'avg_gap':float(np.mean(gaps))}

    kline_cache={}
    print("提取全维度特征 (含筹码, 需加载K线, 较慢)...",file=sys.stderr)
    results=[]
    for i,item in enumerate(data):
        if (i+1)%100==0:
            print(f"  进度: {i+1}/{len(data)} (K线缓存{len(kline_cache)}只)",file=sys.stderr)
        r=extract_one(item, dragon_intervals, kline_cache)
        if r:
            results.append(r)

    print(f"完成: {len(results)}条",file=sys.stderr)

    outfile='d0_features.json'
    with open(outfile,'w',encoding='utf-8') as f:
        json.dump(results,f,ensure_ascii=False,indent=2)
    print(f"💾 {outfile} ({len(results)}条)",file=sys.stderr)

    # 快速统计
    print(f"\n快速统计:")
    print(f"  样本: {len(results)}")
    print(f"  多头: {sum(1 for r in results if r['ma_bull'])} ({sum(1 for r in results if r['ma_bull'])/len(results)*100:.1f}%)")
    print(f"  D0涨停: {sum(1 for r in results if r['d0_is_limit'])}")
    chip_ok = sum(1 for r in results if r.get('chip_profit_ratio') is not None)
    print(f"  筹码数据: {chip_ok}/{len(results)}")
    if chip_ok > 0:
        profits = [r['chip_profit_ratio'] for r in results if r.get('chip_profit_ratio') is not None]
        print(f"  获利比均值: {np.mean(profits):.3f}")
        conc = [r['chip_conc90_width'] for r in results if r.get('chip_conc90_width') is not None]
        if conc:
            print(f"  90%宽度均值: {np.mean(conc):.1f}%")


if __name__=='__main__':
    main()
