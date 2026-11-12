"""Local nonblocking publication queue; only its child touches durable storage.

Admission must call require_ack(reservation_id) before upstream send. A timeout
latches failure; it never spawns another publisher against the same mount.
"""
import hashlib
import base64
import copy
import functools
import threading
import fcntl
import json
import multiprocessing as mp
import os
from pathlib import Path
import queue
import time
from .control import atomic_json


def _publish(local, durable, inbox, acknowledgments):
    # Spawned publishers do not inherit the parent's verified dependency cache.
    # Complete local executable verification before any publication can queue.
    try:
        release=os.environ.get('V3_SCIENTIFIC_RELEASE')
        if release:
            from .scientific_bundle import bundle
            if bundle(release) is None:raise ValueError('publisher scientific pin missing')
        acknowledgments.put(('ready',None,None,None))
    except Exception as exc:
        acknowledgments.put(('readiness-error',None,None,type(exc).__name__+': '+str(exc)[:1000]))
        return
    def write_once(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if json.loads(path.read_bytes()) != value:
                raise ValueError('publication identity reused')
        else:
            atomic_json(path, value)

    while True:
        item = inbox.get()
        if item is None:
            return
        kind, name, digest = item
        if kind == 'restore-journal':
            try:
                from .journal_restore import verify
                result=verify(local,durable,digest)
                acknowledgments.put(('journal-ack',name,None,result))
            except Exception as exc:
                acknowledgments.put(('journal-error',name,None,type(exc).__name__+': '+str(exc)[:1000]))
            continue
        try:
            payload = (Path(local) / name).read_bytes()
            if hashlib.sha256(payload).hexdigest() != digest:
                raise ValueError('local publication changed')
            value = json.loads(payload)
            remote = Path(durable)/name
            if kind == 'replay-publication':
                # Only the fenced publisher child reads the authoritative mount.
                # A prior immutable write is reusable only with identical bytes;
                # Python value equality would admit schema changes (True == 1).
                if remote.is_symlink():raise ValueError('unsafe replay publication')
                if remote.exists():
                    if remote.read_bytes() != payload:
                        raise ValueError('durable replay publication changed')
                else:
                    write_once(remote, value)
            else:
                write_once(remote, value)
            result = None
            if kind == 'operation':
                result_path = Path(durable)/(name+'.result')
                if result_path.exists():
                    saved = json.loads(result_path.read_bytes())
                    if saved['request_sha256'] != digest:
                        raise ValueError('operation identity reused')
                    result = saved['result']
                else:
                    from .runtime_closure import perform
                    from .closure_build import ReportCompiler
                    original_compile = ReportCompiler.compile
                    def measured_compile(compiler, *args, **kwargs):
                        # Dependency verification/staging remains charged to the
                        # storage allowance. Only actual owned engine execution
                        # is the separately bounded120s compiler interval.
                        compiler.invocation_observer=lambda phase: acknowledgments.put(
                            ('compile-'+phase,name,digest,time.monotonic()))
                        try:
                            return original_compile(compiler, *args, **kwargs)
                        finally:
                            compiler.invocation_observer=None
                    ReportCompiler.compile = measured_compile
                    try:
                        arguments = dict(value['payload'])
                        if value.get('source_snapshot'):
                            arguments['source'] = value['source_snapshot']
                        result = perform(value['operation'], arguments)
                    finally:
                        ReportCompiler.compile = original_compile
                    write_once(result_path, {'request_sha256': digest, 'result': result})
            if kind in ('publication','replay-publication') and isinstance(value,dict) and value.get('schema')=='p7-chunked-publication-v1':
                # Replayed roots must not acknowledge a missing/corrupted durable
                # chunk even when the root JSON itself was already present.
                read_publication(Path(durable)/name)
            acknowledgments.put(('ack', name, digest, result))
        except Exception as exc:
            acknowledgments.put(('error', name, digest, type(exc).__name__+': '+str(exc)[:1000]))


def _atomic_bytes(path, data):
    import tempfile
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix='.state-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(data); out.flush(); os.fsync(out.fileno())
        os.replace(temporary, path)
        dfd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:os.fsync(dfd)
        finally:os.close(dfd)
    except BaseException:
        try:os.unlink(temporary)
        except OSError:pass
        raise


# Live 02:08 (dsf write-up): a 185 MB freeze snapshot needed more than the 60 s ack bound and
# failed the job at freeze. Freeze publications get their own allowance.
FREEZE_ACK_SECONDS=1800
# Oldest queued journal entry's age before the run stops. Live 2026-10-13: two
# closings died at 60 s with no write error (P02 13:42, P01 14:47, both rye-02).
# Measured: 46 KB writes 12.5-27 s apart for 90 s on /scratch; 2.8 MB chunk
# writes 1.1-3.2 s on rye-02 (0.03 s on wheat-01), so one writer admission's
# ~30 MB ledger plus queued snapshots drains in ~90 s. Admission still sends
# nothing upstream before its ack; write errors and a dead publisher stay fatal.
ACK_SECONDS=600


PUBLICATION_LIMIT=8*1024*1024
SNAPSHOT_LIMIT=2*1024*1024*1024  # above HISTORY_PRESSURE_BYTES (256 MiB), 10x lift
CHUNK_BYTES=2*1024*1024


def read_publication(path):
    """Reconstruct an acknowledged snapshot without dropping exact JSON values.

    Only same-directory hash-named chunks are admitted. Legacy small records keep
    their original representation. Root acknowledgment follows every chunk write.
    """
    path=Path(path)
    if path.is_symlink() or path.stat().st_size>PUBLICATION_LIMIT:
        raise ValueError('unsafe publication root')
    value=json.loads(path.read_bytes())
    if not isinstance(value,dict) or value.get('schema')!='p7-chunked-publication-v1':return value
    size=value.get('bytes');chunks=value.get('chunks')
    if type(size) is not int or not PUBLICATION_LIMIT<size<=SNAPSHOT_LIMIT or not isinstance(chunks,list) or not 1<=len(chunks)<=SNAPSHOT_LIMIT//CHUNK_BYTES:
        raise ValueError('invalid chunked publication bound')
    result=bytearray()
    for sha in chunks:
        if not isinstance(sha,str) or len(sha)!=64 or any(c not in '0123456789abcdef' for c in sha):
            raise ValueError('unsafe publication chunk identity')
        member=path.parent/('chunk-'+sha+'.json')
        if member.is_symlink() or member.stat().st_size>PUBLICATION_LIMIT:raise ValueError('unsafe publication chunk')
        row=json.loads(member.read_bytes())
        if row.get('schema')!='p7-publication-chunk-v1' or row.get('sha256')!=sha:raise ValueError('publication chunk identity changed')
        data=base64.b64decode(row['data'],validate=True)
        if len(data)>CHUNK_BYTES or len(data)!=row.get('bytes') or hashlib.sha256(data).hexdigest()!=sha:
            raise ValueError('publication chunk bytes changed')
        result.extend(data)
        if len(result)>size:raise ValueError('publication total exceeded')
    if len(result)!=size or hashlib.sha256(result).hexdigest()!=value.get('sha256'):
        raise ValueError('publication reconstruction mismatch')
    return json.loads(result)


class DurabilityUnavailable(RuntimeError):
    pass


def synchronized(method):
    @functools.wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapped


class Publisher:
    def __init__(self, local, durable, *, timeout=ACK_SECONDS, capacity=256, clock=time.monotonic, readiness_timeout=2700):
        self._lock = threading.RLock()
        if not 0 < timeout <= ACK_SECONDS:
            raise ValueError('durability timeout must be at most %d seconds' % ACK_SECONDS)
        self.local = Path(local)
        self.local.mkdir(parents=True, exist_ok=True, mode=0o700)
        from .deployment import local_root
        local_root(self.local)
        self.lock_fd = os.open(self.local/'publisher.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(self.lock_fd)
            raise
        self.clock, self.timeout = clock, timeout
        self.pending, self.acked = {}, {}
        self.operations, self.operation_results, self.compiling = {}, {}, {}
        self.failure = None
        ctx = mp.get_context('spawn')
        self.inbox, self.outbox = ctx.Queue(capacity), ctx.Queue(capacity)
        from .process import SpawnProcess
        self.process = SpawnProcess(target=_publish, args=(str(local), str(durable), self.inbox, self.outbox), daemon=True)
        self.process.start()
        # This constructor runs before initial admission or behind a complete
        # controller handoff fence. No pending journal operation gets extra time.
        try:
            if not 0 < readiness_timeout <= 3600:raise ValueError('publisher readiness bound')  # live 14:59: 600 s was too short for a cold node
            until=time.monotonic()+readiness_timeout
            while True:
                remaining=until-time.monotonic()
                if remaining<=0:raise DurabilityUnavailable('publisher dependency readiness timeout')
                try:kind,name,digest,value=self.outbox.get(timeout=min(.1,remaining))
                except queue.Empty:
                    if not self.process.is_alive():raise DurabilityUnavailable('publisher exited during readiness')
                    continue
                if kind!='ready' or name is not None or digest is not None:
                    raise DurabilityUnavailable('publisher dependency readiness failed: '+str(value))
                break
        except BaseException:
            self.close()
            raise

    @synchronized
    def submit(self, identity, value):
        if not identity or len(identity) > 120 or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in identity):
            raise ValueError('invalid publication identity')
        self.poll()
        name = identity + '.json'
        payload = json.dumps(value, sort_keys=True).encode()
        if len(payload)>SNAPSHOT_LIMIT:raise ValueError('publication exceeds 2 GiB state bound')
        if len(payload)>PUBLICATION_LIMIT:
            chunks=[]
            for offset in range(0,len(payload),CHUNK_BYTES):
                data=payload[offset:offset+CHUNK_BYTES];sha=hashlib.sha256(data).hexdigest()
                self.submit('chunk-'+sha,dict(schema='p7-publication-chunk-v1',sha256=sha,
                    bytes=len(data),data=base64.b64encode(data).decode('ascii')))
                chunks.append(sha)
            value=dict(schema='p7-chunked-publication-v1',sha256=hashlib.sha256(payload).hexdigest(),
                bytes=len(payload),chunks=chunks)
            payload=json.dumps(value,sort_keys=True).encode()
        digest = hashlib.sha256(payload).hexdigest()
        previous = self.pending.get(name) or self.acked.get(name)
        if previous:
            if previous[0] != digest:
                raise ValueError('publication identity reused')
            return identity
        path = self.local / name
        if path.exists() and path.read_bytes() != payload:
            raise ValueError('local publication identity reused')
        _atomic_bytes(path, payload)  # the exact hashed bytes (live 08:50: value changed between hash and write)
        self.pending[name] = (digest, self.clock())
        try:
            self.inbox.put_nowait(('publication', name, digest))
        except queue.Full:
            self.failure = 'publisher backlog full'
            raise DurabilityUnavailable(self.failure)
        return identity

    @synchronized
    def replay_publication(self, identity):
        """Reack immutable journal bytes without rewriting the local journal.

        No cached/local ack is trusted. The child verifies the durable file and
        every chunk before delivering the ordinary hash-bound acknowledgment.
        Missing durable files take the normal fsynced publication path.
        Operations deliberately retain submit_operation and its result protocol.
        """
        if (not identity or len(identity)>120 or identity.startswith('operation-')
                or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in identity)):
            raise ValueError('invalid replay publication identity')
        self.poll()
        name=identity+'.json';path=self.local/name
        if path.is_symlink():raise ValueError('unsafe local replay publication')
        raw=path.read_bytes()
        if len(raw)>PUBLICATION_LIMIT:raise ValueError('replay publication root too large')
        value=json.loads(raw)
        if json.dumps(value,sort_keys=True).encode()!=raw:
            raise ValueError('local replay publication bytes changed')
        digest=hashlib.sha256(raw).hexdigest()
        previous=self.pending.get(name) or self.acked.get(name)
        if previous:
            if previous[0]!=digest:raise ValueError('publication identity reused')
            return identity
        self.pending[name]=(digest,self.clock())
        try:self.inbox.put_nowait(('replay-publication',name,digest))
        except queue.Full:
            self.failure='publisher backlog full'
            raise DurabilityUnavailable(self.failure)
        return identity

    @synchronized
    def restore_journal(self, expected=None, *, seconds=1800):
        """One child request verifies every publication before admission reopens.

        The complete hash map becomes visible only after all files match. No
        operation or provider action is executed by this verification request.
        """
        if self.pending:raise ValueError('journal restore requires drained publisher')
        if not 0 < seconds <= 1800:raise ValueError('journal restore bound')
        self.inbox.put(('restore-journal','journal',expected or {}))
        end=time.monotonic()+seconds
        while True:
            if not self.process.is_alive():raise DurabilityUnavailable('publisher exited during journal restore')
            left=end-time.monotonic()
            if left<=0:
                self.failure='journal restore deadline exceeded'
                raise DurabilityUnavailable(self.failure)
            try:kind,name,digest,result=self.outbox.get(timeout=min(.1,left))
            except queue.Empty:continue
            if kind=='journal-error':
                self.failure='durable publication failed: '+result
                raise DurabilityUnavailable(self.failure)
            if kind!='journal-ack' or name!='journal':
                self.failure='unexpected journal acknowledgment'
                raise DurabilityUnavailable(self.failure)
            self.acked.update({n:(h,self.clock()) for n,h in result.items()})
            return len(result)

    @synchronized
    def submit_operation(self, identity, operation, payload):
        """P2/P6 closure dispatch in this same publisher; never another service."""
        from .cohort_publication import OPERATIONS
        if operation not in ('preflight', 'retain-draft', 'build', 'status', 'select-final', 'close', *OPERATIONS):
            raise ValueError('unknown closure operation')
        if not identity or len(identity)>120 or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in identity):
            raise ValueError('invalid operation identity')
        self.poll()
        name = 'operation-'+identity+'.json'
        if operation == 'cohort-freeze':
            self.__dict__.setdefault('allowance', {})[name] = FREEZE_ACK_SECONDS
        requested = {'operation': operation, 'payload': payload}
        # Caller repeats this exact operation after restart. Never resnapshot a
        # live source for an already journaled identity.
        path = self.local/name
        if path.exists():
            value = json.loads(path.read_bytes())
            if {key:value[key] for key in requested} != requested:
                raise ValueError('operation identity reused')
        else:
            from .deployment import local_root
            if operation in OPERATIONS:
                local_root(Path(payload['root']).parent)
                for rows in payload.get('sections', {}).values():
                    for row in rows:
                        # Reports alone come from the durable closure service.
                        if row.get('durable_report') is not True:local_root(row['root'])
            else:
                for candidate in (payload['scratch'], Path(payload['catalog']['root']).parent):
                    local_root(candidate)
            value = dict(requested)
            if operation == 'build':
                from .evidence_snapshot import copy_evidence
                local_root(payload['source'])
                snapshot = self.local/('source-'+identity)
                inventory = copy_evidence(payload['source'], snapshot)
                value['source_snapshot'] = str(snapshot)
                value['source_inventory'] = inventory
            atomic_json(path, value)
        raw = path.read_bytes()
        if len(raw)>8*1024*1024:
            raise ValueError('operation request too large')
        digest = hashlib.sha256(raw).hexdigest()
        previous = self.pending.get(name) or self.acked.get(name)
        if previous:
            if previous[0] != digest:
                raise ValueError('operation identity reused')
            return identity
        self.operations[identity] = name
        self.pending[name] = (digest, self.clock())
        try:
            self.inbox.put_nowait(('operation', name, digest))
        except queue.Full:
            self.failure = 'publisher backlog full'
            raise DurabilityUnavailable(self.failure)
        return identity

    @synchronized
    def operation_result(self, identity):
        self.poll()
        return copy.deepcopy(self.operation_results.get('operation-'+identity+'.json'))

    @synchronized
    def poll(self):
        while True:
            try:
                kind, name, digest, value = self.outbox.get_nowait()
            except queue.Empty:
                break
            if name not in self.pending or self.pending[name][0] != digest:
                self.failure = 'unexpected durability acknowledgment'
                continue
            if kind == 'error':
                self.failure = 'durable publication failed: ' + value
            elif kind == 'compile-start':
                if name in self.compiling:
                    self.failure = 'duplicate compiler start'
                self.compiling[name] = value
            elif kind == 'compile-end':
                started = self.compiling.pop(name, None)
                # The engine is killed at120s; namespace reap and result
                # serialization may take a further bounded10s. Those extra
                # seconds consume the ordinary durability allowance.
                if started is None or not 0 <= value-started <= 130:
                    self.failure = 'compiler exceeded bounded invocation'
                else:
                    end=started+min(120,value-started)
                    for waiting,(old_digest,queued) in list(self.pending.items()):
                        self.pending[waiting]=(old_digest,queued+max(0,end-max(queued,started)))
            elif kind == 'ack':
                self.acked[name] = self.pending.pop(name)
                if name.startswith('operation-'):
                    self.operation_results[name] = value
            else:
                self.failure = 'unknown publisher event'
        now = self.clock()
        for name, (_, started) in self.pending.items():
            elapsed=now-started
            for compile_start in self.compiling.values():
                if now-compile_start > 130:
                    self.failure = 'compiler acknowledgment timeout'
                # This one publisher also queues other journal entries while
                # compiling. Exclude only their overlap with bounded engine
                # work, retaining every second of storage/teardown delay.
                elapsed-=max(0,min(now,compile_start+120)-max(started,compile_start))
            if elapsed >= getattr(self, 'allowance', {}).get(name, self.timeout):
                self.failure = 'durability acknowledgment timeout'
        if not self.process.is_alive():
            self.failure = self.failure or 'publisher exited'
        if self.failure:
            raise DurabilityUnavailable(self.failure)

    @synchronized
    def require_ack(self, identity):
        self.poll()
        return identity + '.json' in self.acked

    @synchronized
    def close(self, seconds=2):
        """Bounded reaping; false means preserve backlog and report failed cleanup."""
        self.process.terminate()
        self.process.join(timeout=seconds)
        if self.process.is_alive():
            self.process.kill()
            self.process.join(timeout=seconds)
        self.inbox.cancel_join_thread()
        self.outbox.cancel_join_thread()
        reaped = not self.process.is_alive()
        if not reaped:
            # A child in uninterruptible NFS I/O survives SIGKILL. Detach it so
            # interpreter exit (multiprocessing's atexit join) cannot block on
            # it; the publisher lock stays held until this process exits.
            from multiprocessing import process as mp_process
            mp_process._children.discard(self.process)
        if reaped and self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None
        return reaped
