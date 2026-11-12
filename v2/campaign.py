"""Campaign admission, single Slurm allocation, resumable staging, blind status."""
import argparse
import fcntl
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
from decimal import Decimal
from .control import atomic_json
from .runtime import MODELS
from .settings import PAGE_LIMIT
from .contracts import campaign_goal
from .scope import canonical, validate_root, campaign_root

BASE = Path(__file__).resolve().parents[1]
GATE_SECONDS = 45 * 60

def checked_root(manifest):
    data = json.loads(canonical(manifest).read_text())
    if data.get('version') != 1 or data.get('authorization') != 'operator-approved-campaign-root':
        raise ValueError('manifest authorization refused')
    if not Path(campaign_root(data)).is_absolute():
        raise ValueError('absolute campaign root required')
    root = canonical(campaign_root(data))
    # Also holds inside a relocated, pinned release, regardless of scope.BASE.
    if 'runs' in root.parts or 'web-harness' in root.parts:
        raise ValueError('legacy tree refused')
    validate_root(root, 'campaign', manifest)
    return root

def build_plan(args):
    root = checked_root(args.manifest)
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', args.label):
        raise ValueError('campaign label refused')
    lanes = ['dsf','dsp','qwen24t','k3','dsf-swarm','mixed-swarm'] if args.models == 'all' else args.models.split(',')
    if not lanes or len(set(lanes)) != len(lanes) or any(x not in MODELS and x not in ('dsf-swarm','mixed-swarm') for x in lanes):
        raise ValueError('models must be distinct lane names or all')
    if args.replicas < 1 or args.replicas > 100:
        raise ValueError('replicas must be 1..100')
    wall = int(Decimal(args.wall_hours) * 3600)
    cap = int(Decimal(args.cap) * 1000000)
    if not 60 <= wall <= 47 * 3600 or cap <= 0:
        raise ValueError('wall must be 1 minute..47 hours; cap must be positive')
    problem = canonical(args.problem)
    goal = canonical(args.goal_template)
    # Existence only: planning never opens the problem or template.
    if not problem.is_file() or not goal.is_file():
        raise ValueError('problem or GOAL template missing')
    if not goal.is_relative_to(BASE):
        raise ValueError('GOAL template must be in this release')
    swarms=[x for x in lanes if x.endswith('-swarm')]
    if swarms and (args.replicas!=1 or cap!=200000000 or wall>86400):
        raise ValueError('one swarm parent per problem, $200 total, at most 24 hours; use v3 pilot profile for pilot')
    count = len(lanes) * args.replicas
    rows = [dict(name=f'{args.label}-{lane}-{replica}', lane=lane, replica=replica,
                 model=MODELS[lane][0] if lane in MODELS else None, provider=MODELS[lane][1] if lane in MODELS else None, strategy=lane if lane.endswith('-swarm') else lane+'-solo', dynamic_assignments=lane.endswith('-swarm'), wall=wall, cap=cap)
            for replica in range(1, args.replicas + 1) for lane in lanes]
    return dict(version=1, label=args.label, root=str(root), problem=str(problem),
                goal_template=str(goal.relative_to(BASE)), goal_source='v2/contracts.py + PROBLEM.md + web instructions', page_limit=PAGE_LIMIT, runs=rows, wall=wall,
                gate_seconds=GATE_SECONDS, total_cap=cap*count,
                slurm=dict(partition='normal', cpus=max(4, math.ceil(16*count/5)),
                           memory_gib=max(16, math.ceil(64*count/5)), seconds=wall+3600))

def slurm_command(plan, directory):
    s = plan['slurm']
    return ['sbatch', '--parsable', '--partition='+s['partition'],
            '--cpus-per-task='+str(s['cpus']), '--mem='+str(s['memory_gib'])+'G',
            '--time='+str(math.ceil(s['seconds']/60)), '--signal=B:TERM@120',
            '--job-name='+plan['label'], '--output='+str(directory/'slurm-%j.out'),
            '--error='+str(directory/'slurm-%j.err'),
            str(directory/'release/bin/v2-campaign-job'), str(directory/'campaign.json')]

def pin_controller(directory):
    release = directory/'release'
    release.mkdir()
    for name in ('v2', 'bin', 'tests', 'template'):
        shutil.copytree(BASE/name, release/name, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    for p in BASE.glob('*.txt'):
        shutil.copyfile(p, release/p.name)
    return release

def numeric(value, default=None):
    return value if type(value) in (int, float) and math.isfinite(value) else default

def enum(value, allowed, default='other'):
    return value if isinstance(value, str) and value in allowed else default

PHASES = {'research','verification','write-up','grace','checkpoint-requested','billing-pause',
          'done','paused-alert','queued','staged'}
REASONS = {'deadline','budget','verified-blocked','verified-completion','slurm-interrupted',
           'worker-lost','preflight-failed','gate-refused','usage-missing','price-bound-breach',
           'configuration','operator-stop','rejected-claim-report'}

def summary(state, now):
    """Allowlist only; never serialize arbitrary strings, filenames or nested metadata."""
    run = state.get('run', {})
    ledger = state.get('ledger', {})
    settled = ledger.get('settled', {})
    values = list(settled.values()) if isinstance(settled, dict) else []
    spend = sum(values) if all(type(x) is int and x >= 0 for x in values) else None
    observations = state.get('notice_observations', {}).values()
    observations = list(observations)
    def age(field):
        stamp = numeric(run.get(field))
        return max(0, now-stamp) if stamp is not None else None
    errors = state.get('campaign_provider_errors', {})
    terminal = state.get('terminal') or {}
    last_write=numeric(state.get('campaign_last_qualifying_write'))
    write_age=max(0,now-last_write) if last_write is not None else None
    return dict(phase=enum(run.get('phase'), PHASES), session=numeric(state.get('session')),
                session_age=age('session_start'), last_qualifying_write_age=write_age,
                spend=spend, cap=numeric(ledger.get('original_budget')),
                provider_errors={k:numeric(errors.get(k),0) for k in ('billing','rate','timeout','provider','configuration')},
                notices_published=len(observations),
                notices_received=sum(x.get('attached') is True for x in observations),
                build=enum(state.get('campaign_build'), {'passed','failed','pending'}, 'pending'),
                terminal_reason=enum(terminal.get('reason'), REASONS) if terminal else None)

def gate(state, now):
    run = state.get('run', {})
    s = summary(state, now)
    elapsed = max(1, now-run.get('start',now))
    metrics = state.get('request_metrics', [])
    pending = state.get('pending', {})
    # Only completed metered requests plus still-reserved upper charges are compared.
    observed = (sum(x.get('cost',0) for x in metrics) + sum(pending.values())
                + sum(x.get('service_microdollars',0) for x in state.get('web_events',[])))
    spent = s['spend']
    ledger_ok = spent is not None and observed == spent and s['cap'] == run.get('cap')
    projected = None if spent is None else math.ceil(spent*run.get('wall',0)/elapsed)
    checks = dict(notice_receipt=s['notices_published']>0 and s['notices_received']==s['notices_published'],
                  ledger_agreement=ledger_ok,
                  no_crash_loop=len(run.get('worker_crashes',[]))<5 and len(run.get('crashes',[]))<3
                    and s['phase']!='paused-alert' and not state.get('terminal'),
                  preflight_compile=s['build']=='passed',
                  cost_projection=bool(metrics) and projected is not None and projected <= (s['cap'] or 0))
    return dict(passed=all(checks.values()), checks=checks, projected_microdollars=projected,
                hourly_microdollars=None if spent is None else math.ceil(spent*3600/elapsed))

def load_config(path):
    path = canonical(path)
    config = json.loads(path.read_text())
    root = checked_root(path.parent/'manifest.json')
    plan = config['plan']
    if Path(plan['root']) != root or path.parent != root/(plan['label']+'.campaign'):
        raise ValueError('campaign identity mismatch')
    for row in plan['runs']:
        if row['name'] != f"{plan['label']}-{row['lane']}-{row['replica']}" or row['lane'] not in {*MODELS,'dsf-swarm','mixed-swarm'}:
            raise ValueError('run identity mismatch')
        if row['lane'].endswith('-swarm') and (row['replica']!=1 or row['cap']!=200000000 or row['wall']>86400 or row.get('model') is not None):
            raise ValueError('swarm parent allocation mismatch')
        validate_root(root,row['name'],path.parent/'manifest.json')
    return config

def status(path):
    config = load_config(path)
    root = Path(config['plan']['root'])
    rows = []
    for row in config['plan']['runs']:
        p = canonical(root/row['name']/'runtime.json')
        state = json.loads(p.read_text()) if p.exists() else {'run':{'phase':'staged' if row['replica']>1 else 'queued'}}
        safe=summary(state,time.time())
        terminal=config.get('terminal_ledger',{}).get(row['name'],{})
        if not safe['terminal_reason'] and terminal:
            safe['terminal_reason']=enum(terminal.get('reason'),REASONS)
        if not p.exists():
            safe.update(cap=numeric(row.get('cap')),spend=0)
            if terminal:safe['phase']='done'
        rows.append(dict(name=row['name'], **safe))
    g=config.get('gate') or {}
    return dict(runs=rows, gate={'evaluated':bool(g),'passed':g.get('passed') is True,
                                'early':g.get('early') is True,'at':numeric(g.get('at'))})

def reap_children(children, grace=90):
    children=list(children)
    for child in children:
        if child.poll() is None:child.terminate()
    end=time.monotonic()+grace
    for child in children:
        try:child.wait(timeout=max(.01,end-time.monotonic()))
        except subprocess.TimeoutExpired:child.kill();child.wait()

def supervise(path, worker_command=None, poll=.5):
    """One controller owns all processes; injectable command is offline-test-only API."""
    if worker_command is None:
        raise ValueError('legacy independent-worker launcher retired; use the v3 cohort parent ledger and accepted release')
    path = canonical(path)
    config = load_config(path)
    directory = path.parent
    lock = open(directory/'active.lock','a')
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    stopping = False
    def stop(*_):
        nonlocal stopping
        stopping = True
    previous = {s:signal.signal(s,stop) for s in (signal.SIGTERM,signal.SIGINT)}
    children = {}
    root = Path(config['plan']['root'])
    def save(): atomic_json(path,config)
    def start(row):
        if stopping: return
        command = worker_command(row) if worker_command else [sys.executable,'-m','v2.campaign_worker',str(path),row['name']]
        children[row['name']] = subprocess.Popen(command, cwd=directory/'release')
    try:
        if config.get('start') is None:
            config['start']=time.time()
            config['deadline']=config['start']+config['plan']['wall']
        config['admission']='open';config['job']=os.environ.get('SLURM_JOB_ID');save()
        if worker_command is None:
            from .launch import check_routes
            for row in config['plan']['runs']:
                if stopping:break
                if row['replica']==1:check_routes([row])
        for row in config['plan']['runs']:
            if row['replica']==1 or (config.get('gate') or {}).get('passed'):
                start(row)
        while not stopping:
            now=time.time()
            if config.get('gate') is None and (now>=config['start']+GATE_SECONDS or now>=config['deadline'] or all(c.poll() is not None for c in children.values())):
                gates={}
                for row in config['plan']['runs']:
                    if row['replica']!=1:continue
                    p=root/row['name']/'runtime.json'
                    gates[row['lane']]=gate(json.loads(p.read_text()),now) if p.exists() else {'passed':False,'checks':{'runtime_present':False}}
                config['gate']={'at':now,'early':now<config['start']+GATE_SECONDS,
                                'passed':now>=config['start']+GATE_SECONDS and all(g['passed'] for g in gates.values()),'lanes':gates}
                save()
                if config['gate']['passed']:
                    for row in config['plan']['runs']:
                        if row['replica']>1:start(row)
            if all(c.poll() is not None for c in children.values()):break
            # Workers settle pending cost lookups for up to 330 s after their deadline.
            if now>=config['deadline']+420:stopping=True;break
            time.sleep(poll)
    finally:
        try:
            config['admission']='closed'
            # Reaping must run even when durable shutdown publication fails.
            try:save()
            finally:reap_children(children.values())
            # Durable campaign ledger even if a worker died before writing its terminal record.
            config['terminal_ledger']={}
            for row in config['plan']['runs']:
                p=root/row['name']/'runtime.json'
                s=summary(json.loads(p.read_text()),time.time()) if p.exists() else {}
                config['terminal_ledger'][row['name']]={'spent':s.get('spend',0),'cap':row['cap'],
                    'reason':s.get('terminal_reason') or ('slurm-interrupted' if stopping else 'worker-lost' if row['name'] in children else 'gate-refused')}
            config['interrupted']=stopping;save()
        finally:
            for s,handler in previous.items():signal.signal(s,handler)
            lock.close()
    return 143 if stopping else int(any(c.returncode for c in children.values()))

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    launch=sub.add_parser('launch')
    launch.add_argument('--manifest',type=Path,required=True)
    launch.add_argument('--problem',type=Path,required=True)
    launch.add_argument('--label',required=True)
    launch.add_argument('--models',default='all')
    launch.add_argument('--replicas',type=int,default=1)
    launch.add_argument('--wall-hours',default='24')
    launch.add_argument('--cap',default='200')
    launch.add_argument('--goal-template',type=Path,default=BASE/'template/WEB-RESEARCH.txt')
    for name in ('resume','status','job'):
        p=sub.add_parser(name);p.add_argument('campaign',type=Path)
    for p in (launch,sub.choices['resume']):
        p.add_argument('--go',action='store_true');p.add_argument('--dry-run',action='store_true')
    args=parser.parse_args(argv)
    try:
        if args.command=='status':print(json.dumps(status(args.campaign),sort_keys=True));return 0
        if args.command=='job':
            if not os.environ.get('SLURM_JOB_ID'):raise ValueError('job requires Slurm')
            return supervise(args.campaign)
        if args.command=='launch':
            plan=build_plan(args);directory=Path(plan['root'])/(plan['label']+'.campaign')
            if directory.exists() or any((Path(plan['root'])/r['name']).exists() for r in plan['runs']):
                raise ValueError('campaign/run reuse refused; use resume')
            config={'plan':plan,'start':None,'deadline':None,'gate':None,'admission':'queued'}
        else:
            config=load_config(args.campaign);plan=config['plan'];directory=args.campaign.absolute().parent
        command=slurm_command(plan,directory)
        print(json.dumps(dict(plan=plan,command=command,paid=bool(args.go and not args.dry_run),
                             original_start=config.get('start'),original_deadline=config.get('deadline')),sort_keys=True),flush=True)
        if args.dry_run:return 0
        if not args.go:raise ValueError('explicit --go required')
        if args.command=='launch':
            directory.mkdir(parents=True,exist_ok=False)
            release=pin_controller(directory)
            (release/'CAMPAIGN-GOAL.txt').write_text(campaign_goal((BASE/plan['goal_template']).read_text()))
            shutil.copyfile(args.manifest,directory/'manifest.json')
            atomic_json(directory/'campaign.json',config)
        # Submission lock + scheduler liveness prevents duplicate jobs, including queued jobs.
        with open(directory/'submit.lock','a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            submission=directory/'submission.json'
            current=json.loads(submission.read_text()) if submission.exists() else {}
            if current.get('submitted_job'):
                jobs=subprocess.check_output(['squeue','--noheader','--user',str(os.getuid()),'--format=%i'],text=True)
                if str(current['submitted_job']) in jobs.split():
                    raise ValueError('existing Slurm job still active')
            result=subprocess.check_output(command,text=True).strip()
            job=result.split(';')[0]
            if not job.isdigit():raise ValueError('invalid sbatch job identifier')
            atomic_json(submission,{'submitted_job':job})
            print(json.dumps({'submitted_job':job,'campaign':str(directory/'campaign.json')}))
        return 0
    except (ValueError,OSError,KeyError,ArithmeticError,subprocess.SubprocessError):
        # Never echo provider text, arbitrary state or a problem-derived exception.
        print('campaign refused: invalid configuration, conflicting job, or unavailable dependency',file=sys.stderr)
        return 2

if __name__=='__main__':raise SystemExit(main())
