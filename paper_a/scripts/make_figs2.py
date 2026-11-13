import numpy as np, matplotlib, pandas as pd
matplotlib.use('Agg'); import matplotlib.pyplot as plt
from matplotlib.patches import Patch
O='/home/claude/figs'; INK='#2A2925'; ACC='#C8853F'; G1='#6E6B66'; G2='#A9A6A0'; G3='#D6D3CD'; BAR_L='#C4C1BB'; BAR_XL='#E6E3DD'
plt.rcParams.update({'font.family':'DejaVu Sans','font.size':9,'axes.edgecolor':INK,'axes.labelcolor':INK,'xtick.color':INK,'ytick.color':INK,'axes.spines.top':False,'axes.spines.right':False,'savefig.dpi':300})
# ---- need figure: three panels
fig,ax=plt.subplots(1,3,figsize=(7.2,2.9),gridspec_kw={'width_ratios':[1.6,0.8,0.9]})
f=['All fields','Arts and humanities','Other non-S&E','Management','Sales and marketing','Engineering','Computer and math']; v=[6.0,12.0,7.6,5.4,4.6,3.6,3.2]
o=np.argsort(v); y=np.arange(len(f))
ax[0].barh(y,[v[i] for i in o],color=[ACC if f[i]=='Arts and humanities' else (G1 if f[i]=='All fields' else BAR_L) for i in o])
ax[0].set_yticks(y); ax[0].set_yticklabels([f[i] for i in o],fontsize=8)
for yi,i in zip(y,o): ax[0].text(v[i]+0.2,yi,f'{v[i]:.1f}%',va='center',fontsize=7.5)
ax[0].set_xlim(0,14.5); ax[0].set_xlabel('Self-employed (unincorporated), %',fontsize=8); ax[0].set_title('(a) Bachelor\'s holders,\nby field of degree',fontsize=8,loc='left')
ax[1].bar([0,1],[65,33],color=[G1,ACC],width=0.6); ax[1].set_xticks([0,1]); ax[1].set_xticklabels(['Needed\nin work','Acquired\nin school'],fontsize=8)
for x,val in zip([0,1],[65,33]): ax[1].text(x,val+1.5,f'{val}%',ha='center',fontsize=8)
ax[1].set_ylim(0,75); ax[1].set_ylabel('Arts alumni, %',fontsize=8); ax[1].set_title('(b) Business, financial, or\nentrepreneurial skills',fontsize=8,loc='left')
parts=[(19,ACC,'Founded an\norganization'),(71,'#E3B98A','Self-employed,\nnever founded'),(10,BAR_XL,'Neither')]
b=0
for val,c,lab in parts:
    ax[2].bar([0],[val],bottom=b,color=c,width=0.55,edgecolor='white'); ax[2].text(0.36,b+val/2,f'{lab} ({val}%)',va='center',fontsize=7.2); b+=val
ax[2].set_xlim(-0.4,1.7); ax[2].set_xticks([]); ax[2].set_ylim(0,100); ax[2].set_ylabel('Arts alumni, %',fontsize=8); ax[2].set_title('(c) Founding and\nself-employment',fontsize=8,loc='left'); ax[2].spines['bottom'].set_visible(False)
fig.tight_layout(w_pad=1.2); fig.savefig(f'{O}/FigureN_need.png',bbox_inches='tight',facecolor='white'); plt.close(fig)
# ---- waffle 1000 cells, four groups (as in the composition-by-level figure), orange subdivided
shares=[('Making and understanding the work',46.637+25.907+6.945+5.047,BAR_L),('Tools and employment',6.098+3.961,G1),('Liberal arts, pedagogy, other',1.692+0.981+0.269,BAR_XL),('Venture',1.348,ACC),('Self',0.724,'#DDA15E'),('Audience',0.390,'#EEC79A')]
tot=sum(x[1] for x in shares); raw=[x[1]/tot*1000 for x in shares]; n=[int(np.floor(r)) for r in raw]
for k in np.argsort([-(r-np.floor(r)) for r in raw])[:1000-sum(n)]: n[k]+=1
grid=[]; [grid.extend([k]*c) for k,c in enumerate(n)]
W,H=50,20; fig,ax=plt.subplots(figsize=(7.2,3.5))
for idx,k in enumerate(grid):
    r,cpos=divmod(idx,W); living=k>=3
    ax.add_patch(plt.Rectangle((cpos,H-1-r),0.9,0.9,facecolor=shares[k][2],edgecolor=INK if living else 'none',lw=0.5 if living else 0))
ax.set_xlim(-0.2,W); ax.set_ylim(-0.2,H); ax.set_aspect('equal'); ax.axis('off')
handles=[Patch(facecolor=shares[k][2],edgecolor=INK if k>=3 else 'none',lw=0.5,label=f'{shares[k][0]} ({n[k]})') for k in range(6)]
ax.legend(handles=handles,ncol=3,frameon=False,fontsize=7.5,loc='upper center',bbox_to_anchor=(0.5,0.0))
fig.savefig(f'{O}/FigureW_waffle.png',bbox_inches='tight',facecolor='white'); plt.close(fig)
print('cells',dict(zip([x[0] for x in shares],n)))
