"""Draw the reliability-study sample. Fixed seed. Four strata + calibration set.
Usage: python3 draw_sample.py index.csv jsons_dir disputed_158.csv lexicon_129.csv composition_courses.csv out_dir [seed]
Outputs (out_dir): sample_360.csv, calibration_30.csv  (item_id, code, title, description — nothing else)
                   sealed_verdicts.csv  (item_id, stratum, unitid, school, code, T,P,V,S,A classifier flags, source_url)
                   needs_description.csv (items whose description is not in the inputs; pipeline fills from data/courses)
                   draw_log.json (seed, counts, sha256 of sealed file)
Descriptions: present for S1 (from the two evidence files); S2/S3/S4 need the pipeline's records export.
"""
import sys, json, random, hashlib, pandas as pd
idx=pd.read_csv(sys.argv[1]); JD=sys.argv[2]; d158=pd.read_csv(sys.argv[3]); d129=pd.read_csv(sys.argv[4]); compc=pd.read_csv(sys.argv[5]); OUT=sys.argv[6].rstrip('/')
SEED=int(sys.argv[7]) if len(sys.argv)>7 else 20260917
rng=random.Random(SEED)
scored=idx[idx.ranked]; ranked=set(scored.unitid.astype(int)); names=dict(zip(idx.unitid.astype(int),idx['name']))
AX=['technology','practice','venture','self','audience']
# --- every matched art-unit course with its classifier flags ---
flags={}  # (unitid,code) -> dict
for u in ranked:
    j=json.load(open(f'{JD}/{u}.json'))
    for a in AX:
        for c in j['axes'][a]['courses']:
            if not c['in_art_subjects']: continue
            k=(u,c['code']); f=flags.setdefault(k,dict(unitid=u,code=c['code'],title=c['title'],url=c['source_url'],**{x:0 for x in AX}))
            f[a]=1
matched=pd.DataFrame(list(flags.values()))
# --- S1: disputed shape (158 + 129), art-unit, scored schools, cap 8 per school ---
s1pool=pd.concat([d158[['unitid','code','title','description']],d129[['unitid','code','title','description']]]).drop_duplicates(['unitid','code'])
s1pool=s1pool[s1pool.unitid.astype(int).isin(ranked)]
rows=s1pool.sample(frac=1,random_state=SEED).to_dict('records'); per={}; S1=[]
for r in rows:
    if per.get(r['unitid'],0)>=8: continue
    S1.append(r); per[r['unitid']]=per.get(r['unitid'],0)+1
    if len(S1)==100+8: break   # 100 + 8 for calibration
# --- S2: random shortlisted (matched) art-unit courses, any axis, excluding S1 ---
s1keys={(int(r['unitid']),r['code']) for r in S1}
pool2=matched[~matched.apply(lambda r:(int(r.unitid),r.code) in s1keys,axis=1)]
S2=pool2.sample(n=100+8,random_state=SEED+1).to_dict('records')
# --- S4: classifier-labeled self or audience, excluding S1/S2 ---
used={(int(r['unitid']),r['code']) for r in S1}|{(int(r['unitid']),r['code']) for r in S2}
pool4=matched[(matched.self==1)|(matched.audience==1)]; pool4=pool4[~pool4.apply(lambda r:(int(r.unitid),r.code) in used,axis=1)]
S4=pd.concat([pool4[pool4.self==1].sample(n=30+3,random_state=SEED+4),pool4[(pool4.audience==1)&(pool4.self==0)].sample(n=30+3,random_state=SEED+5)]).to_dict('records')
used|={(int(r['unitid']),r['code']) for r in S4}
# --- S3: random art-unit courses NOT matched on any axis (proxy for un-shortlisted; pipeline confirms shortlist status) ---
compc=compc[compc.unitid.astype(int).isin(ranked)]
pool3=compc[~compc.apply(lambda r:(int(r.unitid),r.code) in set(flags)|used,axis=1)]
S3=pool3.sample(n=100+8,random_state=SEED+3).to_dict('records')
# --- assemble, split calibration ---
def mk(rows,stratum,ncal):
    rs=[dict(r) for r in rows]; rng.shuffle(rs)
    for r in rs: r['stratum']=stratum
    return rs[ncal:], rs[:ncal]
S1m,S1c=mk(S1,'S1_disputed',8); S2m,S2c=mk(S2,'S2_shortlisted',8); S3m,S3c=mk(S3,'S3_unshortlisted',8); S4m,S4c=mk(S4,'S4_self_audience',6)
main=S1m[:100]+S2m[:100]+S3m[:100]+S4m[:60]; cal=S1c+S2c+S3c+S4c
rng.shuffle(main); rng.shuffle(cal)
def rowsout(items,prefix):
    pub=[];sealed=[];need=[]
    for i,r in enumerate(items,1):
        iid=f'{prefix}{i:03d}'; u=int(r['unitid']); code=r['code']; f=flags.get((u,code),{})
        desc=r.get('description') if isinstance(r.get('description'),str) else ''
        pub.append(dict(item_id=iid,code=code,title=r.get('title',''),description=desc))
        sealed.append(dict(item_id=iid,stratum=r['stratum'],unitid=u,school=names.get(u,''),code=code,title=r.get('title',''),
            T=f.get('technology',0),P=f.get('practice',0),V=f.get('venture',0),S=f.get('self',0),A=f.get('audience',0),source_url=f.get('url',r.get('source_url',''))))
        if not desc: need.append(dict(item_id=iid,unitid=u,code=code,title=r.get('title','')))
    return pub,sealed,need
pm,sm,nm=rowsout(main,'M'); pc,sc,nc=rowsout(cal,'C')
pd.DataFrame(pm).to_csv(f'{OUT}/sample_360.csv',index=False); pd.DataFrame(pc).to_csv(f'{OUT}/calibration_30.csv',index=False)
sealed=pd.DataFrame(sm+sc); sealed.to_csv(f'{OUT}/sealed_verdicts.csv',index=False)
pd.DataFrame(nm+nc).to_csv(f'{OUT}/needs_description.csv',index=False)
sha=hashlib.sha256(open(f'{OUT}/sealed_verdicts.csv','rb').read()).hexdigest()
log=dict(seed=SEED,drawn=__import__('datetime').date.today().isoformat(),main=len(pm),calibration=len(pc),strata={s:int((sealed.stratum==s).sum()) for s in sealed.stratum.unique()},
         needs_description=len(nm)+len(nc),schools_in_sample=int(sealed.unitid.nunique()),sealed_sha256=sha,
         note='S3 drawn from art-unit courses with no axis match (composition_courses.csv); pipeline to confirm none were shortlisted and to supply descriptions.')
json.dump(log,open(f'{OUT}/draw_log.json','w'),indent=1); print(json.dumps(log,indent=1))
