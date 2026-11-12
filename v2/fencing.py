"""Owned writer fencing before a physical snapshot; never stops arbitrary PIDs."""
import hashlib
import json
import time
from .owned import OwnedProcess


class FencePending(RuntimeError):
    """Owned writers stopped, but their latest bounded publication awaits ACK."""


class WriterScope:
    def __init__(self, arm_id, *, epoch=0):
        self.arm_id = arm_id
        if type(epoch) is not int or epoch < 0:
            raise ValueError('invalid persisted publication epoch')
        self.epoch = epoch
        self.handles = []
        self.retired_writers = 0
        self.fenced = False

    def retire_stopped(self):
        """Forget only handles whose owned teardown already succeeded.

        Keep the cumulative receipt count without retaining closed servers,
        gateway/watchdog cycles and session payloads for the entire run.
        A failed or incomplete stop remains owned and must still be fenced.
        """
        retained = []
        for handle in self.handles:
            if handle.stopped and not handle.alive():
                self.retired_writers += 1
            else:
                retained.append(handle)
        self.handles[:] = retained

    def register(self, handle):
        if self.fenced or not isinstance(handle, OwnedProcess) or handle.stopped:
            raise ValueError('writer registration refused')
        if handle in self.handles:
            raise ValueError('writer already registered')
        self.handles.append(handle)
        return self.epoch

    def permits_publication(self, epoch):
        return not self.fenced and epoch == self.epoch

    def fence(self, *, admission_closed, active_requests, cancelled_request_ids=()):
        """Caller first closes gateway admission and records bounded cancellations.

        stop() verifies owned descendants are reaped. Any exception prevents a
        successful receipt. All publication services must enforce this scope's
        epoch; admission cannot be re-opened on this scope after fencing.
        """
        if not admission_closed or active_requests:
            raise ValueError('cannot fence active admission/requests')
        self.fenced = True
        self.epoch += 1
        for handle in self.handles:
            handle.stop()
            if handle.alive() or not handle.stopped:
                raise RuntimeError('owned writer survived fencing')
        receipt = dict(arm_id=self.arm_id, publication_epoch=self.epoch,
                       owned_writers=self.retired_writers + len(self.handles), owned_survivors=0,
                       admission_closed=True, active_requests=0,
                       cancelled_request_ids=list(cancelled_request_ids),
                       fenced_at=time.time())
        receipt['sha256'] = hashlib.sha256(json.dumps(receipt, sort_keys=True).encode()).hexdigest()
        return receipt
