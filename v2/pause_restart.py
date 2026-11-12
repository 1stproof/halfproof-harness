"""Fresh controller entry for same-job operator state restoration."""
import json
import os
from pathlib import Path
import signal
import sys
from .pause import RunClock, PauseControl
from .deployment import ownership, require_compute, DeploymentRefused
from .cohort_runtime import CohortRuntime
from .pilot_job import credential_loader


def restore_paused(control, *, fake_providers=None, live_transport=None):
    record=json.loads((Path(control['root'])/'controller-handoff.json').read_bytes())['record']
    if record['state'].get('operator_clock')!=control['clock']:
        raise DeploymentRefused('resume clock differs from sealed handoff')
    clock=RunClock(**control['clock'])
    c=CohortRuntime.restore(control['root'],handoff_sha256=control['handoff'],
        fake_providers=fake_providers,clock=clock,live_transport=live_transport)
    try:
        # Restore is complete before the sole admission barrier is removed. Other
        # accounting/billing faults remain untouched.
        with c.store.transaction() as db:
            db.execute("DELETE FROM settings WHERE key='operator-paused'")
            c.store.event(db,'operator-resume',dict(job=os.environ['SLURM_JOB_ID']))
        clock.resume()
        c.state['operator_clock']=clock.snapshot()
        c.checkpoint()
        pause=PauseControl(c,control['manifest'],clock=clock)
        pause.ack=control['ack'];pause.publish()
        pause.log('resumed',excluded_seconds=clock.excluded,release_sha256=c.state['release_sha256'])
        return c,pause
    except BaseException:
        # Initialization can fail after restore returned a live owner, before
        # the shared entry receives it. Never leak its services or admission.
        c.store.block('resume-initialization-failed')
        c.cancelled=True
        c.shutdown()
        raise


def main():
    require_compute()
    path=Path(sys.argv[1]);control=json.loads(path.read_bytes());manifest=control['manifest']
    owner=json.loads((Path(manifest['durable_root'])/'operator-control/status.json').read_bytes())
    if owner['job']!=str(os.environ['SLURM_JOB_ID']) or owner['cohort']!=manifest['cohort_id']:
        raise DeploymentRefused('resume allocation differs from published controller owner')
    def initialize():
        # Keep exact original credential identity; no credential discovery.
        transport=dict(load_key=credential_loader(manifest['credential_file']),
            credential_id=manifest['profile']+'-'+manifest['cohort_id'],
            prices_verified=True,metadata_lookup_free=True)
        return restore_paused(control,live_transport=transport)
    with ownership(control['root']):
        if (control.get('replacement') or {}).get('migration'):
            from .migration import transfer_owner
            stopped=transfer_owner(control)
            if stopped is None:return 0
            # No receiver can own the state: restore here. A stop received during
            # the transfer is replayed into the normal lifecycle, which salvages.
            restore=initialize
            def initialize():
                owner=restore()
                if stopped:signal.raise_signal(stopped)
                return owner
        from .pilot_job import controller_lifecycle
        return controller_lifecycle(manifest, control['root'], initialize)

if __name__=='__main__':raise SystemExit(main())
