"""Linux child processes with synchronized exit-status collection.

multiprocessing starts scan every process in its global child set. That scan
can run in another controller thread while the owner joins a child. Serialize
waitpid AND the returncode update so an already-reaped child cannot temporarily
appear alive to its owner. This does not alter execution or cleanup deadlines.
"""
import os
import signal
import threading
from multiprocessing.context import ForkProcess as BaseForkProcess
from multiprocessing.context import SpawnProcess as BaseSpawnProcess
from multiprocessing.popen_fork import Popen as ForkPopen
from multiprocessing.popen_spawn_posix import Popen as SpawnPopen


def _reset_child_signals():
    # Controller handlers belong to the controller, not its metadata/compiler
    # children. In particular terminate() must not raise in inherited HTTP
    # destructors or turn child cleanup into an operator stop.
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGALRM):
        signal.signal(sig, signal.SIG_DFL)


os.register_at_fork(after_in_child=_reset_child_signals)


class _SynchronizedPoll:
    def __init__(self, *args, **kwargs):
        self._waitpid_lock = threading.RLock()
        super().__init__(*args, **kwargs)

    def poll(self, flag=os.WNOHANG):
        with self._waitpid_lock:
            return super().poll(flag)

    def _send_signal(self, sig):
        # Do not signal a PID another thread has reaped but not yet published.
        # RLock permits the base error path to wait/poll while holding it.
        with self._waitpid_lock:
            return super()._send_signal(sig)


class _ForkPopen(_SynchronizedPoll, ForkPopen):
    pass


class _SpawnPopen(_SynchronizedPoll, SpawnPopen):
    pass


class ForkProcess(BaseForkProcess):
    @staticmethod
    def _Popen(process_obj):
        return _ForkPopen(process_obj)


class SpawnProcess(BaseSpawnProcess):
    @staticmethod
    def _Popen(process_obj):
        return _SpawnPopen(process_obj)
