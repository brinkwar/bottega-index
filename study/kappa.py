"""Inter-rater reliability for the Bottega Index coding study.
Usage: python3 kappa.py sealed_verdicts.csv coding_<set>_<coder>.json [more coder json...] > report.md
Reports per axis (T,P,V,S,A) and per stratum: Fleiss' kappa across coders; pairwise Cohen's kappa
coder-coder and coder-classifier with PABAK; majority-of-humans vs classifier confusion; V/P/none 3-way matrix;
E and X rates per coder; median seconds per item. Writes disagreements.csv beside the report."""
import sys, json, itertools, pandas as pd, numpy as np
sealed=pd.read_csv(sys.argv[1]).set_index('item_id')
coders={}
for f in sys.argv[2:]:
    j=json.load(open(f)); coders[j['coder']]=j
AX=['T','P','V','S','A']
ids=sorted(set.intersection(*[set(j['answers']) for j in coders.values()])&set(sealed.index))
def cohen(a,b):
    a=np.asarray(a);b=np.asarray(b);po=(a==b).mean();cats=set(a)|set(b)
    pe=sum(((a==c).mean())*((b==c).mean()) for c in cats); return (po-pe)/(1-pe) if pe<1 else float('nan'), po
def pabak(po,k=2): return (k*po-1)/(k-1)
def fleiss(mat):  # mat: items x categories counts
    n=mat.sum(1)[0]; N=len(mat); p=mat.sum(0)/(N*n); P=((mat**2).sum(1)-n)/(n*(n-1)); Pb=P.mean(); Pe=(p**2).sum()
    return (Pb-Pe)/(1-Pe) if Pe<1 else float('nan')
names=list(coders); out=[]
out.append(f"# Reliability report — {len(ids)} items, coders {', '.join(names)}\n")
# rates
out.append("## Coder behaviour\n| coder | items | E rate | X rate | median s/item |\n|---|---|---|---|---|")
for c,j in coders.items():
    A=[j['answers'][i] for i in ids]; out.append(f"| {c} | {len(A)} | {np.mean([a['E'] for a in A]):.2f} | {np.mean([a['X'] for a in A]):.2f} | {np.median([a['ms'] for a in A])/1000:.0f} |")
out.append("")
disagree=[]
def axis_block(sub_ids,label):
    out.append(f"## {label} (n={len(sub_ids)})\n| axis | Fleiss κ (humans) | " + " | ".join(f"{a}–{b} κ (PABAK)" for a,b in itertools.combinations(names,2)) + " | " + " | ".join(f"{c}–model κ (PABAK)" for c in names) + " | majority–model κ (PABAK) | model + / human-majority + |\n|" + "---|"*(2+len(list(itertools.combinations(names,2)))+len(names)+2))
    for ax in AX:
        H={c:[coders[c]['answers'][i][ax] for i in sub_ids] for c in names}; M=[int(sealed.loc[i,ax]) for i in sub_ids]
        mat=np.array([[sum(H[c][k]==v for c in names) for v in (0,1)] for k in range(len(sub_ids))])
        fk=fleiss(mat) if len(names)>1 else float('nan')
        pw=[]; 
        for a,b in itertools.combinations(names,2): k,po=cohen(H[a],H[b]); pw.append(f"{k:.2f} ({pabak(po):.2f})")
        cm=[]; 
        for c in names: k,po=cohen(H[c],M); cm.append(f"{k:.2f} ({pabak(po):.2f})")
        maj=[int(sum(H[c][k] for c in names)>len(names)/2) for k in range(len(sub_ids))]; k,po=cohen(maj,M)
        out.append(f"| {ax} | {fk:.2f} | "+" | ".join(pw)+" | "+" | ".join(cm)+f" | {k:.2f} ({pabak(po):.2f}) | {sum(M)} / {sum(maj)} |")
        for idx,i in enumerate(sub_ids):
            if maj[idx]!=M[idx] or len(set(H[c][idx] for c in names))>1:
                disagree.append(dict(item_id=i,stratum=sealed.loc[i,'stratum'],axis=ax,model=M[idx],**{c:H[c][idx] for c in names},majority=maj[idx],code=sealed.loc[i,'code'],title=sealed.loc[i,'title']))
    out.append("")
axis_block(ids,"All items")
for s in sorted(sealed.loc[ids,'stratum'].unique()): axis_block([i for i in ids if sealed.loc[i,'stratum']==s],f"Stratum {s}")
# 3-way V/P/none
def vpn(a): return 'V' if a['V'] else 'P' if a['P'] else 'N'
out.append("## Venture / practice / none, three-way\n")
for c in names:
    H=[vpn(coders[c]['answers'][i]) for i in ids]; M=['V' if sealed.loc[i,'V'] else 'P' if sealed.loc[i,'P'] else 'N' for i in ids]
    k,po=cohen(H,M); ct=pd.crosstab(pd.Series(H,name=f'{c}'),pd.Series(M,name='model'))
    out.append(f"**{c} vs model**: κ={k:.2f}, agreement={po:.2f}\n\n{ct.to_markdown()}\n")
if len(names)>1:
    for a,b in itertools.combinations(names,2):
        Ha=[vpn(coders[a]['answers'][i]) for i in ids]; Hb=[vpn(coders[b]['answers'][i]) for i in ids]; k,po=cohen(Ha,Hb)
        out.append(f"**{a} vs {b}**: κ={k:.2f}, agreement={po:.2f}\n")
pd.DataFrame(disagree).to_csv('disagreements.csv',index=False)
print("\n".join(out)); print(f"\n_{len(disagree)} axis-level disagreements written to disagreements.csv_")
