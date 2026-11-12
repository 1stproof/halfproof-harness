"""Phase-C launcher: no side effects without --go; --dry-run always launches nothing."""
import argparse, datetime, fcntl, json, os, subprocess, sys, time, urllib.request
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
from zoneinfo import ZoneInfo
from .runtime import Runtime, MODELS, BASE
from .scope import validate_root
from .research import description

def plan(root,prefix,lanes):
    budget=json.loads((BASE/'tests/window-budget.json').read_text())
    result=[]
    for row in budget['lanes']:
        lane=row['lane']
        if lane not in lanes:continue
        model,provider,ip,op=MODELS[lane]
        margin=int(((Decimal(row['context'])*Decimal(str(ip))+128000*Decimal(str(op)))*1000000).to_integral_value(rounding=ROUND_CEILING))
        result.append(dict(lane=lane,name=prefix+'-'+lane,model=model,provider=provider,root=str(root),wall=row['minutes']*60,cap=int(Decimal(row['cap'])*1000000),margin=margin,writeup_at_minutes=row['minutes']-row.get('writeup_minutes',row['minutes']*.1),research=description(lane)))
    return result

def check_routes(rows):
    # Price/route refresh is read-only and happens before any key is loaded.
    for row in rows:
        url='https://openrouter.ai/api/v1/models/'+row['model']+'/endpoints'
        with urllib.request.urlopen(url,timeout=30) as response:data=json.load(response)
        endpoint=next((x for x in data['data']['endpoints'] if x.get('tag')==row['provider']),None)
        _,_,ip,op=MODELS[row['lane']]
        context=next(x['context'] for x in json.loads((BASE/'tests/window-budget.json').read_text())['lanes'] if x['lane']==row['lane'])
        pinned=next(x['endpoint'] for x in json.loads((BASE/'tests/routes-round2.json').read_text())['routes'] if x['lane']==row['lane'])
        if endpoint is None or Decimal(endpoint['pricing']['prompt'])>Decimal(str(ip)) or Decimal(endpoint['pricing']['completion'])>Decimal(str(op)) or endpoint['max_completion_tokens']<128000 or endpoint['context_length']!=pinned['context_length'] or endpoint['context_length']<context:
            raise ValueError('route changed: re-cost before C: '+row['lane'])

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--runs-root',type=Path,required=True);p.add_argument('--prefix',required=True)
    p.add_argument('--lane',choices=[*MODELS,'all'],required=True)
    p.add_argument('--go',action='store_true');p.add_argument('--dry-run',action='store_true')
    p.add_argument('--campaign-manifest',type=Path)
    args=p.parse_args(argv)
    rows=plan(args.runs_root,args.prefix,list(MODELS) if args.lane=='all' else [args.lane])
    try:
        for row in rows:validate_root(args.runs_root,row['name'],args.campaign_manifest)
    except (ValueError,OSError,KeyError) as exc:
        print('launch refused: '+str(exc),file=sys.stderr);return 2
    for row in rows:print(json.dumps(dict(row,start='T+10 PDT-relative',end='T+'+str(10+row['wall']//60),paid=bool(args.go and not args.dry_run))),flush=True)
    if args.dry_run:return 0
    if not args.go:p.error('launch refused: explicit operator --go required (or use --dry-run)')
    # Root is explicit; refuse reuse so five lanes cannot accidentally reallocate $115.
    lock=open(args.runs_root/(args.prefix+'.launch.lock'),'a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    for row in rows:
        if (args.runs_root/row['name']).exists():p.error('existing run refused: '+row['name'])
    check_routes(rows)
    if len(rows)>1:
        children=[]
        try:
            for row in rows:
                command=[sys.executable,'-m','v2.launch','--runs-root',str(args.runs_root),'--prefix',args.prefix,'--lane',row['lane'],'--go']
                if args.campaign_manifest:command+=['--campaign-manifest',str(args.campaign_manifest)]
                children.append(subprocess.Popen(command,cwd=BASE))
            # Children take the same launch lock while admitting themselves.
            fcntl.flock(lock,fcntl.LOCK_UN)
            return max(child.wait() for child in children)
        finally:
            for child in children:
                if child.poll() is None:child.terminate()
            for child in children:child.wait()
    row=rows[0]
    key=(Path.home()/'.config/ds41-secrets/openrouter.key').read_text().strip()
    runtime=Runtime(args.runs_root,row['name'],row['lane'],wall=row['wall'],cap=row['cap'],margin=row['margin'],key=key,manifest=args.campaign_manifest,research_live=True)
    fcntl.flock(lock,fcntl.LOCK_UN)
    stamp=datetime.datetime.now(ZoneInfo('America/Los_Angeles')).strftime('%Y-%m-%d %H:%M:%S PDT')
    (runtime.w.path/'launch.txt').write_text(stamp+' T+10\n')
    import signal
    def stopping(*_):raise KeyboardInterrupt
    signal.signal(signal.SIGTERM,stopping)
    try:
        runtime.start()
        while runtime.w.state['terminal'] is None and runtime.w.run.phase!='paused-alert':
            runtime.step();time.sleep(.1)
    finally:
        if runtime.w.run.phase=='paused-alert':runtime.w.stop_worker()
        else:runtime.close()
    return 0
if __name__=='__main__':raise SystemExit(main())
