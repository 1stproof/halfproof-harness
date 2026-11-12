"""Planned two-allocation handoff. Old exec owner has no scientific services.

Paths and sealed bytes are retained verbatim on a different node. The new job
restores under the operator barrier and acknowledges before the old job exits.
Only an acknowledged retirement permits new scientific admission.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import tarfile
import time

from .control import atomic_json
from .deployment import DeploymentRefused, ownership, preflight, require_compute, slurm_command
from .cohort_restore import digest
from .pause import RunClock, PauseControl, verified_source


def file_sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
    return h.hexdigest()


def allocation_seconds(record, clock, margin=3600):
    return allocation_bound(record['state']['cleanup_deadline']-RunClock(**clock)(),margin)


def allocation_bound(remaining, margin=3600):
    seconds=math.ceil(max(0,remaining)+margin)
    if remaining<=0 or not 0<seconds<=172800:
        raise DeploymentRefused('remaining active science + closing + margin exceeds normal two-day limit')
    return seconds


def load_seal(control):
    root=Path(control['root'])
    envelope=json.loads((root/'controller-handoff.json').read_bytes())
    record=envelope['record']
    if (envelope['status']!='prepared' or envelope['sha256']!=control['handoff']
            or digest(record)!=control['handoff'] or record['root']!=str(root)
            or record['state'].get('operator_clock')!=control['clock']
            or control['clock']['since'] is None):
        raise DeploymentRefused('migration requires the exact frozen prepared handoff')
    from .accounting import AccountingStore
    store=AccountingStore(root/'accounting.sqlite',clock=RunClock(**control['clock']))
    store.assert_conservation()
    if store.checkpoint()!=record['accounting']:
        raise DeploymentRefused('migration accounting changed after seal')
    if any(row['slot'] for row in store.export()['attempts']):
        raise DeploymentRefused('migration refuses uncertain upstream slots')
    for name,sha in record['files'].items():
        path=root/name
        good=(path.is_symlink() and os.readlink(path)==sha['link']) if isinstance(sha,dict) else (
            path.is_file() and not path.is_symlink() and file_sha(path)==sha)
        if not good:raise DeploymentRefused('sealed migration member changed: '+name)
    return record


SEAL_FLUSH=256*1024*1024


class SealWriter:
    """Write-only archive sink: hashes as it writes and, every SEAL_FLUSH bytes,
    fsyncs and drops the written pages so the seal's page cache stays bounded
    inside the old job's memory limit. Exclusive create: never overwrites."""
    def __init__(self, path):
        self.fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        self.hash=hashlib.sha256();self.size=0;self.synced=0
    def write(self, data):
        view=memoryview(data)
        while view:
            n=os.write(self.fd,view);self.hash.update(view[:n]);self.size+=n;view=view[n:]
        if self.size-self.synced>=SEAL_FLUSH:self.sync()
        return len(data)
    def tell(self):return self.size
    def sync(self):
        os.fsync(self.fd);os.posix_fadvise(self.fd,0,0,os.POSIX_FADV_DONTNEED);self.synced=self.size
    def close(self):
        if self.fd is None:return
        try:self.sync()
        finally:os.close(self.fd);self.fd=None


def archive_root(root, destination):
    """No following symlinks; SQLite is closed and all service writers fenced.

    Streams one member at a time; source pages are dropped after each file.
    Returns the archive's sha256, computed from the bytes as written."""
    sink=SealWriter(destination)
    try:
        with tarfile.open(fileobj=sink,mode='w') as tar:
            for directory,dirs,files in os.walk(root,followlinks=False):
                parent=Path(directory)
                for name in sorted(dirs+files):
                    path=parent/name
                    relative=path.relative_to(root)
                    # Locks and stale sockets are process artifacts, never state.
                    if str(relative)=='controller.lock' or not (path.is_file() or path.is_dir() or path.is_symlink()):continue
                    info=tar.gettarinfo(str(path),arcname=str(relative))
                    if not info.isreg():tar.addfile(info);continue
                    with path.open('rb') as handle:
                        tar.addfile(info,handle)
                        os.posix_fadvise(handle.fileno(),0,0,os.POSIX_FADV_DONTNEED)
        sink.close()
    except BaseException:
        sink.close();raise
    return sink.hash.hexdigest()


def seal_space(root, directory):
    """Refuse before writing when the archive cannot fit (filesystem free space;
    per-user quota is not visible here, so ENOSPC/EDQUOT still falls back)."""
    need=0
    for base,dirs,files in os.walk(root,followlinks=False):
        for name in dirs+files:need+=os.lstat(os.path.join(base,name)).st_size+1536
    stat=os.statvfs(directory);free=stat.f_bavail*stat.f_frsize
    if need*1.1+(1<<30)>free:
        raise DeploymentRefused('migration archive needs %d bytes; %d free at %s'%(need,free,directory))
    return need


def shared_directory(directory):
    """The archive must be on shared scratch: never node-local, never $HOME
    (a 2026-10-13 seal filled the $HOME quota and killed three runs)."""
    path=Path(directory).resolve()
    homes=[Path('/home').resolve()]
    if os.environ.get('HOME'):homes.append(Path(os.environ['HOME']).resolve())
    if any(path==h or path.is_relative_to(h) for h in homes):
        raise DeploymentRefused('migration directory must not be under $HOME: '+str(path))
    return path


def extract_root(archive, root):
    """Validate every member before any writes; no archive symlink traversal."""
    with tarfile.open(archive) as tar:
        rows=tar.getmembers();links={r.name for r in rows if r.issym()}
        for row in rows:
            path=Path(row.name)
            if (path.is_absolute() or '..' in path.parts or row.islnk()
                    or not (row.isfile() or row.isdir() or row.issym())
                    or any(str(p) in links for p in path.parents)):
                raise DeploymentRefused('unsafe migration archive member')
        # Absolute symlinks are sealed service/dependency pointers. Never follow
        # them during extraction; cohort_restore verifies their exact strings.
        tar.extractall(root, members=rows, filter='fully_trusted')


def wait_json(path, predicate, seconds):
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        if Path(path).exists():
            row=json.loads(Path(path).read_bytes())
            if predicate(row):return row
            raise DeploymentRefused('migration acknowledgment binding differs')
        time.sleep(1)
    raise DeploymentRefused('migration acknowledgment timed out; admission remains closed')


def rebind_request(directory, control, binding):
    """The acknowledged migration command is history, never a second request."""
    path=Path(control['manifest']['durable_root'])/'operator-control/request.json'
    request=json.loads(path.read_bytes())
    if (request['id']!=control['ack'] or request['id']!=binding['id']
            or request['job']!=binding['old_job'] or request['cohort']!=binding['cohort']):
        raise DeploymentRefused('migration operator command changed before transfer')
    atomic_json(Path(directory)/'old-request.json',request)
    atomic_json(path,dict(request,job=binding['new_job']))


def claim_receiver(directory, binding):
    """A shared, exclusive one-shot claim survives node loss and job requeue."""
    claim=Path(directory)/'receiver-claim'
    try:claim.mkdir(mode=0o700,exist_ok=False)
    except FileExistsError:
        raise DeploymentRefused('migration receiver already claimed; replay is forbidden')
    parent=os.open(directory,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(parent)
    finally:os.close(parent)
    atomic_json(claim/'owner.json',dict(binding=binding,job=os.environ['SLURM_JOB_ID'],
        node=os.environ['SLURMD_NODENAME'],pid=os.getpid(),at=time.time()))


NODE_LOCAL=Path('/tmp')


def precheck(manifest, status, replacement):
    """Deterministic refusals, run by pause_run before any request is written."""
    if Path(manifest['local_root']).parent!=NODE_LOCAL:
        raise DeploymentRefused('migration root must be a unique node-local /tmp directory')
    if Path(replacement['release_root']).resolve().is_relative_to(NODE_LOCAL):
        raise DeploymentRefused('migration requires a shared packaged source')
    if not (Path(replacement['release_root'])/'v2/migration.py').is_file():
        raise DeploymentRefused('replacement lacks migration protocol')
    # Paused: the published wall cleanup deadline minus its observation time is
    # exactly the remaining active cleanup that transfer() sizes the job from.
    return allocation_bound(status['cleanup_deadline']-status['observed_at'])


def transfer(control, progress=None):
    require_compute()
    replacement=control['replacement'];verified_source(replacement)
    migration=replacement['migration'];directory=Path(migration['directory'])
    record=load_seal(control);root=Path(control['root'])
    if root.parent!=Path('/tmp'):
        raise DeploymentRefused('migration root must be a unique node-local /tmp directory')
    if Path(record['release']).parent.parent!=root:
        raise DeploymentRefused('migration executable/runtime pins must be inside the sealed root')
    # Updated source is shared, not a node-local development tree.
    if Path(record['source']).is_relative_to('/tmp'):
        raise DeploymentRefused('migration requires a shared packaged source')
    shared_directory(directory)
    archive=directory/'state.tar'
    seal_space(root,directory)
    if progress is not None:progress['archive']=archive
    archive_hash=archive_root(root,archive)
    binding=dict(id=migration['id'],old_job=str(os.environ['SLURM_JOB_ID']),
        old_node=os.environ['SLURMD_NODENAME'],handoff=control['handoff'],archive_sha256=archive_hash,
        control_sha256=digest(control),cohort=record['cohort'],release_sha256=record['release_sha256'])
    atomic_json(directory/'sealed.json',dict(binding=binding,control=control))
    original=control['manifest'];target=dict(original['target'])
    target.update(migration.get('resources') or {})  # operator memory/CPU override only
    target.update(partition='normal',nodes=['*'],seconds=allocation_seconds(record,control['clock']))
    job_manifest=dict(original,action='allocation-migration',target=target,
        release_root=str(Path(replacement['release_root']).resolve()),release_files=replacement['release_files'],
        release_sha256=replacement['release_sha256'],migration_directory=str(directory))
    job_path=directory/'job-manifest.json';atomic_json(job_path,job_manifest)
    command=slurm_command(job_path,job_manifest,output=directory/'new-job.log')
    command.insert(2,'--exclude='+binding['old_node'])
    command.insert(2,'--no-requeue')
    # No automatic retry on ambiguous sbatch response: retain intent/evidence.
    atomic_json(directory/'submit-intent.json',dict(binding=binding,command=command,target=target))
    if progress is not None:progress['binding']=binding;progress['sbatch']=True
    result=subprocess.run(command,text=True,capture_output=True,timeout=120)
    atomic_json(directory/'submit-result.json',dict(returncode=result.returncode,stdout=result.stdout,stderr=result.stderr))
    job=result.stdout.strip().split(';')[0]
    if result.returncode or not job.isdigit():raise DeploymentRefused('new allocation submission failed or ambiguous')
    binding=dict(binding,new_job=job)
    if progress is not None:progress['binding']=binding
    atomic_json(directory/'submitted.json',binding)
    wait_json(directory/'restored.json',lambda r:r==binding,7200)
    # New receiver proved complete reconstruction with operator-paused retained.
    # This process owns no publisher, gateways or workers; returning exits the
    # old sbatch naturally without cancellation, salvage or terminal publication.
    atomic_json(directory/'retired.json',binding)
    atomic_json(directory/'old-result.json',dict(status='migrated',binding=binding,at=time.time()))
    return 0


def fence_receiver(directory, binding, submitted=True):
    """Take the receiver's one-shot claim for the old owner. True: no receiver
    can ever restore this transfer, so the old allocation may restore it.

    The claim directory alone is the fence; its receipt is best effort, since
    the failure being handled may be a full filesystem. Before sbatch ran no
    receiver can exist, so an unwritable fence does not block restoring here."""
    claim=Path(directory)/'receiver-claim'
    for attempt in range(6):
        try:claim.mkdir(mode=0o700,exist_ok=False);break
        except FileExistsError:return False
        except OSError:
            if not submitted:return True
            if attempt==5:raise
            time.sleep(10)
    try:
        parent=os.open(directory,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(parent)
        finally:os.close(parent)
        atomic_json(claim/'owner.json',dict(binding=dict(binding,fenced_by='old-owner'),job=os.environ['SLURM_JOB_ID'],
            node=os.environ['SLURMD_NODENAME'],pid=os.getpid(),at=time.time()))
    except OSError:pass
    return True


def failure_receipt(control, directory, row):
    """failed.json in the migration directory, else in the durable operator
    directory; never raises, so a full disk cannot stop same-job restore."""
    places=[Path(directory)/'failed.json']
    durable=(control.get('manifest') or {}).get('durable_root')
    if durable:places.append(Path(durable)/'operator-control'/('migration-failed-'+str(control['replacement']['migration'].get('id'))+'.json'))
    for place in places:
        try:atomic_json(place,dict(row,receipt=str(place)));return place
        except OSError as exc:row=dict(row,**{'unwritten_'+place.name:type(exc).__name__+': '+str(exc)})
    try:print('migration failure receipt unwritten: '+json.dumps(row,sort_keys=True),file=sys.stderr,flush=True)
    except Exception:pass
    return None


def transfer_owner(control):
    """None after retirement; else the stop signal (or 0) for same-job restore.

    Any failure before a receiver claims the transfer fences the receiver and
    returns, so the caller resumes this allocation (or salvages on SIGTERM).
    Once a receiver has claimed, the old owner only waits to retire.
    """
    directory=Path(control['replacement']['migration']['directory'])
    stop={}
    def stopped(signum,_frame):
        if 'signum' in stop or stop.get('failing'):
            stop.setdefault('signum',signum);return
        stop['signum']=signum
        try:atomic_json(directory/'old-stop.json',dict(job=os.environ['SLURM_JOB_ID'],signal=signum,at=time.time()))
        except OSError:pass  # full disk: the stop is still honoured below
        raise InterruptedError('migration owner allocation stopped')
    signal.signal(signal.SIGTERM,stopped);signal.signal(signal.SIGINT,stopped)
    signal.pthread_sigmask(signal.SIG_UNBLOCK,{signal.SIGTERM,signal.SIGINT})
    progress={}
    try:
        transfer(control,progress);return None
    except BaseException as exc:
        stop['failing']=True
        # From here a further signal is only recorded; the restore path handles it.
        def later(signum,_frame):stop.setdefault('signum',signum)
        signal.signal(signal.SIGTERM,later);signal.signal(signal.SIGINT,later)
        error=type(exc).__name__+': '+str(exc)
        binding=progress.get('binding')
        archive=progress.get('archive')
        if binding is None and archive is not None:
            # Unsealed partial archive: nothing references it; free its space.
            try:Path(archive).unlink(missing_ok=True)
            except OSError:pass
        if binding is None or fence_receiver(directory,binding,submitted=bool(progress.get('sbatch'))):
            cancelled=None
            if binding and binding.get('new_job'):
                # Best effort: the fence already stops the receiver restoring,
                # so a failed scancel must not abort the same-job fallback.
                try:
                    r=subprocess.run(['scancel',binding['new_job']],text=True,capture_output=True,timeout=60)
                    cancelled=dict(job=binding['new_job'],returncode=r.returncode,stderr=r.stderr)
                except Exception as cancel_exc:
                    cancelled=dict(job=binding['new_job'],returncode=None,
                        error=type(cancel_exc).__name__+': '+str(cancel_exc))
            mode='same-job-salvage' if 'signum' in stop else 'same-job-resume'
            failure_receipt(control,directory,dict(status='failed',job=os.environ['SLURM_JOB_ID'],
                error=error,fallback=mode,receiver_fenced=True,cancelled_new_job=cancelled,
                partial_archive_removed=binding is None and archive is not None,
                admission_closed=mode=='same-job-salvage',at=time.time()))
            return stop.get('signum',0)
        # A receiver owns the transfer: never restore here. Retire once it restores.
        failure_receipt(control,directory,dict(status='failed',job=os.environ['SLURM_JOB_ID'],
            error=error,fallback=None,receiver_claimed=True,admission_closed=True,at=time.time()))
        if 'signum' in stop:raise
        while 'signum' not in stop:
            restored=directory/'restored.json'
            if restored.exists() and json.loads(restored.read_bytes())==binding:
                atomic_json(directory/'retired.json',binding)
                atomic_json(directory/'old-result.json',dict(status='migrated',binding=binding,at=time.time()))
                return None
            time.sleep(1)
        raise InterruptedError('migration owner allocation stopped')


def receive(manifest):
    directory=Path(manifest['migration_directory'])
    try:return _receive(manifest)
    except BaseException as exc:
        claim=directory/'receiver-claim'
        owner=claim/'owner.json'
        mine=not claim.exists()
        if owner.exists():
            receipt=json.loads(owner.read_bytes())
            mine=(receipt['job']==os.environ.get('SLURM_JOB_ID') and receipt['node']==os.environ.get('SLURMD_NODENAME') and receipt['pid']==os.getpid())
        failure=directory/('receiver-failed.json' if mine else 'receiver-refused-'+str(os.getpid())+'.json')
        atomic_json(failure,dict(job=os.environ.get('SLURM_JOB_ID'),
            error=type(exc).__name__+': '+str(exc),admission_closed=True,at=time.time()))
        raise


def _receive(manifest):
    require_compute();verified_source(manifest)
    directory=Path(manifest['migration_directory'])
    sealed=json.loads((directory/'sealed.json').read_bytes());control=sealed['control']
    if sealed['binding'].get('control_sha256')!=digest(control):
        raise DeploymentRefused('sealed migration control checksum differs')
    submitted=wait_json(directory/'submitted.json',lambda r:all(r.get(k)==v for k,v in sealed['binding'].items()),180)
    if (submitted['new_job']!=str(os.environ['SLURM_JOB_ID'])
            or submitted['old_node']==os.environ['SLURMD_NODENAME']):
        raise DeploymentRefused('migration allocation/node ownership differs')
    root=Path(control['root'])
    if root.parent!=Path('/tmp'):
        raise DeploymentRefused('unsafe receiver root')
    if file_sha(directory/'state.tar')!=submitted['archive_sha256']:
        raise DeploymentRefused('migration archive checksum differs')
    claim_receiver(directory,submitted)
    root.mkdir(mode=0o700,exist_ok=False)
    extract_root(directory/'state.tar',root)
    preflight(dict(manifest,local_root=str(root)))
    record=load_seal(control)
    # Paths compare resolved: a release named through '..' or a symlinked
    # parent is the same verified source the seal recorded (resolved).
    if (Path(record['source']).resolve()!=Path(manifest['release_root']).resolve(strict=True)
            or record['release_sha256']!=manifest['release_sha256']
            or record['cohort']!=submitted['cohort'] or control['handoff']!=submitted['handoff']
            or control['manifest']['cohort_id']!=record['cohort']
            or control['manifest']['durable_root']!=record['durable']
            or control['manifest']['local_root']!=record['root']):
        raise DeploymentRefused('migration source differs from seal')
    scientific=Path(record['release'])/'scientific-manifest.json.gz'
    if scientific.exists():
        from .scientific_bundle import pin,context
        pin(root,scientific)
        os.environ['V3_SCIENTIFIC_RELEASE']=str(context(root,scientific))
    if manifest.get('action')=='allocation-migration' and manifest.get('mode')=='production':
        os.environ['V3_PRODUCTION_PROVIDER_FAULTS_DISABLED']='1'
    if isinstance(control['manifest'].get('policy_overrides'),dict):
        os.environ['V3_POLICY_OVERRIDES']=json.dumps(control['manifest']['policy_overrides'])
    else:os.environ.pop('V3_POLICY_OVERRIDES',None)
    from .pilot_job import controller_lifecycle,credential_loader
    from .cohort_runtime import CohortRuntime
    def initialize():
        clock=RunClock(**control['clock'])
        transport=dict(load_key=credential_loader(manifest['credential_file']),
            credential_id=manifest['profile']+'-'+manifest['cohort_id'],prices_verified=True,metadata_lookup_free=True)
        c=CohortRuntime.restore(root,handoff_sha256=control['handoff'],fake_providers=None,
            clock=clock,live_transport=transport)
        try:
            # Restore clears only its handoff barrier; operator-paused is intact.
            with c.store.connect() as db:
                paused=db.execute("SELECT value FROM settings WHERE key='operator-paused'").fetchone()
            if paused is None or paused[0]!='true':
                raise DeploymentRefused('receiver lost operator admission barrier')
            c.store.assert_conservation()
            atomic_json(directory/'restored.json',submitted)
            wait_json(directory/'retired.json',lambda r:r==submitted,180)
            rebind_request(directory,control,submitted)
            with c.store.transaction() as db:
                db.execute("DELETE FROM settings WHERE key='operator-paused'")
                c.store.event(db,'operator-allocation-migrated',submitted)
            clock.resume();c.state['operator_clock']=clock.snapshot();c.checkpoint()
            pause=PauseControl(c,control['manifest'],clock=clock)
            pause.ack=control['ack'];pause.publish()
            pause.log('allocation-migrated',**submitted)
            atomic_json(directory/'running.json',dict(binding=submitted,status=pause.publish()))
            return c,pause
        except BaseException:
            c.store.block('migration-initialization-failed');c.cancelled=True;c.shutdown();raise
    with ownership(root):
        return controller_lifecycle(control['manifest'],root,initialize)
