"""Operator wall-clock closing appointment; never supplies scientific content."""
import math
import time
from .deployment import DeploymentRefused


def tick(control):
    path = control.directory / 'closing-deadline.json'
    if not path.exists():
        return
    import json
    try:
        row = json.loads(path.read_bytes())
        valid = (isinstance(row, dict) and set(row) == {'schema', 'cohort', 'close_at', 'deliver_by'} and
                 row['schema'] == 1 and row['cohort'] == control.c.cohort and
                 all(type(row[k]) in (int, float) and math.isfinite(row[k]) for k in ('close_at', 'deliver_by')) and
                 row['close_at'] < row['deliver_by'])
    except (ValueError, OSError):
        valid = False
    if not valid:
        # An administrative input error must not stop research or publication.
        if not getattr(control, '_appointment_rejected', False):
            control.log('closing-deadline-rejected', path=str(path))
            control._appointment_rejected = True
        return
    start, end = row['close_at'], row['deliver_by']
    if time.time() >= end:
        deliver(control, row)
    if time.time() < start or control.phase != 'running' or control.c.complete:
        return
    if not control.c.runtimes:
        return  # Startup has not created the services that must close yet.
    if control.c.state.get('closing_appointment') == row:
        return
    # Each executor's transition is persistent and idempotent. Reapply after a
    # crash between individual arms; do not use a once flag ahead of the calls.
    for runtime in control.c.runtimes.values():
        if not runtime.watchdog.state.get('terminal'):
            runtime.request_delivery('operator-delivery-deadline')
    control.c.state['closing_appointment'] = row
    control.c.checkpoint()
    control.log('closing-deadline-reached', close_at=start, deliver_by=end)


RETRY_SECONDS = 60


def deliver(control, row):
    """Release the newest draft once at deliver-by; a failure never stops the run.

    A failed release is recorded and retried later. The first complete release
    is the deliver-by release; later ticks do not re-snapshot or re-hash.
    """
    import json
    marker = control.directory / 'deadline-delivery-released.json'
    if getattr(control, '_deadline_released', None) == row:
        return
    try:
        if marker.exists() and json.loads(marker.read_bytes()).get('appointment') == row:
            control._deadline_released = row
            return
    except (OSError, ValueError, AttributeError):
        pass  # an unreadable marker is not proof of release; release again
    if time.time() < getattr(control, '_deadline_retry_at', 0):
        return
    try:
        from .deadline_delivery import release_runtime
        receipts = [release_runtime(runtime, control.c.durable, control.c.problem.removeprefix('b3-'),
                                    reason='operator deliver-by reached')
                    for runtime in control.c.runtimes.values()]
    except Exception as exc:
        error = type(exc).__name__+': '+str(exc)[:500]
        control._deadline_retry_at = time.time()+RETRY_SECONDS
        try:
            control.log('deadline-delivery-failed', error=error, retry_in=RETRY_SECONDS)
        except Exception:
            pass
        try:
            from .closure_io import atomic_json
            atomic_json(control.directory/'deadline-delivery-error.json',
                        dict(schema=1, appointment=row, at=time.time(), error=error))
        except Exception:
            pass
        return
    control._deadline_released = row
    try:
        from .closure_io import atomic_json
        atomic_json(marker, dict(schema=1, appointment=row, at=time.time(),
                                 releases=[r.get('release_sha256') for r in receipts]))
        control.log('deadline-delivery-released', releases=len(receipts))
    except Exception:
        pass  # the in-memory flag still prevents repetition in this process
