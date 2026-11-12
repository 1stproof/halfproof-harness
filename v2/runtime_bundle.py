"""Content-addressed node-local Qwen/Node bytes; never patch shared installations."""
import hashlib
import functools
import json
import os
from pathlib import Path
import shutil
import tempfile
from .deployment import local_root


def pin_runtime(root, *, node=None, qwen=None):
    """Compute-only work: bound inventory, reject external symlinks, copy bytes."""
    root = local_root(root)
    node = Path(node or Path.home()/'.local/ds41-node')
    qwen = Path(qwen or Path.home()/'.local/qwen-npm')
    package = qwen/'lib/node_modules/@qwen-code/qwen-code/package.json'
    if json.loads(package.read_text())['version'] != '0.24.6':
        raise ValueError('pinned Qwen version mismatch')
    inventory, sources, size = {}, {}, 0
    candidates = [('node/bin/node', node/'bin/node', node)]
    for directory, dirs, files in os.walk(qwen, followlinks=False):
        dirs.sort(); files.sort()
        base=Path(directory)
        for name in dirs[:]:
            if (base/name).is_symlink():
                candidates.append(('qwen/'+str((base/name).relative_to(qwen)), base/name, qwen))
                dirs.remove(name)
        candidates.extend(('qwen/'+str((base/name).relative_to(qwen)), base/name, qwen) for name in files)
        if len(candidates)>50000: raise ValueError('runtime file limit')
    for name, path, allowed in candidates:
        if path.is_symlink():
            link=os.readlink(path)
            if Path(link).is_absolute() or not path.resolve(strict=True).is_relative_to(allowed.resolve()):
                raise ValueError('runtime external symlink refused')
            inventory[name]={'link':link}
        else:
            before=path.stat()
            if not path.is_file() or before.st_size+size>1024**3:
                raise ValueError('runtime size/type bound')
            digest=hashlib.sha256()
            with path.open('rb') as stream:
                for chunk in iter(lambda: stream.read(1024*1024), b''): digest.update(chunk)
            after=path.stat()
            if (before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_size,after.st_mtime_ns,after.st_ctime_ns):
                raise ValueError('runtime changed during pin')
            inventory[name]={'sha256':digest.hexdigest(), 'executable':bool(before.st_mode & 0o111)}
            size+=before.st_size
        sources[name]=path
    digest=hashlib.sha256(json.dumps(inventory,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    destination=root/'runtime'/digest
    if not destination.exists():
        destination.parent.mkdir(exist_ok=True)
        stage=Path(tempfile.mkdtemp(prefix='.runtime-',dir=destination.parent))
        try:
            for name, row in inventory.items():
                path=stage/name; path.parent.mkdir(parents=True,exist_ok=True)
                if 'link' in row: path.symlink_to(row['link'])
                else:
                    shutil.copyfile(sources[name], path)
                    if hashlib.sha256(path.read_bytes()).hexdigest()!=row['sha256']:
                        raise ValueError('runtime changed during copy')
                    path.chmod(0o555 if row['executable'] else 0o444)
            (stage/'manifest.json').write_text(json.dumps(inventory,sort_keys=True))
            (stage/'manifest.json').chmod(0o444)
            for directory in sorted((p for p in stage.rglob('*') if p.is_dir() and not p.is_symlink()),reverse=True): directory.chmod(0o555)
            stage.chmod(0o555)
            os.rename(stage,destination)
        except BaseException:
            # Preserve failed staging evidence; never publish a partial runtime.
            raise
    elif destination.is_symlink():
        raise ValueError('runtime destination symlink')
    return dict(version='0.24.6', sha256=digest, files=len(inventory), bytes=size)


@functools.lru_cache(maxsize=4)
def sources(release):
    record=json.loads((Path(release)/'runtime-manifest.json').read_text())
    root=Path(release).parent.parent/'runtime'/record['sha256']
    if root.is_symlink() or not root.is_dir():
        raise ValueError('pinned private runtime missing')
    manifest=json.loads((root/'manifest.json').read_text())
    digest=hashlib.sha256(json.dumps(manifest,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    if digest!=record['sha256']:
        raise ValueError('runtime manifest mismatch')
    for name, row in manifest.items():
        path=root/name
        if 'link' in row:
            if not path.is_symlink() or os.readlink(path)!=row['link']:
                raise ValueError('runtime link checksum mismatch')
        else:
            if path.is_symlink() or not path.is_file():
                raise ValueError('runtime member missing')
            digest=hashlib.sha256()
            with path.open('rb') as stream:
                for chunk in iter(lambda: stream.read(1024*1024), b''): digest.update(chunk)
            if digest.hexdigest()!=row['sha256']:
                raise ValueError('runtime bytes checksum mismatch')
    return root/'node',root/'qwen'
