#!/usr/bin/env python3
from __future__ import annotations

import argparse, csv, datetime as dt, html, json, math, re
from pathlib import Path
from typing import Any, Dict, List, Optional

BRAND = 'LANGIT'
VERSION = 'LANGIT v62'
DISCLAIMER = 'Bukan peringatan resmi. Untuk cuaca ekstrem, ikuti BMKG dan kondisi setempat.'
ID_BOUNDS = [[-11.25, 94.0], [6.45, 141.25]]

# ------------------------- safe helpers -------------------------
def esc(x: Any) -> str:
    return html.escape('' if x is None else str(x), quote=True)

def s(x: Any, default: str = '') -> str:
    if x is None:
        return default
    y = str(x).strip()
    return default if not y or y.lower() in {'none','nan','null','undefined'} else y

def n(x: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if x is None:
            return default
        if isinstance(x, str):
            x = x.strip().replace('%','').replace('°C','').replace(',','.')
            if x in {'','—','-','–'}:
                return default
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except Exception:
        return default

def clamp(x: Any, lo: float = 0, hi: float = 100, default: float = 0) -> float:
    v = n(x, default)
    if v is None:
        v = default
    return max(lo, min(hi, v))

def hour(x: Any, default: str = '00:00') -> str:
    t = s(x, default)
    m = re.search(r'(\d{1,2})(?::(\d{2}))?', t)
    if not m:
        return default
    h = max(0, min(23, int(m.group(1))))
    mm = m.group(2) or '00'
    return f'{h:02d}:{mm[:2]}'

def hi(x: Any) -> int:
    try:
        return int(hour(x)[:2])
    except Exception:
        return 0

def pct(x: Any) -> str:
    v = n(x)
    return '—' if v is None else f'{round(clamp(v))}%'

def deg(x: Any) -> str:
    v = n(x)
    return '—' if v is None else f'{v:.1f}°C'

def kmh(x: Any) -> str:
    v = n(x)
    return '—' if v is None else f'{v:.1f} km/jam'

def now() -> str:
    return dt.datetime.now().strftime('%A, %d %B %Y, %H:%M WIB')

def slugify(x: str) -> str:
    y = re.sub(r'[^a-z0-9]+','-',s(x,'location').lower()).strip('-')
    return y or 'location'

def read_json(p: Path, default: Any = None) -> Any:
    try:
        return json.loads(p.read_text(encoding='utf-8', errors='replace'))
    except Exception:
        return default

def write_json(p: Path, data: Any) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')

def read_csv(p: Path) -> List[Dict[str,str]]:
    if not p.exists():
        return []
    try:
        with p.open('r', encoding='utf-8-sig', newline='') as f:
            return [dict(r) for r in csv.DictReader(f)]
    except Exception:
        return []

def write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding='utf-8')

def pick(row: Dict[str,Any], *names: str, default: Any = None) -> Any:
    low = {str(k).lower(): v for k,v in row.items()}
    for name in names:
        if name in row and row[name] not in [None,'']:
            return row[name]
        if name.lower() in low and low[name.lower()] not in [None,'']:
            return low[name.lower()]
    return default

# ------------------------- data repair -------------------------
def rclass(rain: Any = None, score: Any = None, limited: bool = False) -> str:
    if limited:
        return 'limited'
    m = max(clamp(rain, default=0), clamp(score, default=0))
    if m >= 75: return 'danger'
    if m >= 55: return 'rain'
    if m >= 30: return 'watch'
    return 'safe'

def rlabel(c: str) -> str:
    return {'safe':'Aman','watch':'Dipantau','rain':'Waspada','danger':'Risiko tinggi','limited':'Data terbatas'}.get(c,'Dipantau')

def color(c: str) -> str:
    return {'safe':'#25df8f','watch':'#ffc247','rain':'#ff8a3d','danger':'#ff3f6e','limited':'#a7b1ff'}.get(c,'#21a8ff')

def condition(h: str, rain: Any, temp: Any, rh: Any, limited: bool) -> str:
    if limited: return 'Data terbatas'
    p = clamp(rain, default=0); t = n(temp); r = n(rh); k = hi(h)
    if p >= 75: return 'Hujan kuat'
    if p >= 55: return 'Hujan lokal'
    if p >= 35: return 'Awan hujan naik'
    if r is not None and r >= 88 and (k <= 8 or k >= 19): return 'Lembap'
    if t is not None and t >= 30 and 10 <= k <= 15: return 'Panas'
    if 12 <= k <= 18: return 'Awan tumbuh'
    return 'Berawan'

def row_to_hour(row: Dict[str,Any], day='Hari ini', date='') -> Dict[str,Any]:
    h = hour(pick(row,'hour','jam','time','local_time','target_hour','datetime','timestamp', default='00:00'))
    temp = n(pick(row,'temp_c','temperature_c','temperature_2m_c','avg_temperature_c','t2m','suhu'))
    rh = n(pick(row,'humidity_pct','relative_humidity','relative_humidity_2m','rh','kelembapan'))
    heat = n(pick(row,'heat_index_c','apparent_temperature_c','feels_like_c','terasa'), temp)
    rain = n(pick(row,'rain_probability','rain_probability_raw','precip_probability','precipitation_probability','pop','hujan'))
    wind = n(pick(row,'wind_kmh','wind_speed_kmh','wind_speed_10m_kmh','angin'))
    score = n(pick(row,'risk_score','score','risk', default=rain if rain is not None else 0))
    valid = any(v is not None for v in [temp,rh,heat,rain,wind])
    limited = not valid
    cls = s(pick(row,'risk_class', default=''))
    if cls not in {'safe','watch','rain','danger','limited'}:
        cls = rclass(rain, score, limited)
    return {'date':s(pick(row,'date','tanggal',default=date),date),'day_tag':s(pick(row,'day_tag','day','hari',default=day),day),'hour':h,'temp_c':temp,'humidity_pct':rh,'heat_index_c':heat,'rain_probability':rain if rain is not None else None,'wind_kmh':wind,'risk_score':clamp(score, default=35 if limited else 0),'risk_class':cls,'risk_label':rlabel(cls),'condition':s(pick(row,'condition','weather','cuaca',default='')) or condition(h,rain,temp,rh,limited),'valid':valid,'limited':limited}

def mean(vals):
    xs = [n(v) for v in vals]; xs=[x for x in xs if x is not None]
    return sum(xs)/len(xs) if xs else None

def mx(vals):
    xs = [n(v) for v in vals]; xs=[x for x in xs if x is not None]
    return max(xs) if xs else None

def default_hours(day='Hari ini') -> List[Dict[str,Any]]:
    return [row_to_hour({'hour':h}, day) for h in ['00:00','06:00','09:00','12:00','15:00','18:00','21:00']]

def windows(hours: List[Dict[str,Any]]) -> List[str]:
    good = sorted({hi(x['hour']) for x in hours if x.get('risk_class') == 'safe' and x.get('valid')})
    if not good:
        good = sorted({hi(x['hour']) for x in hours if x.get('risk_class') in {'safe','watch'}})
    if not good:
        return ['Cek manual']
    blocks=[]; a=b=good[0]
    for x in good[1:]:
        if x == b+1: b=x
        else: blocks.append((a,b)); a=b=x
    blocks.append((a,b))
    return [f'{a:02d}:00' if a==b else f'{a:02d}:00–{b:02d}:00' for a,b in blocks[:3]]

def period_summary(hours: List[Dict[str,Any]]) -> List[Dict[str,Any]]:
    defs=[('Pagi',5,10),('Siang',11,14),('Sore',15,18),('Malam',19,4)]
    out=[]
    for name,a,b in defs:
        sub=[x for x in hours if (a<=hi(x['hour'])<=b if a<=b else hi(x['hour'])>=a or hi(x['hour'])<=b)]
        valid=[x for x in sub if x.get('valid')]
        basis=valid or sub
        worst=max(basis, key=lambda z:n(z.get('risk_score'),0) or 0) if basis else {}
        cls=s(worst.get('risk_class'),'limited' if not valid else 'watch')
        out.append({'name':name,'hour':s(worst.get('hour'),'—'),'condition':s(worst.get('condition'),'Data terbatas'),'temp_c':mean([x.get('temp_c') for x in valid]),'rain_probability':mx([x.get('rain_probability') for x in valid]),'risk_class':cls,'risk_label':rlabel(cls)})
    return out

def summarize(day: str, rows: List[Dict[str,Any]], date='') -> Dict[str,Any]:
    rows=sorted(rows or default_hours(day), key=lambda x:hi(x['hour']))
    valid=[x for x in rows if x.get('valid')]
    basis=valid or rows
    peak=max(basis, key=lambda z:n(z.get('rain_probability'),-1) if n(z.get('rain_probability')) is not None else -1) if basis else {}
    worst=max(basis, key=lambda z:n(z.get('risk_score'),0) or 0) if basis else {}
    cls=s(worst.get('risk_class'),'limited' if not valid else 'watch')
    if not valid: cls='limited'
    return {'day_tag':day,'date':date,'hours':rows,'periods':period_summary(rows),'peak_rain_probability':n(peak.get('rain_probability')),'peak_rain_hour':s(peak.get('hour'),'—'),'risk_score':clamp(worst.get('risk_score'),default=35 if cls=='limited' else 0),'risk_class':cls,'risk_label':rlabel(cls),'condition':s(worst.get('condition'),'Data terbatas' if cls=='limited' else 'Dipantau'),'avg_temp_c':mean([x.get('temp_c') for x in valid]),'avg_rh':mean([x.get('humidity_pct') for x in valid]),'max_heat_c':mx([x.get('heat_index_c') for x in valid]),'max_wind_kmh':mx([x.get('wind_kmh') for x in valid]),'windows':windows(rows)}

def sentence(loc: str, d: Dict[str,Any]) -> str:
    c=d.get('risk_class','watch'); peak=d.get('peak_rain_hour','—'); rr=pct(d.get('peak_rain_probability')); win=' · '.join(d.get('windows') or [])
    if c=='limited': return f'{loc}: data belum lengkap. Cek langit lokal dulu.'
    if c in {'danger','rain'}: return f'{loc}: siapkan payung. Rawan {peak}, hujan {rr}.'
    if c=='watch': return f'{loc}: masih bisa. Pantau sekitar {peak}.'
    return f'{loc}: relatif aman. Window {win}.'

# ------------------------- loaders -------------------------
def meta_by_slug(root: Path) -> Dict[str,Dict[str,Any]]:
    meta={}
    for name in ['dim_locations.csv','locations.csv']:
        for r in read_csv(root/name):
            slug=s(pick(r,'slug','location_slug',default='')) or slugify(s(pick(r,'location_name','name',default='location')))
            meta[slug]=r
    gj=read_json(root/'langit_all_locations.geojson',{}) or {}
    if isinstance(gj,dict):
        for f in gj.get('features',[]):
            props=f.get('properties') or {}; coords=(f.get('geometry') or {}).get('coordinates') or []
            slug=s(props.get('slug') or props.get('location_slug') or slugify(props.get('location_name') or props.get('name') or ''))
            if slug:
                meta.setdefault(slug,{}).update({'slug':slug,'location_name':props.get('location_name') or props.get('name'),'longitude':coords[0] if len(coords)>0 else None,'latitude':coords[1] if len(coords)>1 else None})
    return meta

def loc_dirs(root: Path) -> List[Path]:
    out=[]
    if not root.exists(): return out
    for p in root.iterdir():
        if p.is_dir() and any((p/x).exists() for x in ['anemos_app.html','anemos_hourly_compact.csv','langit_hourly_intelligence.csv','langit_api_v1.json','anemos_api_v1.json']):
            out.append(p)
    return sorted(out)

def load_api(d: Path, meta: Dict[str,Any]) -> Dict[str,Any]:
    api={}
    for name in ['langit_api_v1.json','anemos_api_v1.json','api.json']:
        if (d/name).exists():
            api=read_json(d/name,{}) or {}; break
    name=s(api.get('location_name'), s(meta.get('location_name'), d.name.replace('-',' ').title()))
    slug=s(api.get('location_slug'), s(meta.get('slug'), d.name))
    lat=n(api.get('latitude'), n(meta.get('latitude'), n(meta.get('lat'))))
    lon=n(api.get('longitude'), n(meta.get('longitude'), n(meta.get('lon'))))
    if lat is None or lon is None:
        gj=read_json(d/'langit_location.geojson',{}) or {}; feats=gj.get('features') or []
        if feats:
            coords=(feats[0].get('geometry') or {}).get('coordinates') or []
            if len(coords)>=2: lon,lat=n(coords[0]),n(coords[1])
    rows=[]
    for fname in ['langit_hourly_intelligence.csv','anemos_hourly_compact.csv','anemos_risk_timeline.csv']:
        rows=read_csv(d/fname)
        if rows: break
    if not rows and isinstance(api.get('days'),list):
        for dayobj in api.get('days',[])[:3]:
            for h in dayobj.get('key_hours') or dayobj.get('hours') or []:
                if isinstance(h,dict):
                    x=dict(h); x.setdefault('day_tag',dayobj.get('day_tag','')); x.setdefault('date',dayobj.get('date','')); rows.append(x)
    parsed=[row_to_hour(r) for r in rows] if rows else default_hours()
    groups={}
    for r in parsed:
        key=(s(r.get('day_tag'),'Hari ini'),s(r.get('date'),'')); groups.setdefault(key,[]).append(r)
    names=['Hari ini','Besok','Lusa']; days=[]
    for i,key in enumerate(list(groups.keys())[:3]):
        tag,date=key; days.append(summarize(names[i] if tag in {'','Hari'} else tag, groups[key], date))
    while len(days)<3:
        i=len(days); days.append(summarize(names[i], default_hours(names[i])))
    sources=[]
    for fname in ['source_status.csv','source_status_all_locations.csv','langit_source_status.csv']:
        sources=read_csv(d/fname)
        if sources: break
    return {'brand':BRAND,'version':VERSION,'generated_at':now(),'location_name':name,'location_slug':slug,'latitude':lat,'longitude':lon,'days':days,'today':days[0],'sources':sources}

# ------------------------- components -------------------------
CSS = '''
:root{--bg:#06111f;--panel:#102238;--panel2:#172b43;--line:#274861;--text:#f6f9ff;--muted:#9fb5cc;--blue:#21a8ff;--green:#25df8f;--amber:#ffc247;--orange:#ff8a3d;--red:#ff3f6e;--limited:#a7b1ff;--shadow:0 24px 90px rgba(0,0,0,.35)}
*{box-sizing:border-box}body{margin:0;color:var(--text);font-family:Inter,Manrope,"Plus Jakarta Sans",system-ui,-apple-system,Segoe UI,sans-serif;background:radial-gradient(circle at 74% 11%,rgba(33,168,255,.34),transparent 28%),radial-gradient(circle at 16% -2%,rgba(37,223,143,.13),transparent 21%),linear-gradient(180deg,#06111f,#081a2e 52%,#06111f);letter-spacing:-.02em}body:before{content:"";position:fixed;inset:0;pointer-events:none;opacity:.20;background-image:linear-gradient(rgba(255,255,255,.05) 1px,transparent 1px),linear-gradient(90deg,rgba(255,255,255,.05) 1px,transparent 1px);background-size:36px 36px}a{text-decoration:none;color:inherit}.top{position:sticky;top:0;z-index:50;display:flex;justify-content:space-between;align-items:center;gap:18px;padding:16px clamp(16px,5vw,84px);background:rgba(6,17,31,.84);backdrop-filter:blur(22px);border-bottom:1px solid rgba(120,170,220,.18)}.brand{display:flex;align-items:center;gap:12px}.logo{width:38px;height:38px;border-radius:14px;background:radial-gradient(circle at 30% 25%,#65f3ff,transparent 25%),linear-gradient(135deg,#176bff,#21a8ff 45%,#20df8f);box-shadow:0 0 34px rgba(33,168,255,.48)}.brand b{display:block;font-size:18px}.brand small{color:var(--muted);font-size:11px}.navbar{display:flex;gap:8px;flex-wrap:wrap;justify-content:flex-end}.nav,.btn{border:1px solid rgba(148,190,235,.28);background:rgba(255,255,255,.045);border-radius:999px;padding:9px 13px;font-size:12px;font-weight:850;color:#d8e7f7}.nav.active,.btn.primary{background:linear-gradient(135deg,#0b8dff,#28c6ff);border-color:transparent;color:#fff;box-shadow:0 12px 30px rgba(33,168,255,.28)}.wrap{width:min(1380px,calc(100% - 32px));margin:0 auto;padding:26px 0 70px}.hero-grid{display:grid;grid-template-columns:minmax(0,1.7fr) minmax(270px,.58fr);gap:18px}.hero{min-height:280px;border:1px solid rgba(73,210,255,.42);border-radius:34px;padding:34px;background:radial-gradient(circle at 85% 72%,rgba(73,210,255,.60),transparent 26%),linear-gradient(135deg,#123e82,#176bff 52%,#1dc4ff);box-shadow:var(--shadow);overflow:hidden;position:relative}.hero:after{content:"";position:absolute;right:-90px;bottom:-110px;width:360px;height:360px;border-radius:999px;background:rgba(255,255,255,.18)}.chips{display:flex;gap:8px;flex-wrap:wrap;position:relative;z-index:1}.chip{font-size:11px;font-weight:900;text-transform:uppercase;letter-spacing:.06em;background:rgba(255,255,255,.13);border:1px solid rgba(255,255,255,.28);padding:7px 10px;border-radius:999px}.hero h1{font-size:clamp(46px,7vw,94px);line-height:.9;margin:38px 0 14px;position:relative;z-index:1}.hero p{font-size:clamp(16px,2vw,22px);margin:0;color:#eaf5ff;position:relative;z-index:1}.side{display:grid;grid-template-columns:1fr 1fr;gap:14px}.tile,.panel,.big,.period,.activity,.hour,.daycard{background:linear-gradient(180deg,rgba(255,255,255,.07),rgba(255,255,255,.035));border:1px solid rgba(148,190,235,.22);border-radius:20px;box-shadow:0 16px 50px rgba(0,0,0,.18)}.tile{padding:22px;display:flex;flex-direction:column;justify-content:space-between}.weather-tile{grid-column:1/-1;min-height:145px}.tile small,.kpi span{font-size:11px;color:var(--muted);font-weight:900;text-transform:uppercase;letter-spacing:.08em}.tile strong{font-size:42px}.tile b{font-size:24px}.notice{margin:16px 0 18px;padding:9px 14px;border:1px solid rgba(255,194,71,.45);background:rgba(255,194,71,.08);border-radius:999px;color:#ffd98a;font-size:12px;font-weight:850}.decision{display:grid;grid-template-columns:minmax(0,1.36fr) minmax(280px,.64fr);gap:18px;margin-top:18px}.big{padding:30px;border-radius:30px;min-height:250px;display:flex;flex-direction:column;justify-content:space-between}.badge{display:inline-flex;width:max-content;padding:7px 10px;border-radius:999px;border:1px solid rgba(255,255,255,.2);font-size:12px;font-weight:900}.badge.safe{color:#b9ffe1;border-color:rgba(37,223,143,.45)}.badge.watch{color:#ffe5a3;border-color:rgba(255,194,71,.48)}.badge.rain{color:#ffd1b5;border-color:rgba(255,138,61,.48)}.badge.danger{color:#ffc2d0;border-color:rgba(255,63,110,.48)}.badge.limited{color:#d5d9ff;border-color:rgba(167,177,255,.48)}.big h2{font-size:clamp(34px,5vw,66px);line-height:.94;margin:18px 0}.big p{color:#cfe0f5}.kpis{display:grid;grid-template-columns:1fr 1fr;gap:12px}.kpi{padding:18px;border-radius:18px;background:rgba(255,255,255,.065);border:1px solid rgba(148,190,235,.22)}.kpi strong{display:block;font-size:28px;margin:8px 0 4px}.panel{margin-top:22px;padding:24px;border-radius:30px}.head{display:flex;justify-content:space-between;align-items:end;gap:14px;margin-bottom:18px}.head h2{margin:0;font-size:25px}.head p{margin:0;color:var(--muted)}.timeline{display:grid;grid-template-columns:repeat(var(--n,8),1fr);gap:10px;align-items:end;min-height:170px}.bar{display:flex;flex-direction:column;align-items:center;justify-content:end;gap:8px}.bar .v{width:100%;border-radius:14px 14px 8px 8px;min-height:8px;background:linear-gradient(180deg,var(--c),rgba(255,255,255,.06));box-shadow:0 14px 30px rgba(0,0,0,.18)}.bar b{font-size:14px}.bar small{font-size:11px;color:var(--muted)}.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}.grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}.grid4{display:grid;grid-template-columns:repeat(4,1fr);gap:16px}.period,.activity,.daycard{padding:20px}.period h3,.activity h3,.daycard h3{margin:0 0 8px;font-size:22px}.period p,.activity p,.daycard p{margin:0;color:#c8d8eb}.minirow{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:16px}.mini{padding:11px;border-radius:14px;background:rgba(11,37,65,.78);border:1px solid rgba(73,128,184,.34)}.mini span{display:block;color:var(--muted);font-size:10px}.mini b{font-size:18px}.hours{display:grid;gap:10px}.hour{display:grid;grid-template-columns:82px minmax(0,1.5fr) repeat(5,minmax(92px,.55fr));align-items:center;gap:10px;padding:13px;border-left:5px solid var(--accent)}.hour .time{font-size:20px;font-weight:950}.hour h3{margin:0;font-size:17px}.hour p{margin:2px 0 0;color:var(--muted);font-size:12px}.hbox{padding:12px;border-radius:14px;background:#0e2948;border:1px solid rgba(58,119,184,.42)}.hbox b{display:block;font-size:17px}.hbox span{font-size:10px;color:var(--muted)}.activity-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}.activity{border-color:var(--accent);min-height:150px}.focus{margin-top:14px;color:#89cdfd;font-size:12px;font-weight:900;text-transform:uppercase}.map{width:100%;height:460px;border:0;border-radius:22px;background:#020b15}.map.small{height:380px}.actions{display:flex;gap:10px;flex-wrap:wrap;margin-top:14px}details{border:1px solid rgba(148,190,235,.18);border-radius:18px;padding:14px;background:rgba(255,255,255,.035)}summary{cursor:pointer;font-weight:900}table{width:100%;border-collapse:collapse;min-width:600px}th,td{text-align:left;padding:12px;border-bottom:1px solid rgba(148,190,235,.15)}th{font-size:11px;color:#8fd0ff;text-transform:uppercase}.footer{text-align:center;color:var(--muted);font-size:12px;margin:30px 0}@media(max-width:980px){.hero-grid,.decision,.grid2,.grid3,.grid4{grid-template-columns:1fr}.hour{grid-template-columns:70px 1fr 1fr 1fr}.hour .hbox:nth-last-child(-n+2){display:none}.activity-grid{grid-template-columns:1fr}.top{align-items:flex-start;flex-direction:column}}@media(max-width:620px){.wrap{width:min(100% - 22px,1380px)}.hero{padding:24px;border-radius:26px}.hero h1{font-size:44px}.side,.kpis,.minirow{grid-template-columns:1fr 1fr}.timeline{overflow:auto;display:flex;min-height:155px}.bar{min-width:54px}.bar .v{width:44px}.hour{grid-template-columns:64px 1fr}.hour .hbox{display:none}.hour .rainbox,.hour .riskbox{display:block}.panel{padding:16px}}
'''

def nav(api, active):
    items=[('Hari ini','anemos_app.html','today'),('3 hari','anemos_3day.html','3day'),('Aktivitas','anemos_activity.html','activity'),('Peta','langit_map_room.html','map'),('Data','langit_model_court.html','data'),('Akurasi','sentinel_x_accuracy_public.html','accuracy')]
    links=''.join(f'<a class="nav {"active" if k==active else ""}" href="{h}">{l}</a>' for l,h,k in items)
    return f'<header class="top"><a class="brand" href="../index.html"><span class="logo"></span><span><b>{BRAND}</b><small>{esc(api["location_name"])} · {VERSION}</small></span></a><nav class="navbar">{links}</nav></header>'

def doc(api, active, title, body):
    return f'<!doctype html><html lang="id"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)}</title><meta name="theme-color" content="#06111f"><style>{CSS}</style></head><body>{nav(api,active)}<main class="wrap">{body}<p class="footer">{BRAND} · {VERSION} · {esc(api.get("generated_at",now()))}</p></main></body></html>'

def kpi(label, value, sub=''):
    return f'<div class="kpi"><span>{esc(label)}</span><strong>{esc(value)}</strong><small>{esc(sub)}</small></div>'

def hero(api, heading, sub, d):
    return f'<section class="hero-grid"><article class="hero"><div class="chips"><span class="chip">{BRAND}</span><span class="chip">{VERSION}</span><span class="chip">Update {esc(api.get("generated_at",now()))}</span></div><h1>{esc(heading)}</h1><p>{esc(sub)}</p></article><aside class="side"><div class="tile weather-tile"><small>Cuaca</small><strong>{deg(d.get("avg_temp_c"))}</strong><p>{esc(d.get("condition","Cuaca lokal"))}</p></div><div class="tile"><small>Hujan</small><b>{pct(d.get("peak_rain_probability"))}</b><small>puncak</small></div><div class="tile"><small>Status</small><b>{esc(d.get("risk_label","Dipantau"))}</b><small>ringkasan</small></div></aside></section><div class="notice">{esc(DISCLAIMER)}</div>'

def decision(api,d):
    c=d.get('risk_class','watch')
    return f'<section class="decision"><article class="big"><div><span class="badge {esc(c)}">{esc(d.get("risk_label"))}</span><h2>{esc(sentence(api["location_name"],d))}</h2></div><p>Fokus: {esc(d.get("peak_rain_hour","—"))}. Window: {esc(" · ".join(d.get("windows") or []))}.</p></article><aside class="kpis">{kpi("Risk",f"{round(clamp(d.get("risk_score"))):.0f}/100",d.get("risk_label",""))}{kpi("Puncak hujan",pct(d.get("peak_rain_probability")),"sekitar "+s(d.get("peak_rain_hour"),"—"))}{kpi("Window", " · ".join(d.get("windows") or ["—"]), "aktivitas")}{kpi("Confidence", "Sedang" if c != "limited" else "Rendah", "data publik")}</aside></section>'

def timeline(hours):
    bars=[]; hh=hours[:12] if len(hours)>12 else hours
    for x in hh:
        p=clamp(x.get('rain_probability'),default=0); c=x.get('risk_class','limited'); height=max(8,round(18+p*1.25))
        bars.append(f'<div class="bar"><b>{pct(x.get("rain_probability"))}</b><div class="v" style="height:{height}px;--c:{color(c)}"></div><small>{esc(x.get("hour"))}</small></div>')
    return f'<div class="timeline" style="--n:{max(1,len(hh))}">{"".join(bars)}</div>'

def periods_html(d):
    cards=[]
    for p in d.get('periods',[]):
        c=p.get('risk_class','limited')
        cards.append(f'<article class="period" style="border-color:{color(c)}"><h3>{esc(p.get("name"))}</h3><p>{esc(p.get("condition"))}</p><small>Jam perhatian: {esc(p.get("hour"))}</small><div class="minirow"><div class="mini"><span>Suhu</span><b>{deg(p.get("temp_c"))}</b></div><div class="mini"><span>Hujan</span><b>{pct(p.get("rain_probability"))}</b></div><div class="mini"><span>Risiko</span><b>{esc(p.get("risk_label"))}</b></div></div></article>')
    return '<div class="grid4">'+''.join(cards)+'</div>'

def hours_html(d,title='Detail jam'):
    rows=[]
    for x in d.get('hours',[]):
        c=x.get('risk_class','limited')
        rows.append(f'<div class="hour" style="--accent:{color(c)}"><div class="time">{esc(x.get("hour"))}</div><div><h3>{esc(x.get("condition"))}</h3><p>{esc(rlabel(c))}</p></div><div class="hbox"><b>{deg(x.get("temp_c"))}</b><span>Suhu</span></div><div class="hbox"><b>{pct(x.get("humidity_pct"))}</b><span>RH</span></div><div class="hbox"><b>{deg(x.get("heat_index_c"))}</b><span>Terasa</span></div><div class="hbox rainbox"><b>{pct(x.get("rain_probability"))}</b><span>Hujan</span></div><div class="hbox riskbox"><b>{esc(rlabel(c))}</b><span>Risiko</span></div></div>')
    return f'<section class="panel"><div class="head"><h2>{esc(title)}</h2><p>Detail angka.</p></div><div class="hours">{"".join(rows)}</div></section>'

def acts(d):
    c=d.get('risk_class','watch'); peak=d.get('peak_rain_hour','—'); win=' · '.join(d.get('windows') or ['Cek manual'])
    if c in {'danger','rain'}: data=[('Motor','Bawa jas hujan',f'Hindari {peak}.',peak,c),('Jalan kaki','Rute teduh','Cari titik berteduh.',peak,c),('Jemur','Pagi saja','Jangan ditinggal.','pagi','watch'),('Outdoor','Plan B','Siapkan opsi indoor.',peak,c),('Olahraga','Pilih window',win,win,'watch'),('Foto/city walk','Pantau awan','Lindungi elektronik.',peak,'watch')]
    elif c=='limited': data=[('Motor','Cek manual','Data belum lengkap.','sekarang','limited'),('Jalan kaki','Aman bersyarat','Lihat awan lokal.','sekarang','limited'),('Jemur','Jangan ditinggal','Pantau berkala.','pagi','limited'),('Outdoor','Fleksibel','Siapkan teduh.','manual','limited'),('Olahraga','Durasi pendek','Cek radar/BMKG.','manual','limited'),('Foto/city walk','Cek langit','Pantau perubahan cepat.','manual','limited')]
    elif c=='watch': data=[('Motor','Masih bisa','Payung lebih aman.',peak,'watch'),('Jalan kaki','Aman bersyarat','Pantau awan.',win,'safe'),('Jemur','Pagi–siang','Angkat sebelum sore.','pagi','safe'),('Outdoor','Bisa fleksibel','Siapkan teduh.',win,'watch'),('Olahraga','Pilih jam nyaman',win,win,'safe'),('Foto/city walk','Cek langit','Cahaya bisa bagus.',peak,'watch')]
    else: data=[('Motor','Aman','Tetap pantau lokal.',win,'safe'),('Jalan kaki','Cocok','Pilih jam nyaman.',win,'safe'),('Jemur','Cukup aman','Angkat sebelum malam.','pagi–siang','safe'),('Outdoor','Bisa','Tetap fleksibel.',win,'safe'),('Olahraga','Aman','Pagi/sore nyaman.',win,'safe'),('Foto/city walk','Cocok','Pantau cahaya.',win,'safe')]
    return '<div class="activity-grid">'+''.join(f'<article class="activity" style="--accent:{color(cls)}"><h3>{esc(a)}</h3><p><b>{esc(st)}</b></p><p>{esc(h)}</p><div class="focus">Fokus: {esc(f)}</div></article>' for a,st,h,f,cls in data)+'</div>'

def map_section():
    return '<section class="panel"><div class="head"><h2>Map Room</h2><p>Zona risiko dan slider waktu.</p></div><iframe class="map small" src="langit_map_room.html" loading="lazy"></iframe><div class="actions"><a class="btn primary" href="langit_map_room.html">Buka peta penuh</a><a class="btn" href="langit_location.geojson">GeoJSON</a></div></section>'

def share(api,d):
    msg=f'{BRAND} · {api["location_name"]}\n{sentence(api["location_name"],d)}\n{DISCLAIMER}'
    return f'<section class="grid2"><article class="panel"><div class="head"><h2>Share singkat</h2><p>Format WA.</p></div><textarea readonly style="width:100%;min-height:130px;border-radius:18px;background:#06111f;border:1px solid rgba(148,190,235,.25);color:white;padding:16px;font-family:ui-monospace,monospace">{esc(msg)}</textarea></article><article class="panel"><div class="head"><h2>Catatan</h2><p>Ringkas.</p></div><p style="color:#cfe0f5;line-height:1.7">{esc(DISCLAIMER)} Hujan lokal dapat bergeser beberapa kilometer atau berubah beberapa jam.</p></article></section>'

def today_page(api):
    d=api['today']
    body=hero(api,f'Prakiraan {api["location_name"]}',sentence(api['location_name'],d),d)+decision(api,d)+f'<section class="panel"><div class="head"><h2>Timeline hujan</h2><p>Visual utama hari ini.</p></div>{timeline(d["hours"])}</section>'+f'<section class="panel"><div class="head"><h2>Pagi–malam</h2><p>Ringkas per periode.</p></div>{periods_html(d)}</section>'+f'<section class="panel"><div class="head"><h2>Saran aktivitas</h2><p>Tanpa banyak teks.</p></div>{acts(d)}</section>'+map_section()+hours_html(d,'Detail jam')
    return doc(api,'today',f'{BRAND} — {api["location_name"]}',body)

def day3_page(api):
    cards=[]; strips=[]
    for d in api['days'][:3]:
        c=d.get('risk_class','watch')
        cards.append(f'<article class="daycard" style="border-color:{color(c)}"><small>{esc(d.get("day_tag"))}</small><h3>{esc(d.get("risk_label"))}</h3><p>{esc(sentence(api["location_name"],d))}</p><div class="minirow"><div class="mini"><span>Hujan</span><b>{pct(d.get("peak_rain_probability"))}</b></div><div class="mini"><span>Jam</span><b>{esc(d.get("peak_rain_hour"))}</b></div><div class="mini"><span>Window</span><b>{esc(" · ".join(d.get("windows") or []))}</b></div></div></article>')
        strips.append(f'<section class="panel"><div class="head"><h2>{esc(d.get("day_tag"))}</h2><p>{esc(d.get("risk_label"))}</p></div>{timeline(d["hours"])}</section>')
    body=hero(api,'Prakiraan 3 hari',f'{api["location_name"]}: bandingkan risiko tanpa baca tabel panjang.',api['today'])+f'<section class="panel"><div class="head"><h2>Ringkasan 3 hari</h2><p>Fokus keputusan.</p></div><div class="grid3">{"".join(cards)}</div></section>'+''.join(strips)+''.join(hours_html(d,'Detail · '+d.get('day_tag','')) for d in api['days'][:3])
    return doc(api,'3day',f'{BRAND} 3 Hari — {api["location_name"]}',body)

def activity_page(api):
    d=api['today']
    body=hero(api,'Saran aktivitas','Motor, jalan kaki, jemur, olahraga, outdoor, dan foto/city walk.',d)+decision(api,d)+f'<section class="panel"><div class="head"><h2>Keputusan cepat</h2><p>Visual-first.</p></div>{acts(d)}</section>'+f'<section class="panel"><div class="head"><h2>Timeline hujan</h2><p>Jam rawan terlihat langsung.</p></div>{timeline(d["hours"])}</section>'+hours_html(d,'Jam rawan')+share(api,d)
    return doc(api,'activity',f'{BRAND} Aktivitas — {api["location_name"]}',body)

def data_page(api):
    active=sum(1 for x in api.get('sources',[]) if '200' in ' '.join(map(str,x.values())) or 'aktif' in ' '.join(map(str,x.values())).lower()); total=len(api.get('sources',[])) or 9; pc=round(active/max(1,total)*100)
    rows=''.join('<tr>'+''.join(f'<td>{esc(v)}</td>' for v in list(r.values())[:5])+'</tr>' for r in api.get('sources',[])[:16]) or '<tr><td>Data sumber tidak tersedia di output publik.</td><td>—</td></tr>'
    body=hero(api,'Data confidence','Ringkasan sumber dibuat compact. Detail teknis disimpan di bawah.',api['today'])+f'<section class="panel"><div class="head"><h2>Confidence operasional</h2><p>{active}/{total} sumber aktif/terbaca.</p></div><div style="height:14px;border-radius:999px;background:#10233a;overflow:hidden"><i style="display:block;height:100%;width:{pc}%;background:linear-gradient(90deg,#25df8f,#21a8ff)"></i></div></section><section class="panel"><details><summary>Tabel teknis sumber</summary><div style="overflow:auto;margin-top:12px"><table><tbody>{rows}</tbody></table></div></details></section>'+map_section()+share(api,api['today'])
    return doc(api,'data',f'{BRAND} Data — {api["location_name"]}',body)

def accuracy_page(api):
    body=hero(api,'Akurasi','Skor akurasi ditampilkan setelah pasangan prakiraan–observasi cukup.',api['today'])+'<section class="panel"><div class="head"><h2>Belum cukup data</h2><p>Target awal 30 pasangan.</p></div><div style="height:14px;border-radius:999px;background:#10233a;overflow:hidden"><i style="display:block;height:100%;width:0%;background:#21a8ff"></i></div><p style="color:#cfe0f5">Halaman ini tidak mengklaim akurasi sebelum data observasi cukup.</p></section>'
    return doc(api,'accuracy',f'{BRAND} Akurasi — {api["location_name"]}',body)

MAP_CSS='''html,body,#map{height:100%;margin:0;background:#06111f;font-family:Inter,Manrope,system-ui;color:#f6f9ff}.panel{position:absolute;z-index:900;left:20px;top:20px;width:min(360px,calc(100vw - 40px));background:rgba(6,17,31,.86);border:1px solid rgba(148,190,235,.32);border-radius:24px;padding:18px;box-shadow:0 24px 80px rgba(0,0,0,.38);backdrop-filter:blur(18px)}.panel h1{font-size:22px;margin:0 0 8px}.panel p{font-size:12px;color:#c9d8ea;margin:0 0 12px;line-height:1.45}.btn{display:inline-flex;border-radius:999px;padding:9px 13px;background:#21a8ff;color:white;text-decoration:none;font-weight:900;font-size:12px}.legend{position:absolute;right:18px;bottom:18px;z-index:900;background:rgba(6,17,31,.86);border:1px solid rgba(148,190,235,.32);border-radius:18px;padding:12px;backdrop-filter:blur(18px);font-size:12px}.legend div{display:flex;align-items:center;gap:8px;margin:5px}.dot{width:10px;height:10px;border-radius:50%}.timebar{position:absolute;left:50%;bottom:24px;transform:translateX(-50%);z-index:900;display:flex;gap:8px;max-width:calc(100vw - 40px);overflow:auto;padding:12px;border-radius:999px;background:rgba(6,17,31,.82);border:1px solid rgba(148,190,235,.25);backdrop-filter:blur(18px)}.timebar button{border:1px solid rgba(148,190,235,.25);background:rgba(255,255,255,.07);color:#e9f4ff;border-radius:999px;padding:9px 12px;font-weight:900;cursor:pointer}.timebar button.active{background:#21a8ff;border-color:#21a8ff;color:white}.loading{position:absolute;inset:0;display:grid;place-items:center;color:#9fb5cc;z-index:600;pointer-events:none}.spinner{width:28px;height:28px;border-radius:50%;border:2px solid rgba(255,255,255,.15);border-top-color:#21a8ff;animation:spin 1s linear infinite}@keyframes spin{to{transform:rotate(360deg)}}.leaflet-container{background:#06111f}.leaflet-control-attribution{background:rgba(6,17,31,.72)!important;color:#9fb5cc!important}@media(max-width:620px){.panel{left:12px;top:12px}.legend{right:10px;bottom:82px}.timebar{bottom:12px}}'''

def geo(api):
    feats=[]; lat=n(api.get('latitude')); lon=n(api.get('longitude'))
    if lat is None or lon is None: lat,lon=-6.889,107.61
    for h in api['today']['hours']:
        c=h.get('risk_class','limited')
        feats.append({'type':'Feature','properties':{'name':api['location_name'],'hour':h.get('hour'),'risk_class':c,'risk_label':rlabel(c),'rain_probability':h.get('rain_probability'),'risk_score':h.get('risk_score'),'condition':h.get('condition'),'radius_m':2200},'geometry':{'type':'Point','coordinates':[lon,lat]}})
    return {'type':'FeatureCollection','features':feats}

def map_html(title, back, gj, multi=False):
    data=json.dumps(gj,ensure_ascii=False); center=[-6.889,107.61]
    try:
        co=gj['features'][0]['geometry']['coordinates']; center=[co[1],co[0]]
    except Exception: pass
    return f'''<!doctype html><html lang="id"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)}</title><link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"><style>{MAP_CSS}</style></head><body><div id="map"></div><div class="loading" id="loading"><div class="spinner"></div></div><section class="panel"><h1>{esc(title)}</h1><p>Layer risiko, peluang hujan, dan batas Indonesia. Zoom out dibatasi.</p><a class="btn" href="{esc(back)}">Kembali</a></section><div class="legend"><div><span class="dot" style="background:#25df8f"></span>Aman</div><div><span class="dot" style="background:#ffc247"></span>Dipantau</div><div><span class="dot" style="background:#ff8a3d"></span>Waspada</div><div><span class="dot" style="background:#ff3f6e"></span>Risiko tinggi</div><div><span class="dot" style="background:#a7b1ff"></span>Data terbatas</div></div><div class="timebar" id="timebar"></div><script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script><script>(function(){{const DATA={data};const indonesiaBounds=L.latLngBounds({json.dumps(ID_BOUNDS)});const center={json.dumps(center)};const col=c=>({{safe:'#25df8f',watch:'#ffc247',rain:'#ff8a3d',danger:'#ff3f6e',limited:'#a7b1ff'}}[c]||'#21a8ff');const map=L.map('map',{{center:center,zoom:{5 if multi else 11},minZoom:5,maxZoom:17,maxBounds:indonesiaBounds,maxBoundsViscosity:1.0,worldCopyJump:false,scrollWheelZoom:true}});const dark=L.tileLayer('https://{{s}}.basemaps.cartocdn.com/dark_all/{{z}}/{{x}}/{{y}}{{r}}.png',{{subdomains:'abcd',maxZoom:19,noWrap:true,bounds:indonesiaBounds,attribution:'&copy; OpenStreetMap & CARTO'}}).addTo(map);const detail=L.tileLayer('https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png',{{maxZoom:19,minZoom:5,noWrap:true,bounds:indonesiaBounds,attribution:'&copy; OpenStreetMap'}});L.control.layers({{'Dark':dark,'Detail':detail}},null,{{position:'topright'}}).addTo(map);let layer=L.layerGroup().addTo(map);const feats=(DATA&&DATA.features)?DATA.features:[];const hours=Array.from(new Set(feats.map(f=>(f.properties||{{}}).hour).filter(Boolean))).sort();const bar=document.getElementById('timebar');function pop(p){{return `<b>${{p.name||'Zona'}}</b><br>Status: ${{p.risk_label||'-'}}<br>Hujan: ${{p.rain_probability??'—'}}%<br>Score: ${{p.risk_score??'—'}}/100<br><small>${{p.condition||''}}</small>`}}function draw(h){{layer.clearLayers();feats.filter(f=>!h||!((f.properties||{{}}).hour)||(f.properties||{{}}).hour===h).forEach(f=>{{if(!f.geometry||!f.geometry.coordinates)return;const p=f.properties||{{}},latlng=[f.geometry.coordinates[1],f.geometry.coordinates[0]],cc=col(p.risk_class);L.circle(latlng,{{radius:p.radius_m||2200,color:cc,fillColor:cc,fillOpacity:.17,weight:2}}).bindPopup(pop(p)).addTo(layer);L.circleMarker(latlng,{{radius:10,color:cc,fillColor:cc,fillOpacity:.95,weight:2}}).bindPopup(pop(p)).addTo(layer)}});try{{const g=L.featureGroup(layer.getLayers());if(g.getLayers().length)map.fitBounds(g.getBounds().pad(.25),{{maxZoom:{8 if multi else 12}}});}}catch(e){{}}}}(hours.length?hours:['now']).forEach((h,i)=>{{const b=document.createElement('button');b.textContent=h;b.onclick=()=>{{document.querySelectorAll('#timebar button').forEach(x=>x.classList.remove('active'));b.classList.add('active');draw(h==='now'?null:h)}};if(i===0)b.classList.add('active');bar.appendChild(b)}});draw(hours[0]||null);setTimeout(()=>{{const el=document.getElementById('loading');if(el)el.style.display='none';map.invalidateSize();}},500);}})();</script></body></html>'''

def portal(apis,root):
    fake={'location_name':'Portal','generated_at':now()}; cards=[]; feats=[]
    for api in apis:
        d=api['today']; c=d.get('risk_class','watch'); slug=api['location_slug']
        cards.append(f'<article class="daycard" style="border-color:{color(c)}"><h3>{esc(api["location_name"])}</h3><p>{esc(sentence(api["location_name"],d))}</p><div class="minirow"><div class="mini"><span>Hujan</span><b>{pct(d.get("peak_rain_probability"))}</b></div><div class="mini"><span>Jam</span><b>{esc(d.get("peak_rain_hour"))}</b></div><div class="mini"><span>Score</span><b>{round(clamp(d.get("risk_score"))):.0f}</b></div></div><div class="actions"><a class="btn primary" href="{slug}/anemos_app.html">Buka</a><a class="btn" href="{slug}/anemos_3day.html">3 hari</a><a class="btn" href="{slug}/anemos_activity.html">Aktivitas</a></div></article>')
        lat=n(api.get('latitude')); lon=n(api.get('longitude'))
        if lat is not None and lon is not None:
            feats.append({'type':'Feature','properties':{'name':api['location_name'],'hour':'now','risk_class':c,'risk_label':rlabel(c),'rain_probability':d.get('peak_rain_probability'),'risk_score':d.get('risk_score'),'condition':d.get('condition'),'radius_m':5500},'geometry':{'type':'Point','coordinates':[lon,lat]}})
    gj={'type':'FeatureCollection','features':feats}; write_json(root/'langit_all_locations.geojson',gj); write(root/'langit_portal_map.html',map_html('LANGIT Portal Map','index.html',gj,True))
    body=hero(fake,'Cuaca lokal visual','Pilih lokasi, lihat status, timeline, dan peta tanpa membaca terlalu banyak.',{'avg_temp_c':None,'peak_rain_probability':None,'risk_label':'Dipantau','condition':'Cuaca lokal'})+f'<section class="panel"><div class="head"><h2>Pilih lokasi</h2><p>Ringkasan cepat tiap wilayah.</p></div><div class="grid3">{"".join(cards)}</div></section><section class="panel"><div class="head"><h2>Peta lokasi</h2><p>Zona risiko semua lokasi.</p></div><iframe class="map small" src="langit_portal_map.html" loading="lazy"></iframe><div class="actions"><a class="btn primary" href="langit_portal_map.html">Buka peta penuh</a><a class="btn" href="langit_all_locations.geojson">GeoJSON</a></div></section><section class="panel"><details><summary>Data publik / advanced</summary><div class="actions"><a class="btn" href="forecast_all_locations.csv">Forecast CSV</a><a class="btn" href="ensemble_all_locations.csv">Ensemble CSV</a><a class="btn" href="source_status_all_locations.csv">Status sumber</a><a class="btn" href="forecast_batch_summary.json">Batch summary</a></div></details></section>'
    navless=f'<header class="top"><a class="brand" href="index.html"><span class="logo"></span><span><b>{BRAND}</b><small>Portal · {VERSION}</small></span></a><nav class="navbar"><a class="nav active" href="index.html">Lokasi</a><a class="nav" href="langit_portal_map.html">Peta</a></nav></header>'
    return f'<!doctype html><html lang="id"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{BRAND} Portal</title><style>{CSS}</style></head><body>{navless}<main class="wrap">{body}<p class="footer">{BRAND} · {VERSION} · {now()}</p></main></body></html>'

# ------------------------- rebuild + verify -------------------------
def rebuild(root: Path, base: str='') -> int:
    meta=meta_by_slug(root); dirs=loc_dirs(root)
    if not dirs:
        raise SystemExit('Tidak ada folder lokasi di outputs/. Jalankan forecast dulu.')
    apis=[]
    for d in dirs:
        api=load_api(d, meta.get(d.name, {'slug':d.name})); apis.append(api)
        gj=geo(api); write_json(d/'langit_location.geojson',gj); write_json(d/'langit_api_v1.json',api); write_json(d/'langit_map_layers.json',{'brand':BRAND,'version':VERSION,'geojson':gj})
        write(d/'anemos_app.html',today_page(api)); write(d/'langit_app.html',today_page(api)); write(d/'anemos_today.html',today_page(api))
        write(d/'anemos_3day.html',day3_page(api)); write(d/'langit_3day.html',day3_page(api)); write(d/'anemos_activity.html',activity_page(api)); write(d/'langit_activity.html',activity_page(api))
        write(d/'langit_model_court.html',data_page(api)); write(d/'sentinel_x_accuracy_public.html',accuracy_page(api)); write(d/'langit_map_room.html',map_html(f'LANGIT Map Room — {api["location_name"]}','anemos_app.html',gj,False))
    write(root/'index.html',portal(apis,root)); write_json(root/'langit_portal_manifest.json',{'brand':BRAND,'version':VERSION,'generated_at':now(),'public_base_url':base,'locations':[{'slug':a['location_slug'],'name':a['location_name']} for a in apis]})
    print(f'OK: {VERSION} visual rebuild selesai. lokasi={len(apis)}')
    return verify(root)

def verify(root: Path) -> int:
    errors=[]
    for p in [root/'index.html',root/'langit_portal_map.html']:
        if not p.exists(): errors.append(f'missing {p}')
    for p in root.rglob('*.html'):
        txt=p.read_text(encoding='utf-8',errors='replace')
        if '[.new Set' in txt or 'const hours=[.new Set' in txt: errors.append(f'broken JS syntax {p}')
        if 'ANEMOS sedang' in txt or 'AETHER Sentinel' in txt: errors.append(f'old visible branding {p}')
        if '<pre' in txt and 'L.map' in txt: errors.append(f'raw JS visible {p}')
    if errors:
        for e in errors: print('ERROR:',e)
        return 1
    print('OK: verify passed.')
    return 0

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--root',default='outputs'); ap.add_argument('--public-base-url',default=''); ap.add_argument('--verify-only',action='store_true'); args=ap.parse_args(); root=Path(args.root)
    return verify(root) if args.verify_only else rebuild(root,args.public_base_url)

if __name__ == '__main__':
    raise SystemExit(main())
