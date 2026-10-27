"""Round-2 analysis — pre-registered with PREREGISTRATION_round2.md. Run once, after both exports are complete.
Usage: python3 round2_analysis.py <sealed.csv> <exports_dir> <round1_dir> > round2_results.md
sealed.csv columns (from the pipeline): item_id, stratum (T1..T8), pool_size, T,P,V,S,A (classifier 0/1 where applicable),
comp_category, oracle_verdict, board_verdict, artunit_in, round1_item_id (T8 only).
exports_dir: coder exports (JSON text pasted from the tool), any filenames; round1_dir: coding_main_R2.json, coding_main_R4.json."""
import sys, json, glob, os, math, numpy as np, pandas as pd
from scipy.stats import beta
sealed=pd.read_csv(sys.argv[1],dtype=str).dropna(subset=['item_id']).set_index('item_id'); EXP=sys.argv[2]; R1=sys.argv[3]
rng=np.random.default_rng(20261027); AX='TPVSA'
ans={}  # ans[coder][item_id] = dict
for f in glob.glob(os.path.join(EXP,'*')):
    try: d=json.load(open(f))
    except Exception: continue
    if d.get('study')!='bottega-index-reliability-round2' or d['set'].startswith('p_'): continue
    ans.setdefault(d['coder'],{}).update(d['answers'])
C=sorted(ans); assert len(C)==2, f'expected two coders, got {C}'
def wilson(x,n,z=1.96):
    if n==0: return (float('nan'),)*3
    p=x/n; d=1+z*z/n; c=(p+z*z/(2*n))/d; h=z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/d; return p,c-h,c+h
def cohen(a,b):
    a=np.asarray(a);b=np.asarray(b);cats=np.union1d(a,b);po=(a==b).mean();pe=sum((a==c).mean()*(b==c).mean() for c in cats)
    return (po-pe)/(1-pe) if pe<1 else float('nan')
def boot(f,*arrs,B=10000):
    n=len(arrs[0]);v=[f(*[np.asarray(x)[ix] for x in arrs]) for ix in (rng.integers(0,n,n) for _ in range(B))];v=[x for x in v if not np.isnan(x)]
    return np.percentile(v,[2.5,97.5]) if v else (float('nan'),)*2
STR=sealed.groupby(level=0)['stratum'].apply(set)
SR=sealed.reset_index().set_index(['item_id','stratum'])
def row(i,s): r=SR.loc[(i,s)]; return r.iloc[0] if isinstance(r,pd.DataFrame) else r
def items(s): return [i for i in STR.index if s in STR[i] and all(i in ans[c] for c in C)]
out=[]; P=out.append
P('# Round 2 results (pre-registered analysis)\n'); P(f'Coders: {C}\n')
# 1 venture precision
I=items('T1'); both=sum(all(ans[c][i]['V'] for c in C) for i in I); eith=sum(any(ans[c][i]['V'] for c in C) for i in I)
p,lo,hi=wilson(both,len(I)); P(f'## 1. Venture precision (T1, n={len(I)})\nboth coders: {both}/{len(I)} = {p:.3f} [{lo:.3f}, {hi:.3f}]; either coder: {eith}/{len(I)}')
for c in C: x=sum(ans[c][i]['V'] for i in I); P(f'- {c}: {x}/{len(I)} = {x/len(I):.3f}')
# 2 recall among negatives — Horvitz–Thompson over T2a (by Haiku sub-stratum) and T2b, using inclusion probabilities
P('\n## 2. Recall among classifier negatives (T2a + T2b), Horvitz–Thompson with inclusion probabilities')
neg=[(i,s) for s in ('T2a','T2b') for i in items(s)]
w=np.array([1/float(row(i,s)['inclusion_prob']) for i,s in neg]); sub=np.array([row(i,s)['substratum'] for i,s in neg])
for k in AX:
    y=np.array([int(all(ans[c][i][k] for c in C)) for i,s in neg]); est=(w*y).sum()
    bs=[]
    for _ in range(5000):
        ix=np.concatenate([rng.choice(np.where(sub==u)[0],(sub==u).sum()) for u in np.unique(sub)]); bs.append((w[ix]*y[ix]).sum())
    lo,hi=np.percentile(bs,[2.5,97.5]); rows=', '.join(f'{u}: {int(y[sub==u].sum())}/{int((sub==u).sum())}' for u in np.unique(sub))
    P(f'- {k}: estimated missed ≈ {est:.0f} [{lo:.0f}, {hi:.0f}]  ({rows})')
P('(Recall per axis = released count ÷ (released count + estimated missed). A stratum with zero hits contributes zero to the point estimate; its Wilson upper bound × pool is reported in the write-up as the upper bound on misses there.)')
# 3 composition
I=items('T3'); a=[ans[C[0]][i]['cat'] for i in I]; b=[ans[C[1]][i]['cat'] for i in I]; k=cohen(a,b); ci=boot(cohen,a,b)
P(f'\n## 3. Composition (T3, n={len(I)})\ncoder–coder κ {k:.2f} [{ci[0]:.2f}, {ci[1]:.2f}]; raw agreement {np.mean(np.array(a)==np.array(b)):.3f}')
MAP={'1':'studio','2':'history_theory','3':'independent','4':'capstone','5':'technology','6':'practice','7':'liberal_arts','8':'pedagogy','9':'venture','0':'self','Q':'audience','W':'other'}
clf=[row(i,'T3')['comp_category'] for i in I]; agree=[i for i,x,y in zip(I,a,b) if x==y]
P(f'both-agree label vs classifier: {sum(MAP.get(ans[C[0]][i]["cat"])==row(i,'T3')['comp_category'] for i in agree)}/{len(agree)}')
for c in C: P(f'- {c} vs classifier κ {cohen([MAP.get(ans[c][i]["cat"],"x") for i in I],clf):.2f}')
# 4 outside unit
I=items('T4'); conf=[i for i in I if all(ans[c][i]['ent']=='Y' for c in C)]; p,lo,hi=wilson(len(conf),len(I))
P(f'\n## 4. Outside-unit venture matches (T4, n={len(I)})\nprecision (both Y): {len(conf)}/{len(I)} = {p:.3f} [{lo:.3f}, {hi:.3f}]')
for q,labels in (('orient',{'C':'arts/creative','G':'general business','M':'mixed','U':'cannot tell'}),('restr',{'R':'states restriction','O':'states open','S':'silent'})):
    ag=[i for i in conf if ans[C[0]][i][q]==ans[C[1]][i][q]]
    P(f'- {q} (confirmed courses where coders agree, n={len(ag)}): '+', '.join(f'{v} {sum(ans[C[0]][i][q]==k for i in ag)}' for k,v in labels.items())+f'; coder agreement {len(ag)}/{len(conf)}')
P('  The restriction share is a lower bound on restricted access; silence is not read as openness.')
# 5 oracle and 6 board, from five-axis marks; 7 art unit
def vp(a): return 'E' if a.get('E') else 'V' if a.get('V') else 'P' if a.get('P') else 'N'
for s,ref,name,mapref in (('T5','oracle_verdict','Oracle',lambda r:{'venture':'V','practice':'P','none':'N','either':'E'}.get(str(r).lower(),'X')),
                          ('T6','board_verdict','Industry-role board',lambda r:{'KEEP':'V','REMOVE':'R','BORDERLINE':'B'}.get(str(r).upper(),'X'))):
    I=items(s); r=[mapref(row(i,s)[ref]) for i in I]
    lab={c:[vp(ans[c][i]) for i in I] for c in C}
    if s=='T6': lab={c:['V' if x=='V' else 'R' for x in v] for c,v in lab.items()}
    dec=[k for k,x in enumerate(r) if x in ('V','P','N','R')]
    P(f'\n## {name} ({s}, n={len(I)}; decided rows {len(dec)})\ncoder–coder κ {cohen(lab[C[0]],lab[C[1]]):.2f}; '+'; '.join(f'{c} vs author κ {cohen([lab[c][k] for k in dec],[r[k] for k in dec]):.2f}, agreement {np.mean([lab[c][k]==r[k] for k in dec]):.3f}' for c in C))
    both=[(lab[C[0]][k],r[k]) for k in dec if lab[C[0]][k]==lab[C[1]][k]]; P(f'both-agree label matches author on {sum(x==y for x,y in both)}/{len(both)}')
I=items('T7'); a=[ans[C[0]][i]['in'] for i in I]; b=[ans[C[1]][i]['in'] for i in I]; r=['Y' if str(row(i,'T7')['artunit_in']).lower() in ('1','true','yes','in') else 'N' for i in I]
P(f'\n## Art unit (T7, n={len(I)})\ncoder–coder κ {cohen(a,b):.2f}; '+'; '.join(f'{c} vs pipeline κ {cohen(x,r):.2f}' for c,x in zip(C,(a,b))))
# 8 test-retest
P('\n## 8. Test–retest (T8)')
for c in C:
    r1=json.load(open(os.path.join(R1,f'coding_main_{c}.json')))['answers']; I=items('T8')
    P(f'- {c}: '+', '.join(f'{k} {np.mean([ans[c][i][k]==r1[row(i,'T8')['round1_item_id']][k] for i in I]):.2f}' for k in AX))
print('\n'.join(out))
