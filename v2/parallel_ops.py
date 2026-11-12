"""Operational isolation and retry spreading for the authorised parallel campaign."""
from contextlib import contextmanager
import fcntl
import hashlib
import math
from pathlib import Path
import random
import time


def retry_delay(base, retry_after=0, rng=None):
    """Add independent jitter without violating a provider's Retry-After floor."""
    base = float(base)
    floor = float(retry_after)
    if not math.isfinite(base) or base < 0: raise ValueError('invalid backoff')
    if not math.isfinite(floor) or floor < 0: floor = 0
    return max(base, floor) + (rng or random.SystemRandom()).uniform(0, min(60, base / 2))


def route_offset(cohort):
    return int.from_bytes(hashlib.sha256(str(cohort).encode()).digest()[:8], 'big') if cohort else 0


@contextmanager
def exclusive(path, *, wait_seconds=0):
    """Cross-process lock with a bounded wait and an explicit contention failure."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as stream:
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline: raise TimeoutError('operational lock busy: ' + str(path))
                time.sleep(min(.1, max(0, deadline - time.monotonic())))
        try: yield stream
        finally: fcntl.flock(stream, fcntl.LOCK_UN)


def validate_isolation(manifests):
    seen = {key:set() for key in ('problem_id','cohort_id','local_root','durable_root')}
    for m in manifests:
        if m.get('live_arms') != ['mixed-swarm']: raise ValueError('parallel campaign requires mixed-swarm only')
        for key, values in seen.items():
            value = str(Path(m[key]).resolve()) if key.endswith('_root') else m[key]
            if value in values: raise ValueError('parallel identity reused: ' + key)
            values.add(value)
    for key in ('local_root','durable_root'):
        paths = [Path(p) for p in seen[key]]
        if any(a in b.parents for a in paths for b in paths if a != b):
            raise ValueError('nested parallel roots: ' + key)
    return True


STAGES = ((2,3), (4,7,8), (9,10))


def staged_layout(stages=STAGES, completed_prior=()):
    """Two fresh canaries and five live runs; retain an already-ended p02 as history."""
    stages=tuple(tuple(wave) for wave in stages)
    prior=tuple(completed_prior)
    flat=[n for wave in stages for n in wave]
    if prior not in ((),(2,)) or len(stages)!=3 or tuple(len(w) for w in stages)!=(2,3,2-len(prior)):
        raise ValueError('invalid staged capacity layout')
    if any(type(n) is not int for n in flat) or len(set(flat))!=len(flat) or set(flat)|set(prior)!={2,3,4,7,8,9,10} or set(flat)&set(prior):
        raise ValueError('layout must cover exactly the eligible remaining problems once')
    if not prior and 2 not in stages[0]:raise ValueError('live adopted p02 belongs in first stage')
    return stages


def expansion_gate(rows, *, min_seconds=7200):
    """Conservative measured operational gate; no mathematical judgment."""
    reasons=[]
    for row in rows:
        name=row['problem_id']
        if row.get('measurement_error'): reasons.append(name+': measurement unavailable'); continue
        if row.get('elapsed_seconds',0) < min_seconds: reasons.append(name+': checkpoint not due')
        if row.get('harness_defects'): reasons.append(name+': harness defect')
        if row.get('snapshot_age_seconds',float('inf')) > 900: reasons.append(name+': publication stale')
        if row.get('job_state') != 'RUNNING': reasons.append(name+': job not running')
        reasons.extend(service_gate([row])[1])
        if row.get('attempts',0) < 20: reasons.append(name+': insufficient ordinary-call service')
        if row.get('planner_plans',0) < 1: reasons.append(name+': no published planner plan')
    return not reasons,reasons


def service_gate(rows):
    """Check all active runs and recent deltas so earlier good service cannot mask decay."""
    reasons=[]
    for row in rows:
        name=row['problem_id']
        for label, models in (('cumulative',row.get('models',{})),('recent',row.get('recent_models',{}))):
            for model,metrics in models.items():
                n=metrics.get('attempts',0)
                prefix=name+': '+model+' '+label
                if n >= 20:
                    if metrics.get('429',0)/n > .05: reasons.append(prefix+' 429 >5%')
                    if metrics.get('transport_timeouts',0)/n > .10: reasons.append(prefix+' transport timeout >10%')
                elif metrics.get('429',0) >= 3 or metrics.get('transport_timeouts',0) >= 3:
                    reasons.append(prefix+' repeated provider failures with small sample')
                completed=metrics.get('terminal_sessions',0)
                if completed >= 10 and metrics.get('failed_sessions',0)/completed > .20:
                    reasons.append(prefix+' session failures >20%')
    return not reasons,reasons
