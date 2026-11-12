"""Cohort registry and scientific snapshots inside the single publisher.

Only the trusted controller dispatches these operations. No worker, inference,
credential discovery, timeout withdrawal, or judging transport is exposed here.
"""
from dataclasses import asdict
import json
import os
from pathlib import Path
import stat

from .closure_io import ClosureError, MAX_FILE, MAX_TOTAL, MAX_FILES, read_file, sha256, canonical
from .freeze import FrozenCohort, FenceReceipt

OPERATIONS = ('cohort-register', 'cohort-start', 'cohort-freeze', 'cohort-status')


FREEZE_BYTES = 8 * 1024 * 1024 * 1024
FREEZE_FILES = MAX_FILES - 1000  # room for summaries and freeze/closure files


def read_tree(root, *, transcripts_only=False, budget=None):
    """Bounded no-follow inventory, including hidden scientific files. Over-budget
    files become omission markers; a freeze never fails on size or count."""
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ClosureError('scientific section root must be a real directory')
    budget = budget if budget is not None else dict(bytes=FREEZE_BYTES, files=FREEZE_FILES)
    files = {}
    skipped = []
    for directory, dirs, names in os.walk(root, followlinks=False):
        relative = Path(directory).relative_to(root)
        if len(relative.parts) > 16:
            dirs[:] = []; skipped.append(str(relative) + '/ (depth)'); continue
        for name in list(dirs):
            if not stat.S_ISDIR(os.lstat(Path(directory)/name).st_mode):dirs.remove(name)
        for name in sorted(names):
            if transcripts_only and Path(name).suffix not in ('.jsonl', '.txt'):
                continue
            key = str(relative/name)
            mode = os.lstat(Path(directory)/name)
            if not stat.S_ISREG(mode.st_mode):
                skipped.append(key + ' (not a regular file)'); continue
            size = mode.st_size
            # Review a505d2e #5: keep 10% of the file budget for markers, summaries and metadata.
            if size > MAX_FILE or size > budget['bytes'] or budget['files'] <= budget.setdefault('reserve', max(2, budget['files'] // 10)):
                # Production dsf-swarm (00:40) and open (07:06): an oversized file or
                # section failed the whole freeze. Record an omission instead.
                if budget['files'] > budget['reserve'] // 2 + 2:  # markers never eat the last half of the reserve
                    files[key + '.omitted.json'] = canonical({'omitted': key, 'bytes': size,
                        'reason': 'exceeds per-file or freeze publication budget'})
                    budget['files'] -= 1
                else:
                    skipped.append(key)
                continue
            data = read_file(root, key, MAX_FILE)
            budget['bytes'] -= len(data); budget['files'] -= 1
            files[key] = data
    if skipped:
        files['.harness-omitted-summary.json'] = canonical({'omitted': skipped[:20000], 'count': len(skipped)})
        budget['files'] -= 1
    return files


def perform(operation, payload):
    if operation not in OPERATIONS:
        raise ValueError('unknown cohort publication operation')
    root, artifacts = payload['root'], payload['artifact_root']
    if operation == 'cohort-register':
        registration = dict(payload['registration'])
        registration['problem'] = registration.pop('problem_text').encode()
        cohort = FrozenCohort.register(root, artifact_root=artifacts, **registration)
        return cohort.embargo_status(payload['now'])
    cohort = FrozenCohort(root, artifact_root=artifacts)
    if operation == 'cohort-start':
        # A replay after commit but before operation-result publication is safe.
        arm = cohort._load()['arms'][payload['arm']]
        if arm['state'] == 'active' and arm['started_at'] == payload['started_at']:
            return {'started_at': arm['started_at']}
        cohort.mark_started(payload['arm'], started_at=payload['started_at'])
        return {'started_at': payload['started_at']}
    if operation == 'cohort-status':
        return cohort.embargo_status(payload['now'])
    fence = FenceReceipt(**payload['fence'])
    state = cohort._load()
    arm = state['arms'][payload['arm']]
    if arm['state'] == 'frozen':
        manifest = arm['manifest']
        if (manifest['fence'] != json.loads(json.dumps(asdict(fence)))
                or manifest['frozen_at'] != payload['frozen_at']):
            raise ClosureError('freeze replay changed authenticated fence or clock')
        cohort._verify_arm(arm)
        return {'manifest': manifest, 'snapshot_id': arm['snapshot_id']}
    sections = {key: {} for key in ('reports', 'drafts', 'board', 'supporting')}
    budget = dict(bytes=FREEZE_BYTES, files=FREEZE_FILES)
    order = {'reports': 0, 'board': 1, 'drafts': 2, 'supporting': 3}
    for section, sources in sorted(payload['sections'].items(), key=lambda kv: order.get(kv[0], 9)):
        if section not in sections:
            raise ClosureError('undeclared scientific section')
        for row in sources:
            for name, data in read_tree(row['root'], transcripts_only=row.get('transcripts_only', False), budget=budget).items():
                key = row.get('prefix', '') + name
                if key in sections[section]:
                    raise ClosureError('duplicate scientific artifact')
                sections[section][key] = data
    manifest = cohort.freeze(payload['arm'], sections=sections, fence=fence,
        frozen_at=payload['frozen_at'], closure_manifest=payload['closure_manifest'],
        ledger_export_id=payload['ledger_export_id'],board_versions=payload.get('board_versions',()))
    return {'manifest': manifest, 'snapshot_id': sha256(canonical(manifest))}
