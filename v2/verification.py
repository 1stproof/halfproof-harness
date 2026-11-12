"""Bounded, no-follow candidate snapshots for fresh same-model verification."""
import hashlib
import json
import os
from pathlib import Path
import stat

MAX_FILES = 40960  # 10x lift for 24 h runs
MAX_BYTES = 640 * 1024 * 1024
SKIP = {'VERIFICATION.json', 'CLAIM.json', '.git', '__pycache__'}
FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


def snapshot(source, destination=None):
    """Hash every included byte; fail closed on missing/unstable/oversized evidence.

    A destination, if supplied, must be private to this new verifier session.
    No source content is written to telemetry. No symlink is dereferenced.
    """
    remaining = MAX_BYTES
    visited = 0
    entries = {}

    def walk(root_fd, prefix='', depth=0):
        nonlocal remaining, visited
        if depth > 16:
            raise ValueError('candidate-depth-limit')
        for name in sorted(os.listdir(root_fd)):
            if name in SKIP:
                continue
            relative = prefix + name
            if visited >= MAX_FILES:
                raise ValueError('candidate-file-limit')
            visited += 1
            info = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                raise ValueError('candidate-symlink')
            fd = os.open(name, FLAGS, dir_fd=root_fd)
            try:
                before = os.fstat(fd)
                if stat.S_ISDIR(before.st_mode):
                    if destination is not None:
                        (destination/relative).mkdir()
                    walk(fd, relative+'/', depth+1)
                    continue
                if not stat.S_ISREG(before.st_mode) or before.st_size > remaining:
                    raise ValueError('candidate-size-or-type')
                data = bytearray()
                while len(data) <= before.st_size:
                    part = os.read(fd, min(65536, before.st_size+1-len(data)))
                    if not part:
                        break
                    data.extend(part)
                attrs = lambda s: (s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
                after = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
                if (len(data) != before.st_size or attrs(before) != attrs(os.fstat(fd))
                        or attrs(before) != attrs(after)):
                    raise ValueError('candidate-changed')
                remaining -= len(data)
                entries[relative] = hashlib.sha256(data).hexdigest()
                if destination is not None:
                    target = destination/relative
                    target.write_bytes(data)
                    target.chmod(0o444)
            finally:
                os.close(fd)
    root_fd = os.open(source, FLAGS | os.O_DIRECTORY)
    try:
        walk(root_fd)
    finally:
        os.close(root_fd)
    digest = hashlib.sha256(json.dumps(entries,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return digest, entries
