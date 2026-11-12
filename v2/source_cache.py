"""Shared version-keyed bytes, hash checked on every read; no generated conclusions.

A per-URL flock coalesces concurrent downloads and identical access failures. Failure
receipts expire, and refresh explicitly bypasses them for disputes/new access routes.
Unversioned URLs are never reused as if they named an immutable version.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
import urllib.parse


def canonical_url(url):
    p = urllib.parse.urlsplit(url)
    if p.scheme == "https" and p.netloc in ("arxiv.org", "export.arxiv.org"):
        return urllib.parse.urlunsplit(("https", "arxiv.org", p.path, p.query, p.fragment))
    return url

FAILURE_TTL = 120


def shared_root(watchdog):
    owner = getattr(watchdog, 'owner', None)
    return (owner.path if owner is not None else watchdog.path) / 'source-cache'


def _write(path, data):
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.source-')
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)


def get(root, url, loader, *, cap, refresh=False, clock=time.time, lock_timeout=10):
    """loader returns (bytes, HTTP headers, final URL); result adds cache status/key."""
    url = canonical_url(url)
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink(): raise ValueError('source cache link refused')
    key = hashlib.sha256(url.encode()).hexdigest()
    paths = {k: root/(key + '.' + k) for k in ('lock', 'body', 'json', 'failure')}
    if any(p.is_symlink() for p in paths.values()): raise ValueError('source cache link refused')
    pinned = bool(re.search(r'/(?:e-print|html)/.+v[1-9]\d*$', url))
    fd = os.open(paths['lock'], os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        deadline=time.monotonic()+lock_timeout
        while True:
            try:
                fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);break
            except BlockingIOError:
                if time.monotonic()>=deadline:raise TimeoutError('source cache lock deadline')
                time.sleep(min(.05,max(0,deadline-time.monotonic())))
        if pinned and not refresh and paths['body'].is_file() and paths['json'].is_file():
            try:
                meta = json.loads(paths['json'].read_text())
                data = paths['body'].read_bytes()
                if meta['url'] == url and len(data) <= cap and hashlib.sha256(data).hexdigest() == meta['sha256']:
                    return data, meta['headers'], meta['final_url'], 'hit', key
            except (OSError, ValueError, KeyError): pass
        if not refresh and paths['failure'].is_file():
            try:
                receipt = json.loads(paths['failure'].read_text())
                if receipt['url'] == url and 0 <= clock() - receipt['at'] < FAILURE_TTL:
                    raise RuntimeError('shared source access failure receipt ' + key)
            except (OSError, ValueError, KeyError): pass
        try:
            data, headers, final_url = loader()
            if not isinstance(data, bytes) or len(data) > cap: raise ValueError('source byte bound exceeded')
            if data.startswith(b'%PDF-'): raise ValueError('PDF discarded; use HTML fallback')
        except Exception as exc:
            # Exception prose can contain credentials/conclusions; keep only its class.
            receipt = dict(url=url, at=clock(), error=type(exc).__name__, id=key)
            _write(paths['failure'], json.dumps(receipt, sort_keys=True).encode())
            raise
        meta = dict(url=url, sha256=hashlib.sha256(data).hexdigest(), bytes=len(data),
                    headers=headers, final_url=final_url, at=clock())
        if pinned:
            _write(paths['body'], data)
            _write(paths['json'], json.dumps(meta, sort_keys=True).encode())
        paths['failure'].unlink(missing_ok=True)
        return data, headers, final_url, 'miss', key
    finally:
        os.close(fd)


SOURCE_CAP = 32*1024*1024


def host_bytes(worker, url, *, refresh=False, verifier=False):
    """Only host downloads enter the shared cache; callers supply no bytes or paths."""
    from .verification_tools import source_urls, fetch
    canonical = canonical_url(url)
    if canonical not in source_urls(canonical):
        raise ValueError('source endpoint requires pinned source URL')
    w = worker.gateway.watchdog
    allowed = (worker.role in ('internal-verifier', 'peer-reviewer') if verifier else
               worker.capability_role == 'research' and not w.state.get('report_lock'))
    if not w.role_admission(worker) or not allowed:
        raise ValueError('authenticated active source capability required')
    remaining = getattr(w, 'stage_deadline', float('inf')) - w.clock() if hasattr(w, 'clock') else 10
    if remaining <= 0:raise ValueError('source stage ended')
    def load():
        if worker.gateway.fake is not None:
            loader = getattr(worker.gateway.fake, 'retrieve_source', None)
            if not callable(loader):raise ValueError('fake source unavailable; network forbidden')
            data = loader(canonical)
        else:
            data = fetch(canonical, timeout=min(10, remaining), cap=SOURCE_CAP)
        return data, {}, canonical
    return get(shared_root(w), canonical, load, cap=SOURCE_CAP, refresh=refresh,lock_timeout=min(10,remaining))
