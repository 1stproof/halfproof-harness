"""Recover the missing GP binary without changing the live CAS installation."""
import os
import functools
import hashlib
import io
from pathlib import Path
import subprocess
import tarfile
import zipfile

ARCHIVE=Path(os.environ.get('HARNESS_PARI_ARCHIVE', os.path.expanduser(os.environ.get('HARNESS_DATA','~/oss-harness-data'))+'/cas/pkgs/pari-2.17.3-hab59ada_0_pthread.conda'))
SHA256='44d3b5b8c9ef49548adaeb36b3b03a81c768d43c5937551f3c8cd724d69c379b'

@functools.lru_cache(maxsize=1)
def gp_binary():
    raw=ARCHIVE.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=SHA256:raise ValueError('cached PARI package checksum mismatch')
    with zipfile.ZipFile(io.BytesIO(raw)) as package:
        compressed=package.read('pkg-pari-2.17.3-hab59ada_0_pthread.tar.zst')
    raw=subprocess.run(['zstd','-d','-q','-c'],input=compressed,capture_output=True,check=True,timeout=10).stdout
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        return archive.extractfile('bin/gp-2.17').read()

SAGE_ARCHIVE=ARCHIVE.with_name('sagelib-10.9-np2py312hfcacda7_0.conda')
SAGE_SHA256='00e685346d7f38f092ef8f1493b67d918c7a243b62762fbc59158cd3ab179aab'

def sage_overlay(root):
    """Cached immutable overlay; only package data, no installers or shell hooks."""
    import os
    import tempfile
    target=root/'cas-sagelib-10.9'
    if target.is_dir():return target
    raw=SAGE_ARCHIVE.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=SAGE_SHA256:raise ValueError('cached Sage package checksum mismatch')
    with zipfile.ZipFile(io.BytesIO(raw)) as package:
        compressed=package.read('pkg-sagelib-10.9-np2py312hfcacda7_0.tar.zst')
    stage=Path(tempfile.mkdtemp(prefix='.sage-',dir=root))
    # Stream decompression to disk: no full uncompressed archive in memory.
    with tempfile.TemporaryFile() as packed:
        packed.write(compressed);packed.seek(0)
        proc=subprocess.Popen(['zstd','-d','-q','-c'],stdin=packed,stdout=subprocess.PIPE)
        try:
            with tarfile.open(fileobj=proc.stdout,mode='r|') as archive:
                for member in archive:
                    prefix='lib/python3.12/site-packages/sage/'
                    if member.name.startswith(prefix) and member.isfile():
                        relative=Path(member.name[len(prefix):])
                        if relative.is_absolute() or '..' in relative.parts:raise ValueError('unsafe package path')
                        path=stage/relative;path.parent.mkdir(parents=True,exist_ok=True)
                        path.write_bytes(archive.extractfile(member).read());path.chmod(0o444)
            if proc.wait(timeout=10):raise ValueError('Sage decompression failed')
        finally:
            if proc.poll() is None:proc.kill();proc.wait()
    publish(stage,target)
    return target

def python_overlay(root,cas):
    """Restore missing Python packages from the installation's cached archives.
    Existing packages remain read-only via /research/original-cas. No conda hooks.
    """
    import json
    import os
    import tempfile
    target=root/'cas-python-overlay-v1'
    if target.is_dir():return target
    stage=Path(tempfile.mkdtemp(prefix='.cas-python-',dir=root))
    site=cas/'lib/python3.12/site-packages'
    for item in site.iterdir():
        (stage/item.name).symlink_to('/research/original-cas/lib/python3.12/site-packages/'+item.name)
    (stage/'sage').unlink(missing_ok=True)
    (stage/'sage').symlink_to('/research/sage')
    repaired=[]
    for meta in sorted((cas/'conda-meta').glob('*.json')):
        data=json.loads(meta.read_text())
        prefix='lib/python3.12/site-packages/'
        files=[x for x in data.get('files',[]) if x.startswith(prefix) and x.endswith(('.py','.so'))]
        if not files or (cas/files[0]).exists() or data.get('name')=='sagelib':continue
        archive_path=Path(data['link']['source']+'.conda')
        if not archive_path.exists():archive_path=Path(data['link']['source']+'.tar.bz2')
        raw=archive_path.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=data['sha256']:raise ValueError('CAS package checksum mismatch')
        if archive_path.suffix=='.conda':
            with zipfile.ZipFile(io.BytesIO(raw)) as package:
                compressed=package.read(next(x for x in package.namelist() if x.startswith('pkg-')))
            raw=subprocess.run(['zstd','-d','-q','-c'],input=compressed,capture_output=True,check=True,timeout=10).stdout
        with tarfile.open(fileobj=io.BytesIO(raw),mode='r:*') as archive:
            for member in archive:
                found=next((p for p in (prefix,'site-packages/') if member.name.startswith(p)),None)
                if not found or not member.isfile():continue
                relative=Path(member.name[len(found):])
                if relative.is_absolute() or '..' in relative.parts:raise ValueError('unsafe CAS package path')
                top=stage/relative.parts[0]
                if top.is_symlink():top.unlink()
                path=stage/relative;path.parent.mkdir(parents=True,exist_ok=True)
                path.write_bytes(archive.extractfile(member).read());path.chmod(0o444)
        repaired.append({'package':meta.stem,'sha256':data['sha256']})
    (stage/'REPAIR-MANIFEST.json').write_text(json.dumps(repaired,indent=2)+'\n')
    publish(stage,target)
    return target

def native_overlay(root,cas):
    """Restore dangling native libraries and GAP runtime data, in v2 only."""
    import json
    import os
    import tempfile
    target=root/'cas-native-overlay-v7'
    if target.is_dir():return target
    stage=Path(tempfile.mkdtemp(prefix='.cas-native-',dir=root))
    (stage/'lib').mkdir()
    needed=set()
    for path in (cas/'lib').iterdir():
        if path.is_symlink() and not path.exists():
            needed.add('lib/'+path.resolve().name)
            (stage/'lib'/path.name).symlink_to(path.readlink())
    for meta in sorted((cas/'conda-meta').glob('*.json')):
        data=json.loads(meta.read_text())
        selected=set(data.get('files',[])) & needed
        selected.update(x for x in data.get('files',[]) if x.startswith('bin/') and not (cas/x).exists())
        gap=any(name.startswith('share/gap/') for name in data.get('files',[]))
        singular=any(name.startswith(('share/singular/','lib/singular/')) for name in data.get('files',[]))
        if not selected and not gap and not singular:continue
        archive_path=Path(data['link']['source']+'.conda')
        if not archive_path.exists():archive_path=Path(data['link']['source']+'.tar.bz2')
        raw=archive_path.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=data['sha256']:raise ValueError('native package checksum mismatch')
        if archive_path.suffix=='.conda':
            with zipfile.ZipFile(io.BytesIO(raw)) as package:
                compressed=package.read(next(x for x in package.namelist() if x.startswith('pkg-')))
            raw=subprocess.run(['zstd','-d','-q','-c'],input=compressed,capture_output=True,check=True,timeout=10).stdout
        with tarfile.open(fileobj=io.BytesIO(raw),mode='r:*') as archive:
            for member in archive:
                if not member.isfile() or not (member.name in selected
                        or gap and member.name.startswith(('lib/','share/gap/','bin/'))
                        or singular and member.name.startswith(('lib/','share/singular/','bin/'))):continue
                relative=Path(member.name)
                if relative.is_absolute() or '..' in relative.parts:raise ValueError('unsafe native package path')
                path=stage/relative;path.parent.mkdir(parents=True,exist_ok=True)
                path.unlink(missing_ok=True)
                path.write_bytes(archive.extractfile(member).read());path.chmod(member.mode & 0o555)
    for binary in (cas/'bin').iterdir():
        path=stage/'bin'/binary.name
        if not path.exists() and not path.is_symlink():path.symlink_to('/research/original-cas/bin/'+binary.name)
    # The package's GAP binary retains its build-time default root. Select the
    # mounted, checksum-restored data explicitly rather than modifying shared CAS.
    gap = stage/'bin/gap'
    gap.rename(stage/'bin/gap-real')
    gap.write_text('#!/bin/sh\nexec "$(dirname "$0")/gap-real" -l "$DS_CAS_ENV/share/gap" "$@"\n')
    gap.chmod(0o555)
    repair_python_bin(stage,cas)
    publish(stage,target)
    return target


def repair_python_bin(stage,cas):
    import shutil
    for name in ('python','python3','python3.12'):
        (stage/'bin'/name).unlink(missing_ok=True)
    shutil.copyfile(cas/'bin/python3.12',stage/'bin/python3.12')
    (stage/'bin/python3.12').chmod(0o555)
    for name in ('python','python3'):(stage/'bin'/name).symlink_to('python3.12')

def publish(stage,target):
    import os,errno,shutil
    try:os.rename(stage,target)
    except OSError as exc:
        if exc.errno not in (errno.EEXIST,errno.ENOTEMPTY) or not target.is_dir():raise
        shutil.rmtree(stage)
