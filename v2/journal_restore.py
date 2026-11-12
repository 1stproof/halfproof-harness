"""Bounded parallel verification of a fenced publisher journal, never paid replay."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import time


def verify(local, durable, expected=None, *, seconds=1800, workers=8):
    from .durability import PUBLICATION_LIMIT, _atomic_bytes, read_publication
    local, durable = Path(local), Path(durable)
    until = time.monotonic() + seconds
    paths = sorted(p for p in local.glob('*.json') if not p.name.startswith('operation-'))
    if len(paths) > 250000:raise ValueError('journal restore inventory bound')
    expected = expected or {}
    if not set(expected).issubset({p.name for p in paths}):
        raise ValueError('sealed journal entry missing')

    def read(path):
        with path.open('rb') as stream:
            raw=stream.read()
            # File cache belongs to the job's cgroup on this cluster. Releasing
            # verified pages avoids keeping the 88GB history resident.
            if hasattr(os,'posix_fadvise'):
                os.posix_fadvise(stream.fileno(),0,0,os.POSIX_FADV_DONTNEED)
        return raw

    def one(path):
        if time.monotonic() >= until:raise TimeoutError('journal restore deadline')
        identity=path.stem
        if (not identity or len(identity)>120 or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in identity)):
            raise ValueError('invalid replay publication identity')
        if path.is_symlink() or path.stat().st_size > PUBLICATION_LIMIT:
            raise ValueError('unsafe local replay publication')
        raw = read(path)
        digest = hashlib.sha256(raw).hexdigest()
        if path.name in expected and digest != expected[path.name]:
            raise ValueError('sealed journal publication changed')
        if path.name not in expected:
            value=json.loads(raw)
            if json.dumps(value,sort_keys=True).encode()!=raw:
                raise ValueError('local replay publication bytes changed')
        remote = durable/path.name
        if remote.is_symlink():raise ValueError('unsafe replay publication')
        if remote.exists():
            if read(remote) != raw:raise ValueError('durable replay publication changed')
        else:
            durable.mkdir(parents=True, exist_ok=True)
            _atomic_bytes(remote, raw)
        # A sealed ack binds the exact encoded bytes already validated by the
        # old publisher. Old seals have no such proof: keep their full validator.
        return path.name, digest

    # executor.map consumes lazily from this fixed-size input; only hashes return.
    with ThreadPoolExecutor(max_workers=workers) as pool:
        result = dict(pool.map(one, paths))
        # Copy/hash phase completes first: legacy roots can reference chunks
        # missing from the durable tree at the beginning of restore.
        def legacy(path):
            if path.name in expected:return
            if time.monotonic() >= until:raise TimeoutError('journal restore deadline')
            value=json.loads(read(path))
            if isinstance(value,dict) and value.get('schema')=='p7-chunked-publication-v1':
                read_publication(durable/path.name)
        for _ in pool.map(legacy,paths):pass
    if time.monotonic() >= until:raise TimeoutError('journal restore deadline')
    return result
