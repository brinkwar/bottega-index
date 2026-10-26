"""Build the coding tool page for one item set.
Usage: python3 build_tool.py tool_template.html items.csv calibration|main codebook_url out.html
items.csv columns: item_id, code, title, description (nothing else is embedded)."""
import sys, json, pandas as pd
t=open(sys.argv[1]).read(); d=pd.read_csv(sys.argv[2]).fillna(''); SET=sys.argv[3]; URL=sys.argv[4]
items=[dict(item_id=r.item_id,code=r.code,title=r.title,description=r.description) for r in d.itertuples()]
assert set(d.columns)=={'item_id','code','title','description'}, d.columns
out=t.replace('__ITEMS__',json.dumps(items,ensure_ascii=False)).replace('__SET__',SET).replace('__CODEBOOK_URL__',URL)
open(sys.argv[5],'w').write(out); print(SET,len(items),'items ->',sys.argv[5])
