"""Best-effort salvage on cancel/SIGTERM, and settlement of held liability.

Both paths are bounded in wall time and never raise: a cancelled run must still
reap.  Settlement goes through AccountingStore.settle so the conservation
invariants hold; an attempt whose lookup fails stays unresolved (liability kept).
"""
import shutil
import time
from pathlib import Path

from .accounting import micros

TRANSCRIPT_DIRS = ('qwen-state', 'role-output')


def save_transcripts(runtime_path, durable_dir, *, seconds=60, clock=time.monotonic):
    """Copy every transcript folder (qwen-state, role-output) found anywhere under the
    runtime root -- swarm workers/*/session-*, the primary writer's session-*, solo
    runtimes -- to durable_dir, keeping relative paths; stop at the time bound."""
    import os
    until = clock() + seconds
    copied = skipped = 0
    root = Path(runtime_path)
    if not root.is_dir():
        return dict(copied=0, skipped=0, timed_out=False)
    for here, dirs, files in os.walk(root):
        dirs.sort()
        if Path(here).name not in TRANSCRIPT_DIRS and not any(p in TRANSCRIPT_DIRS for p in Path(here).relative_to(root).parts):
            continue
        for name in sorted(files):
            if clock() >= until:
                return dict(copied=copied, skipped=skipped, timed_out=True)
            f = Path(here) / name
            dest = Path(durable_dir) / f.relative_to(root)
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(f, dest)
                copied += 1
            except OSError:
                skipped += 1
    return dict(copied=copied, skipped=skipped, timed_out=False)


def bounded_lookup(lookup, generation, timeout):
    """One lookup attempt that returns within timeout seconds (daemon thread)."""
    import threading
    box = {}
    def run():
        try:box['r'] = lookup(generation)
        except Exception:box['r'] = None
    t = threading.Thread(target=run, daemon=True)
    t.start(); t.join(max(0, timeout))
    return box.get('r')


def settle_unresolved(store, lookup, *, seconds=120, zero_ungenerated=False, dry_run=False, clock=time.monotonic):
    """One pass over unresolved attempts: settle each at its OpenRouter total_cost.

    zero_ungenerated: attempts with no provider generation id settle at $0 (dead runs).
    Returns a summary in microdollars."""
    until = clock() + seconds
    out = dict(attempts=0, settled=0, settled_microusd=0, released_microusd=0,
               zero_ungenerated=0, failed=0, timed_out=False, held_before_microusd=0)
    # Provisionally settled replies are still looked up; their real record corrects the amount.
    provisional = store.provisional_ids() if hasattr(store, 'provisional_ids') else set()
    rows = [r for r in store.export()['attempts'] if r['state'] == 'unresolved' or r['id'] in provisional]
    out['attempts'] = len(rows)
    out['held_before_microusd'] = sum(r['liability'] for r in rows)
    for r in rows:
        if clock() >= until:
            out['timed_out'] = True
            break
        cost = None
        if r.get('lookup_active'):
            out['failed'] += 1  # a live metadata lookup owns it
            continue
        if not r['generation']:
            if not zero_ungenerated:
                continue
            cost = 0
            out['zero_ungenerated'] += 1
        else:
            record = bounded_lookup(lookup, r['generation'], min(15, until - clock()))
            if isinstance(record, dict) and record.get('total_cost') is not None:
                try:
                    cost = micros(record['total_cost'])
                except ValueError:
                    cost = None
                # A placeholder must not finalize a provisional estimate and end
                # its correction horizon. Explicitly complete zero bills are real.
                if cost == 0 and r.get('estimate') and record.get('complete') is not True:
                    cost = None
        if cost is None:
            out['failed'] += 1
            continue
        if not dry_run:
            try:
                store.settle(r['id'], cost)
            except Exception:
                out['failed'] += 1
                continue
        out['settled'] += 1
        out['settled_microusd'] += cost
        out['released_microusd'] += max(0, r['liability'] - cost)
    return out
