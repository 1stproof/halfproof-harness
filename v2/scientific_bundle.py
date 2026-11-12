"""Complete content pin for executable CAS, Python, TeX and system dependencies.

Inventory generation and verified private copying run only on Slurm compute.
No secret directories, literature cache, problem or campaign data are inspected.
The release carries the expected inventory; workers mount only copied bytes.
"""
import contextlib
import functools
import gzip
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import tarfile

from .deployment import require_compute, local_root

MAX_FILES=1000000
MAX_BYTES=80*1024**3
# Cold copies are ~720k mostly small files read from NFS; per-file latency, not
# bandwidth, bound the serial copy (16.5 min, job 1794530). Scan, copy and hash in
# spawned worker processes. Threads were measured slower than the serial copy: a
# CPU-bound tar thread convoys every I/O thread on the GIL.
COPY_WORKERS=24
# Persistent, user-private archive of a verified bundle, one file per manifest
# hash (deployment/build-dependency-archive.py). Node /tmp is wiped between jobs;
# one sequential NFS read replaces ~720k small-file reads whose latency grows
# with server load (21 min serial, job 1794833).
ARCHIVE_ROOT=Path(os.path.expanduser(os.environ.get('HARNESS_DATA','~/oss-harness-data')))/'dependency-archive'


def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':')).encode()


def _copy_host_tool(source, target):
    """Refresh a private tool atomically even when its prior copy is read-only."""
    fd, name = tempfile.mkstemp(prefix='.host-tool-', dir=target.parent)
    os.close(fd)
    stage = Path(name)
    try:
        shutil.copyfile(source, stage)
        stage.chmod(0o555)
        os.replace(stage, target)
    finally:
        stage.unlink(missing_ok=True)


def components(root):
    from .research import CAS,PYTHON
    from .tex import TREE,LIBS
    from .cas_overlay import sage_overlay,python_overlay,native_overlay
    tools=root/'host-tools';tools.mkdir(exist_ok=True)
    # The approved inventory binds the operator's user-local build. Ambient
    # PATH differs between development shells and isolated Slurm bootstraps;
    # selecting a node's /usr/bin build would change these executable bytes.
    # Copy the declared source and retain the exact manifest hash check below.
    executable=Path.home()/'.local/bin/bwrap'
    _copy_host_tool(executable, tools/'bwrap')
    rows={'host-tools':(tools,'/harness-tools'),'cas':(CAS,str(CAS)), 'python':(PYTHON,str(PYTHON)),
          'tex':(TREE,str(TREE)), 'system':(Path('/usr'),'/usr')}
    for index,path in enumerate(LIBS):
        if path.is_relative_to(TREE):continue
        rows['tex-library-'+str(index)]=(path,str(path))
    for name in ('fonts','alternatives'):
        path=Path('/etc')/name
        if path.is_dir():rows['system-'+name]=(path,str(path))
    if not (CAS/'lib/python3.12/site-packages/sage/all.py').exists():
        rows['sage-overlay']=(sage_overlay(root),'/research/sage')
        rows['python-overlay']=(python_overlay(root,CAS),str(CAS/'lib/python3.12/site-packages'))
        rows['native-overlay']=(native_overlay(root,CAS),'/research/native')
    return rows


def entries(root):
    """Enumerate directories too; a missing empty mountpoint changes the pin."""
    pending=[Path(root)]
    while pending:
        directory=pending.pop()
        with os.scandir(directory) as scan:
            children=sorted(scan,key=lambda e:e.name)
        for entry in children:
            found=_row(root,entry)
            if found is None:continue
            yield found
            if 'directory' in found[1]:pending.append(Path(entry.path))


def _row(root, entry):
    path=Path(entry.path);name=str(path.relative_to(root))
    # Privileged host authentication/network/time-daemon tools are not
    # harness dependencies. Omit them from the private mounted copy.
    if Path(root)==Path('/usr') and name in ('share/chrony','libexec/sssd','sbin/lldpcli'):return None
    info=entry.stat(follow_symlinks=False)
    if stat.S_ISLNK(info.st_mode):return name,{'link':os.readlink(path)}
    if stat.S_ISDIR(info.st_mode):return name,{'directory':True}
    if stat.S_ISREG(info.st_mode):return name,{'bytes':info.st_size,'executable':bool(info.st_mode & 0o111)}
    raise ValueError('special file in scientific dependency: '+str(path))


def file_hash(path):
    before=path.stat(follow_symlinks=False)
    digest=hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):digest.update(chunk)
    after=path.stat(follow_symlinks=False)
    if (before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns):
        raise ValueError('dependency changed while hashing: '+str(path))
    return digest.hexdigest()


def inventory(root):
    require_compute()
    result={};count=total=0
    for label,(source,target) in components(root).items():
        files={}
        for name,row in entries(source):
            count+=1;total+=row.get('bytes',0)
            if count>MAX_FILES or total>MAX_BYTES:raise ValueError('scientific dependency inventory limit')
            if 'bytes' in row:row['sha256']=file_hash(source/name)
            files[name]=row
        result[label]={'target':target,'files':files}
    value=dict(schema=1,components=result,files=count,bytes=total)
    return dict(value,sha256=hashlib.sha256(canonical(value)).hexdigest())


def verify_manifest(value):
    raw={k:v for k,v in value.items() if k!='sha256'}
    if value.get('schema')!=1 or hashlib.sha256(canonical(raw)).hexdigest()!=value['sha256']:
        raise ValueError('scientific dependency manifest checksum mismatch')
    for label,component in value['components'].items():
        if not label or '/' in label or label in ('.','..'):raise ValueError('unsafe component label')
        for name,row in component['files'].items():
            path=Path(name)
            if path.is_absolute() or '..' in path.parts:raise ValueError('unsafe dependency member')
    return value


def pin(root, manifest):
    require_compute();root=local_root(root)
    # One immutable dependency copy per node and user, keyed by manifest hash and
    # re-verified byte-for-byte before reuse. Copying it fresh per allocation took
    # 7 of the 10 minutes before the first model call (live swarm check 9).
    job=os.environ['SLURM_JOB_ID']
    if not job.isdigit():raise ValueError('numeric allocation identity required')
    shared=Path('/tmp')/('v3-dependencies-'+str(os.getuid())+'-cache')
    from .startup_log import phase
    phase('dependency cache: acquiring node copy lock')
    shared.mkdir(mode=0o700,exist_ok=True)
    if shared.is_symlink() or shared.stat().st_uid!=os.getuid() or shared.stat().st_mode & 0o077:
        raise ValueError('private allocation dependency cache required')
    from .parallel_ops import exclusive
    with exclusive(shared/'copy.lock', wait_seconds=3600):
        phase('dependency cache: lock held')
        result=_pin(shared,manifest)
        pointer=root/'scientific'
        if pointer.is_symlink():
            if pointer.resolve()!=shared/'scientific':raise ValueError('dependency cache pointer changed')
        elif pointer.exists():raise ValueError('unexpected dependency cache directory')
        else:pointer.symlink_to(shared/'scientific',target_is_directory=True)
        return result


@functools.lru_cache(maxsize=8)
def _verified(root, sha):
    value=verify_manifest(json.loads((Path(root)/'manifest.json').read_bytes()))
    if value['sha256']!=sha:raise ValueError('private scientific manifest differs')
    verify_copy(Path(root),value)
    return True


def _pin(root, manifest):
    value=verify_manifest(read_manifest(manifest))
    target=root/'scientific'/value['sha256']
    if target.exists():
        if target.is_symlink():raise ValueError('scientific bundle symlink')
        stamp=root/('verified-'+value['sha256'])
        if stamp.exists():
            # Byte-verified once in this private (0700) per-node cache; later reuse
            # checks the inventory (names, types, sizes, links) without rehashing,
            # which took minutes per launch (live swarm check 11).
            from .startup_log import phase
            phase('dependency cache: warm, inventory check')
            check_inventory(target,value)
            _mark_checked(root,value['sha256'])
            phase('dependency cache: inventory check done')
            return value['sha256']
        _verified(str(target.resolve()),value['sha256'])
        stamp.write_text(value['sha256'])
        _mark_checked(root,value['sha256'])
        return value['sha256']
    target.parent.mkdir(exist_ok=True)
    # The caller holds the per-node copy lock: any staging directory left here
    # is from a failed or killed attempt. Remove it before writing another ~28 GB.
    for stale in target.parent.glob('.scientific-*'):_remove(stale)
    stage=Path(tempfile.mkdtemp(prefix='.scientific-',dir=target.parent))
    from .startup_log import phase
    phase('dependency cache: cold')
    if not _from_archive(root,stage,value):
        phase('dependency cache: copying from sources')
        _remove(stage)
        stage=Path(tempfile.mkdtemp(prefix='.scientific-',dir=target.parent))
        _from_sources(root,stage,value)
    (stage/'manifest.json').write_bytes(canonical(value))
    (stage/'manifest.json').chmod(0o444);stage.chmod(0o555)
    os.rename(stage,target)
    phase('dependency cache: placed and verified')
    # Every copied byte was hashed above, so the fresh copy is verified; stamp it so
    # the next job on this node takes the quick inventory check (live 06:57: a missing
    # stamp cost a second job a ~20 min full rehash).
    (root/('verified-'+value['sha256'])).write_text(value['sha256'])
    _mark_checked(root,value['sha256'])
    return value['sha256']


def _component_targets(root):
    """Worker: the live host's component labels and mount targets."""
    return {label:(str(source),target) for label,(source,target) in components(Path(root)).items()}


def _check_components(actual, value):
    if set(actual)!=set(value['components']):raise ValueError('scientific component set changed')
    for label,component in value['components'].items():
        if actual[label][1]!=component['target']:raise ValueError('scientific mount target changed')


def _from_sources(root, stage, value):
    """Copy every member from its live source, verifying against the manifest."""
    actual=components(root)
    _check_components(actual,value)
    # Directories and links come from the manifest; every source directory is then
    # listed, compared and its files copied and hashed by parallel workers, so NFS
    # metadata latency overlaps instead of adding up (the serial scan of one
    # component alone took over 7 minutes on a loaded node).
    from concurrent.futures import as_completed
    with _pool(COPY_WORKERS) as pool:
        futures=[];artifacts=[]
        for label,component in value['components'].items():
            source=actual[label][0]
            if 'artifact' in component:
                artifacts.append(pool.submit(_layout,(str(source),component,str(stage/label),label)))
                continue
            futures+=[pool.submit(_sync,job) for job in _sync_jobs(source,component['files'],stage/label,label)]
        for done in as_completed(artifacts):
            jobs=done.result()
            futures+=[pool.submit(_place,jobs[i:i+1000]) for i in range(0,len(jobs),1000)]
        for done in as_completed(futures):done.result()
    for label,component in value['components'].items():
        copy=stage/label
        if 'artifact' in component:continue
        for directory in sorted((p for p in copy.rglob('*') if p.is_dir() and not p.is_symlink()),key=lambda p:len(p.parts),reverse=True):directory.chmod(0o555)
        copy.chmod(0o555)


def _from_archive(root, stage, value):
    """Extract the persistent archive for this manifest into stage. Members must
    be exactly the manifest's (names, types, links, sizes) and every regular file
    is hashed against the manifest, as in a copy from sources; on any mismatch
    this returns False and the caller copies from sources instead."""
    import sys
    path=ARCHIVE_ROOT/(value['sha256']+'.tar')
    if not path.exists() and not path.is_symlink():return False
    try:
        if path.is_symlink() or not path.is_file():raise ValueError('archive is not a regular file')
        # The live host must still match the manifest's components and targets,
        # exactly as a copy from sources requires; checked alongside extraction.
        from .startup_log import phase
        phase('dependency archive: extracting %.1f GB'%(path.stat().st_size/1e9))
        with _pool(1) as pool:
            hosts=pool.submit(_component_targets,str(root))
            _extract_archive(path,stage,value)
            actual=hosts.result()
        _check_components(actual,value)
        return True
    except Exception as exc:
        from .deployment import DeploymentRefused
        if isinstance(exc,DeploymentRefused):raise  # the setup deadline, not an archive fault
        print('ALERT: dependency archive unusable, copying from sources: %s: %s'%(type(exc).__name__,str(exc)[:500]),file=sys.stderr,flush=True)
        return False


def _remove(path):
    """Delete a private staging tree whose directories may be sealed read-only."""
    path=Path(path)
    if path.is_symlink() or not path.is_dir():return
    for directory,_,_ in os.walk(path):os.chmod(directory,0o700)
    shutil.rmtree(path)


def _extract_archive(path, stage, value):
    from concurrent.futures import as_completed
    expected={}
    for label,component in value['components'].items():
        expected[label]={'directory':True}
        for name,row in component['files'].items():expected[label+'/'+name]=row
    import time
    from .startup_log import phase
    seen=set();pending=[];futures=[];written=0;report=2e9;began=time.monotonic()
    with _pool(COPY_WORKERS) as pool,path.open('rb') as raw,tarfile.open(fileobj=raw,mode='r|') as archive:
        for member in archive:
            name=member.name.removeprefix('./')
            if name in seen or name not in expected:raise ValueError('unbound dependency archive member: '+name)
            seen.add(name);row=expected[name];target=stage/name
            for parent in target.parents:
                if parent==stage:break
                if parent.is_symlink() or not parent.is_dir():raise ValueError('unsafe archive parent')
            if 'directory' in row and member.isdir():target.mkdir()
            elif 'link' in row and member.issym() and member.linkname==row['link']:target.symlink_to(row['link'])
            elif 'bytes' in row and member.isfile() and member.size==row['bytes']:
                with archive.extractfile(member) as incoming,target.open('xb') as output:shutil.copyfileobj(incoming,output,1024*1024)
                written+=member.size
                if written>=report:
                    phase('dependency archive: %.1f GB written, %.0f MB/s'%(written/1e9,written/1e6/max(.001,time.monotonic()-began)));report+=2e9
                pending.append((None,str(target),row['sha256'],row['executable'],name))
                if len(pending)>=1000:futures.append(pool.submit(_place,pending));pending=[]
            else:raise ValueError('dependency archive member type changed: '+name)
        if pending:futures.append(pool.submit(_place,pending))
        phase('dependency archive: extracted, waiting for hash checks')
        for done in as_completed(futures):done.result()
        phase('dependency archive: hash checks done')
    if seen!=set(expected):raise ValueError('dependency archive incomplete')
    for directory in sorted((name for name,row in expected.items() if 'directory' in row),key=lambda n:len(Path(n).parts),reverse=True):
        (stage/directory).chmod(0o555)


@contextlib.contextmanager
def _pool(workers):
    """Spawned worker pool. On any error, including the setup alarm, queued work
    is cancelled and workers are terminated rather than drained, so the caller's
    deadline still bounds start-up."""
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor
    pool=ProcessPoolExecutor(max_workers=max(1,workers),mp_context=multiprocessing.get_context('spawn'))
    try:
        yield pool
    except BaseException:
        processes=list((pool._processes or {}).values())
        pool.shutdown(wait=False,cancel_futures=True)
        for process in processes:
            if process.is_alive():process.terminate()
        for process in processes:process.join(10)
        raise
    pool.shutdown(wait=True)


def _layout(job):
    """Worker: extract the system artifact; its files still need hash verification."""
    source,component,copy,label=job
    return copy_artifact(component,Path(copy),defer=True)


def _sync_jobs(source, expected, copy, label, files_per_job=400):
    """Create directories and links from the manifest; group source directories
    into worker jobs of bounded file count."""
    children={'.':{}}
    for name,row in expected.items():
        children.setdefault(str(Path(name).parent),{})[name]=row
        if 'directory' in row:children.setdefault(name,{})
    copy.mkdir()
    for name,row in sorted(expected.items(),key=lambda pair:(len(Path(pair[0]).parts),pair[0])):
        if 'directory' in row:(copy/name).mkdir()
        elif 'link' in row:(copy/name).symlink_to(row['link'])
    jobs=[];batch={};count=0
    for directory,rows in sorted(children.items()):
        batch[directory]=rows;count+=1+sum(1 for row in rows.values() if 'bytes' in row)
        if count>=files_per_job:jobs.append((str(source),str(copy),label,batch));batch={};count=0
    if batch:jobs.append((str(source),str(copy),label,batch))
    return jobs


def _sync(job):
    """Worker: list each source directory, require exactly the manifest's members
    with the same type/size/link, then copy, hash and seal its regular files."""
    source,copy,label,batch=job
    source,copy=Path(source),Path(copy)
    for directory,rows in batch.items():
        with os.scandir(source/directory) as scan:
            found=dict(row for row in (_row(source,entry) for entry in scan) if row is not None)
        if set(found)!=set(rows):raise ValueError('scientific file inventory changed: '+label)
        for name,row in rows.items():
            if {k:v for k,v in row.items() if k!='sha256'}!=found[name]:
                raise ValueError('scientific file type/size/link changed: '+label+'/'+name)
            if 'bytes' in row:
                path=copy/name
                shutil.copyfile(source/name,path)
                if file_hash(path)!=row['sha256']:raise ValueError('scientific bytes changed: '+label+'/'+name)
                path.chmod(0o555 if row['executable'] else 0o444)


def _place(chunk):
    """Worker: copy (unless already extracted), hash against the manifest, seal."""
    for origin,path,sha,executable,name in chunk:
        path=Path(path)
        if origin is not None:shutil.copyfile(origin,path)
        if file_hash(path)!=sha:raise ValueError('scientific bytes changed: '+name)
        path.chmod(0o555 if executable else 0o444)


def _check_component(job):
    target,files=job
    found=dict(entries(Path(target)))
    if set(found)!=set(files) or any(found[n]!={k:v for k,v in r.items() if k!='sha256'} for n,r in files.items()):
        raise ValueError('private scientific inventory changed')


def check_inventory(target, value):
    """Names, types, sizes and links of every member; one worker per component."""
    import multiprocessing
    labels=list(value['components'])
    jobs=[(str(Path(target)/label),value['components'][label]['files']) for label in labels]
    if multiprocessing.current_process().daemon:
        # A daemonic child (the publisher) cannot start workers; scan serially.
        for job in jobs:_check_component(job)
        return
    with _pool(min(len(labels),COPY_WORKERS)) as pool:
        for _ in pool.map(_check_component,jobs):pass


def _checked_marker(root, sha):
    # Allocation identity: job id plus its start time, so a reused job id never
    # matches an older allocation's marker.
    job=os.environ.get('SLURM_JOB_ID','');start=os.environ.get('SLURM_JOB_START_TIME','')
    if not job.isdigit() or not start.isdigit():return None
    return Path(root)/('inventory-'+sha+'-'+job+'-'+start)


def _mark_checked(root, sha):
    # The controller checked this cache's inventory in this allocation; spawned
    # children of the same job (publisher, compilers) reuse that check instead of
    # rescanning ~720k entries (72 s in the publisher, job 1794811).
    marker=_checked_marker(root,sha)
    if marker is None:return
    marker.write_text(sha)


def copy_artifact(component, destination, defer=False):
    """Copy only manifest members; never extract links or paths through tar APIs."""
    artifact=component['artifact'];path=Path(artifact['path'])
    if path.is_symlink() or path.stat().st_size!=artifact['bytes'] or file_hash(path)!=artifact['sha256']:
        raise ValueError('scientific system artifact changed')
    destination.mkdir();seen=set();expected=component['files'];pending=[]
    with tarfile.open(path,'r:') as archive:
        for member in archive:
            name=member.name.removeprefix('./')
            if name=='.' and member.isdir():continue
            if name not in expected or name in seen:raise ValueError('unbound system artifact member')
            seen.add(name);row=expected[name];target=destination/name
            # Every parent must already be a real directory, never a symlink.
            for parent in target.parents:
                if parent==destination:break
                if parent.is_symlink() or not parent.is_dir():raise ValueError('unsafe artifact parent')
            if 'directory' in row and member.isdir():target.mkdir()
            elif 'link' in row and member.issym() and member.linkname==row['link']:target.symlink_to(row['link'])
            elif 'bytes' in row and member.isfile() and member.size==row['bytes']:
                with archive.extractfile(member) as incoming,target.open('xb') as output:shutil.copyfileobj(incoming,output)
                if defer:pending.append((None,str(target),row['sha256'],row['executable'],'system/'+name));continue
                if file_hash(target)!=row['sha256']:raise ValueError('system artifact bytes changed')
                target.chmod(0o555 if row['executable'] else 0o444)
            else:raise ValueError('system artifact type changed')
    if seen!=set(expected):raise ValueError('system artifact inventory incomplete')
    for name,row in sorted(expected.items(),key=lambda p:len(Path(p[0]).parts),reverse=True):
        if 'directory' in row:(destination/name).chmod(0o555)
    destination.chmod(0o555)
    return pending


def verify_copy(root,value):
    for label,component in value['components'].items():
        found=dict(entries(root/label))
        if set(found)!=set(component['files']):raise ValueError('private scientific inventory changed')
        for name,row in component['files'].items():
            # Private regular-file permissions are intentionally made read-only.
            if found[name]!={k:v for k,v in row.items() if k!='sha256'}:
                raise ValueError('private scientific member changed')
            if 'sha256' in row and file_hash(root/label/name)!=row['sha256']:
                raise ValueError('private scientific checksum mismatch')


@functools.lru_cache(maxsize=8)
def bundle(release):
    release=Path(release)
    path=release/'scientific-manifest.json.gz'
    if not path.is_file():return None
    value=verify_manifest(read_manifest(path))
    root=release.parent.parent/'scientific'/value['sha256']
    if root.is_symlink() or not root.is_dir():raise ValueError('private scientific bundle missing')
    real=root.resolve()
    # Live 14:59: the spawned publisher re-hashed the whole ~33 GB bundle and missed its
    # 600 s readiness bound. A byte-verified per-node copy carries a stamp; check its
    # inventory (names, types, sizes, links) as _pin does instead of rehashing.
    stamp=real.parent.parent/('verified-'+value['sha256'])
    if stamp.is_file() and stamp.read_text().strip()==value['sha256']:
        marker=_checked_marker(real.parent.parent,value['sha256'])
        if marker is None or not marker.is_file() or marker.read_text().strip()!=value['sha256']:
            check_inventory(real,value)
            _mark_checked(real.parent.parent,value['sha256'])
        return root,value
    _verified(str(real),value['sha256'])
    return root,value


def source(release,label,default):
    pinned=bundle(str(release))
    return pinned[0]/label if pinned else Path(default)


def mounts(release, *, research=False):
    pinned=bundle(str(release))
    if not pinned:return []
    root,value=pinned
    result=[]
    # Base trees first, then overlays of subdirectories.
    for label,component in sorted(value['components'].items(),key=lambda pair:len(Path(pair[1]['target']).parts)):
        if not research and not label.startswith(('tex','system')):continue
        result+=['--ro-bind',str(root/label),component['target']]
    if research and 'native-overlay' in value['components']:
        from .research import CAS
        result+=['--ro-bind',str(root/'cas'),'/research/original-cas']
        for relative,destination in (('lib','/research/cas-libs'),('bin',str(CAS/'bin')),
            ('share/gap',str(CAS/'share/gap')),('share/singular',str(CAS/'share/singular')),
            ('lib/singular',str(CAS/'lib/singular'))):
            result+=['--ro-bind',str(root/'native-overlay'/relative),destination]
    return result


def executable(release, name, fallback):
    pinned=bundle(str(release)) if release else None
    return str(pinned[0]/'host-tools'/name) if pinned else str(fallback)


def read_manifest(path):
    path=Path(path)
    raw=path.read_bytes()
    if path.suffix=='.gz':raw=gzip.decompress(raw)
    if len(raw)>256*1024*1024:raise ValueError('scientific manifest exceeds256MiB')
    return json.loads(raw)


def context(root, manifest):
    """Stable allocation-scoped compiler pin, independent of test scratch lifetime."""
    value=verify_manifest(read_manifest(manifest))
    shared=(Path(root)/'scientific').resolve().parent
    target=shared/'contexts'/value['sha256']
    target.mkdir(parents=True,exist_ok=True)
    path=target/'scientific-manifest.json.gz'
    raw=Path(manifest).read_bytes()
    if path.exists():
        if path.is_symlink() or path.read_bytes()!=raw:raise ValueError('compiler context changed')
    else:path.write_bytes(raw);path.chmod(0o444)
    target.chmod(0o555)
    return target
