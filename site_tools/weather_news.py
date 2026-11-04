"""Meteorology headlines from publisher feeds; no article bodies are republished."""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
from email.utils import parsedate_to_datetime
import html
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import ssl
import sys
from urllib.parse import urljoin,urlsplit,urlunsplit
from urllib.request import Request,urlopen
import xml.etree.ElementTree as ET

NAME='weather_news.json'
WIB=dt.timezone(dt.timedelta(hours=7))
SOURCES=[('ANTARA Jabar','https://jabar.antaranews.com/rss/jabar-terkini.xml','rss'),
         ('ANTARA','https://www.antaranews.com/rss/terkini.xml','rss'),
         ('ANTARA','https://www.antaranews.com/rss/top-news.xml','rss'),
         ('BMKG','https://www.bmkg.go.id/berita/utama','html')]
WEATHER=re.compile(r'cuaca|hujan|meteorologi|atmosfer|hidrometeor|iklim|kemarau|monsun|siklon|puting beliung|angin kencang|gelombang tinggi|el ni[ñn]o|la ni[ñn]a',re.I)
REGIONAL=re.compile(r'bandung|cimahi|jawa barat|jabar|sumedang|cirebon|garut|tasikmalaya|sukabumi|cianjur|bogor|bekasi|purwakarta|subang|majalengka|indramayu',re.I)
FOREIGN=re.compile(r'amerika|jamaika|tiongkok|china|vietnam|filipina|thailand|jepang|eropa|kanada|india\b',re.I)
INDONESIA=re.compile(r'indonesia|bmkg|nusantara|sumatra|sumatera|jawa|kalimantan|sulawesi|papua|maluku|bali|nusa tenggara|jakarta',re.I)

def clean(value):
    return ' '.join(html.unescape(value or '').split())

def safe_url(value):
    p=urlsplit(value)
    if p.scheme!='https' or not (p.hostname=='bmkg.go.id' or (p.hostname or '').endswith('.bmkg.go.id') or p.hostname=='antaranews.com' or (p.hostname or '').endswith('.antaranews.com')):return None
    return urlunsplit((p.scheme,p.netloc,p.path,'',''))

class Articles(HTMLParser):
    def __init__(self):
        super().__init__();self.items=[];self.current=None;self.heading=False;self.time=False
    def handle_starttag(self,tag,attrs):
        a=dict(attrs)
        if tag=='article':self.current={'title':'','date':'','url':''}
        if self.current is None:return
        if tag=='h2':self.heading=True
        if tag=='time':self.time=True
        if tag=='a' and not self.current['url']:self.current['url']=a.get('href','')
    def handle_endtag(self,tag):
        if tag=='h2':self.heading=False
        if tag=='time':self.time=False
        if tag=='article' and self.current is not None:self.items.append(self.current);self.current=None
    def handle_data(self,value):
        if self.current is not None:
            if self.heading:self.current['title']+=value
            if self.time:self.current['date']+=value

def parse_date_only(value):
    words=clean(value).split()
    if len(words)!=3:raise ValueError('Unknown publication date')
    months={'jan':1,'feb':2,'mar':3,'apr':4,'may':5,'mei':5,'jun':6,'jul':7,'aug':8,'agu':8,'sep':9,'oct':10,'okt':10,'nov':11,'dec':12,'des':12}
    return dt.date(int(words[2]),months[words[1].lower()[:3]],int(words[0]))

def candidate(title,url,source,published_at=None,published_date=None,now=None):
    title=clean(title);url=safe_url(url)
    if not url or not 10<=len(title)<=300 or not WEATHER.search(title):return None
    if FOREIGN.search(title) and not INDONESIA.search(title) and not REGIONAL.search(title) and source!='BMKG':return None
    now=now or dt.datetime.now(dt.timezone.utc)
    date=published_at.astimezone(WIB).date() if published_at else published_date
    if date is None or (now.astimezone(WIB).date()-date).days not in range(8):return None
    if published_at and published_at>now+dt.timedelta(minutes=15):return None
    return {'title':title,'url':url,'source':source,'published_at':published_at.isoformat() if published_at else None,
            'published_date':date.isoformat(),'region':'jabar' if REGIONAL.search(title) else 'indonesia'}

def parse_rss(body,source,now=None):
    if b'<!DOCTYPE' in body.upper():raise ValueError('Unexpected feed declaration')
    root=ET.fromstring(body);out=[]
    for item in root.findall('./channel/item'):
        try:
            published=parsedate_to_datetime(item.findtext('pubDate',''))
            if published.tzinfo is None:continue
            entry=candidate(item.findtext('title',''),item.findtext('link',''),source,published_at=published,now=now)
            if entry:out.append(entry)
        except (ValueError,TypeError,OverflowError):continue
    return out

def parse_bmkg(body,now=None):
    parser=Articles();parser.feed(body.decode('utf-8'));out=[]
    for item in parser.items:
        try:
            entry=candidate(item['title'],urljoin('https://www.bmkg.go.id',item['url']),'BMKG',published_date=parse_date_only(item['date']),now=now)
            if entry:out.append(entry)
        except (ValueError,KeyError):continue
    return out

def validate(doc):
    if doc.get('schema_version')!=1 or doc.get('status') not in {'ready','unavailable'}:raise ValueError('Unknown news schema')
    if len(doc.get('items',[]))>12:raise ValueError('Too many headlines')
    seen=set()
    for item in doc.get('items',[]):
        if not safe_url(item['url']) or item['url'] in seen or item['region'] not in {'jabar','indonesia'}:raise ValueError('Invalid article')
        seen.add(item['url']);dt.date.fromisoformat(item['published_date'])
        if not isinstance(item['title'],str) or not 10<=len(item['title'])<=300:raise ValueError('Invalid headline')
    return doc

def write(path,doc):
    validate(doc);path.parent.mkdir(parents=True,exist_ok=True);temp=path.with_suffix('.tmp')
    temp.write_text(json.dumps(doc,ensure_ascii=False,separators=(',',':')));temp.replace(path)

def ensure(root):
    path=root/NAME
    if not path.exists():write(path,{'schema_version':1,'status':'unavailable','fetched_at':None,'items':[]})
    else:validate(json.loads(path.read_text()))

def refresh(root):
    context=ssl.create_default_context()
    try:
        import certifi
        context.load_verify_locations(certifi.where())
    except ImportError:pass
    now=dt.datetime.now(dt.timezone.utc)
    def fetch(source):
        name,url,kind=source
        try:
            with urlopen(Request(url,headers={'User-Agent':'LANGIT-weather/1.0','Accept':'application/rss+xml,text/html'}),timeout=18,context=context) as r:body=r.read(2_000_001)
            if len(body)>2_000_000:raise ValueError('Feed too large')
            return True,(parse_rss(body,name,now) if kind=='rss' else parse_bmkg(body,now))
        except (OSError,ValueError,ET.ParseError) as e:
            print('News source unavailable: '+name+' · '+str(e),file=sys.stderr);return False,[]
    with ThreadPoolExecutor(max_workers=3) as pool:results=list(pool.map(fetch,SOURCES))
    path=root/NAME
    if not any(ok for ok,_ in results):
        try:
            old=validate(json.loads(path.read_text()));age=now-dt.datetime.fromisoformat(old['fetched_at'])
            if dt.timedelta(0)<=age<=dt.timedelta(hours=48):return 0
        except (ValueError,KeyError,TypeError,OSError):pass
        write(path,{'schema_version':1,'status':'unavailable','fetched_at':None,'items':[]});return 0
    previous=[]
    try:
        old=validate(json.loads(path.read_text()))
        previous=[e for e in old['items'] if (now.astimezone(WIB).date()-dt.date.fromisoformat(e['published_date'])).days in range(8)]
    except (OSError,ValueError,KeyError,TypeError):pass
    unique={e['url']:e for e in previous}
    unique.update({entry['url']:entry for _,items in results for entry in items})
    entries=sorted(unique.values(),key=lambda e:e.get('published_at') or e['published_date'],reverse=True)
    chosen=[]
    for region in ['jabar','indonesia']:chosen.extend([e for e in entries if e['region']==region][:4])
    chosen.sort(key=lambda e:e.get('published_at') or e['published_date'],reverse=True)
    write(path,{'schema_version':1,'status':'ready','fetched_at':now.isoformat(),'items':chosen,'partial':not all(ok for ok,_ in results)})
    print('Weather headlines:',len(chosen));return 0

if __name__=='__main__':
    parser=argparse.ArgumentParser(description='Update meteorology headlines');parser.add_argument('--root',type=Path,default=Path('outputs'))
    raise SystemExit(refresh(parser.parse_args().root))
