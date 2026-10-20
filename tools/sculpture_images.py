#!/usr/bin/env python3
"""補圖管線（任務 sculpture_images）。可中斷續跑：所有 HTTP GET 結果快取在
_work/sculpture_images/cache.sqlite；已下載的圖檔不會重抓。

步驟：
  python tools/sculpture_images.py artists   # 解析雕塑家 Wikidata Q 號與生卒年
  python tools/sculpture_images.py match     # 作品 → Wikidata 條目配對與評分
  python tools/sculpture_images.py images    # 為 pd 作品蒐集候選圖並評分
  python tools/sculpture_images.py download  # 下載高信心圖片（只限 pd=true）
  python tools/sculpture_images.py patches   # 產生 tools/patches/100、110 與報告用 JSON
  python tools/sculpture_images.py all
"""
import json, os, re, sys, time, sqlite3, hashlib, threading, unicodedata, io
from difflib import SequenceMatcher
from concurrent.futures import ThreadPoolExecutor
import requests

R = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = os.path.join(os.path.dirname(R), '_work', 'sculpture_images')
os.makedirs(WORK, exist_ok=True)
UA = 'AtlasEnhancer/1.0 (briantsai.vc@gmail.com)'
TODAY = '2026-10-20'
S = requests.Session(); S.headers['User-Agent'] = UA
_db_lock = threading.Lock()
DB = sqlite3.connect(os.path.join(WORK, 'cache.sqlite'), timeout=60, check_same_thread=False)
DB.execute('PRAGMA journal_mode = WAL')
DB.execute('PRAGMA busy_timeout = 60000')
DB.execute('CREATE TABLE IF NOT EXISTS c (k TEXT PRIMARY KEY, v TEXT)')
WD_API = 'https://www.wikidata.org/w/api.php'
CM_API = 'https://commons.wikimedia.org/w/api.php'
SPARQL = 'https://query.wikidata.org/sparql'


def get(url, params=None, ttl_key=None):
    """Cached GET returning parsed JSON (None on hard failure)."""
    params = dict(params or {})
    k = hashlib.sha1((url + '?' + json.dumps(params, sort_keys=True)).encode()).hexdigest()
    with _db_lock:
        row = DB.execute('SELECT v FROM c WHERE k=?', (k,)).fetchone()
    if row:
        return json.loads(row[0])
    delay = 2
    for attempt in range(6):
        try:
            r = S.get(url, params=params, timeout=60,
                      headers={'Accept': 'application/sparql-results+json'} if url == SPARQL else None)
        except requests.RequestException:
            time.sleep(delay); delay *= 2; continue
        if r.status_code in (429, 503, 502, 504):
            time.sleep(int(r.headers.get('Retry-After', delay)) if str(r.headers.get('Retry-After', '')).isdigit() else delay)
            delay = min(delay * 2, 120); continue
        if r.status_code == 404:
            data = None
        else:
            try:
                data = r.json()
            except ValueError:
                time.sleep(delay); delay *= 2; continue
        with _db_lock:
            DB.execute('INSERT OR REPLACE INTO c VALUES (?,?)', (k, json.dumps(data)))
            DB.commit()
        return data
    return None


def pmap(fn, items, n=4):
    with ThreadPoolExecutor(n) as ex:
        return list(ex.map(fn, items))


def L(name):
    with open(os.path.join(R, 'data', name), encoding='utf-8') as f:
        return json.load(f)


def save(name, obj):
    with open(os.path.join(WORK, name), 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


def load(name):
    with open(os.path.join(WORK, name), encoding='utf-8') as f:
        return json.load(f)

# ---------------------------------------------------------------- helpers

def norm(s):
    s = unicodedata.normalize('NFKD', s or '')
    s = ''.join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"[’'`]", '', s)
    s = re.sub(r'[^\w\u4e00-\u9fff]+', ' ', s)
    s = re.sub(r'\b(the|a|an|of|le|la|les|il|lo|der|die|das|de|del|della|di|du|des|von|van)\b', ' ', s)
    return re.sub(r'\s+', ' ', s).strip()


def variants(t):
    out = {t}
    out.add(re.sub(r'\s*\(.*?\)\s*', ' ', t))          # drop parenthetical
    for m in re.findall(r'\((.*?)\)', t):
        out.add(m)
    for part in re.split(r'\s*[,;:/]\s*| or | – | - ', t):
        if len(part) > 3: out.add(part)
    return {norm(x) for x in out if norm(x)}


def sim(a_set, b_set):
    best = 0.0
    for a in a_set:
        for b in b_set:
            if not a or not b: continue
            r = SequenceMatcher(None, a, b).ratio()
            ta, tb = set(a.split()), set(b.split())
            if ta and tb:
                j = len(ta & tb) / len(ta | tb)
                cont = len(ta & tb) / min(len(ta), len(tb))
                r = max(r, j, 0.85 * cont if min(len(ta), len(tb)) >= 2 else 0)
            best = max(best, r)
    return best


def parse_year(y):
    if not y: return None
    m = re.search(r'(\d{1,4})\s*(BC|BCE|B\.C\.)?', y)
    if not m: return None
    v = int(m.group(1))
    if 'century' in y.lower() or re.search(r'\d(st|nd|rd|th)\b', y):
        v = (v - 1) * 100 + 50
    if m.group(2) or (re.search(r'\bBC', y) and not re.search(r'\bAD\b.*' + m.group(1), y)):
        v = -v
    return v


def wd_time_year(claim):
    try:
        t = claim['mainsnak']['datavalue']['value']['time']
        return int(t[:t.index('-', 1)])
    except Exception:
        return None


def claim_ids(ent, p):
    out = []
    for c in ent.get('claims', {}).get(p, []):
        try: out.append(c['mainsnak']['datavalue']['value']['id'])
        except Exception: pass
    return out


def claim_strs(ent, p):
    out = []
    for c in ent.get('claims', {}).get(p, []):
        try: out.append(c['mainsnak']['datavalue']['value'])
        except Exception: pass
    return out


ENT_CACHE = {}


def entities(qids, props='labels|aliases|claims|sitelinks'):
    qids = [q for q in dict.fromkeys(qids) if q and q not in ENT_CACHE]
    chunks = [qids[i:i + 50] for i in range(0, len(qids), 50)]
    def f(ch):
        d = get(WD_API, {'action': 'wbgetentities', 'ids': '|'.join(ch), 'props': props, 'format': 'json'})
        return (d or {}).get('entities', {})
    for res in [f(ch) for ch in chunks]:
        ENT_CACHE.update(res)
    return ENT_CACHE


def labels_of(ent):
    out = set()
    for v in ent.get('labels', {}).values(): out.add(v['value'])
    for vs in ent.get('aliases', {}).values():
        for v in vs: out.add(v['value'])
    return out


def en_label(ent):
    for l in ('en', 'zh', 'fr', 'it', 'de', 'es'):
        if l in ent.get('labels', {}): return ent['labels'][l]['value']
    return next(iter(ent.get('labels', {}).values()), {}).get('value', '')

MAT_MAP = {'大理石': ['marble'], '青銅': ['bronze'], '木': ['wood', 'oak', 'lime', 'walnut', 'linden'],
           '石': ['stone', 'limestone', 'sandstone'], '陶土／赤陶': ['terracotta', 'clay', 'ceramic', 'earthenware'],
           '石膏': ['plaster'], '象牙': ['ivory'], '黃金／貴金屬': ['gold', 'silver'], '鋼鐵／金屬': ['steel', 'iron', 'aluminium']}

# ---------------------------------------------------------------- stage: artists

def stage_artists():
    works = L('works.json'); arts = L('artists.json'); bios = L('bios.json')
    names = sorted({w['a'] for w in works if not w.get('img')})
    def resolve(name):
        info = arts.get(name, {})
        if name.lower().startswith('unknown') or 'anonymous' in name.lower() or 'workshop' in name.lower():
            return name, None
        queries = [name] + list((bios.get(name) or {}).get('aka', []))[:3]
        cands = []
        for q in queries:
            d = get(WD_API, {'action': 'wbsearchentities', 'search': q, 'language': 'en', 'limit': 8, 'type': 'item', 'format': 'json'})
            cands += [x['id'] for x in (d or {}).get('search', [])]
            if cands and q == name and len(cands) >= 3: break
        cands = list(dict.fromkeys(cands))
        ents = entities(cands)
        best, bs = None, -9
        for i, q in enumerate(cands):
            e = ents.get(q)
            if not e or 'Q5' not in claim_ids(e, 'P31'): continue
            b = [wd_time_year(c) for c in e['claims'].get('P569', [])]
            dd = [wd_time_year(c) for c in e['claims'].get('P570', [])]
            occ = set(claim_ids(e, 'P106'))
            s = 2 - i * 0.2
            if occ & {'Q1281618', 'Q483501', 'Q1028181', 'Q3391743', 'Q15296811', 'Q42973', 'Q211423', 'Q1925963'}: s += 2
            tol = 40 if (info.get('birth') or 0) < 1000 else 3
            if info.get('death') is not None and any(x is not None and abs(x - info['death']) <= tol for x in dd): s += 3
            elif info.get('death') is not None and dd and all(x is not None for x in dd): s -= 3
            if info.get('birth') is not None and any(x is not None and abs(x - info['birth']) <= tol for x in b): s += 2
            if s > bs: best, bs = q, s
        if best is None or bs < 3:
            return name, None
        e = ents[best]
        dd = [wd_time_year(c) for c in e['claims'].get('P570', [])]
        return name, {'q': best, 'score': round(bs, 1), 'label': en_label(e),
                      'death_wd': next((x for x in dd if x is not None), None)}
    res = dict(pmap(resolve, names))
    save('artists.json', res)
    print('artists resolved', sum(1 for v in res.values() if v), '/', len(res))

# ---------------------------------------------------------------- stage: match

def batch_artist_works(aqs):
    out = {aq: [] for aq in aqs}
    for i in range(0, len(aqs), 30):
        chunk = aqs[i:i + 30]
        vals = ' '.join('wd:' + q for q in chunk)
        q = f"""SELECT DISTINCT ?w ?a WHERE {{ VALUES ?a {{ {vals} }}
              {{ ?w wdt:P170 ?a }} UNION {{ ?w wdt:P1773 ?a }} UNION {{ ?w wdt:P50 ?a }} }} LIMIT 10000"""
        d = get(SPARQL, {'query': q, 'format': 'json'})
        for b in (d or {}).get('results', {}).get('bindings', []):
            try:
                w_id = b['w']['value'].rsplit('/', 1)[1]
                a_id = b['a']['value'].rsplit('/', 1)[1]
                if a_id in out: out[a_id].append(w_id)
            except Exception: pass
        time.sleep(0.4)
    return out


def text_search(title, aq=None):
    sr = f'{title} haswbstatement:P170={aq}' if aq else title
    d = get(WD_API, {'action': 'query', 'list': 'search', 'srsearch': sr, 'srlimit': 6, 'srnamespace': 0, 'format': 'json'})
    return [x['title'] for x in (d or {}).get('query', {}).get('search', [])]


def mu_names(ents_needed):
    return ents_needed


def score_entity(w, e, aq, mus_lab):
    tv = variants(w['t'])
    labs = labels_of(e)
    s_t = sim(tv, {norm(x) for x in labs})
    if w.get('z') and any(w['z'] == x for x in labs): s_t = max(s_t, 0.95)
    creators = set(claim_ids(e, 'P170')) | set(claim_ids(e, 'P1773')) | set(claim_ids(e, 'P50'))
    for c in e.get('claims', {}).get('P170', []):
        for qv in c.get('qualifiers', {}).get('P1773', []):
            try: creators.add(qv['datavalue']['value']['id'])
            except Exception: pass
    sc = 4 * s_t if s_t >= 0.5 else 0
    why = [f'題名相似 {s_t:.2f}']
    a_ok = None
    if aq and aq in creators: sc += 3; a_ok = True; why.append('作者相符')
    elif aq and creators - {'Q4233718', 'Q19595382'}: sc -= 4; a_ok = False; why.append('作者不符')
    wy = parse_year(w['y'])
    ys = [wd_time_year(c) for c in e.get('claims', {}).get('P571', [])] + [wd_time_year(c) for c in e.get('claims', {}).get('P580', [])]
    ys = [y for y in ys if y is not None]
    if wy is not None and ys:
        dmin = min(abs(y - wy) for y in ys)
        tol = 25 if wy < 0 else 1
        if dmin <= 3 * tol: sc += 1.5; why.append('年代相符')
        elif dmin <= 15 * tol: sc += 0.5
        elif dmin > 40 * tol: sc -= 2; why.append(f'年代不符({ys[0]})')
    colls = claim_ids(e, 'P195') + claim_ids(e, 'P276')
    cl = [mus_lab.get(c, '') for c in colls]
    mu = w.get('mu') or ''
    if mu and mu != 'Lost' and cl:
        cs = sim(variants(mu), {norm(x) for x in cl if x})
        if cs >= 0.6: sc += 2; why.append('館藏相符')
        elif cs < 0.35: sc -= 1.5; why.append('館藏不同')
    mats = [mus_lab.get(c, '').lower() for c in claim_ids(e, 'P186')]
    if any(any(k in m for k in MAT_MAP.get(w.get('mat'), [])) for m in mats): sc += 0.5; why.append('材質相符')
    return round(sc, 2), round(s_t, 2), a_ok, why


def stage_match():
    works = L('works.json'); A = load('artists.json')
    todo = [w for w in works if not w.get('img')]
    print('no-image works:', len(todo), ' pd=true', sum(1 for w in todo if w['pd']))
    aqs = sorted({v['q'] for v in A.values() if v})
    aw = batch_artist_works(aqs)
    print('artist works fetched', sum(len(v) for v in aw.values()))
    # candidate entity ids per work
    cand = {}
    def find(w):
        a = A.get(w['a']); aq = a['q'] if a else None
        ids = list(aw.get(aq, [])) if aq else []
        if (w.get('wd') or {}).get('q') and (w['wd'].get('via') != 'P180'):
            ids.append(w['wd']['q'])
        if not ids:
            t2 = re.sub(r'\s*\(.*?\)', '', w['t'])
            ids += text_search(f'{t2} {w["a"]}')[:5]
            ids += text_search(w['t'])[:3]
        return w['id'], list(dict.fromkeys(ids))
    cand = dict(pmap(find, todo))
    allids = {q for v in cand.values() for q in v}
    print('candidate entities', len(allids))
    entities(sorted(allids))
    # labels for collections / materials
    aux = {q for e in (ENT_CACHE.get(x) for x in allids) if e for p in ('P195', 'P276', 'P186') for q in claim_ids(e, p)}
    entities(sorted(aux), props='labels')
    lab = {q: ENT_CACHE[q].get('labels', {}).get('en', {}).get('value', '') for q in aux if q in ENT_CACHE}
    out = {}
    for w in todo:
        a = A.get(w['a']); aq = a['q'] if a else None
        scored = []
        for q in cand[w['id']]:
            e = ENT_CACHE.get(q)
            if not e or 'missing' in e: continue
            p31 = set(claim_ids(e, 'P31'))
            if p31 & {'Q5', 'Q16521', 'Q4167410', 'Q13442814', 'Q571', 'Q7725634', 'Q482994', 'Q11424', 'Q5398426'}: continue
            sc, st, a_ok, why = score_entity(w, e, aq, lab)
            scored.append({'q': q, 'score': sc, 'sim': st, 'a_ok': a_ok, 'why': why, 'label': en_label(e)})
        scored.sort(key=lambda x: -x['score'])
        top = scored[:3]
        lvl = 'none'
        if top:
            b = top[0]
            second = top[1]['score'] if len(top) > 1 else -9
            if b['a_ok'] and b['sim'] >= 0.75 and b['score'] >= 7 and b['score'] - second >= 1: lvl = 'high'
            elif b['a_ok'] is None and not aq and b['sim'] >= 0.9 and '館藏相符' in b['why'] and b['score'] >= 6: lvl = 'high'
            elif b['a_ok'] is not False and b['score'] >= 5 and b['sim'] >= 0.6: lvl = 'medium'
        out[w['id']] = {'level': lvl, 'cands': top}
    save('match.json', out)
    import collections
    print(collections.Counter(v['level'] for v in out.values()))

# ---------------------------------------------------------------- stage: images

def commons_info(titles):
    """imageinfo + categories for File: titles (cached)."""
    res = {}
    chunks = [titles[i:i + 40] for i in range(0, len(titles), 40)]
    def f(ch):
        return get(CM_API, {'action': 'query', 'titles': '|'.join(ch), 'prop': 'imageinfo|categories', 'cllimit': 'max',
                            'iiprop': 'url|size|mime|extmetadata', 'iiurlwidth': 1280, 'iiextmetadatafilter': 'ImageDescription|LicenseShortName|ObjectName',
                            'format': 'json', 'redirects': 1})
    for d in pmap(f, chunks):
        for p in ((d or {}).get('query', {}).get('pages', {}) or {}).values():
            if 'imageinfo' in p: res[p['title']] = p
        for n in ((d or {}).get('query', {}).get('normalized', []) or []):
            if n['to'] in res: res[n['from']] = res[n['to']]
    return res


BAD_FILE = re.compile(r'detail|détail|dettaglio|ausschnitt|close[- ]?up|head of|testa|kopf\b|\bhand\b|signature|inscription|plaque|label|map|plan\b|\.svg$|\.pdf$|\.djvu$|\.tif', re.I)


def sdc_files(q):
    d = get(CM_API, {'action': 'query', 'list': 'search', 'srsearch': f'haswbstatement:P180={q}', 'srnamespace': 6, 'srlimit': 15, 'format': 'json'})
    return [x['title'] for x in (d or {}).get('query', {}).get('search', [])]


def cat_files(cat):
    d = get(CM_API, {'action': 'query', 'list': 'categorymembers', 'cmtitle': 'Category:' + cat, 'cmtype': 'file', 'cmlimit': 30, 'format': 'json'})
    return [x['title'] for x in (d or {}).get('query', {}).get('categorymembers', [])]


def pageimage(lang, title):
    d = get(f'https://{lang}.wikipedia.org/w/api.php', {'action': 'query', 'titles': title, 'prop': 'pageimages', 'piprop': 'name', 'pilicense': 'free', 'format': 'json', 'redirects': 1})
    for p in ((d or {}).get('query', {}).get('pages', {}) or {}).values():
        if p.get('pageimage'): return 'File:' + p['pageimage'].replace('_', ' ')
    return None


def commons_text(w):
    sur = w['a'].split()[-1] if not w['a'].lower().startswith('unknown') else ''
    t = re.sub(r'\s*\(.*?\)', '', w['t'])
    d = get(CM_API, {'action': 'query', 'list': 'search', 'srsearch': f'{t} {sur} filetype:bitmap', 'srnamespace': 6, 'srlimit': 10, 'format': 'json'})
    return [x['title'] for x in (d or {}).get('query', {}).get('search', [])]


def copies_of(q):
    d = get(SPARQL, {'query': f'SELECT DISTINCT ?c WHERE {{ {{?c wdt:P144 wd:{q}}} UNION {{?c wdt:P1877 wd:{q}}} UNION {{?c wdt:P180 wd:{q}. ?c wdt:P31/wdt:P279* wd:Q860861}} UNION {{?c wdt:P180 wd:{q}. ?c wdt:P31 wd:Q41207}} }} LIMIT 50', 'format': 'json'})
    return [b['c']['value'].rsplit('/', 1)[1] for b in (d or {}).get('results', {}).get('bindings', [])]


LOST_KINDS = [
    (re.compile(r'coin|numism|drachm|tetradrachm|stater|denarius|sestertius|aureus|obol|münze|moneta|monnaie', re.I), '古錢幣圖像'),
    (re.compile(r'reconstruct|rekonstruk|ricostruz|restitution|replica.*(nashville|modern)|nashville|model|modell|maquette', re.I), '現代復原'),
    (re.compile(r'plaster|gips|calco|moulage|cast\b|abguss', re.I), '石膏模型'),
    (re.compile(r'engraving|etching|lithograph|woodcut|print|gravure|kupferstich|stich|drawing|zeichnung|disegno|dessin|illustration', re.I), '歷史版畫／素描'),
    (re.compile(r'roman copy|copy|copia|kopie|copie|after (the )?original|replica', re.I), '後世（多為羅馬時期）複製品'),
]


def lost_kind(text):
    for rx, k in LOST_KINDS:
        if rx.search(text): return k
    return None


def strip_html(s):
    return re.sub(r'<[^>]+>', ' ', s or '')


def stage_images():
    works = {w['id']: w for w in L('works.json')}
    M = load('match.json'); A = load('artists.json')
    todo = [works[i] for i in M if works[i]['pd']]
    def collect(w):
        m = M[w['id']]; cands = []
        lost = w['mu'] == 'Lost'
        if m['level'] in ('high', 'medium'):
            q = m['cands'][0]['q']; e = ENT_CACHE.get(q) or entities([q])[q]
            for f in claim_strs(e, 'P18'): cands.append(('File:' + f, 'P18', q))
            for f in sdc_files(q): cands.append((f, 'SDC P180', q))
            for c in claim_strs(e, 'P373')[:1]:
                for f in cat_files(c): cands.append((f, 'Commons 分類', q))
            for lang in ('en', 'it', 'fr', 'de', 'es', 'el', 'nl'):
                sl = e.get('sitelinks', {}).get(lang + 'wiki')
                if sl:
                    f = pageimage(lang, sl['title'])
                    if f: cands.append((f, f'{lang}.wikipedia pageimage', q))
            if lost:
                cps = copies_of(q); entities(cps)
                for c in cps:
                    for f in claim_strs(ENT_CACHE.get(c, {}), 'P18'): cands.append(('File:' + f, f'複製品條目 {c} P18', q))
        for f in commons_text(w): cands.append((f, 'Commons 搜尋', None))
        return w['id'], cands
    raw = dict(pmap(collect, todo))
    titles = sorted({c[0] for v in raw.values() for c in v})
    info = commons_info(titles)
    out = {}
    for w in todo:
        m = M[w['id']]; lost = w['mu'] == 'Lost'
        best = []
        seen = set()
        sur = norm(w['a'].split()[-1]) if not w['a'].lower().startswith('unknown') else ''
        for (f, how, q) in raw[w['id']]:
            if f in seen: continue
            seen.add(f)
            p = info.get(f)
            if not p: continue
            ii = p['imageinfo'][0]
            if ii.get('mime', '').split('/')[0] != 'image' or ii.get('mime') == 'image/svg+xml': continue
            if min(ii.get('width', 0), ii.get('height', 0)) < 300: continue
            meta = ii.get('extmetadata', {})
            desc = strip_html(meta.get('ImageDescription', {}).get('value', ''))
            cats = ' '.join(c['title'] for c in p.get('categories', []))
            text = f + ' ' + desc + ' ' + cats
            fname = f[5:].rsplit('.', 1)[0]
            ent_lvl = m['level'] if q else 'none'
            conf = 0.0; notes = [how]
            if how == 'P18': conf = 9
            elif how.endswith('pageimage'): conf = 7.5
            elif how == 'SDC P180': conf = 7.5
            elif how == 'Commons 分類': conf = 6.5
            elif how.startswith('複製品條目'): conf = 7
            else:  # free text search
                s_t = sim(variants(w['t']), {norm(fname), norm(desc[:200])})
                conf = 4 * s_t + (1.5 if sur and sur in norm(text) else 0) + (1 if w.get('mu') and sim(variants(w['mu']), {norm(text)}) > 0.5 else 0)
                notes.append(f'檔名相似 {s_t:.2f}')
            if ent_lvl == 'medium': conf -= 2
            if BAD_FILE.search(fname): conf -= 3; notes.append('疑似局部/非主體')
            kind = None
            if lost:
                kind = lost_kind(text)
                if not kind: conf -= 3; notes.append('佚失作品，無法判斷圖為何種替代')
                else: notes.append('替代：' + kind)
            best.append({'file': f, 'how': how, 'q': q, 'conf': round(conf, 2), 'notes': notes, 'kind': kind,
                         'thumb': ii.get('thumburl'), 'w': ii.get('width'), 'h': ii.get('height'),
                         'desc': desc[:200], 'license': meta.get('LicenseShortName', {}).get('value', '')})
        best.sort(key=lambda x: -x['conf'])
        lvl = 'none'
        if best:
            b = best[0]
            if b['q'] and m['level'] == 'high' and b['conf'] >= 7: lvl = 'high'
            elif b['conf'] >= 5: lvl = 'medium'
        out[w['id']] = {'level': lvl, 'cands': best[:4]}
    save('images.json', out)
    import collections
    print(collections.Counter(v['level'] for v in out.values()))

# ---------------------------------------------------------------- stage: download

def stage_download():
    from PIL import Image
    I = load('images.json')
    os.makedirs(os.path.join(R, 'img', '800'), exist_ok=True); os.makedirs(os.path.join(R, 'img', 'thumb'), exist_ok=True)
    jobs = [(i, v['cands'][0]) for i, v in I.items() if v['level'] == 'high']
    done = load('downloads.json') if os.path.exists(os.path.join(WORK, 'downloads.json')) else {}
    def dl(job):
        i, c = job
        p800 = os.path.join(R, 'img', '800', i + '.webp'); pth = os.path.join(R, 'img', 'thumb', i + '.webp')
        if done.get(i) == c['file'] and os.path.exists(p800) and os.path.exists(pth): return i, c['file']
        url = c['thumb']
        for k in range(6):
            try:
                r = S.get(url, timeout=90)
            except requests.RequestException:
                time.sleep(2 ** k); continue
            if r.status_code == 429 or r.status_code >= 500: time.sleep(5 * 2 ** k); continue
            if r.status_code != 200: return i, None
            try:
                im = Image.open(io.BytesIO(r.content)); im.load()
            except Exception:
                return i, None
            if im.mode not in ('RGB', 'RGBA'): im = im.convert('RGB')
            for path, side, q in ((p800, 800, 80), (pth, 260, 75)):
                x = im.copy(); x.thumbnail((side, side), Image.LANCZOS); x.save(path, 'WEBP', quality=q)
            time.sleep(0.5)
            return i, c['file']
        return i, None
    for i, f in pmap(dl, jobs, n=3):
        if f: done[i] = f
        else: done.pop(i, None)
    save('downloads.json', done)
    print('downloaded', len(done), '/', len(jobs))

# ---------------------------------------------------------------- stage: patches

ID_HINT = re.compile(r'artwork|object|collection|catalog|inventory|work id|public art|sculpture|museum|accession', re.I)


def best_link(e):
    """Priority: P973 / museum-catalogue ID → Wikipedia → Wikidata."""
    for u in claim_strs(e, 'P973'):
        if isinstance(u, str) and u.startswith('http'): return u, 'P973 官方頁', 'Museum'
    ext = [(p, cs) for p, cs in e.get('claims', {}).items() if cs and cs[0]['mainsnak'].get('datatype') == 'external-id']
    if ext:
        props = [p for p, _ in ext]
        entities(props, props='labels|claims')
        for p, cs in ext:
            pe = ENT_CACHE.get(p, {})
            lab = pe.get('labels', {}).get('en', {}).get('value', '')
            if not ID_HINT.search(lab) or re.search(r'Commons|Freebase|Google|Library|VIAF|Getty AAT|Wikimedia|Europeana|Joconde|Mérimée|Palissy|POP', lab, re.I):
                continue
            fmts = claim_strs(pe, 'P1630')
            try: v = cs[0]['mainsnak']['datavalue']['value']
            except Exception: continue
            if fmts:
                return fmts[0].replace('$1', str(v)), f'館藏 ID（{p} {lab}）', 'Museum'
    for lang in ('zh', 'en', 'fr', 'it', 'de', 'es', 'nl', 'ja'):
        sl = e.get('sitelinks', {}).get(lang + 'wiki')
        if sl and lang != 'zh':
            return f'https://{lang}.wikipedia.org/wiki/' + sl['title'].replace(' ', '_'), f'{lang}.wikipedia 條目', 'Wikipedia'
        if sl and lang == 'zh':
            zh = sl
    if e.get('sitelinks', {}).get('enwiki') is None and e.get('sitelinks', {}).get('zhwiki'):
        return 'https://zh.wikipedia.org/wiki/' + e['sitelinks']['zhwiki']['title'].replace(' ', '_'), 'zh.wikipedia 條目', 'Wikipedia'
    return f'https://www.wikidata.org/wiki/{e["id"]}', 'Wikidata 條目', 'Wikidata'


def fp(fname):
    from urllib.parse import quote
    return 'https://commons.wikimedia.org/wiki/Special:FilePath/' + quote(fname[5:], safe=",()'!*-_.~") + '?width=1000'


def wd_obj(e, via):
    ys = [wd_time_year(c) for c in e.get('claims', {}).get('P571', [])]
    mus = [ENT_CACHE.get(c, {}).get('labels', {}).get('en', {}).get('value', '') for c in claim_ids(e, 'P195')]
    return {'q': e['id'], 'via': via, 'label': en_label(e), 'y': [y for y in ys if y is not None], 'mu': [m for m in mus if m]}


def stage_patches():
    works = {w['id']: w for w in L('works.json')}
    M = load('match.json'); I = load('images.json'); D = load('downloads.json'); A = load('artists.json')
    arts = L('artists.json')
    allq = {c['q'] for v in M.values() for c in v['cands']}
    entities(sorted(allq))
    aux = {q for x in allq for q in claim_ids(ENT_CACHE.get(x, {}), 'P195')}
    entities(sorted(aux), props='labels')
    p100 = {'file': 'works.json', 'key': 'id', 'description': f'sculpture_images：為無圖的公有領域作品補圖（Wikidata P18／Commons SDC／Wikipedia pageimage），{TODAY}',
            'set': {}, 'evidence': {}}
    p110 = {'file': 'works.json', 'key': 'id', 'description': f'sculpture_images：未進公有領域作品的 src_url 由 Wikipedia 搜尋改為官方頁／條目，並補 wd.q，{TODAY}',
            'set': {}, 'evidence': {}}
    review = {'img_medium': [], 'match_medium_B': [], 'pd_review': [], 'img_none': []}
    for i, v in I.items():
        w = works[i]
        if v['level'] == 'high' and D.get(i) == v['cands'][0]['file']:
            c = v['cands'][0]; e = ENT_CACHE[c['q']]; mc = M[i]['cands'][0]
            fname = c['file']
            verif = (f"以 Wikidata 配對作品條目 {c['q']}（{'、'.join(mc['why'])}），圖取自 {c['how']}"
                     f"（Commons 檔案 {fname[5:]}，授權 {c['license'] or 'Commons 自由授權'}），{TODAY} 下載轉檔")
            rec = {'local': True, 'img': f'img/800/{i}.webp', 'thumb': f'img/thumb/{i}.webp', 'src': 'Commons',
                   'src_url': 'https://commons.wikimedia.org/wiki/' + fname.replace(' ', '_'), 'img_ext': fp(fname)}
            if w['mu'] == 'Lost':
                note = f"圖為{c['kind']}，非原作（原作已佚失）"
                rec['img_note'] = note; verif = note + '。' + verif
            rec['verif'] = verif
            if not (w.get('wd') or {}).get('q') or w['wd'].get('via') == 'P180':
                rec['wd'] = wd_obj(e, 'P170+題名')
            p100['set'][i] = rec
            p100['evidence'][i] = f"https://www.wikidata.org/wiki/{c['q']} ; https://commons.wikimedia.org/wiki/{fname.replace(' ', '_')}"
        elif v['cands'] and v['level'] in ('medium', 'high'):
            c = v['cands'][0]
            review['img_medium'].append({'id': i, 't': w['t'], 'a': w['a'], 'mu': w['mu'], 'q': c['q'], 'file': c['file'],
                                         'url': 'https://commons.wikimedia.org/wiki/' + c['file'].replace(' ', '_'),
                                         'conf': c['conf'], 'notes': c['notes'], 'match': M[i]['level']})
        else:
            review['img_none'].append({'id': i, 't': w['t'], 'a': w['a'], 'mu': w['mu'], 'match': M[i]['level'],
                                       'q': M[i]['cands'][0]['q'] if M[i]['cands'] else None})
    # B: links
    for i, m in M.items():
        w = works[i]
        if w['pd']: continue
        if not (w.get('src_url') or '').startswith('https://en.wikipedia.org/w/index.php?search='): continue
        if m['level'] == 'high':
            e = ENT_CACHE[m['cands'][0]['q']]
            url, how, src = best_link(e)
            p110['set'][i] = {'src_url': url, 'src': src, 'wd': wd_obj(e, 'P170+題名')}
            p110['evidence'][i] = f"https://www.wikidata.org/wiki/{e['id']}（{'、'.join(m['cands'][0]['why'])}）；連結來源：{how}"
        elif m['level'] == 'medium':
            c = m['cands'][0]
            review['match_medium_B'].append({'id': i, 't': w['t'], 'a': w['a'], 'q': c['q'], 'label': c['label'], 'score': c['score'], 'why': c['why']})
    # pd review
    for i, w in works.items():
        if w.get('img') or w['pd']: continue
        a = A.get(w['a']) or {}
        dw = a.get('death_wd'); dl = arts.get(w['a'], {}).get('death')
        if w['a'].lower().startswith('unknown') or (dw is not None and dw <= 1955) or (dl is not None and dl <= 1955):
            review['pd_review'].append({'id': i, 't': w['t'], 'a': w['a'], 'death_local': dl, 'death_wd': dw, 'artist_q': a.get('q')})
    for name, p in (('100_sculpture_images.json', p100), ('110_sculpture_links.json', p110)):
        with open(os.path.join(R, 'tools', 'patches', name), 'w', encoding='utf-8') as f:
            json.dump(p, f, ensure_ascii=False, indent=1)
    # artist death discrepancies for report
    disc = [{'a': n, 'death_local': arts.get(n, {}).get('death'), 'death_wd': v['death_wd'], 'q': v['q']}
            for n, v in A.items() if v and v.get('death_wd') is not None and arts.get(n, {}).get('death') is not None
            and abs(v['death_wd'] - arts[n]['death']) > 1 and arts[n]['death'] > 1000]
    review['death_disc'] = disc
    save('review.json', review)
    print('patch100', len(p100['set']), 'patch110', len(p110['set']), {k: len(v) for k, v in review.items()})


if __name__ == '__main__':
    st = sys.argv[1] if len(sys.argv) > 1 else 'all'
    steps = {'artists': stage_artists, 'match': stage_match, 'images': stage_images, 'download': stage_download, 'patches': stage_patches}
    for k, f in steps.items():
        if st in (k, 'all'):
            print('==', k); f()
