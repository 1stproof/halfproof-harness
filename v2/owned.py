"""Owned subprocess handle shared by sandbox workers and offline fault probes.
No attach/stop-by-PID or discovery API. The caller supplies a sandbox command;
only a child created here is signalable. A stopped handle is never reused.
"""
import os
import signal
import subprocess
import time
from pathlib import Path

class OwnedProcess:
    def __init__(self, command, *, env=None, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL):
        self.process=subprocess.Popen(command,env=env,stdin=stdin,stdout=stdout,stderr=stderr,
                                      start_new_session=True,text=True)
        self.stopped=False
    def alive(self): return self.process.poll() is None
    def stop(self):
        if self.stopped: return
        def descendants(pid):
            children=set()
            try:
                for task in Path(f'/proc/{pid}/task').iterdir():
                    try: children.update(map(int,(task/'children').read_text().split()))
                    except (FileNotFoundError,ProcessLookupError):pass
            except (FileNotFoundError,ProcessLookupError):return []
            return list(children)+[child for child_pid in children for child in descendants(child_pid)]
        tracked=descendants(self.process.pid) if self.process.poll() is None else []
        owned_fds=[]
        for pid in tracked:
            try:owned_fds.append(os.pidfd_open(pid))
            except ProcessLookupError:pass
        # Process group identity is retained only for this child. If it has
        # exited, its bubblewrap child dies with parent; no stale PID signal.
        if self.process.poll() is None:
            os.killpg(self.process.pid,signal.SIGTERM)
            try: self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid,signal.SIGKILL); self.process.wait(timeout=3)
        else: self.process.wait()
        # The outer bwrap launcher can exit before namespace init finishes.
        # pidfds captured from this child's tree prevent both orphaning and PID reuse.
        for fd in owned_fds:
            try:signal.pidfd_send_signal(fd,signal.SIGKILL)
            except ProcessLookupError:pass
            finally:os.close(fd)
        deadline=time.monotonic()+3
        while any(Path(f'/proc/{pid}').exists() for pid in tracked) and time.monotonic()<deadline:
            time.sleep(.02)
        if any(Path(f'/proc/{pid}').exists() for pid in tracked):
            raise RuntimeError('owned namespace descendants not reaped')
        for pipe in (self.process.stdin,self.process.stdout,self.process.stderr):
            if pipe is not None: pipe.close()
        self.stopped=True
    def notice(self,notice):
        raise NotImplementedError('transport adapter must implement notice')


def run_owned(command, *, env=None, timeout=None, stdout=subprocess.DEVNULL,
              stderr=subprocess.DEVNULL, capture_output=False):
    """Synchronous sandbox command; namespace cleanup precedes caller teardown."""
    if capture_output:stdout=stderr=subprocess.PIPE
    handle=OwnedProcess(command,env=env,stdin=subprocess.DEVNULL,stdout=stdout,stderr=stderr)
    try:
        out,err=handle.process.communicate(timeout=timeout)
        return subprocess.CompletedProcess(command,handle.process.returncode,out,err)
    finally:handle.stop()
