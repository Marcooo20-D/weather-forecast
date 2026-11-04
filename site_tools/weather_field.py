"""Native GFS surface grid for the regional map, independent of campus forecasts."""
from __future__ import annotations
import argparse
import datetime as dt
import json
import math
from pathlib import Path
import ssl
import sys
from urllib.parse import urlencode
from urllib.request import Request, urlopen

NAME = "weather_field_v1.json"
WIB = dt.timezone(dt.timedelta(hours=7))
VARIABLES = ["temperature_2m", "relative_humidity_2m", "wind_speed_10m", "wind_direction_10m", "cloud_cover", "precipitation"]
URL = "https://api.open-meteo.com/v1/forecast?" + urlencode({
    "bounding_box": "-7.5,106.75,-6.25,109.0", "models": "gfs013", "hourly": ",".join(VARIABLES),
    "forecast_days": 3, "timezone": "Asia/Jakarta", "wind_speed_unit": "ms", "elevation": "nan", "cell_selection": "nearest"})
RANGES = {"temperature": (-100,70), "humidity": (0,100), "cloud": (0,100), "precipitation": (0,1000), "u": (-150,150), "v": (-150,150)}

def finite(v):
    return not isinstance(v,bool) and isinstance(v,(int,float)) and math.isfinite(v)

def vector(speed,direction):
    """Meteorological FROM direction to east/north components in m/s."""
    if not finite(speed) or not finite(direction) or not 0 <= speed <= 150 or not 0 <= direction <= 360:
        return None,None
    a=math.radians(direction)
    return round(-speed*math.sin(a),3),round(-speed*math.cos(a),3)

def unavailable():
    return {"schema_version":1,"status":"unavailable","model":{"id":"gfs013","name":"NOAA GFS"},"fetched_at":None,"times":[],"fields":{}}

def validate(doc):
    if doc.get("schema_version") != 1: raise ValueError("Unsupported field schema")
    if doc.get("status") == "unavailable":
        if doc.get("times") or doc.get("fields"): raise ValueError("Unavailable field contains values")
        return doc
    if doc.get("status") != "ready" or doc.get("model",{}).get("id") != "gfs013": raise ValueError("Unexpected model")
    if dt.datetime.fromisoformat(doc["fetched_at"]).tzinfo is None: raise ValueError("Missing retrieval timezone")
    g=doc["grid"]
    for key in ("latitudes","longitudes"):
        a=g[key]
        if len(a)<2 or not all(finite(x) for x in a) or any(x>=y for x,y in zip(a,a[1:])): raise ValueError("Invalid grid axis")
    n=len(g["latitudes"])*len(g["longitudes"])
    if n>1000 or g["nx"]!=len(g["longitudes"]) or g["ny"]!=len(g["latitudes"]): raise ValueError("Invalid grid size")
    times=[dt.datetime.fromisoformat(t) for t in doc["times"]]
    if not 24<=len(times)<=96 or any(t.tzinfo is None for t in times) or any(b-a!=dt.timedelta(hours=1) for a,b in zip(times,times[1:])): raise ValueError("Invalid time axis")
    for key,(lo,hi) in RANGES.items():
        frames=doc["fields"][key]
        if len(frames)!=len(times) or any(len(f)!=n or any(v is not None and (not finite(v) or not lo<=v<=hi) for v in f) for f in frames): raise ValueError("Invalid "+key+" values")
    if any((u is None)!=(v is None) for a,b in zip(doc["fields"]["u"],doc["fields"]["v"]) for u,v in zip(a,b)): raise ValueError("Wind coverage differs")
    return doc

def normalize(cells,fetched_at=None):
    if not isinstance(cells,list) or len(cells)<9: raise ValueError("Native grid required")
    lats=sorted({c["latitude"] for c in cells});lons=sorted({c["longitude"] for c in cells})
    points={(c["latitude"],c["longitude"]):c for c in cells}
    if len(points)!=len(cells) or len(lats)*len(lons)!=len(cells): raise ValueError("Nonrectangular or duplicate native grid")
    raw=cells[0]["hourly"]["time"]
    times=[dt.datetime.fromisoformat(t).replace(tzinfo=WIB).isoformat() for t in raw]
    fields={k:[[] for _ in times] for k in RANGES}
    units=dict(zip(VARIABLES,["°C","%","m/s","°","%","mm"]))
    for lat in lats:
        for lon in lons:
            c=points[(lat,lon)];h=c["hourly"]
            if c.get("utc_offset_seconds")!=25200 or h["time"]!=raw: raise ValueError("Time axes differ")
            if any(c["hourly_units"].get(k)!=v for k,v in units.items()): raise ValueError("Unexpected model units")
            if any(len(h[k])!=len(times) for k in VARIABLES): raise ValueError("Incomplete time series")
            for i in range(len(times)):
                for key,source in [("temperature","temperature_2m"),("humidity","relative_humidity_2m"),("cloud","cloud_cover"),("precipitation","precipitation")]:
                    v=h[source][i];lo,hi=RANGES[key];fields[key][i].append(v if finite(v) and lo<=v<=hi else None)
                u,v=vector(h["wind_speed_10m"][i],h["wind_direction_10m"][i]);fields["u"][i].append(u);fields["v"][i].append(v)
    if any(sum(v is not None for f in frames for v in f)<len(times)*len(cells)*.8 for frames in fields.values()): raise ValueError("Insufficient field coverage")
    return validate({"schema_version":1,"status":"ready","model":{"id":"gfs013","name":"NOAA GFS","resolution_km":13,"wind_height_m":10,"temperature_height_m":2},
        "fetched_at":(fetched_at or dt.datetime.now(dt.timezone.utc)).isoformat(),
        "source":{"provider":"Open-Meteo","url":URL,"documentation":"https://open-meteo.com/en/docs/gfs-api","license":"CC BY 4.0","license_url":"https://creativecommons.org/licenses/by/4.0/"},
        "grid":{"latitudes":lats,"longitudes":lons,"nx":len(lons),"ny":len(lats),"native":True,"order":"latitude_ascending_longitude_ascending"},
        "times":times,"fields":fields,"units":{"temperature":"°C","humidity":"%","cloud":"%","precipitation":"mm in previous hour","u":"m/s","v":"m/s"},
        "interpolation":"Bilinear display interpolation of native values and u/v. No extrapolation. Retrieval time is not model issuance time."})

def save(path,doc):
    validate(doc);path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix('.tmp');temp.write_text(json.dumps(doc,ensure_ascii=False,separators=(',',':'),allow_nan=False));temp.replace(path)

def ensure(root):
    p=root/NAME
    if not p.exists(): save(p,unavailable())
    else: validate(json.loads(p.read_text()))

def refresh(root,*,snapshot=None,strict=False):
    p=root/NAME
    try:
        if snapshot:
            cells=json.loads(snapshot.read_text());fetched=dt.datetime.fromtimestamp(snapshot.stat().st_mtime,dt.timezone.utc)
        else:
            context=ssl.create_default_context()
            try:
                import certifi
                context.load_verify_locations(certifi.where())
            except ImportError: pass
            with urlopen(Request(URL,headers={'User-Agent':'LANGIT-weather/1.0','Accept':'application/json'}),timeout=30,context=context) as r: cells=json.load(r)
            fetched=dt.datetime.now(dt.timezone.utc)
        doc=normalize(cells,fetched);save(p,doc)
        print(f"GFS: {doc['grid']['nx']}×{doc['grid']['ny']} native cells, {len(doc['times'])} hours");return 0
    except (OSError,ValueError,KeyError,TypeError) as error:
        print('Regional field unavailable: '+str(error),file=sys.stderr)
        if strict:return 1
        try:
            old=validate(json.loads(p.read_text()));age=dt.datetime.now(dt.timezone.utc)-dt.datetime.fromisoformat(old['fetched_at'])
            if old['status']=='ready' and dt.timedelta(0)<=age<=dt.timedelta(hours=18):return 0
        except (OSError,ValueError,KeyError,TypeError):pass
        save(p,unavailable());return 0

if __name__=='__main__':
    parser=argparse.ArgumentParser(description='Update regional GFS surface data')
    parser.add_argument('--root',type=Path,default=Path('outputs'));parser.add_argument('--snapshot',type=Path);parser.add_argument('--strict',action='store_true')
    args=parser.parse_args();raise SystemExit(refresh(args.root,snapshot=args.snapshot,strict=args.strict))
