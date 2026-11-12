"""SLURM-only provider transport, immutable candidate files and offline compilation."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from .core import ROUTES, STOP, WHOLE_REQUEST_SECONDS, digest, save, request_allowed
from v2.request_policy import normalize
from .safety import pinned_route, policy
from .core import atomic_write
from contracts.route import build, check, OffRoute


def require_slurm():
    if not os.environ.get('SLURM_JOB_ID'): raise RuntimeError('computation requires SLURM')


def transport():
    spec=json.load(sys.stdin)
    req=urllib.request.Request('https://openrouter.ai/api/v1/chat/completions',
        data=json.dumps(spec['request']).encode(),
        headers={'Authorization':'Bearer '+spec['key'],'Content-Type':'application/json'})
    try:
        with urllib.request.urlopen(req,timeout=900) as response:
            sys.stdout.write(response.read().decode())
    except Exception as exc:
        details={'error':type(exc).__name__,'http_status':getattr(exc,'code',None)}
        if hasattr(exc,'read'):
            details['detail']=exc.read().decode(errors='replace')[:4000].replace(spec['key'],'[redacted]')
        sys.stderr.write(json.dumps(details));sys.exit(1)


class Client:
    def __init__(self,contract,out,default_lane='qwen24t'):
        self.contract=contract;self.out=Path(out)
        self.roster=set(contract['arm_profiles']['mixed-swarm']['models'])
        self.key=Path(contract['credential_file']).read_text().strip()
        self.routes=pinned_route(contract)
        self.rules=policy(contract)
        self.disabled=set()
        self.default_lane=default_lane

    def call(self,lane,system,payload,name,json_mode=False):
        require_slurm()
        routes=self.routes
        if lane not in self.roster or lane not in routes: raise ValueError('unapproved roster')
        rules=self.rules
        if rules['deadline'] is not None and time.time()+rules['request_seconds']>rules['deadline']:
            raise TimeoutError('no whole request window remains')
        model,provider=routes[lane]
        directory=self.out/(name+'-'+uuid.uuid4().hex)
        directory.mkdir(parents=True)
        request=normalize(dict(model=model,messages=[dict(role='system',content=system),
            dict(role='user',content=json.dumps(payload,ensure_ascii=False))],
            max_tokens=65536,
            usage=dict(include=True)),model)
        request=build(request,model,self.contract)
        if json_mode: request['response_format']={'type':'json_object'}
        save(directory/'request.json',request)
        receipt=dict(sent=True,model=model,provider=provider,start=time.time(),cost_usd=None,status='sent-unresolved')
        save(directory/'receipt.json',receipt)
        try:
            result=subprocess.run([sys.executable,'-m','writing.runtime','transport'],
                input=json.dumps(dict(request=request,key=self.key)),text=True,capture_output=True,timeout=rules['request_seconds'])
            if result.returncode:
                receipt.update(error=result.stderr,status='transport-error')
                raise RuntimeError(result.stderr)
            response=json.loads(result.stdout);save(directory/'response.json',response)
            receipt.update(status='received',generation_id=response.get('id'),usage=response.get('usage',{}))
            cost=response.get('usage',{}).get('cost')
            if isinstance(cost,(float,int)) and cost>=0: receipt['cost_usd']=cost
            if response.get('error') is not None or not response.get('choices'):
                detail=json.dumps(response.get('error',{'message':'provider returned no choices'}))[:4000].replace(self.key,'[redacted]')
                receipt.update(status='provider-error',error=detail)
                raise RuntimeError(detail)
            try:receipt['served_provider']=check(request,response)
            except OffRoute as exc:
                receipt.update(status='off-route-rejected',error=str(exc))
                raise
            choice=response['choices'][0]
            if choice.get('finish_reason')=='length': raise ValueError('truncated response retained, not accepted')
            content=choice['message']['content']
            if not isinstance(content,str) or not content.strip(): raise ValueError('empty response')
            return content
        except subprocess.TimeoutExpired as exc:
            receipt.update(status='transport-timeout',error=str(exc))
            raise RuntimeError('transport timeout') from exc
        finally:
            receipt['end']=time.time();save(directory/'receipt.json',receipt)

    def own_call(self,system,payload,name,json_mode=False,preferred=None):
        # Only operational provider failures cause route fallback within verified roster.
        preferred=preferred or self.default_lane
        lanes=[preferred]+[x for x in self.routes if x!=preferred]
        errors=[]
        for lane in lanes:
            for retry in range(self.rules['transport_retries']+1):
                try: return self.call(lane,system,payload,name,json_mode)
                except RuntimeError as exc: errors.append(str(exc))
        raise RuntimeError('pinned routes failed: '+'; '.join(errors))


def clean_tex(raw):
    raw=raw.strip()
    if raw.startswith('```'): raw=raw.split('\n',1)[1].rsplit('```',1)[0].strip()
    if not raw.startswith('\\documentclass') or not raw.endswith('\\end{document}'):
        raise ValueError('incomplete standalone manuscript')
    if re.search(r'\\(?:input|include|write18|openout|openin|read|catcode|directlua)\b',raw):
        raise ValueError('external/executable LaTeX forbidden')
    return raw


def compile_tex(source,out):
    require_slurm()
    from v2 import tex
    out=Path(out);out.mkdir(parents=True,exist_ok=True)
    atomic_write(out/'FINAL.tex',source.encode())
    env=dict(os.environ,PATH=str(tex.TREE/'bin/x86_64-linux')+':/usr/bin:/bin',
        LD_LIBRARY_PATH=':'.join(map(str,tex.LIBS)),openin_any='p',openout_any='p')
    code=None
    with tempfile.TemporaryDirectory(prefix='astra-tex-') as work:
        shutil.copyfile(out/'FINAL.tex',Path(work)/'FINAL.tex')
        with (out/'compile.log').open('w') as log:
            for _ in range(2):
                try:
                    p=subprocess.run([str(tex.TREE/'bin/x86_64-linux/pdflatex'),'-no-shell-escape',
                        '-interaction=nonstopmode','-halt-on-error','FINAL.tex'],cwd=work,env=env,
                        stdout=log,stderr=subprocess.STDOUT,timeout=120)
                    code=p.returncode
                except subprocess.TimeoutExpired: code='timeout'
                if code!=0: break
        if code==0: atomic_write(out/'FINAL.pdf',(Path(work)/'FINAL.pdf').read_bytes())
    log=(out/'compile.log').read_text(errors='replace')
    pages=re.findall(r'Output written on FINAL\.pdf \((\d+) pages?',log)
    result=dict(ok=code==0,pages=int(pages[-1]) if pages else None,
        errors=re.findall(r'^! .*$',log,re.M),diagnostic_tail=log[-5000:])
    save(out/'compilation.json',result)
    return result

if __name__=='__main__': transport()
