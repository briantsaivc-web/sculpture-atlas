#!/usr/bin/env python3
import sys
if '--force' not in sys.argv:
    sys.exit("已停用：tools/patches/*.json 已於 2026-10-21 整合進 main，且其後修正了審核發現的錯誤。重跑會把錯誤寫回。如確定要重跑請加 --force。")
"""Apply data patches in tools/patches/*.json to data/*.json (idempotent).

Why: main keeps changing, so edits are kept as patches instead of hand-editing
the big JSON files. To merge, pull the latest main, run this script, then run
tools/validate.py.

Patch format (one JSON file per topic; applied in file-name order):
{
  "file": "works.json",            # target file under data/
  "key": "id",                     # list files: field used to find a record (dict files: ignored)
  "description": "...",
  "set":   { "<key>": {"field": value, ...}, ... },   # update existing records (field-level)
  "unset": { "<key>": ["field", ...] },               # optional: remove fields
  "add":   [ {record}, ... ]  or  { "<key>": {record} },  # new records (skipped if key already exists)
  "remove": ["<key>", ...],                           # optional: remove records
  "evidence": { "<key>": "url or note" }              # optional, not written to data
}
Usage: python tools/apply_patches.py [--dry-run] [--only name_substring]
"""
import json, os, sys, glob, argparse

R = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ap = argparse.ArgumentParser()
ap.add_argument('--dry-run', action='store_true')
ap.add_argument('--only', default='')
args = ap.parse_args()

cache, stats, indents = {}, [], {}

def load(name):
    if name not in cache:
        with open(os.path.join(R, 'data', name), encoding='utf-8') as f:
            txt = f.read()
        cache[name] = json.loads(txt)
        # keep original layout: count leading spaces of the 2nd line (None = minified)
        lines = txt.splitlines()
        indents[name] = (len(lines[1]) - len(lines[1].lstrip(' '))) if len(lines) > 1 else None
    return cache[name]

for pf in sorted(glob.glob(os.path.join(R, 'tools', 'patches', '*.json'))):
    if args.only and args.only not in os.path.basename(pf):
        continue
    with open(pf, encoding='utf-8-sig') as f:
        p = json.load(f)
    data = load(p['file'])
    key = p.get('key', 'id')
    s = {'patch': os.path.basename(pf), 'set': 0, 'unchanged': 0, 'missing': [], 'added': 0, 'removed': 0}
    if isinstance(data, list):
        idx = {r.get(key): r for r in data}
        get = idx.get
    else:
        get = data.get
    for k, fields in p.get('set', {}).items():
        rec = get(k)
        if rec is None:
            s['missing'].append(k); continue
        changed = False
        for fk, fv in fields.items():
            if rec.get(fk) != fv:
                rec[fk] = fv; changed = True
        s['set' if changed else 'unchanged'] += 1
    for k, fl in p.get('unset', {}).items():
        rec = get(k)
        if rec is not None:
            for fk in fl:
                rec.pop(fk, None)
    add = p.get('add', [])
    if isinstance(data, list):
        for rec in (add if isinstance(add, list) else list(add.values())):
            if rec.get(key) not in idx:
                data.append(rec); idx[rec[key]] = rec; s['added'] += 1
        rm = set(p.get('remove', []))
        if rm:
            before = len(data); data[:] = [r for r in data if r.get(key) not in rm]; s['removed'] = before - len(data)
    else:
        for k, rec in (add.items() if isinstance(add, dict) else []):
            if k not in data:
                data[k] = rec; s['added'] += 1
        for k in p.get('remove', []):
            if data.pop(k, None) is not None:
                s['removed'] += 1
    stats.append(s)

for s in stats:
    miss = f", missing {len(s['missing'])} (e.g. {s['missing'][:5]})" if s['missing'] else ''
    print(f"{s['patch']}: set {s['set']}, unchanged {s['unchanged']}, added {s['added']}, removed {s['removed']}{miss}")

if not args.dry_run:
    for name, data in cache.items():
        with open(os.path.join(R, 'data', name), 'w', encoding='utf-8', newline='\n') as f:
            ind = indents.get(name)
            json.dump(data, f, ensure_ascii=False, indent=ind,
                      separators=(',', ':') if ind is None else (',', ': '))
    print('written:', ', '.join(cache) or '(nothing)')
