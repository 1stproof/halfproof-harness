"""Operator-only pause: seal services, retain job/state, restore without clock renewal.

Scientific deadlines stay in active-time coordinates. RunClock translates real
wall time into that domain; status exposes their shifted wall-time equivalents.
Provider I/O and reconciliation retain bounded real-time timeouts.
"""
import hashlib
import json
import os
from pathlib import Path
import sys
import signal
import time
import threading
import uuid
from .control import atomic_json
from .deployment import DeploymentRefused


class RunClock:
    def __init__(self, *, excluded=0., since=None, wall=time.time, reasons=None):
        self.excluded, self.since, self.wall = excluded, since, wall
        self.reasons = set(reasons or (['operator'] if since is not None else []))
        self.mutex = threading.RLock()
    def __call__(self):
        with self.mutex:
            return (self.since if self.since is not None else self.wall()) - self.excluded
    def pause(self, reason='operator'):
        with self.mutex:
            self.reasons.add(reason)
            if self.since is None:self.since = self.wall()
    def resume(self, reason='operator'):
        with self.mutex:
            self.reasons.discard(reason)
            if self.since is not None and not self.reasons:
                self.excluded += max(0., self.wall()-self.since)
                self.since = None
    def snapshot(self):
        with self.mutex:
            row = dict(excluded=self.excluded, since=self.since)
            if self.reasons - {'operator'}:row['reasons'] = sorted(self.reasons)
            return row
    def shift(self):
        with self.mutex:
            return self.excluded + (max(0.,self.wall()-self.since) if self.since is not None else 0.)


def verified_source(manifest):
    """Verify the entire packaged source, before importing replacement modules."""
    root=Path(manifest['release_root']).resolve(strict=True)
    files=manifest['release_files']
    if not files or len(files)>10000:raise DeploymentRefused('replacement inventory bound')
    actual={str(p.relative_to(root)) for p in root.rglob('*') if p.is_file()}
    if any(p.is_symlink() for p in root.rglob('*')) or actual!=set(files):
        raise DeploymentRefused('replacement source inventory differs')
    digest=hashlib.sha256()
    for name, sha in sorted(files.items()):
        relative=Path(name)
        if relative.is_absolute() or '..' in relative.parts:raise DeploymentRefused('unsafe replacement path')
        if hashlib.sha256((root/name).read_bytes()).hexdigest()!=sha:
            raise DeploymentRefused('replacement source checksum differs')
        digest.update(name.encode()+b'\0'+sha.encode()+b'\n')
    if digest.hexdigest()!=manifest['release_sha256']:
        raise DeploymentRefused('replacement source digest differs')
    if not (root/'v2/pause.py').is_file():raise DeploymentRefused('replacement lacks pause protocol')
    return root


class PauseControl:
    def __init__(self, controller, manifest, *, clock=None):
        self.c, self.manifest = controller, manifest
        self.directory=controller.durable/'operator-control'
        self.directory.mkdir(parents=True, exist_ok=True)
        self.clock=clock or (controller.clock if isinstance(controller.clock, RunClock) else RunClock())
        controller.clock=controller.store.clock=self.clock
        self.job=str(os.environ.get('SLURM_JOB_ID',''))
        if not self.job:raise DeploymentRefused('pause control requires SLURM ownership')
        self.phase='running';self.ack=None;self.error=None;self.handoff=None;self.next_poll=0.;self.in_progress=None
        controller.store.lookup_clock=time.time
        self.provider = None
        if getattr(controller,'live',False):
            from .provider_refusal import ProviderControl
            self.provider = ProviderControl(self)
            controller.provider_control = self.provider
            self.provider.tick(monitor=False)
        self.publish()

    def publish(self):
        totals=self.c.store.totals()
        held=totals['liability']+totals['reservation']
        shift=self.clock.shift()  # one observation for every translated deadline
        row=dict(schema=1,job=self.job,cohort=self.c.cohort,phase=self.phase,
            paused_since=self.clock.since,excluded_seconds=shift,
            active_elapsed_seconds=max(0,self.clock()-self.c.start),
            deadline=self.c.deadline+shift,cleanup_deadline=self.c.cleanup_deadline+shift,
            held_microusd=held,ack=self.ack,in_progress=self.in_progress,error=self.error,observed_at=time.time(),
            provider_refusal=self.c.state.get('provider_refusal'),
            release_sha256=self.c.state['release_sha256'],resume_mode='save-state + restart-from-state')
        row['closing_deadline_protocol'] = 1
        atomic_json(self.directory/'status.json',row)
        pacer = (self.c.state.get('provider_refusal') or {}).get('pacer')
        if pacer:atomic_json(self.directory/'daily-spend-pacer.json',pacer)
        return row

    def log(self, event, **detail):
        with (self.directory/'events.jsonl').open('a') as handle:
            handle.write(json.dumps(dict(at=time.time(),event=event,job=self.job,**detail))+'\n')
            handle.flush();os.fsync(handle.fileno())

    def pause(self, *, drain_seconds=3300, cancel_after=None):
        if self.phase=='paused':return
        if self.c.complete:raise DeploymentRefused('terminal run cannot pause')
        # Transaction serializes this admission barrier with racing reserve().
        with self.c.store.transaction() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES ('operator-paused','true')")
            self.c.store.event(db,'operator-pause',dict(job=self.job))
        self.clock.pause();self.phase='pausing';self.publish();self.log('pause-requested')
        end=time.monotonic()+drain_seconds
        next_status=time.monotonic()+1
        cancel_at=None if cancel_after is None else time.monotonic()+max(0,cancel_after)
        cancelled_transports=False
        while True:
            if getattr(self.c,'cancelled',False):raise InterruptedError('external cohort stop during pause drain')
            self.c.publisher.poll()
            watchdogs=[w for r in self.c.runtimes.values() for w in (r.owned_watchdogs() if hasattr(r,'owned_watchdogs') else [r.watchdog,*getattr(r,'slots',{}).values()])]
            for w in watchdogs:
                with w.mutex:
                    if hasattr(w,'consume'):w.consume()
            if cancel_at is not None and not cancelled_transports and time.monotonic()>=cancel_at:
                for runtime in self.c.runtimes.values():
                    for gateway in runtime.gateways:
                        if gateway.active_handlers:gateway.cancel_worker(None)
                cancelled_transports=True;self.log('pause-transports-cancelled')
            active=any(g.active_handlers for r in self.c.runtimes.values() for g in r.gateways)
            provider_busy = bool(self.provider and self.provider.busy())
            if not active and not provider_busy and not any(w.awaiting_reply() for w in watchdogs):break
            if time.monotonic()>=next_status:
                self.publish();next_status=time.monotonic()+1
            if time.monotonic()>=end:raise DeploymentRefused('pause drain timed out; admission remains closed')
            time.sleep(.05)
        # Existing whole-controller path preserves board DB, claim revisions,
        # verdicts, executor cursors, leases and accounting, never claim_import.
        self.handoff=self.c.prepare_replacement(release=self.c.release,
            release_sha256=self.c.state['release_sha256'])
        self.phase='paused';self.publish();self.log('paused',handoff=self.handoff)

    def reconcile(self):
        # No scientific service runs after the handoff. Reconciliation may advance
        # on real time while the run clock remains frozen; retain unknown liability.
        from .accounting import Reconciler
        seen=set()
        for runtime in self.c.runtimes.values():
            for gateway in runtime.gateways:
                if (gateway.key or gateway.fake is not None) and gateway.admission and gateway.admission.credential not in seen:
                    seen.add(gateway.admission.credential)
                    Reconciler(self.c.store,gateway.lookup,credential=gateway.admission.credential).drain(seconds=16)
        self.c.store.assert_conservation()

    def prepare_resume(self, replacement=None):
        if self.phase!='paused':raise DeploymentRefused('run must be fully paused before resume')
        self.reconcile()
        if any(row['slot'] for row in self.c.store.export()['attempts']):
            raise DeploymentRefused('upstream request settlement pending; run remains paused, retry resume after settlement')
        path=self.c.root/'controller-handoff.json'
        envelope=json.loads(path.read_bytes());record=envelope['record']
        if replacement:
            source=verified_source(replacement)
            if replacement['contracts_sha256']!=self.c.contracts_sha256:
                raise DeploymentRefused('replacement cannot change contracts or allocation')
            from .release import pin_release,verify_release
            release=pin_release(source,self.c.root)
            if json.loads((release/'contracts-manifest.json').read_text())['sha256']!=self.c.contracts_sha256:
                raise DeploymentRefused('replacement contract bytes changed')
            # New release files are outside the sealed service inventory. Every
            # original board/ledger file is still verified by restore().
            record.update(source=str(source),release=str(release),
                release_sha256=replacement['release_sha256'],executable_sha256=verify_release(release))
        self.reconcile()
        record['accounting']=self.c.store.checkpoint()
        # Paused time includes setup/restart: restart entry computes the offset
        # immediately before restore, then unfreezes after reconstruction.
        record['state']['operator_clock']=self.clock.snapshot()
        from .cohort_restore import digest
        sha=digest(record)
        atomic_json(self.directory/('resume-record-'+sha+'.json'),dict(record=record,sha256=sha,status='prepared'))
        atomic_json(path,dict(record=record,sha256=sha,status='prepared'))
        atomic_json(self.directory/'resume.json',dict(root=str(self.c.root),handoff=sha,
            manifest=self.manifest,clock=self.clock.snapshot(),ack=self.in_progress or self.ack,
            replacement=replacement,previous_handoff=self.handoff))
        self.log('resume-prepared',handoff=sha,release_sha256=record['release_sha256'])
        return sha,Path(record['source'])

    def tick(self):
        if time.monotonic()<self.next_poll:return self.phase!='running'
        self.next_poll=time.monotonic()+1
        from .closing_deadline import tick as closing_deadline_tick
        closing_deadline_tick(self)
        request=self.directory/'request.json'
        if request.exists():
            row=json.loads(request.read_bytes())
            if row.get('job')!=self.job or row.get('cohort')!=self.c.cohort:
                raise DeploymentRefused('operator request ownership mismatch')
            if row['id']!=self.ack:
                self.in_progress=row['id'];self.error=None;self.publish()
                try:
                    if row['action']=='pause':
                        if row.get('cancel_inflight_after') is None:self.pause()
                        else:self.pause(cancel_after=row['cancel_inflight_after'])
                    elif row['action']=='resume':
                        if self.phase=='running':self.ack=row['id'];self.in_progress=None;self.publish();return False
                        sha,source=self.prepare_resume(row.get('replacement'))
                        self.publish()
                        # exec keeps SLURM allocation and /tmp services. Python
                        # starts afresh, so changed code actually owns the controller.
                        self.exec_source(source)
                    else:raise DeploymentRefused('unknown pause action')
                except InterruptedError:raise
                except Exception as exc:
                    self.error=type(exc).__name__+': '+str(exc)
                    self.log('control-failed',error=self.error)
                    if self.phase=='pausing':self.phase='pause-failed'
                finally:
                    self.ack=row['id'];self.in_progress=None
        if self.phase in ('paused','pause-failed'):self.reconcile()
        self.publish()
        return self.phase!='running'

    def exec_source(self, source):
        # Carry pending cancellation through Python startup. The fresh shared
        # lifecycle installs handlers before unblocking these inherited signals.
        previous=signal.pthread_sigmask(signal.SIG_BLOCK,{signal.SIGTERM,signal.SIGINT})
        try:
            if getattr(self.c,'cancelled',False):raise InterruptedError('external cohort stop before resume exec')
            os.chdir(source)
            os.execv(sys.executable,[sys.executable,'-s','-m','v2.pause_restart',str(self.directory/'resume.json')])
        finally:
            # Reached only if exec fails (or a test intercepts it).
            signal.pthread_sigmask(signal.SIG_SETMASK,previous)
