"""Precision of the classifier's self and audience positives, from three blind coders on the full positive population (S5).
Usage: python3 s5_precision.py s5_sealed.csv coding_supplementary_R1.json coding_supplementary_R2.json coding_supplementary_R3.json
Reports, per axis: n positives, human-majority agreement (precision) with 95% bootstrap CI, per-coder agreement, Fleiss' kappa among coders on that axis, and the list of items the majority rejects."""
import sys, json, numpy as np, pandas as pd
z=pd.read_csv(sys.argv[1]); coders={}
for f in sys.argv[2:]: j=json.load(open(f)); coders[j['coder']]=j['answers']
names=list(coders); rng=np.random.default_rng(20260923)
def fleiss(mat):
    n=mat.sum(1)[0]; N=len(mat); p=mat.sum(0)/(N*n); P=((mat**2).sum(1)-n)/(n*(n-1)); Pe=(p**2).sum(); return (P.mean()-Pe)/(1-Pe) if Pe<1 else float('nan')
rows=[]
for ax,key in [('self','S'),('audience','A')]:
    items=[i for i in z.item_id if key in str(z.set_index('item_id').loc[i,'classifier_axes']) and all(i in coders[c] for c in names)]
    H=np.array([[coders[c][i][key] for c in names] for i in items]); maj=(H.sum(1)>len(names)/2).astype(int)
    prec=maj.mean(); boots=[rng.choice(maj,len(maj)).mean() for _ in range(2000)]; lo,hi=np.percentile(boots,[2.5,97.5])
    mat=np.array([[ (H[k]==v).sum() for v in (0,1)] for k in range(len(items))])
    rows.append(dict(axis=ax,n=len(items),precision=round(prec,3),ci=f'{lo:.3f}–{hi:.3f}',**{f'{c}_agree':round(H[:,j].mean(),3) for j,c in enumerate(names)},fleiss=round(fleiss(mat),3)))
    rej=[i for k,i in enumerate(items) if maj[k]==0]; pd.DataFrame(dict(item_id=rej)).merge(z,on='item_id').to_csv(f's5_rejected_{ax}.csv',index=False)
print(pd.DataFrame(rows).to_markdown(index=False))
