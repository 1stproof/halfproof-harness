"""Fenced whole-controller replacement on retained node-local service storage.

A planned handoff durably seals all services before the old publisher exits.
Unplanned/node-loss recovery has no implicit resume path. Its unacknowledged
normal replies require operator disposition, never automatic paid replay.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import time

from .accounting import AccountingStore
from .contracts_v3 import canonical_json, load_contracts
from .control import atomic_json
from .deployment import DeploymentRefused, local_root
from .durability import Publisher


def digest(value):
    return hashlib.sha256(canonical_json(value)).hexdigest()


def drain(publisher, *, seconds=180):
    end = time.monotonic() + seconds
    while True:
        publisher.poll()
        if not publisher.pending:return
        if time.monotonic() >= end:
            raise DeploymentRefused('handoff publication deadline exceeded')
        time.sleep(.01)


def replay_journal(publisher, expected=None):
    """Verify historical publications in one bounded child request.

    Service operations retain their original identities, snapshots and result
    protocol. Publications are fully verified before any service operation runs.
    """
    publisher.restore_journal(expected)
    for p in sorted(publisher.local.glob('operation-*.json')):
        value=json.loads(p.read_bytes())
        publisher.submit_operation(p.stem[len('operation-'):],value['operation'],value['payload'])
        drain(publisher)


def prepare(c, *, release, release_sha256):
    if getattr(c, 'handoff', False) or c.complete or c.clock() >= c.deadline:
        raise DeploymentRefused('terminal/expired/transferred controller cannot upgrade')
    release = Path(release).resolve(strict=True)
    from .release import verify_release
    executable_sha256=verify_release(release)
    if not re.fullmatch('[0-9a-f]{64}', release_sha256):
        raise DeploymentRefused('tested replacement release identity required')
    contract = json.loads((release/'contracts-manifest.json').read_bytes())
    if contract['sha256'] != c.contracts_sha256:
        raise DeploymentRefused('upgrade cannot change contracts or original allocations')
    generation = c.state.get('controller_generation', 0) + 1
    barrier = 'controller-handoff-' + str(generation)
    # Keep the failed handoff barrier closed across retry. Only this process's
    # receipt can authorize reuse; an unrelated fault always wins.
    with c.store.transaction() as db:
        row = db.execute("SELECT value FROM settings WHERE key='blocked'").fetchone()
        owned = getattr(c, '_failed_handoff', None)
        last = db.execute("SELECT seq,payload FROM events WHERE kind='admission-blocked' ORDER BY seq DESC LIMIT 1").fetchone()
        paused = db.execute("SELECT value FROM settings WHERE key='operator-paused'").fetchone()
        if row:
            if not (owned and owned == (generation, last[0] if last else None)
                    and json.loads(row[0]) == dict(reason=barrier)
                    and last and json.loads(last[1]) == dict(reason=barrier)
                    and paused and paused[0] == 'true' and not c.handoff
                    and not db.execute("SELECT 1 FROM events WHERE kind='accounting-anomaly' AND seq>?", (owned[1],)).fetchone()
                    and not c.store._audit(db)):
                raise DeploymentRefused('fault-blocked accounting cannot upgrade')
            barrier_seq = owned[1]
        else:
            db.execute("INSERT INTO settings VALUES ('blocked',?)", (json.dumps(dict(reason=barrier),separators=(',',':')),))
            c.store.event(db,'admission-blocked',dict(reason=barrier))
            barrier_seq = db.execute('SELECT last_insert_rowid()').fetchone()[0]
    c._failed_handoff = None
    c.handoff = True
    fences = {}; swarms = {}
    try:
        for arm, runtime in c.runtimes.items():
            watchdogs=(runtime.owned_watchdogs() if hasattr(runtime,'owned_watchdogs') else [runtime.watchdog,*getattr(runtime,'slots',{}).values()])
            for w in watchdogs:
                with w.mutex:
                    if hasattr(w,'consume'):w.consume()
                    if w.awaiting_reply():
                        raise DeploymentRefused('normal reply must be consumed before planned upgrade')
                    if w.worker:w.stop_worker()
                    if w.state.get('receipt_failure'):
                        raise DeploymentRefused('receipt fault forbids upgrade')
                    w.save()
            for gateway in runtime.gateways:gateway.cancel_worker(None)
            until = time.monotonic() + 20
            for gateway in runtime.gateways:
                while gateway.active_handlers:
                    if getattr(c,'cancelled',False):raise InterruptedError('external cohort stop during handoff fence')
                    if time.monotonic() >= until:
                        raise DeploymentRefused('old request handler survived upgrade fence')
                    time.sleep(.01)
            # Existing reconciliation drains are bounded at 330 real seconds.
            # Give them that bound plus projection/exit time, independently of
            # the handler fence. Never declare a surviving owner fenced.
            metadata_until = time.monotonic() + 350
            for gateway in runtime.gateways:
                while ((gateway.reconciliation_thread and gateway.reconciliation_thread.is_alive())
                       or getattr(gateway,'reconciling',None)):
                    if getattr(c,'cancelled',False):raise InterruptedError('external cohort stop during metadata fence')
                    if time.monotonic() >= metadata_until:
                        raise DeploymentRefused('old metadata owner survived upgrade fence')
                    time.sleep(.05)
            if hasattr(runtime,'stop_services'):runtime.stop_services()
            fences[arm] = runtime.writer_scope.fence(admission_closed=True, active_requests=0,
                cancelled_request_ids=list(dict.fromkeys(row['request'] for w in watchdogs for row in w.state.get('bounded_cancellations',[]))))
            if arm.endswith('-swarm'):
                from .swarm_restore import capture
                swarms[arm]=capture(runtime)
        drain(c.publisher)
        c.store.assert_conservation()
        c.state['controller_generation'] = generation
        c.checkpoint()
        drain(c.publisher)
        # This inventory is read only after writers and publisher operations drain.
        # It includes board stores, sealed packets, leases and allocation counters
        # when dispatch owns such service directories under the controller root.
        files = {}
        for path in sorted(c.root.rglob('*')):
            name = str(path.relative_to(c.root))
            if path.is_symlink():
                files[name] = {'link': os.readlink(path)}
                continue
            if not path.is_file():continue
            if name.startswith('publisher/') or name in ('controller.lock', 'accounting.sqlite',
                    'accounting.sqlite-wal', 'accounting.sqlite-shm', 'controller-handoff.json'):
                continue
            # Earlier completed handoffs are immutable history, not current state.
            if name.startswith('accounting-before-handoff-'):continue
            if path.stat().st_size > 128*1024*1024 or len(files) >= 50000:
                raise DeploymentRefused('controller state inventory bound exceeded')
            files[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        record = dict(schema=1, root=str(c.root), source=str(c.source), release=str(release),
            previous_release_sha256=c.state['release_sha256'], release_sha256=release_sha256,
            executable_sha256=executable_sha256,
            contracts_sha256=c.contracts_sha256, durable=str(c.durable),
            cohort=c.cohort, problem=c.problem, problem_text=c.problem_text,
            profile=c.profile, prices=c.prices, layouts=c.layouts, deployment=c.deployment,
            credential_id=c.credential_id,
            events=c.events, streams=sorted(c.streams), state=c.state,
            fences=c.fences, frozen=c.frozen, writer_fences=fences,
            operations=c.operations, accounting=c.store.checkpoint(),
            files=files, barrier=barrier,swarms=swarms,
            journal={n:h for n,(h,_) in c.publisher.acked.items() if not n.startswith('operation-')})
        # Snapshot now; later state mutations must not alter the submitted object.
        record = json.loads(json.dumps(record))
        sha = digest(record)
        identity = 'controller-handoff-' + str(generation)
        c.publisher.submit(identity, record)
        drain(c.publisher)
        if not c.publisher.require_ack(identity):
            raise DeploymentRefused('handoff not durable')
        if not c.publisher.close():
            raise DeploymentRefused('old publisher not reaped')
        atomic_json(c.root/'controller-handoff.json',dict(record=record,sha256=sha,status='prepared'))
        return sha
    except BaseException:
        # Failure never reopens admission or manufactures a restore receipt.
        c.handoff = False
        # Receipt is process-local, tied to the exact generation/block event.
        # It permits another full fence, never restoration or admission.
        c._failed_handoff = (generation, barrier_seq)
        raise


def restore(cls, root, *, handoff_sha256, fake_providers, clock, live_transport=None):
    root = local_root(root)
    path = root/'controller-handoff.json'
    envelope = json.loads(path.read_bytes())
    record = json.loads(json.dumps(envelope['record']))
    if envelope['status'] != 'prepared' or digest(record) != handoff_sha256 or envelope['sha256'] != handoff_sha256:
        raise DeploymentRefused('missing, changed, or consumed handoff')
    live=bool(record['state'].get('live',False))
    if live:
        if fake_providers is not None or not live_transport or not callable(live_transport.get('load_key')):
            raise DeploymentRefused('live replacement requires deferred credential loading')
        if live_transport.get('credential_id')!=record['credential_id']:
            raise DeploymentRefused('replacement cannot change credential reconciliation identity')
        if record['profile'] in ('production','swarm-check','solo-check','closing-check','prod-check','open-check','open-production','prod-continue','open-continue'):
            if clock is not time.time and not (type(clock) is __import__('v2.pause',fromlist=['RunClock']).RunClock and clock.wall is time.time):
                raise DeploymentRefused('live production restore requires the real wall clock')
            from .production import validate_problem
            validate_problem(record['deployment']['problem'])
        else:
            from .pilot import problem_record
            if record['deployment']['problem']!=problem_record():raise DeploymentRefused('original problem changed')
        fake_providers={arm:None for arm in record['streams']}
    elif live_transport is not None:raise DeploymentRefused('fake checkpoint cannot become a paid cohort')
    if record['root'] != str(root) or sorted(fake_providers or {}) != record['streams']:
        raise DeploymentRefused('restore storage/strategy scope changed')
    for name, sha in record['files'].items():
        p = root/name
        if isinstance(sha,dict):
            valid=p.is_symlink() and os.readlink(p)==sha['link']
        else:
            valid=not p.is_symlink() and p.is_file() and hashlib.sha256(p.read_bytes()).hexdigest()==sha
        if not valid:raise DeploymentRefused('retained service state changed: '+name)
    from .release import verify_controller_source
    if verify_controller_source(record['release'])!=record['executable_sha256']:
        raise DeploymentRefused('replacement executable identity changed')
    # Acquire the existing publisher lock before touching accounting or admission.
    # A hung/live publisher prevents replacement; no second remote writer starts.
    publisher = Publisher(root/'publisher',Path(record['durable'])/'journal',
        readiness_timeout=min(600,max(.01,record['state']['cleanup_deadline']-clock())))
    try:
        # Replay immutable local journal entries to recover results/acks, never
        # resnapshot evidence or choose new operation identities.
        replay_journal(publisher,record.get('journal'))
        c=cls.__new__(cls)
        c.root,c.source,c.release=root,Path(record['source']),Path(record['release'])
        c.durable,c.clock,c.profile=Path(record['durable']),clock,record['profile']
        c.contracts_sha256=record['contracts_sha256']
        c.policy=load_contracts(expected_sha256=c.contracts_sha256)
        c.cohort,c.problem,c.problem_text=record['cohort'],record['problem'],record['problem_text']
        c.streams,c.prices,c.layouts=fake_providers,record['prices'],record['layouts']
        c.live=live;c.credential_id=record['credential_id'];c.transport_key=None
        c.deployment,c.events=record['deployment'],record['events']
        c.state=json.loads(json.dumps(record['state']));c.state['release_sha256']=record['release_sha256']
        c.start=c.state['original_start'];c.deadline=c.state['deadline'];c.cleanup_deadline=c.state['cleanup_deadline']
        c.readiness=c.state['readiness'];c.operations=record['operations']
        c.fences,c.frozen=record['fences'],record['frozen']
        c.publisher,c.failed,c.handoff=publisher,None,False
        c.runtimes={};c.started=set(c.state['started_arms'])
        previous=AccountingStore(root/'accounting.sqlite',clock=clock)
        if previous.checkpoint()!=record['accounting']:
            raise DeploymentRefused('accounting changed after fenced checkpoint')
        # Retain original database as audit evidence; replace from complete P3
        # checkpoint so all reservations become conservative unresolved liability.
        backup=root/('accounting-before-handoff-'+str(c.state['controller_generation']))
        (root/'accounting.sqlite').rename(backup)
        for suffix in ('-wal','-shm'):
            p=root/('accounting.sqlite'+suffix)
            if p.exists():p.rename(Path(str(backup)+suffix))
        c.store=AccountingStore.restore_checkpoint(root/'accounting.sqlite',record['accounting'],
            fence_proof=handoff_sha256,clock=clock,
            lookup_clock=time.time if type(clock) is __import__('v2.pause',fromlist=['RunClock']).RunClock else None)
        if type(clock) is __import__('v2.pause',fromlist=['RunClock']).RunClock:
            c.store.lookup_clock=time.time
        if live:
            c.transport_key=live_transport['load_key']()
            if not isinstance(c.transport_key,str) or not c.transport_key.strip():
                raise DeploymentRefused('empty restored credential')
        c._restored_swarms=record.get('swarms',{})
        c._handoff_writer_fences=record['writer_fences']
        c._make_runtimes()
        for arm,runtime in c.runtimes.items():
            runtime.writer_scope.epoch=record['writer_fences'].get(arm,{}).get('publication_epoch',0)
            w=runtime.watchdog
            if w.state.get('receipt_failure') or w.awaiting_reply():
                raise DeploymentRefused('unacknowledged reply forbids restored admission')
        drain(publisher)
        c.checkpoint();drain(publisher)
        # A consumed handoff cannot be replayed to reset clocks or counters.
        atomic_json(path,dict(record=record,sha256=handoff_sha256,status='consumed'))
        with c.store.transaction() as db:
            row=db.execute("SELECT value FROM settings WHERE key='blocked'").fetchone()
            if row is None or json.loads(row[0]) != dict(reason=record['barrier']):
                raise DeploymentRefused('restore cannot clear an unrelated accounting fault')
            db.execute("DELETE FROM settings WHERE key='blocked'")
            c.store.event(db,'controller-restored',dict(handoff=handoff_sha256))
        c.checkpoint();drain(publisher)
        return c
    except BaseException:
        if 'c' in locals():
            if hasattr(c,'store'):
                c.store.block('restore-operational-stop')
                try:c.cancel_salvage(seconds=180)
                except Exception:pass
            for runtime in c.runtimes.values():
                if hasattr(runtime,'stop_services'):
                    try:runtime.stop_services()
                    except Exception:pass
        publisher.close()
        raise
