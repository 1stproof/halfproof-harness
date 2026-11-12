"""Verifier-only, model-free source retrieval with authenticated retained bytes.

No provider credential, generated search summary, shell, author write or research
assignment is exposed. The worker namespace can reach this endpoint only through
its own host-owned session proxy. Source URLs are confined to approved hosts.
"""
import base64
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import urllib.error
import urllib.parse
import urllib.request
from .control import atomic_json
from .owned import run_owned

MAX_BYTES=4*1024*1024


def source_urls(url):
    parsed=urllib.parse.urlsplit(url)
    if parsed.scheme!='https' or parsed.netloc not in ('arxiv.org','export.arxiv.org') or parsed.query or parsed.fragment:
        raise ValueError('source URL must use approved arXiv HTTPS source host')
    match=re.fullmatch(r'/(?:abs|pdf|e-print|html)/([A-Za-z.-]+/\d{7}(?:v\d+)?|\d{4}\.\d{4,5}(?:v\d+)?)(?:\.pdf)?',parsed.path)
    if not match:raise ValueError('recognized arXiv identifier required')
    paper=match.group(1)
    if not re.search(r'v[1-9]\d*$', paper):raise ValueError('explicit arXiv version required')
    return ['https://arxiv.org/e-print/'+paper,'https://arxiv.org/html/'+paper]


def fetch(url,*,timeout=10,cap=MAX_BYTES):
    # urllib's socket timeout alone does not bound a trickling header/body.
    # An owned child bounds the entire DNS/TLS/redirect/download operation.
    try:
        result=run_owned([sys.executable,'-I',str(Path(__file__).with_name('verification_fetch.py')),url,str(cap)],capture_output=True,timeout=timeout,env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'})
    except subprocess.TimeoutExpired as exc:raise ValueError('source total wall exceeded') from exc
    if result.returncode:raise ValueError('source fetch failed')
    if len(result.stdout)>((cap+2)//3)*4:raise ValueError('source exceeds retrieval byte bound')
    data=base64.b64decode(result.stdout,validate=True)
    if len(data)>cap:raise ValueError('source exceeds retrieval byte bound')
    return data


def text_sources(data):
    """Bound decompression before parsing archive metadata; never extract paths."""
    if len(data)>MAX_BYTES:raise ValueError('source byte bound')
    if data.startswith(b'\x1f\x8b'):
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:raw=stream.read(MAX_BYTES+1)
        if len(raw)>MAX_BYTES:raise ValueError('unpacked source bound')
    else:raw=data
    rows=[];total=0
    try:
        with tarfile.open(fileobj=io.BytesIO(raw),mode='r:') as archive:
            for index,member in enumerate(archive):
                if index>=1024 or len(member.name)>1024:raise ValueError('source archive index bound')
                total+=member.size
                if member.size<0 or total>MAX_BYTES:raise ValueError('unpacked source bound')
                if not member.isfile() or not member.name.endswith(('.tex','.bib')):continue
                stream=archive.extractfile(member);content=stream.read(MAX_BYTES+1)
                rows.append(dict(location=member.name,text=content.decode(errors='replace')))
    except tarfile.ReadError:
        rows=[dict(location='source',text=raw.decode(errors='replace'))]
    return rows


def source_slice(sources,*,location=None,start=0,limit=12000):
    if location is not None and not isinstance(location,str):raise ValueError('source location must be a string')
    if type(start) is not int or start<0 or type(limit) is not int or not 1<=limit<=16000:raise ValueError('invalid bounded source slice')
    index=[];selected=[];remaining=limit
    for row in sources:
        text=row['text'];match=location is None or location==row['location']
        excerpt=text[start:start+remaining] if match and remaining else ''
        index.append(dict(location=row['location'],characters=len(text),omitted=not excerpt or start!=0 or len(excerpt)!=len(text)))
        if excerpt:
            selected.append(dict(location=row['location'],text=excerpt,start=start,end=start+len(excerpt)))
            remaining-=len(excerpt)
    return dict(sources=selected,source_index=index,slice_limit=limit)


def retrieve(worker,url,*,location=None,start=0,limit=12000):
    source_slice([],location=location,start=start,limit=limit)
    w=worker.gateway.watchdog
    if worker.role not in ('internal-verifier','peer-reviewer') or not w.role_admission(worker):
        raise ValueError('authenticated active verifier required')
    root=w.path/'retrieved-sources';root.mkdir(exist_ok=True)
    errors=[]
    for target in source_urls(url):
        try:
            if not w.role_admission(worker):raise ValueError('verifier stage ended')
            cached=next((r for r in w.state.get('source_receipts',{}).values() if r['url']==target and r['session']==worker.session),None)
            provider=worker.gateway.fake
            if cached:
                path=root/cached['sha256']
                if path.is_symlink():raise ValueError('source cache link refused')
                data=path.read_bytes()
                if hashlib.sha256(data).hexdigest()!=cached['sha256']:raise ValueError('retained source changed')
            elif provider is not None:
                loader=getattr(provider,'retrieve_source',None)
                if not callable(loader):raise ValueError('fake source unavailable; network forbidden in fake run')
                data=loader(target)
            else:
                remaining=getattr(w,'stage_deadline',float('inf'))-w.clock() if hasattr(w,'clock') else 10
                if remaining<=0:raise ValueError('verifier stage ended')
                from .source_cache import host_bytes
                data, _, _, _, _ = host_bytes(worker, target, verifier=True)
            if not isinstance(data,bytes) or len(data)>MAX_BYTES:raise ValueError('invalid source bytes')
            sources=text_sources(data);digest=hashlib.sha256(data).hexdigest()
            rid='source:'+hashlib.sha256((str(worker.run)+target+digest).encode()).hexdigest()
            path=root/digest
            receipt=dict(id=rid,url=target,sha256=digest,bytes=len(data),worker=w.path.name,
                session=worker.session,source_first=True,paid_tool_microusd=0)
            with w.mutex:
                if not w.role_admission(worker):raise ValueError('verifier stage ended')
                if not path.exists():path.write_bytes(data)
                w.state.setdefault('source_receipts',{})[rid]=receipt;w.save()
                atomic_json(root/(digest+'.receipt.json'),receipt)
            return dict(receipt=receipt,**source_slice(sources,location=location,start=start,limit=limit))
        except (ValueError,RuntimeError,OSError,urllib.error.URLError) as exc:errors.append(type(exc).__name__)
    return dict(status='unavailable',attempts=source_urls(url),errors=errors)
