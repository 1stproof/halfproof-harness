"""Manifest-resolved compute entry; intentionally no legacy credential loader."""
import json
import os
from pathlib import Path
import sys
from .deployment import preflight, ownership, DeploymentRefused


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    from .startup_log import phase
    phase('job entry: release verified by wrapper, node '+os.environ.get('SLURMD_NODENAME','?'))
    manifest = json.loads(Path(args[0]).read_text())
    if manifest.get('action')=='chain':
        from .chain import resume
        return resume(manifest['chain_registry'],waited_owner=manifest.get('chain_waited_owner'))
    if manifest.get('action')=='allocation-migration':
        from .migration import receive
        return receive(manifest)
    if manifest.get('action') in ('pilot-cohort','production-cohort','swarm-check-cohort','solo-check-cohort','closing-check-cohort','prod-check-cohort','open-check-cohort','open-production-cohort','prod-continue-cohort'):
        from .deployment import require_compute
        if manifest['action'] == 'swarm-check-cohort':
            from .swarm_check import validate
        elif manifest['action'] == 'solo-check-cohort':
            from .solo_check import validate
        elif manifest['action'] == 'closing-check-cohort':
            from .closing_check import validate
        elif manifest['action'] == 'prod-check-cohort':
            from .prod_check import validate
        elif manifest['action'] == 'open-check-cohort':
            from .open_check import validate
        elif manifest['action'] == 'open-production-cohort':
            from .open_production import validate
        elif manifest['action'] == 'prod-continue-cohort':
            from .prod_continue import validate
        elif manifest['action'] == 'production-cohort':
            from .production import validate
        else:
            from .pilot import validate
        require_compute()
        validate(manifest)
        expected=Path('/tmp')/('v3-'+manifest['profile']+'-'+manifest['cohort_id'])
        if Path(manifest['local_root'])!=expected:
            raise DeploymentRefused('fixed unique cohort scratch path required')
        expected.mkdir(mode=0o700,exist_ok=False)
    # Production cohorts cannot consume the checks' global fault-injection file.
    # Inherited by publisher and owned transport/session children.
    if manifest.get('action') in ('production-cohort','open-production-cohort','prod-continue-cohort'):
        os.environ['V3_PRODUCTION_PROVIDER_FAULTS_DISABLED']='1'
    release, scratch, mounts = preflight(manifest)
    phase('preflight done')
    with ownership(scratch):
        if manifest.get('action') in ('production-cohort','open-production-cohort','prod-continue-cohort'):
            from .chain import register
            register(manifest)
        if manifest.get('action') in ('pilot-cohort','production-cohort','swarm-check-cohort','solo-check-cohort','closing-check-cohort','prod-check-cohort','open-check-cohort','open-production-cohort','prod-continue-cohort'):
            from .pilot_job import run
            return run(manifest,release,scratch)
        if manifest.get('action') == 'fake-cohort':
            from .cohort_job import run
            run(manifest, release, scratch)
            return 0
        if manifest.get('action') != 'preflight':
            raise DeploymentRefused('undeclared deployment action')
        print(json.dumps(dict(status='preflight-passed', release=manifest['release_sha256'],
                              node=os.environ['SLURMD_NODENAME'], fixture_mounts=len(manifest.get('fixtures', [])))))
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
