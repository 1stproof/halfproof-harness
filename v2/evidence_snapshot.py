"""Bounded frozen role evidence copied without following worker-controlled links."""
import hashlib
import os
from pathlib import Path
import stat


def copy_evidence(source, destination, *, max_files=4096, max_bytes=64*1024*1024):
    destination = Path(destination)
    destination.mkdir(mode=0o700)
    count, remaining = 0, max_bytes
    inventory = {}
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK

    def walk(directory, target, prefix='', depth=0):
        nonlocal count, remaining
        if depth > 16:
            raise ValueError('evidence depth exceeded')
        for name in sorted(os.listdir(directory)):
            if name.startswith('.') or name == '__pycache__':
                continue
            count += 1
            if count > max_files:
                raise ValueError('evidence file count exceeded')
            fd = os.open(name, flags, dir_fd=directory)
            try:
                before = os.fstat(fd)
                if stat.S_ISDIR(before.st_mode):
                    child = target/name; child.mkdir()
                    walk(fd, child, prefix+name+'/', depth+1)
                    child.chmod(0o555)
                elif stat.S_ISREG(before.st_mode):
                    if before.st_size > remaining:
                        raise ValueError('evidence byte bound exceeded')
                    data = bytearray()
                    while len(data) <= before.st_size:
                        chunk = os.read(fd, min(65536, before.st_size+1-len(data)))
                        if not chunk: break
                        data.extend(chunk)
                    attrs = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                    if len(data) != before.st_size or attrs(before) != attrs(os.fstat(fd)) or attrs(before) != attrs(os.stat(name, dir_fd=directory, follow_symlinks=False)):
                        raise ValueError('evidence changed during snapshot')
                    remaining -= len(data)
                    path = target/name; path.write_bytes(data); path.chmod(0o444)
                    inventory[prefix+name] = hashlib.sha256(data).hexdigest()
                else:
                    raise ValueError('nonregular evidence refused')
            finally:
                os.close(fd)
    fd = os.open(source, flags | os.O_DIRECTORY)
    try: walk(fd, destination)
    finally: os.close(fd)
    destination.chmod(0o555)
    return inventory
