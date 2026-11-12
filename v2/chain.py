"""Durable research -> closing -> release-write -> delivery continuation.

The native executor owns research and closing. An afterany successor registered
BEFORE research starts consumes its last acknowledged state, including on node
loss. It never restores an unread model reply or reopens research accounting.
Each stage commits output hashes before advancing; retries keep prior artifacts.
"""
import argparse
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

from contracts.route import routes
from .durability import read_publication

BASE=Path(__file__).resolve().parents[1]
MAX_RECOVERIES=4


def sha(data):
    return hashlib.sha256(data).hexdigest()


def write(path, data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(prefix='.'+path.name+'-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as f:
            f.write(data);f.flush();os.fsync(f.fileno())
        os.replace(name,path)
        fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)
    finally:
        if os.path.exists(name):os.unlink(name)


def save(path, value):
    write(path,json.dumps(value,sort_keys=True,indent=2).encode())


def load(path):
    return json.loads(Path(path).read_bytes())


def register(manifest, *, submit=True):
    """Called at job entry; no credential read and no paid operation."""
    directory=Path(manifest['durable_root'])/'chain'
    directory.mkdir(parents=True,exist_ok=True)
    registry=directory/'registry.json'
    with (directory/'registry.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        digest=sha(json.dumps(manifest,sort_keys=True).encode())
        if registry.exists():
            state=load(registry)
            if state['run_sha256']!=digest:raise ValueError('chain run identity changed')
        else:
            state=dict(schema=1,run_sha256=digest,run=manifest,stage='research',
                stages={},recoveries=0,successors={},errors=[])
            save(registry,state)
        if submit:schedule(registry,state)
    return registry


def schedule(registry,state, *, dependency=None):
    """One queued successor per owner. afterany is also the owner-death fence.

    A successor queues its own replacement before working, so its node loss or
    preemption cannot require a login session. Completed registries are no-ops.
    """
    if state['stage'] in ('delivered','recovery-failed') or state['recoveries']>MAX_RECOVERIES+1:return
    owner=os.environ['SLURM_JOB_ID']
    if owner in state['successors']:return
    dependency=dependency or owner
    if not str(dependency).isdigit():raise ValueError('numeric dependency owner required')
    manifest=dict(state['run'],action='chain',chain_registry=str(Path(registry).resolve()),
        chain_waited_owner=str(dependency))
    manifest['target']=dict(manifest['target'],nodes=['*'],cpus=4,memory_gib=16,seconds=8*3600)
    manifest_path=Path(registry).parent/('successor-'+owner+'.json')
    save(manifest_path,manifest)
    from .deployment import slurm_command
    command=slurm_command(manifest_path,manifest,output=Path(registry).parent/'chain-%j.log')
    # Writer gets its own allocation; research's original deadline stays fixed.
    command[command.index(next(x for x in command if x.startswith('--time='))) ]='--time=480'
    command[2:2]=['--dependency=afterany:'+str(dependency),'--job-name=hc-chain-'+state['run']['cohort_id']]
    job=subprocess.check_output(command,text=True,timeout=60).strip().split(';')[0]
    if not job.isdigit():raise ValueError('invalid chain scheduler receipt')
    state['successors'][owner]=job
    save(registry,state)


def latest_swarm(run):
    journal=Path(run['durable_root'])/'journal'
    prefix='swarm-mixed-swarm-'
    paths=[p for p in journal.glob(prefix+'*.json') if p.stem[len(prefix):].isdigit()]
    if not paths:raise RuntimeError('no acknowledged swarm checkpoint available')
    path=max(paths,key=lambda p:int(p.stem[len(prefix):]))
    swarm=read_publication(path)
    return swarm,dict(path=str(path),sha256=sha(path.read_bytes()))


def provisional_settlement(run):
    """Provisional (estimate) settlements in the last acknowledged accounting checkpoint."""
    from .accounting import provisional_summary
    journal=Path(run['durable_root'])/'journal';prefix='cohort-checkpoint-'
    paths=[p for p in journal.glob(prefix+'*.json') if p.stem[len(prefix):].isdigit()]
    if not paths:return None
    path=max(paths,key=lambda p:int(p.stem[len(prefix):]))
    return dict(provisional_summary(read_publication(path)['accounting']['tables']),checkpoint=path.name)


def close_records(run, directory, *, swarm=None):
    """Seal certified arguments from the saved board. No new mathematical choice.

    If research's owner died, publication acknowledgment is the resume boundary.
    Its unfinished requests remain unknown; they never become certified inputs.
    """
    from .closing_frozen import FrozenBoard
    from .claim_ledger import load_policy
    from .claim_manifest import manifest
    from .proof_coverage import certified_reasoning
    directory=Path(directory)
    if swarm is None:swarm,origin=latest_swarm(run)
    else:origin=dict(kind='supplied-frozen-checkpoint')
    saved=swarm['board'];raw=saved['snapshot'].encode()
    if sha(raw)!=saved['sha256']:raise ValueError('saved board checksum mismatch')
    board=json.loads(raw)
    if (board['problem_id']!=run['problem_id'] or board['cohort_id']!=run['cohort_id']
            or board['arm_id']!='mixed-swarm' or board['contracts_sha256']!=run['contracts_sha256']):
        raise ValueError('saved board belongs to another run')
    for artifact in board.get('artifacts',[]):
        if sha(base64.b64decode(artifact['base64'],validate=True))!=artifact['sha256']:
            raise ValueError('saved board artifact changed')
    previous=os.environ.get('V3_POLICY_OVERRIDES')
    try:
        os.environ['V3_POLICY_OVERRIDES']=json.dumps(run.get('policy_overrides',{}))
        policy=load_policy('mixed-swarm')
    finally:
        if previous is None:os.environ.pop('V3_POLICY_OVERRIDES',None)
        else:os.environ['V3_POLICY_OVERRIDES']=previous
    claims=FrozenBoard(dict(swarm=swarm)).ledger(None,policy)
    packet=manifest(claims,policy,run['cohort_id']+':mixed-swarm')
    for c in packet['claims']:c['certified']=certified_reasoning(c)
    directory.mkdir(parents=True,exist_ok=True)
    save(directory/'CLAIMS-MANIFEST.json',packet)
    save(directory/'closing.json',dict(origin=origin,board_sha256=saved['sha256'],
        native_mode=swarm.get('mode'),recovered=swarm.get('mode')!='terminal',
        certified_records=[c['current_record'] for c in packet['claims'] if c['certified']],
        unread_replies='excluded; original liabilities retained'))
    return directory/'CLAIMS-MANIFEST.json'


def authority(run, manifest, directory):
    contract=Path(directory)/'run-contract.json';save(contract,run)
    result=dict(contract=str(contract),contract_sha256=sha(contract.read_bytes()),
        manifest=str(manifest),manifest_sha256=sha(Path(manifest).read_bytes()),
        routes_verified=True,routes=[dict(model=m,provider=p,contract_route_allowed=True)
                                    for m,p in routes(run).values()])
    path=Path(directory)/'authority.json';save(path,result)
    return contract,path


def writer(run, manifest, directory, *, execute=subprocess.run):
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    from writing.safety import allocation_deadline
    run=dict(run,writer_policy=dict(run.get('writer_policy',{}),deadline=allocation_deadline()))
    contract,auth=authority(run,manifest,directory)
    # The writer owns its implementation and deterministic candidate choice.
    # This is the ONLY writer interface used by the lifecycle.
    output=directory/('output-'+str(time.time_ns()))
    prior=sorted(directory.glob('output*/attempt-*'))
    seed=next((p for p in reversed(prior) if (p/'plan.json').exists() and (p/'candidate-index.json').exists()),None)
    command=[sys.executable,str(BASE/'bin/release-write'),'--out',str(output),
        '--contract',str(contract),'--manifest',str(manifest),'--authority',str(auth),
        '--problem',run['problem_id']]
    if seed is not None:command+=['--candidates-from',str(seed)]
    with (directory/'writer.log').open('ab') as log:
        result=execute(command,stdout=log,stderr=subprocess.STDOUT,timeout=7*3600,check=False)
    if result.returncode:raise RuntimeError('release-write exited '+str(result.returncode))
    receipt=output/'delivery/manifest.json'
    if not receipt.exists():raise RuntimeError('release-write did not persist result')
    save(directory/'completed.json',dict(output=str(output)))
    return output


def inventory(directory):
    return {str(p.relative_to(directory)):sha(p.read_bytes())
            for p in sorted(Path(directory).rglob('*')) if p.is_file() and not p.is_symlink()}


def verify(directory, hashes):
    for name,digest in hashes.items():
        path=Path(directory)/name
        if path.is_symlink() or sha(path.read_bytes())!=digest:raise ValueError('chain stage output changed: '+name)


def deliver(run, writing, manifest, destination, *, flags=()):
    destination=Path(destination)
    writing=Path(writing)
    if (writing/'delivery/manifest.json').is_file():writing=writing/'delivery'
    if destination.is_symlink():raise ValueError('delivery symlink refused')
    if destination.exists():
        data=load(destination/'manifest.json');verify(destination,data['files']);return destination
    destination.parent.mkdir(parents=True,exist_ok=True)
    stage=Path(tempfile.mkdtemp(prefix='.delivery-',dir=destination.parent))
    try:
        files={}
        for target,names in [('paper.tex',('paper.tex','FINAL.tex')),('paper.pdf',('paper.pdf','FINAL.pdf'))]:
            for name in names:
                source=Path(writing)/name
                if source.is_file():
                    data=source.read_bytes();write(stage/target,data);files[target]=sha(data);break
        records=load(manifest)
        statuses=list(flags)
        if 'paper.pdf' not in files:statuses.append('no-compiled-paper')
        if 'paper.tex' not in files:statuses.append('no-roster-manuscript')
        try:provisional=provisional_settlement(run)
        except Exception as exc:provisional=dict(error=type(exc).__name__+': '+str(exc)[:200])
        if provisional and provisional.get('used'):statuses.append('provisional-settlement')
        data=dict(schema=1,run=run['cohort_id'],problem=run['problem_id'],files=files,
            certified_records=[c['current_record'] for c in records['claims'] if c.get('certified')],
            certified_manifest_sha256=sha(Path(manifest).read_bytes()),
            writer_output=str(writing),flags=statuses,provisional_settlement=provisional)
        # Preserve the writer's richer delivery metadata when available.
        own=Path(writing)/'manifest.json'
        if own.is_file():
            data['writer_manifest']=load(own)
            data['flags']+=data['writer_manifest'].get('flags',[])
        save(stage/'manifest.json',data)
        os.rename(stage,destination)
        fd=os.open(destination.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)
    finally:
        if stage.exists():shutil.rmtree(stage)
    return destination


def advance(registry, *, closing=close_records, writing=writer, delivery=deliver):
    """Restartable stages. A crash after output commit reuses exact saved bytes."""
    registry=Path(registry);directory=registry.parent
    with (directory/'registry.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        state=load(registry);run=state['run']
        if state['stage']=='delivered':
            verify(directory/'delivery',state['stages']['delivery']);return directory/'delivery'
        for name,hashes in state['stages'].items():verify(directory/name,hashes)
        if 'closing' not in state['stages']:
            state['stage']='closing';save(registry,state)
            closing(run,directory/'closing')
            state['stages']['closing']=inventory(directory/'closing');save(registry,state)
        manifest=directory/'closing/CLAIMS-MANIFEST.json'
        output=directory/'writing/output'
        completed=directory/'writing/completed.json'
        if completed.exists():output=Path(load(completed)['output'])
        if 'writing' not in state['stages']:
            state['stage']='writing';save(registry,state)
            # A completed entry-point receipt survives the registry commit window.
            if not completed.exists() and not (output/'writing-result.json').is_file():
                try:output=writing(run,manifest,directory/'writing')
                except Exception as exc:
                    state['errors'].append(dict(stage='writing',error=type(exc).__name__+': '+str(exc)))
                    save(registry,state);raise
            state['stages']['writing']=inventory(directory/'writing');save(registry,state)
        state['stage']='delivery';save(registry,state)
        destination=delivery(run,output,manifest,directory/'delivery')
        state['stages']['delivery']=inventory(destination)
        state['stage']='delivered';save(registry,state)
        return destination


def saved_writer_output(directory):
    """Use the latest nonempty writer publication; never synthesize a candidate."""
    directory=Path(directory)
    published=sorted(directory.glob('output*/delivery/manifest.json'))
    for path in reversed(published):
        if load(path).get('selected_candidate') is not None:return path.parent.parent
    return published[-1].parent.parent if published else directory/'output'


def resume(registry, *, waited_owner=None):
    from .deployment import require_compute
    require_compute()
    registry=Path(registry)
    with (registry.parent/'registry.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        state=load(registry)
        if state['stage'] in ('delivered','recovery-failed'):return 0
        if state['recoveries']>MAX_RECOVERIES+1:
            state['stage']='recovery-failed';save(registry,state);return 1
        status=Path(state['run']['durable_root'])/'operator-control/status.json'
        if state['stage']=='research' and waited_owner and status.is_file():
            owner=load(status)
            if owner.get('cohort')!=state['run']['cohort_id']:raise ValueError('foreign research owner status')
            # Planned allocation migration may outlive the first owner. Wait for
            # its successor too; a live board is never treated as ended research.
            if str(owner['job'])!=str(waited_owner):
                schedule(registry,state,dependency=str(owner['job']))
                return 0
        state['recoveries']+=1;save(registry,state)
        schedule(registry,state)
    try:
        if state['recoveries']>MAX_RECOVERIES:raise RuntimeError('automatic writer recovery exhausted')
        if state['run'].get('schema')=='hc-offline-chain-fixture-v1':
            from .chain_fixture import advance as fixture_advance
            fixture_advance(registry)
        else:advance(registry)
    except Exception as exc:
        state=load(registry)
        state['errors'].append(dict(stage=state['stage'],error=type(exc).__name__+': '+str(exc)))
        save(registry,state)
        if state['recoveries']<MAX_RECOVERIES:raise
        # Final automatic backup publishes evidence and diagnostics, never invents a paper.
        directory=registry.parent;manifest=directory/'closing/CLAIMS-MANIFEST.json'
        if not manifest.exists():save(manifest,dict(schema=1,claims=[]))
        try:
            destination=deliver(state['run'],saved_writer_output(directory/'writing'),manifest,directory/'delivery',
                flags=['automatic-recovery-exhausted',type(exc).__name__+': '+str(exc)])
        except Exception as delivery_error:
            state['errors'].append(dict(stage='delivery',error=str(delivery_error)))
            if state['recoveries']>=MAX_RECOVERIES+1:state['stage']='recovery-failed'
            save(registry,state)
            return 1
        state['stages']['delivery']=inventory(destination);state['stage']='delivered';save(registry,state)
    return 0


def main():
    p=argparse.ArgumentParser();p.add_argument('registry');a=p.parse_args()
    return resume(a.registry)

if __name__=='__main__':raise SystemExit(main())
