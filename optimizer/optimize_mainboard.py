import csv, itertools
from collections import defaultdict

rows = []
with open('analysis_output/dragon_ohlcv.csv', encoding='utf-8-sig') as f:
    for r in csv.DictReader(f): rows.append(r)

by_code = defaultdict(list)
for r in rows: by_code[r['code']].append(r)
for c in by_code: by_code[c].sort(key=lambda x: x['time'])

def sd(v):
    if len(v)<2: return 0
    m=sum(v)/len(v)
    return (sum((x-m)**2 for x in v)/len(v))**0.5

runs = defaultdict(list)
for r in rows: runs[(r['code'],r['run_first_limit_date'])].append(r)
first = []
seen = set()
for (code,fd),rl in runs.items():
    if code[:3].startswith(('300','301','688')): continue
    for r in rl:
        if r['time']==fd and (code,fd) not in seen:
            seen.add((code,fd)); first.append(r); break

all_feats = []
for r in first:
    code=r['code']; cl=by_code[code]; idx=None
    for i,cr in enumerate(cl):
        if cr['time']==r['time']: idx=i; break
    if idx is None or idx<6: continue
    pc=float(cl[idx-1]['close'])
    fc=float(r['close']); fo=float(r['open']); fh=float(r['high']); fl=float(r['low'])
    if pc<=0: continue
    # 停牌复牌过滤
    open_gap=(fo/pc-1) if pc>0 else 0
    if open_gap>0.196: continue  # 主板涨停阈值*2=9.8%*2
    lup=pc*1.098
    if abs(fo-lup)/lup<0.01 and (fh-fl)/pc<0.01: continue
    seal=(fc-fl)/fc*100 if fc>0 else 999
    amp=(fh-fl)/pc*100
    upper=(fh-fc)/pc*100
    rets=[]
    for j in range(max(1,idx-5),idx):
        c1=float(cl[j-1]['close']); c2=float(cl[j]['close'])
        if c1>0: rets.append(c2/c1-1)
    vol=sd(rets)*100 if len(rets)>=3 else 999
    vol_ratio=0.0
    if idx>=5:
        vols=[float(cl[j]['volume']) for j in range(idx-5,idx)]
        avg_v=sum(vols)/5
        if avg_v>0: vol_ratio=float(cl[idx]['volume'])/avg_v
    prev5_ret=0.0
    if idx>=6:
        p5c=float(cl[idx-6]['close'])
        if p5c>0: prev5_ret=(pc/p5c-1)*100
    open_ret=(fo/pc-1)*100
    c_arr=[float(x['close']) for x in cl]
    h_arr=[float(x['high']) for x in cl]
    o_arr=[float(x['open']) for x in cl]
    all_feats.append({
        'seal':seal,'vol':vol,'upper':upper,'amp':amp,
        'vol_ratio':vol_ratio,'prev5_ret':prev5_ret,'open_ret':open_ret,
        'idx':idx,'c':c_arr,'h':h_arr,'o':o_arr,
    })

print(f'主板首板: {len(all_feats)}')

def bt(fl, tp=10, sl=10, ta=5, tc=8):
    trades = []
    for f in fl:
        idx=f['idx']; bi=idx+1; c=f['c']; h=f['h']; o=f['o']
        if bi>=len(c): continue
        entry=o[bi]
        if entry<=0: continue
        peak=entry; hold=0; pnl=None
        for pos in range(bi+1, len(c)):
            hold+=1
            prev_c=c[pos-1]
            if prev_c<=0: continue
            if h[pos]>peak: peak=h[pos]
            p=(c[pos]/entry-1)*100
            if p<=-sl+0.01: pnl=p; break
            lk=prev_c*1.098
            is_lim=(lk-c[pos])/lk<0.02
            if not is_lim:
                if p>=tp: pnl=p; break
                pk=(peak/entry-1)*100
                if pk>=ta and (peak-c[pos])/entry*100>=tc: pnl=p; break
                pnl=p; break
            if hold>=20: pnl=p; break
        if pnl is None: pnl=(c[-1]/entry-1)*100
        trades.append(pnl)
    if len(trades)<5: return None
    w=[p for p in trades if p>0]; l=[p for p in trades if p<0]
    n=len(trades); wr=len(w)/n*100; avg=sum(trades)/n
    aw=sum(w)/len(w) if w else 0; al=abs(sum(l)/len(l)) if l else 0.01
    return n,wr,avg,aw/al

# 分层
for name, key, ranges in [
    ('封板%','seal',[(0,2),(2,3),(3,4),(4,5),(5,6),(6,8),(8,12)]),
    ('波动%','vol',[(0,2),(2,4),(4,6),(6,8),(8,12)]),
    ('振幅%','amp',[(0,2),(2,4),(4,6),(6,8),(8,12)]),
    ('量比','vol_ratio',[(0,1),(1,2),(2,3),(3,5),(5,10)]),
    ('开盘涨%','open_ret',[(0,2),(2,4),(4,6),(6,8),(8,12)]),
    ('前5天%','prev5_ret',[(-20,-5),(-5,0),(0,5),(5,10),(10,20)]),
]:
    print(f'\n=== {name} ===')
    for lo,hi in ranges:
        sub=[f for f in all_feats if lo<=f[key]<hi]
        r=bt(sub)
        if r: print(f'  {lo:>5}~{hi:<5}: {r[0]:>3d}笔 胜率={r[1]:.1f}% 均值={r[2]:+.2f}% 盈亏比={r[3]:.2f}')

# 组合搜索
print(f'\n=== 组合搜索 ===')
best=[]
for sm,vx in itertools.product([3,4,5,5.5,6,7,8],[3,4,5,6,8,10]):
    for vr in [1,2,3,5]:
        for am in [3,4,5,6,8]:
            sub=[f for f in all_feats if f['seal']<=sm and f['vol']<=vx and f['vol_ratio']<=vr and f['amp']<=am]
            r=bt(sub)
            if r and r[0]>=15:
                best.append((sm,vx,vr,am)+r)

best.sort(key=lambda x: -x[7])
print(f'有效: {len(best)}')
print(f'\n封板  波动  量比  振幅  | 笔数 胜率   均值   盈亏比')
for x in best[:30]:
    sm,vx,vr,am,n,wr,avg,pf=x
    print(f'{sm:>4.0f}% {vx:>4.0f}% {vr:>4.0f}  {am:>4.0f}% | {n:>3d} {wr:>5.1f}% {avg:>+6.2f}% {pf:>5.2f}')

best2=[b for b in best if b[5]>=20]
best2.sort(key=lambda x: -x[6])
print(f'\n胜率排序(n>=20):')
print(f'封板  波动  量比  振幅  | 笔数 胜率   均值   盈亏比')
for x in best2[:20]:
    sm,vx,vr,am,n,wr,avg,pf=x
    print(f'{sm:>4.0f}% {vx:>4.0f}% {vr:>4.0f}  {am:>4.0f}% | {n:>3d} {wr:>5.1f}% {avg:>+6.2f}% {pf:>5.2f}')

best3=[b for b in best if b[5]>=15]
best3.sort(key=lambda x: -x[8])
print(f'\n盈亏比排序(n>=15):')
print(f'封板  波动  量比  振幅  | 笔数 胜率   均值   盈亏比')
for x in best3[:20]:
    sm,vx,vr,am,n,wr,avg,pf=x
    print(f'{sm:>4.0f}% {vx:>4.0f}% {vr:>4.0f}  {am:>4.0f}% | {n:>3d} {wr:>5.1f}% {avg:>+6.2f}% {pf:>5.2f}')
