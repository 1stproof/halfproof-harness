"""Bounded, no-follow storage primitives for the trusted closure service.

Call from the isolated publisher on durable storage, never under controller locks.
Worker namespaces must not mount these destinations writable (P7).
"""
from contextlib import contextmanager
import errno
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile
import fcntl

from .contracts_v3 import canonical_json

MAX_FILE = 16 * 1024 * 1024
# Lifted for 24 h runs (monitor 07:07: freeze hit 4096 files / 64 MiB; mixed projects to
# ~1.1 GiB / 14k files at 24 h). Freeze trims to a softer budget before reaching these.
MAX_TOTAL = 100 * 1024 * 1024 * 1024
MAX_FILES = 1000000

class ClosureError(ValueError):
    pass


def canonical(value):
    return canonical_json(value)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def relative_name(name):
    if not isinstance(name, str) or not name or '\\' in name or '\x00' in name:
        raise ClosureError('invalid artifact path')
    p = PurePosixPath(name)
    if p.is_absolute() or any(x in ('', '.', '..') for x in name.split('/')):
        raise ClosureError('artifact path must be canonical and relative')
    if len(p.parts) > 16:
        raise ClosureError('artifact path too deep')
    return p


def read_file(root, name, limit=MAX_FILE):
    """Open every component no-follow and reject writes/replacements during read."""
    parts = relative_name(name).parts
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    descriptors = []
    try:
        parent = os.open(root, flags | os.O_DIRECTORY)
        descriptors.append(parent)
        for part in parts[:-1]:
            parent = os.open(part, flags | os.O_DIRECTORY, dir_fd=parent)
            descriptors.append(parent)
        fd = os.open(parts[-1], flags, dir_fd=parent)
        descriptors.append(fd)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise ClosureError('artifact is not a bounded regular file')
        data = bytearray()
        while len(data) <= before.st_size:
            block = os.read(fd, min(65536, before.st_size + 1 - len(data)))
            if not block:
                break
            data.extend(block)
        attrs = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        named = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
        if len(data) != before.st_size or attrs(before) != attrs(os.fstat(fd)) or attrs(before) != attrs(named):
            raise ClosureError('artifact changed during snapshot')
        return bytes(data)
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_json(path, value):
    """Trusted service state only; parent must be private and on local storage."""
    path = Path(path)
    fd, name = tempfile.mkstemp(prefix='.state-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(canonical(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        fsync_dir(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def locked(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(root / '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def publish_tree(parent, name, files):
    """Fsync then exclusively link an immutable staging copy into a new tree.

    Returns only after durable acknowledgment. P7 bounds the publisher process
    and stops admission after 60 seconds without an acknowledgment.
    """
    relative_name(name)
    if '/' in name:
        raise ClosureError('publication identifier must be a basename')
    if len(files) > MAX_FILES or sum(len(x) for x in files.values()) > MAX_TOTAL:
        raise ClosureError('publication too large')
    parent = Path(parent)
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = parent / name
    hashes = {k: sha256(v) for k, v in files.items()}
    if target.exists() or target.is_symlink():
        verify_or_complete(target, files, hashes)
        return target
    stage = Path(tempfile.mkdtemp(prefix='.publish-', dir=parent))
    try:
        for key, data in sorted(files.items()):
            relative_name(key)
            dest = stage / key
            dest.parent.mkdir(parents=True, exist_ok=True)
            with dest.open('xb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            dest.chmod(0o444)
        for directory, _, _ in os.walk(stage, topdown=False):
            os.chmod(directory, 0o555)
            fsync_dir(directory)
        from .nfs_install import publish_noreplace, match_modes
        try:publish_noreplace(stage,target)
        except FileExistsError:
            verify_or_complete(target, files, hashes)
        else:
            verify_tree(target, hashes)
            match_modes(stage,target)
        fsync_dir(parent)
        return target
    finally:
        if stage.exists():
            for directory, _, _ in os.walk(stage):
                os.chmod(directory, 0o700)
            try:
                shutil.rmtree(stage)
            except OSError as error:
                # NFS may retain an open inode as .nfs*: the verified target
                # is already durable. Keep this private cleanup evidence.
                if error.errno != errno.ENOTEMPTY:
                    raise


def verify_or_complete(target, files, hashes):
    """Verify an existing target, finishing a crashed publication of this identity.

    Exclusive mkdir plus per-file links means a kill can leave a partial tree.
    Every present file must already hold the expected bytes; a foreign or
    differing file is refused. Only missing files are added (never replaced),
    then the tree is frozen and verified.
    """
    try:
        return verify_tree(target, hashes)
    except ClosureError:
        pass
    target = Path(target)
    if target.is_symlink() or not target.is_dir():
        raise ClosureError('missing or linked immutable tree')
    present = set()
    for directory, dirs, names in os.walk(target, followlinks=False):
        for name in dirs:
            if (Path(directory) / name).is_symlink():
                raise ClosureError('linked immutable directory')
        present.update(str((Path(directory) / name).relative_to(target)) for name in names)
    if not present.issubset(hashes):
        raise ClosureError('immutable artifact set changed')
    for name in sorted(present):
        if sha256(read_file(target, name)) != hashes[name]:
            raise ClosureError('immutable artifact hash changed')
    for directory, _, _ in os.walk(target):
        os.chmod(directory, 0o700)
    for key, data in sorted(files.items()):
        if key in present:
            continue
        relative_name(key)
        dest = target / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            stream = dest.open('xb')
        except FileExistsError:
            continue  # a concurrent publisher of the same identity; verified below
        with stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        dest.chmod(0o444)
    for directory, _, _ in os.walk(target, topdown=False):
        os.chmod(directory, 0o555)
        fsync_dir(directory)
    fsync_dir(target.parent)
    return verify_tree(target, hashes)


def verify_tree(root, hashes):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ClosureError('missing or linked immutable tree')
    names = set()
    visited = 0
    for directory, dirs, files in os.walk(root, followlinks=False):
        visited += len(dirs) + len(files)
        if visited > MAX_FILES or len(Path(directory).relative_to(root).parts) > 16:
            raise ClosureError('immutable tree too large')
        for name in dirs:
            if (Path(directory) / name).is_symlink():
                raise ClosureError('linked immutable directory')
        names.update(str((Path(directory) / name).relative_to(root)) for name in files)
    if names != set(hashes):
        raise ClosureError('immutable artifact set changed')
    total = 0
    for name, digest in hashes.items():
        data = read_file(root, name)
        total += len(data)
        if total > MAX_TOTAL:
            raise ClosureError('immutable tree exceeds byte bound')
        if sha256(data) != digest:
            raise ClosureError('immutable artifact hash changed')
    return True
