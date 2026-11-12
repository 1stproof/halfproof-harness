"""Bulky production output belongs on shared scratch, never the home quota."""
import os
from pathlib import Path
from .deployment import DeploymentRefused


def shared_output(path):
    """Optionally confine output to HARNESS_OUTPUT_ROOTS (colon-separated directories)."""
    path=Path(path).resolve()
    roots=[r for r in os.environ.get('HARNESS_OUTPUT_ROOTS','').split(':') if r]
    if roots and not any(path.is_relative_to(Path(root).resolve()) for root in roots):
        raise DeploymentRefused('production output must be under HARNESS_OUTPUT_ROOTS: '+':'.join(roots))
    return path


def scheduler_output(manifest):
    durable=shared_output(manifest['durable_root'])
    # Outside the as-yet uncreated run root; production requires a new root.
    directory=durable.parent/'scheduler-logs'
    directory.mkdir(parents=True,exist_ok=True)
    cohort=manifest['cohort_id']
    if not cohort or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in cohort):
        raise DeploymentRefused('unsafe scheduler log cohort name')
    return str(directory/(cohort+'-%j.log'))
