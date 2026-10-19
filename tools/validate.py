#!/usr/bin/env python3
"""資料一致性檢查：新增畫家／作品／流派後執行 `python3 tools/validate.py`。"""
import json,os,sys
R=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
L=lambda n:json.load(open(os.path.join(R,'data',n),encoding='utf-8'))
mv=L('movements.json');arts=L('artists.json');works=L('works.json');mus=L('museums.json');themes=L('themes.json')
mids={m['id'] for m in mv}; tids={t['id'] for t in themes}; err=[]
ids=set()
for w in works:
    for k in ('id','m','a','t','z','y','mu','mu_id','th','mat'):
        if k not in w: err.append(f"{w.get('id')} 缺欄位 {k}")
    if w['id'] in ids: err.append(f"重複 id {w['id']}")
    ids.add(w['id'])
    if w['m'] not in mids: err.append(f"{w['id']} 流派不存在: {w['m']}")
    if w['a'] not in arts: err.append(f"{w['id']} 畫家不存在: {w['a']}")
    if w['mu_id'] not in mus: err.append(f"{w['id']} 館藏 id 不存在: {w['mu_id']}")
    for t in w['th']:
        if t not in tids: err.append(f"{w['id']} 主題不存在: {t}")
    for k in ('img','thumb'):
        if w.get(k) and not os.path.exists(os.path.join(R,w[k])): err.append(f"{w['id']} 圖檔不存在: {w[k]}")
for m in mv:
    for k in ('id','zh','en','start','end'):
        if k not in m: err.append(f"流派 {m.get('id')} 缺 {k}")
    for f in m.get('from',[])+m.get('against',[]):
        if f not in mids: err.append(f"流派 {m['id']} 關聯不存在: {f}")
print(f"{len(mv)} 流派 / {len(arts)} 畫家 / {len(works)} 作品 / {len(mus)} 館藏 / {len(themes)} 主題")
if err:
    print('\n'.join(err[:50])); print(f'共 {len(err)} 個問題'); sys.exit(1)
print('OK 無問題')
