"""Bounded descriptor-relative artifact reconciliation; no symlink traversal."""
import hashlib
import os
import stat

ARTIFACTS = {'NOTES.md', 'EXPERIMENTS.md', 'FINAL.tex', 'PROVENANCE.md', 'AUDIT.md', 'BLOCKED.md'}
EXCLUDED = {'__pycache__', '.git', '.libs', 'node_modules', 'build', 'dist', '.cache'}
FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC

def scan(root, max_bytes=8*1024*1024, max_files=4096, max_depth=16):
    result = {}; remaining = max_bytes; visited = 0
    def file(parent, name, relative):
        nonlocal remaining, visited
        if visited >= max_files or remaining <= 0: return
        visited += 1
        fd = None
        try:
            fd = os.open(name, FLAGS, dir_fd=parent)
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_size > remaining: return
            digest = hashlib.sha256(); count = 0
            while count <= before.st_size:
                chunk = os.read(fd, min(65536, before.st_size + 1 - count))
                if not chunk: break
                count += len(chunk); remaining -= len(chunk); digest.update(chunk)
            after = os.fstat(fd)
            # Replacement/unlink and writes during hashing cannot claim progress.
            named = os.stat(name, dir_fd=parent, follow_symlinks=False)
            attrs = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
            if count == before.st_size and attrs(before) == attrs(after) == attrs(named):
                result[relative] = digest.hexdigest()
        except OSError:
            pass
        finally:
            if fd is not None: os.close(fd)
    def directory(fd, prefix, depth):
        nonlocal visited
        if depth > max_depth: return
        try:
            with os.scandir(fd) as entries:
                for entry in entries:
                    if visited >= max_files or remaining <= 0: break
                    if entry.name in EXCLUDED or entry.name.endswith(('.tmp', '.lock', '~')): continue
                    relative = prefix + '/' + entry.name
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            visited += 1
                            child = os.open(entry.name, FLAGS | os.O_DIRECTORY, dir_fd=fd)
                            try: directory(child, relative, depth + 1)
                            finally: os.close(child)
                        else: file(fd, entry.name, relative)
                    except OSError: pass
        except OSError: pass
    try: fd = os.open(root, FLAGS | os.O_DIRECTORY)
    except OSError: return result
    try:
        for name in sorted(ARTIFACTS): file(fd, name, name)
        try: code = os.open('code', FLAGS | os.O_DIRECTORY, dir_fd=fd)
        except OSError: code = None
        if code is not None:
            try: directory(code, 'code', 0)
            finally: os.close(code)
    finally: os.close(fd)
    return result


def report_content(root, name, limit=8*1024*1024):
    """Bounded no-follow inspection; a concurrent writer earns no readiness."""
    parent=fd=None
    try:
        parent=os.open(root,FLAGS|os.O_DIRECTORY)
        fd=os.open(name,FLAGS,dir_fd=parent)
        before=os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size>limit:return b''
        chunks=[];left=before.st_size
        while left:
            chunk=os.read(fd,min(65536,left))
            if not chunk:return b''
            chunks.append(chunk);left-=len(chunk)
        after=os.fstat(fd);named=os.stat(name,dir_fd=parent,follow_symlinks=False)
        attrs=lambda s:(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
        return b''.join(chunks) if attrs(before)==attrs(after)==attrs(named) else b''
    except OSError:return b''
    finally:
        if fd is not None:os.close(fd)
        if parent is not None:os.close(parent)
