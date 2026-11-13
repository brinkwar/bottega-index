import pandas as pd, numpy as np, re, json, matplotlib
matplotlib.use('Agg'); import matplotlib.pyplot as plt
from scipy.stats import pointbiserialr, spearmanr
H='/home/claude/handoff9'; O='/home/claude/figs'
INK='#2A2925'; ACC='#C8853F'; G1='#6E6B66'; G2='#A9A6A0'; G3='#D6D3CD'; NAVY='#0B1524'; BAR_L='#C4C1BB'; BAR_XL='#E6E3DD'
from matplotlib.patches import Patch
plt.rcParams.update({'font.family':'DejaVu Sans','font.size':9,'axes.edgecolor':INK,'axes.labelcolor':INK,'xtick.color':INK,'ytick.color':INK,'axes.spines.top':False,'axes.spines.right':False,'savefig.dpi':300})
def save(fig,name): fig.savefig(f'{O}/{name}.png',bbox_inches='tight',facecolor='white'); plt.close(fig)
idx=pd.read_csv(f'{H}/pipeline/index.csv'); r=idx[idx.ranked==True].copy()
cen=pd.read_csv(f'{H}/study_sample/census_samples/census_confirmed_courses.csv',dtype=str); add=cen.groupby('unitid').size()
r['v2']=r.count_venture+r.unitid.astype(str).map(add).fillna(0).astype(int)
comp=pd.read_csv(f'{H}/pipeline/composition_courses.csv'); comp=comp[comp.unitid.isin(r.unitid)]
def lvl(code):
    m=re.search(r'(\d{3,4})',str(code));
    return None if not m else ('Graduate' if int(m.group(1)[0])>=5 else 'Undergraduate')
comp['level']=comp.code.map(lvl)
grp={'studio':'Making and understanding the work','history_theory':'Making and understanding the work','independent':'Making and understanding the work','capstone':'Making and understanding the work',
     'technology':'Tools and employment','practice':'Tools and employment','professional_practice':'Tools and employment',
     'venture':'Living from the work','self':'Living from the work','audience':'Living from the work'}
comp['group']=comp.bucket.map(grp).fillna('Liberal arts, pedagogy, other')
print('buckets',sorted(comp.bucket.unique()))
order=['Making and understanding the work','Tools and employment','Liberal arts, pedagogy, other','Living from the work']
cols=[BAR_L,G1,BAR_XL,ACC]
rows={'All courses':comp,'Undergraduate':comp[comp.level=='Undergraduate'],'Graduate':comp[comp.level=='Graduate']}
sh=pd.DataFrame({k:v.group.value_counts(normalize=True).reindex(order).fillna(0)*100 for k,v in rows.items()}).T
print(sh.round(1))
# F1
fig,ax=plt.subplots(figsize=(6.5,2.4)); left=np.zeros(3)
for g,c in zip(order,cols):
    ax.barh(list(sh.index)[::-1],sh[g].values[::-1],left=left[::-1] if False else None) if False else None
y=np.arange(3)[::-1]; left=np.zeros(3)
for g,c in zip(order,cols):
    v=sh[g].values; ax.barh(y,v,left=left,color=c,edgecolor='white',height=0.6,label=g)
    for yi,vi,li in zip(y,v,left):
        if vi>=4 and g in ('Making and understanding the work','Tools and employment'): ax.text(li+vi/2,yi,f'{vi:.0f}%',ha='center',va='center',color='white' if c in (G1,ACC) else INK,fontsize=8)
        elif g=='Living from the work': ax.text(li+vi+0.6,yi,f'{vi:.1f}%',ha='left',va='center',color=ACC,fontsize=8,fontweight='bold')
    left+=v
ax.set_yticks(y); ax.set_yticklabels(list(sh.index)); ax.set_xlim(0,100); ax.set_xlabel('Share of art-unit courses by primary subject (%)')
ax.legend(ncol=2,frameon=False,loc='upper center',bbox_to_anchor=(0.5,-0.32),fontsize=8); save(fig,'Figure1_composition')
# F2 measures named vs corrected
M=['Technology','Employment','Venture','Audience','Self']; named=[9.15,4.10,0.49,0.42,0.27]; corr=[9.7,5.2,1.8,0.56,0.42]; lo=[7.1,3.4,0.7,0.49,0.33]; hi=[12.2,7.0,3.2,0.64,0.52]
fig,ax=plt.subplots(figsize=(6.5,2.8)); x=np.arange(5); w=0.36
ax.bar(x-w/2,named,w,color=BAR_L)
ax.bar(x+w/2,corr,w,color=[G1,G1,ACC,ACC,ACC])
ax.errorbar(x+w/2,corr,yerr=[np.array(corr)-np.array(lo),np.array(hi)-np.array(corr)],fmt='none',ecolor=INK,capsize=3,lw=0.8)
for xi,v in zip(x+w/2,corr): ax.text(xi,hi[list(x+w/2).index(xi)]+0.25,f'{v:g}%',ha='center',fontsize=8)
ax.set_xticks(x); ax.set_xticklabels(M); ax.set_ylabel('Share of art-unit courses (%)'); ax.legend(handles=[Patch(color=BAR_L,label='Named by the instrument'),Patch(color=G1,label='Corrected: technology and employment'),Patch(color=ACC,label='Corrected: living from the work')],frameon=False,fontsize=8)
save(fig,'Figure2_measures')
# F3 Lorenz
def lorenz(v):
    v=np.sort(np.asarray(v,float))[::-1]; c=np.cumsum(v)/v.sum(); return np.r_[0,np.arange(1,len(v)+1)/len(v)],np.r_[0,c]
fig,ax=plt.subplots(figsize=(4.6,4.2))
for col,lab,c,ls in [('count_technology','Technology',G2,'-'),('count_practice','Employment',G1,'--'),('v2','Venture (reviewed)',ACC,'-'),('count_audience','Audience',INK,':'),('count_self','Self',INK,'-.')]:
    a,b=lorenz(r[col]); ax.plot(a*100,b*100,color=c,ls=ls,lw=1.6,label=lab)
ax.plot([0,100],[0,100],color=G3,lw=0.8); ax.axvline(20,color=G3,lw=0.8)
ax.set_xlabel('Programs, ranked by number of courses on the measure (%)'); ax.set_ylabel('Cumulative share of courses (%)')
ax.legend(frameon=False,fontsize=8,loc='lower right'); ax.text(21,5,'top fifth\nof programs',fontsize=7,color=G1)
save(fig,'Figure3_concentration')
# F4 inside vs outside
fig,ax=plt.subplots(figsize=(6.5,2.2))
ax.barh([1],[337],color=ACC,height=0.55); ax.text(337+15,1,'337 venture courses inside art units',va='center',fontsize=8)
l=0
for v,c,lab in [(355,ACC,'arts or\ncreative\n355'),(444,'#E3B98A','mixed\n444'),(911,G1,'general business\n911')]:
    ax.barh([0],[v],left=l,color=c,height=0.6,edgecolor='white'); ax.text(l+v/2,0,lab,ha='center',va='center',fontsize=7,color='white' if c in (ACC,G1) else INK); l+=v
ax.text(l+20,0,'≥1,710 outside\nthe art unit',va='center',fontsize=8)
ax.set_yticks([1,0]); ax.set_yticklabels(['Inside','Outside']); ax.set_xlim(0,2150); ax.set_xlabel('Entrepreneurship courses at the 221 whole-catalog universities (named)')
save(fig,'Figure4_inside_outside')
# F5 rankings
t=open(f'{H}/pipeline/usnews_gap_stage16b.txt').read()
u=pd.DataFrame(re.findall(r'#(\d+)\s+\d+\s+(.+?)\s{2,}[A-Z]{2}\s+(\w[\w ]*)',t),columns=['usn','school','status']); u['usn']=u.usn.astype(int)
norm=lambda s: re.sub(r'[^a-z]','',s.lower().replace('university','').replace('the',''))
r['k']=r.name.map(norm); u['k']=u.school.map(norm); m=u.merge(r,on='k'); m['vs']=m.v2/m.n_art_courses*100
fig,ax=plt.subplots(figsize=(6.5,3.0))
z=m[m.v2==0]; nz=m[m.v2>0]
ax.scatter(nz.usn,nz.vs,s=14,color=ACC,edgecolor='none',alpha=0.9)
ax.scatter(z.usn,z.vs,s=16,facecolors='none',edgecolors=G1,linewidths=0.8)
ax.set_xlabel('U.S. News fine-arts rank (1 = highest)'); ax.set_ylabel('Venture share of art-unit courses (%)')
pass
save(fig,'Figure5_rankings')
print('rank n',len(m))
# F6 FRBNY
s=pd.read_csv(f'{H}/research/nyfed/fine_arts_series.csv'); s['yr']=s.release_date.str[:4].astype(int)
fig,ax=plt.subplots(figsize=(6.5,2.8))
ax.plot(s.yr,s.fine_arts_underemployment_pct,'-o',color=ACC,lw=1.8,ms=4,label='Fine arts')
ax.plot(s.yr,s.all_majors_underemployment_pct,'-o',color=G1,lw=1.4,ms=3.5,label='All majors')
for _,row in s.iterrows(): ax.text(row.yr,row.fine_arts_underemployment_pct+1.0,f"{int(row.fine_arts_underemployment_rank_of_73)}",ha='center',fontsize=7,color=ACC)
ax.set_ylim(30,65); ax.set_ylabel('Recent graduates underemployed (%)'); ax.set_xlabel('Release year (2021 release not available)')
ax.set_xticks(s.yr); ax.legend(frameon=False,fontsize=8,loc='lower left'); ax.text(0.99,0.03,'labels: fine arts rank among 73 majors',transform=ax.transAxes,ha='right',fontsize=7,color=G1)
save(fig,'Figure6_underemployment')
# ---------- appendix figures
# AF1 histogram venture per program (reviewed)
v=r.v2.values; bins=[0,1,2,3,5,10,20,100]; labs=['0','1','2','3–4','5–9','10–19','20+']
cnt=[((v>=bins[i])&(v<bins[i+1])).sum() for i in range(len(bins)-1)]
fig,ax=plt.subplots(figsize=(6.5,2.6)); ax.bar(labs,cnt,color=[ACC]+[BAR_L]*6)
for i,c in enumerate(cnt): ax.text(i,c+1.5,str(c),ha='center',fontsize=8)
ax.set_xlabel('Venture courses in the art unit (reviewed count)'); ax.set_ylabel('Programs'); save(fig,'FigureH1_venture_distribution')
print('hist',dict(zip(labs,cnt)))
# AF2 sector composition
ij=pd.read_csv(f'{H}/paper/ipeds_join.csv'); sec=dict(zip(ij.unitid,ij.sector))
comp['sector']=comp.unitid.map(sec).map({'public':'Public','private nonprofit':'Private nonprofit','private for-profit':'Private for-profit'})
ss=pd.crosstab(comp.sector,comp.group,normalize='index').reindex(columns=order)*100
print(ss.round(1)); print(comp.groupby('sector').unitid.nunique())
fig,ax=plt.subplots(figsize=(6.5,2.4)); left=np.zeros(len(ss)); yy=np.arange(len(ss))[::-1]
for g,c in zip(order,cols):
    vv=ss[g].values; ax.barh(yy,vv,left=left,color=c,edgecolor='white',height=0.6,label=g)
    for yi,vi,li in zip(yy,vv,left):
        if vi>=4 and g in ('Making and understanding the work','Tools and employment'): ax.text(li+vi/2,yi,f'{vi:.0f}%',ha='center',va='center',fontsize=8,color='white' if c in (G1,ACC) else INK)
        elif g=='Living from the work': ax.text(100.6,yi,f'{vi:.1f}%',va='center',fontsize=8,color=ACC,fontweight='bold')
    left+=vv
ns=comp.groupby('sector').unitid.nunique()
ax.set_yticks(yy); ax.set_yticklabels([f'{i} ({ns[i]})' for i in ss.index]); ax.set_xlim(0,100); ax.set_xlabel('Share of art-unit courses by primary subject (%); programs in parentheses')
ax.legend(ncol=2,frameon=False,loc='upper center',bbox_to_anchor=(0.5,-0.32),fontsize=8); save(fig,'FigureH2_sector_composition')
# AF3 capstones
fig,ax=plt.subplots(figsize=(6.5,2.6)); cats=['Exhibition or audience','Career or professional','Business, funding, or venture']; ug=[26,27,8]; gr=[20,14,5]; x=np.arange(3)
ax.bar(x-0.18,ug,0.36,color=BAR_L,label='Undergraduate capstones (2,255)'); ax.bar(x+0.18,gr,0.36,color=ACC,label='Graduate capstones and theses (1,287)')
for xi,a,b in zip(x,ug,gr): ax.text(xi-0.18,a+0.6,f'{a}%',ha='center',fontsize=8); ax.text(xi+0.18,b+0.6,f'{b}%',ha='center',fontsize=8)
ax.set_xticks(x); ax.set_xticklabels(cats); ax.set_ylabel('Descriptions mentioning (%)'); ax.set_ylim(0,33); ax.legend(frameon=False,fontsize=8,loc='upper center',bbox_to_anchor=(0.5,-0.15),ncol=2); save(fig,'FigureH3_capstones')
# keyword table for appendix H
kw={'Film and video':r'\bfilm|cinema|screenwrit|video(?! ?gam)','Photography':r'photo','Graphic and communication design':r'graphic design|typograph|communication design|visual communication','Drawing':r'drawing','Painting':r'painting','Animation':r'animat','Sculpture':r'sculpt','Ceramics':r'ceramic','Printmaking':r'printmak|print media|intaglio|lithograph|screenprint','Fashion':r'fashion|apparel|garment',
 'Game design and interactive media':r'\bgam(e|es|ing)\b|gameplay|interactive','UX, UI, and interaction design':r'\bUX\b|\bUI\b|user experience|interaction design|interface','Virtual, augmented, and immersive media':r'\bVR\b|\bAR\b|\bXR\b|virtual reality|augmented|immersive|extended reality','Artificial intelligence, machine learning, generative tools':r'\bA\.?I\.?\b|artificial intelligence|machine learning|generative|neural|creative coding|algorithm','3-D printing and digital fabrication':r'3-?D print|digital fabrication|CNC|laser|rapid prototyp',
 'Sustainability and ecology':r'sustainab|ecolog|climate|environmental (justice|ethics|issues|humanities|activism|art\b|arts\b)|eco-?(art|design)','Social practice and community art':r'social practice|socially engaged|community[- ](based|engaged|engagement|art|arts|practice|project|projects|partnership|partnerships)|arts? (in|and) (the )?communit|civic|public art|participatory','Accessibility and disability':r'accessib|disabilit|inclusive design|universal design'}
N=len(comp); P=comp.unitid.nunique(); rows=[]
for k,p in kw.items():
    mm=comp.title.str.contains(p,case=False,regex=True,na=False); rows.append((k,int(mm.sum()),round(mm.sum()/N*100,1),int(comp[mm].unitid.nunique())))
K=pd.DataFrame(rows,columns=['Subject','Courses','Share of titles (%)','Programs (of 231)']); K.to_csv(f'{O}/keyword_table.csv',index=False); print(K.to_string(index=False))
fig,ax=plt.subplots(figsize=(6.5,4.2)); KK=K.sort_values('Programs (of 231)')
ax.barh(KK.Subject,KK['Programs (of 231)'],color=[ACC if s in ('Artificial intelligence, machine learning, generative tools','Accessibility and disability','Game design and interactive media','Virtual, augmented, and immersive media') else BAR_L for s in KK.Subject])
for i,v in enumerate(KK['Programs (of 231)']): ax.text(v+2,i,str(v),va='center',fontsize=7.5)
ax.set_xlabel('Programs with at least one course title on the subject (of 231)'); ax.set_xlim(0,250); save(fig,'FigureH4_subjects')
ss.round(1).to_csv(f'{O}/sector_table.csv'); sh.round(1).to_csv(f'{O}/composition_levels.csv')
