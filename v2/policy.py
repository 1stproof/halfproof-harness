"""Offline executable specification. Not wired to inherited launchers.
Time is supplied by caller in seconds; money is integer microdollars.
"""
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path

SESSION = dict(k3=7200, dsf=7200, qwen24t=7200, dsp=7200, glm=3600)


def cli_args(model):
    return ['--model', model, '--input-format', 'stream-json',
            '--output-format', 'stream-json', '--max-tool-calls', '-1']


def user_message(text):
    return {'type': 'user', 'message': {'role': 'user', 'content': text}}


# OpenRouter's in-flight credit check (2026-10-11 campaign): a 402 that asks to retry after
# in-flight requests settle is a transient admission limit, not a billing failure. Treating it
# as billing paused the whole arm with no resume path and every later session failed.
INFLIGHT_402 = ('in-flight requests', 'in-flight request', 'inflight requests')


def error_class(status, detail=None):
    if status == 402:
        text = str(detail or '').lower()
        try:
            source = json.loads(detail).get('error',{}).get('metadata',{}).get('limit_source')
            if source == 'openrouter_in_flight_budget':return 'rate'
            if source in ('openrouter_credits','openrouter_key_limit'):return 'billing'
        except (ValueError,TypeError,AttributeError):pass
        return 'rate' if any(k in text for k in INFLIGHT_402) else 'billing'
    if status == 429:
        return 'rate'
    if status in (408, 504, 'timeout'):
        return 'timeout'
    if isinstance(status, int) and 500 <= status < 600:
        return 'provider'
    return 'configuration'


def backoff(attempt):
    return min(600, 60 * 2 ** min(attempt, 4))


@dataclass
class Ledger:
    """Cumulative run-local estimates, one immutable original budget."""
    cap: int
    settled: dict = field(default_factory=dict)

    @property
    def spent(self):
        return sum(self.settled.values())

    @property
    def settled_spend(self):
        return self.spent

    def snapshot(self):
        return {'original_budget': self.cap, 'settled': dict(self.settled)}

    @classmethod
    def restore(cls, snapshot):
        if set(snapshot) != {'original_budget', 'settled'}:
            raise ValueError('invalid cumulative ledger')
        cap, settled = snapshot['original_budget'], snapshot['settled']
        if type(cap) is not int or cap <= 0 or not isinstance(settled, dict):
            raise ValueError('invalid ledger')
        if any(not isinstance(k, str) or type(v) is not int or v < 0 for k,v in settled.items()):
            raise ValueError('invalid estimate')
        return cls(cap, dict(settled))

    def settle(self, request, actual):
        if type(actual) is not int or actual < 0:
            raise ValueError('invalid estimate')
        if request in self.settled and self.settled[request] != actual:
            raise ValueError('conflicting settlement')
        self.settled[request] = actual

    def stop_due(self, worst_request):
        return self.spent + worst_request >= self.cap

    def save(self, path):
        from .control import atomic_json
        atomic_json(path, self.snapshot())

    def agrees(self, other):
        return abs(self.spent - other) <= max(1_000_000, self.cap // 100)


def campaign_allowance(allocations, external=0):
    if external < 0 or any(x < 0 for x in allocations):
        raise ValueError('negative allocation')
    return sum(allocations) + external


@dataclass
class Run:
    lane: str
    start: float
    wall: float
    cap: int
    session_start: float = 0
    last_write: float = 0
    last_notes: float = 0
    phase: str = 'research'
    audit: bool = False
    writeup: bool = False
    notice_due: float | None = None
    notice_delivered: bool = False
    cycles: int = 0
    verify_start: float | None = None
    outage_start: float | None = None
    crashes: list = field(default_factory=list)
    worker_crashes: list = field(default_factory=list)
    loops: dict = field(default_factory=dict)
    recovery_start: float | None = None
    wall_stopped: bool = False
    session_limit: int | None = None
    outage_kind: str | None = None
    writeup_at: float | None = None

    def __post_init__(self):
        if self.lane not in SESSION or self.wall <= 0 or self.cap <= 0:
            raise ValueError('invalid run')
        self.session_start = self.last_write = self.last_notes = self.start

    def write(self, now, notes=False):
        self.last_write = now
        if notes:
            self.last_notes = now

    def restart(self, now):
        if self.outage_start is not None:
            raise ValueError('cannot admit a new session during outage')
        self.session_start = self.last_write = self.last_notes = now
        self.wall_stopped = False
        self.phase = ('verification' if self.verify_start is not None else
                      'audit-polish' if self.audit else 'write-up' if self.writeup else 'research')
        # A persistent write-up notice must be delivered to the new session too.
        self.notice_delivered = False

    def tick(self, now, spend, boundary=True):
        if self.phase == 'done':
            return self.phase
        if now >= self.start + self.wall or spend >= self.cap:
            self.phase = 'done'
            return 'stop'
        if self.phase == 'paused-alert':
            return self.phase
        if not self.writeup and (now >= self.start + (self.writeup_at if self.writeup_at is not None else .9 * self.wall) or spend >= .9 * self.cap):
            self.writeup = True
            self.notice_due = min(now, self.start + .9 * self.wall)
            self.notice_delivered = False
        age = now - self.session_start
        # Outages freeze activity clocks, never the session's hard wall.
        if age >= (self.session_limit or SESSION[self.lane]) and not self.wall_stopped:
            return 'stop-wall-during-outage' if self.outage_start is not None else 'restart-wall'
        if self.outage_start is not None:
            if now - self.outage_start >= 5400:
                self.phase = 'paused-alert'
                return 'billing-exhausted'
            return 'billing-wait'
        if self.recovery_start is not None and now - self.recovery_start >= 600:
            self.phase = 'paused-alert'
            return 'loop-recovery-exhausted'
        if self.writeup and not self.notice_delivered:
            if now - self.notice_due >= 240:
                return 'restart-for-writeup'
            return 'deliver-writeup'
        if self.verify_start is not None and now - self.verify_start >= 1800:
            return 'verification-timeout'
        if now - self.last_write >= 2100 or now - self.last_notes >= 3900:
            if boundary or now - self.last_write >= 2700 or now - self.last_notes >= 4500:
                return 'restart-checkpoint'
            return 'wait-response-boundary'
        if now - self.last_write >= 1800 or now - self.last_notes >= 3600:
            self.phase = 'grace'
            return 'checkpoint-grace'
        if age >= (self.session_limit or SESSION[self.lane]) - 300:
            return 'checkpoint-wall'
        if now - self.last_write >= 1200 or now - self.last_notes >= 1800:
            self.phase = 'checkpoint-requested'
            return 'checkpoint'
        self.phase = ('verification' if self.verify_start is not None else
                      'audit-polish' if self.audit else 'write-up' if self.writeup else 'research')
        return 'continue'

    def billing(self, now, kind='billing'):
        if self.outage_start is None:
            self.outage_start = now
        self.outage_kind = kind
        self.phase = 'billing-pause'

    def healthy(self, now):
        if self.outage_start is None:
            raise ValueError('no outage to resume')
        delta = now - self.outage_start
        self.last_write += delta
        self.last_notes += delta
        self.outage_start = None
        self.outage_kind = None
        self.phase = ('verification' if self.verify_start is not None else
                      'audit-polish' if self.audit else 'write-up' if self.writeup else 'research')

    def claim(self, now):
        if self.cycles >= 2:
            self.phase = 'paused-alert'
            return 'alert'
        self.cycles += 1
        self.verify_start = now
        self.phase = 'verification'
        return 'fresh-verification'

    def verdict(self, valid):
        self.verify_start = None
        self.phase = ('done' if valid else 'paused-alert' if self.cycles >= 2 else
                      'audit-polish' if self.audit else 'write-up' if self.writeup else 'research')

    def wall_cleanup_ack(self):
        """Runtime calls only after outage-time worker termination is confirmed."""
        self.wall_stopped = True

    def recovery_succeeded(self, checkpoint, different_successful_operation):
        if checkpoint and different_successful_operation:
            self.recovery_start = None

    def worker_crash(self, now):
        self.worker_crashes = [t for t in self.worker_crashes if now-t < 3600]+[now]
        if len(self.worker_crashes) >= 5:
            self.phase = 'paused-alert'
            return 'pause-alert'
        return 'restart-backoff'

    def crash(self, now, worker_alive):
        self.crashes = [t for t in self.crashes if now - t < 3600] + [now]
        if len(self.crashes) >= 3:
            self.phase = 'paused-alert'
            return 'pause-alert'
        return 'stop-worker-and-restart' if worker_alive else 'restart-once'

    def loop(self, fingerprint, now=0):
        n = self.loops.get(fingerprint, 0) + 1
        self.loops[fingerprint] = n
        if n >= 4:
            self.phase = 'paused-alert'
            return 'pause-alert'
        if n == 3:
            self.recovery_start = now
            return 'bounded-recovery'
        return 'fresh-targeted'


from .artifacts import scan


def changed(before, after):
    return {p for p, digest in after.items() if before.get(p) != digest}
