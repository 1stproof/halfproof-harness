"""Fail-closed deployment preflight, fixture mounts and single-owner controls."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import time
from .control import atomic_json


class DeploymentRefused(ValueError):
    pass


def single_task(env=None):
    env = os.environ if env is None else env
    for key in ('SLURM_NTASKS', 'SLURM_STEP_NUM_TASKS'):
        if key in env and env[key] != '1':
            raise DeploymentRefused('exactly one control task required')
    for key in ('SLURM_PROCID', 'SLURM_LOCALID'):
        if key in env and env[key] != '0':
            raise DeploymentRefused('only task zero may control')


def require_compute(env=None):
    env = os.environ if env is None else env
    single_task(env)
    if not env.get('SLURM_JOB_ID') or not env.get('SLURMD_NODENAME'):
        raise DeploymentRefused('Slurm compute allocation required; no login fallback')


def local_root(path):
    """Reject remote filesystems before creating locks or live journals."""
    path = Path(path).resolve(strict=True)
    if not path.is_dir():
        raise DeploymentRefused('local scratch directory required')
    mounts = []
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        before, after = line.split(' - ', 1)
        mount = Path(before.split()[4].replace('\\040', ' '))
        if path.is_relative_to(mount):
            mounts.append((len(mount.parts), after.split()[0]))
    kind = max(mounts)[1] if mounts else ''
    if kind not in ('ext2', 'ext3', 'ext4', 'xfs', 'btrfs', 'tmpfs', 'overlay'):
        raise DeploymentRefused('node-local filesystem required: ' + kind)
    return path


@contextmanager
def ownership(root, *, compute=True):
    if compute:
        require_compute()
    else:
        single_task()
    root = local_root(root)
    fd = os.open(root / 'controller.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def fixture_mounts(fixtures, *, mode):
    """File-only, checksum-pinned mounts; no undeclared directory contents."""
    if mode not in ('test', 'pilot', 'production'):
        raise DeploymentRefused('unknown deployment mode')
    if fixtures and mode == 'production':
        raise DeploymentRefused('production refuses test fixture/pacing flags')
    mounts, destinations = [], set()
    for row in fixtures:
        source = Path(row['source'])
        # Inspect both spelling and canonical path before reading any contents.
        canonical = source.resolve(strict=True)
        if any(x in ('batch-3-problems', 'b3-problems', 'runs-v2') for x in (*source.parts, *canonical.parts)):
            raise DeploymentRefused('campaign/problem fixture source refused')
        if source.is_symlink() or any(p.is_symlink() for p in source.parents) or not canonical.is_file():
            raise DeploymentRefused('fixture must be a regular file')
        target = PurePosixPath(row['target'])
        if not target.is_absolute() or '..' in target.parts or not (str(target).startswith('/work/fixtures/') or str(target).startswith('/work/pilot-fixture/') or str(target) == '/work/PILOT-PROBLEM.md'):
            raise DeploymentRefused('fixture target is outside declared pilot locations')
        if str(target) in destinations:
            raise DeploymentRefused('duplicate fixture mount')
        if canonical.stat().st_size > 16 * 1024 * 1024:
            raise DeploymentRefused('fixture exceeds bounded size')
        if hashlib.sha256(canonical.read_bytes()).hexdigest() != row['sha256']:
            raise DeploymentRefused('fixture checksum mismatch')
        destinations.add(str(target))
        mounts += ['--ro-bind', str(canonical), str(target)]
    roots = sorted({('/work/pilot-fixture' if dest.startswith('/work/pilot-fixture/') else '/work/fixtures') for dest in destinations if dest != '/work/PILOT-PROBLEM.md'})
    return [item for root in roots for item in ('--tmpfs', root)] + mounts + [item for root in roots for item in ('--remount-ro', root)]


def release_root(manifest):
    root = Path(manifest['release_root']).resolve(strict=True)
    expected = manifest['release_sha256']
    if not re.fullmatch('[0-9a-f]{64}', expected):
        raise DeploymentRefused('release digest required')
    files = manifest['release_files']
    if not files or len(files) > 10000:
        raise DeploymentRefused('bounded release inventory required')
    digest = hashlib.sha256()
    for name, sha in sorted(files.items()):
        relative = PurePosixPath(name)
        if relative.is_absolute() or '..' in relative.parts:
            raise DeploymentRefused('unsafe release member')
        path = root / name
        if path.is_symlink() or not path.resolve(strict=True).is_relative_to(root):
            raise DeploymentRefused('release symlink refused')
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != sha:
            raise DeploymentRefused('release file checksum mismatch: ' + name)
        digest.update(name.encode() + b'\0' + sha.encode() + b'\n')
    if digest.hexdigest() != expected:
        raise DeploymentRefused('release inventory checksum mismatch')
    return root


def preflight(manifest, *, env=None):
    """Called before credentials load. Does not read any credential."""
    require_compute(env)
    root = release_root(manifest)
    scratch = local_root(manifest['local_root'])
    if manifest['mode'] == 'production' and any(manifest.get(key) for key in ('pacing', 'fake_provider', 'accelerated_clock', 'test_clock', 'synthetic')):
        raise DeploymentRefused('production test-only flags refused')
    mounts = fixture_mounts(manifest.get('fixtures', []), mode=manifest['mode'])
    target = manifest['target']
    actual = os.environ if env is None else env
    if actual.get('SLURM_JOB_PARTITION') != target['partition']:
        raise DeploymentRefused('unqualified partition substitution')
    any_node = target['nodes'] == ['*']  # any partition node, production included (operator ruling 2026-10-06)
    if not any_node and actual.get('SLURMD_NODENAME') not in target['nodes']:
        raise DeploymentRefused('unqualified node substitution')
    if int(actual.get('SLURM_CPUS_PER_TASK', '0')) < target['cpus']:
        raise DeploymentRefused('insufficient allocated CPUs')
    memory_mib = int(actual.get('SLURM_MEM_PER_NODE', '0'))
    if not memory_mib:
        memory_mib = int(actual.get('SLURM_MEM_PER_CPU', '0')) * int(actual.get('SLURM_CPUS_PER_TASK', '0'))
    if memory_mib < target['memory_gib'] * 1024:
        raise DeploymentRefused('insufficient allocated memory')
    return root, scratch, mounts


def slurm_command(manifest_path, manifest, *, output):
    target = manifest['target']
    if target['cpus'] < 1 or target['memory_gib'] < 1 or target['seconds'] < 1:
        raise DeploymentRefused('invalid resource request')
    root = release_root(manifest)
    return ['sbatch', '--parsable', '--ntasks=1', '--nodes=1',
            '--partition=' + target['partition'], *([] if target['nodes'] == ['*'] else ['--nodelist=' + ','.join(target['nodes'])]),
            '--cpus-per-task=' + str(target['cpus']), '--mem=' + str(target['memory_gib']) + 'G',
            '--time=' + str((target['seconds'] + 59)//60), '--signal=B:TERM@120',
            '--output=' + str(output), str(root/'bin/v3-job'), str(Path(manifest_path).resolve())]


def apply_control(root, request_id, operation):
    """Under ownership(): transform one complete JSON checkpoint atomically.

    operation is a pure transform, not an external side effect. The request id
    and its result commit with state so replay cannot apply an override twice.
    """
    if not re.fullmatch('[A-Za-z0-9_-]{1,120}', request_id):
        raise DeploymentRefused('invalid control request identity')
    path = Path(root) / 'checkpoint.json'
    state = json.loads(path.read_text())
    if request_id in state.get('applied_controls', {}):
        return state['applied_controls'][request_id]
    result = operation(state)
    state.setdefault('applied_controls', {})[request_id] = result
    atomic_json(path, state)
    return result


IMMUTABLE = ('arm_id', 'started_at', 'deadline', 'original_cap', 'ledger', 'leases',
             'review_counters', 'cursors', 'report_lock', 'recovery_counters')


def prepare_upgrade(state, new_release, *, now=None):
    """An upgrade begins fenced; caller drains/reaps before completing it."""
    now = time.time() if now is None else now
    if state.get('frozen') or state.get('terminal') or now >= state['deadline']:
        raise DeploymentRefused('terminal/frozen/expired upgrade refused')
    if state.get('upgrade', {}).get('status') == 'prepared':
        raise DeploymentRefused('upgrade already pending')
    if not re.fullmatch('[0-9a-f]{64}', new_release):
        raise DeploymentRefused('tested release digest required')
    original = {key: value for key, value in state.items() if key not in ('upgrade', 'admission_closed', 'release_sha256')}
    digest = hashlib.sha256(json.dumps(original, sort_keys=True).encode()).hexdigest()
    was_closed = bool(state.get('admission_closed', False))
    state['admission_closed'] = True
    state['upgrade'] = dict(status='prepared', release=new_release, preserved_sha256=digest, was_admission_closed=was_closed)


def complete_upgrade(state, *, checkpoint_ack, owned_reaped, active_calls, now=None):
    now = time.time() if now is None else now
    upgrade = state['upgrade']
    if upgrade['status'] != 'prepared' or not checkpoint_ack or not owned_reaped or active_calls:
        raise DeploymentRefused('upgrade requires durable checkpoint, drained calls and reaped ownership')
    if state.get('frozen') or state.get('terminal') or now >= state['deadline']:
        raise DeploymentRefused('original stop wins over upgrade')
    digest = hashlib.sha256(json.dumps({key: value for key, value in state.items() if key not in ('upgrade', 'admission_closed', 'release_sha256')}, sort_keys=True).encode()).hexdigest()
    if digest != upgrade['preserved_sha256']:
        raise DeploymentRefused('upgrade changed preserved state')
    state['release_sha256'] = upgrade['release']
    upgrade['status'] = 'completed'
    # Report mode stays locked; restored admission is still subject to role policy.
    state['admission_closed'] = upgrade['was_admission_closed']


def check_fixture_namespace(command, env, fixtures, *, timeout=20):
    """Probe actual worker mounts before any credential is loaded."""
    from .owned import run_owned
    if not fixtures:
        return
    marker = '/harness/supervisor.py'
    if marker in command:
        cut = command.index(marker)-1
    else:
        cut = command.index('/bin/bash')
    checks = [{'path': row['target'], 'sha256': row['sha256']} for row in fixtures]
    script = """import errno,hashlib,json,os,sys
for row in json.loads(sys.argv[1]):
 p=row['path']
 assert hashlib.sha256(open(p,'rb').read()).hexdigest()==row['sha256']
 try:
  fd=os.open(p,os.O_WRONLY)
 except OSError as exc:
  assert exc.errno in (errno.EROFS,errno.EACCES,errno.EPERM)
 else:
  os.close(fd)
  raise RuntimeError('fixture mount is writable')
"""
    result = run_owned(command[:cut]+['/usr/bin/python3', '-c', script, json.dumps(checks)],
                       env=env, timeout=timeout, capture_output=True)
    if result.returncode:
        raise DeploymentRefused('actual worker fixture mount probe failed: '+result.stderr[-1000:])


def control_command(job_id, argv):
    """Every maintenance/resume command is one task in its existing allocation."""
    if not re.fullmatch('[0-9]+', str(job_id)) or not argv or not all(isinstance(x, str) for x in argv):
        raise DeploymentRefused('invalid control command')
    return ['srun', '--jobid='+str(job_id), '--ntasks=1', '--nodes=1', '--overlap', *argv]


def worker_fixtures(config):
    """Host-owned deployment context, usable without extending worker constructors."""
    deployment = getattr(getattr(config, 'gateway', None), 'deployment', {})
    fixtures = getattr(config, 'fixtures', deployment.get('fixtures', []))
    mode = getattr(config, 'deployment_mode', deployment.get('mode', 'production'))
    return fixtures, mode


def problem_mounts(config):
    """Real pilot input is distinct from the prohibited offline fixture path."""
    deployment=getattr(getattr(config,'gateway',None),'deployment',{})
    row=getattr(config,'problem_mount',deployment.get('problem'))
    if row is None:return []
    mode=getattr(config,'deployment_mode',deployment.get('mode'))
    if mode == 'production':
        from .production import validate_problem
        validate_problem(row)
    elif mode == 'pilot':
        from .pilot import problem_record
        if row!=problem_record():raise DeploymentRefused('real pilot problem checksum changed')
    else:raise DeploymentRefused('undeclared real-problem mount mode')
    # Existing generic worker goals read PROBLEM.md; both names expose exactly
    # the same immutable operator input, never a closed-model interpretation.
    return ['--ro-bind',row['source'],'/work/PROBLEM.tex',
            '--ro-bind',row['source'],'/work/PROBLEM.md']
