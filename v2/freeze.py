"""Scientific freeze and same-problem embargo. No inference adapter.

The caller is the trusted controller, never a worker. FenceReceipt is evidence
from P2 admission and P7 ownership services, not a worker-authored JSON file.
Physical isolation remains mandatory: worker namespaces cannot access service
state or snapshot roots; judges receive only read-only snapshot mounts.
"""
from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path

from .closure_contracts import digest, unix_ms, freeze_record, withdrawal_record, contract_directory, registration_record
from .contracts_v3 import contracts_sha256
from .closure_io import ClosureError, atomic_json, canonical, locked, publish_tree, read_file, relative_name, sha256, verify_tree


@dataclass(frozen=True)
class FenceReceipt:
    arm_id: str
    problem_id: str
    admission_closed: bool
    pending_requests: int
    active_writers: int
    namespaces_fenced: bool
    admission_receipt: str
    writer_receipt: str
    cancellation_records: tuple = ()

    def validate(self, arm_id, problem_id):
        if (self.arm_id, self.problem_id) != (arm_id, problem_id):
            raise ClosureError('fence receipt identity mismatch')
        if self.admission_closed is not True or self.namespaces_fenced is not True:
            raise ClosureError('admission and namespace fence must both be closed')
        if type(self.pending_requests) is not int or type(self.active_writers) is not int or self.pending_requests != 0 or self.active_writers != 0:
            raise ClosureError('requests or scientific writers remain active')
        if not self.admission_receipt or not self.writer_receipt:
            raise ClosureError('authenticated fence evidence required')
        if any(not isinstance(x, str) or not x for x in self.cancellation_records) or len(set(self.cancellation_records)) != len(self.cancellation_records):
            raise ClosureError('unique cancellation evidence identifiers required')


class FrozenCohort:
    """Immutable registered roster, persistent terminal states, no resume method."""
    def __init__(self, root, *, artifact_root=None):
        self.root = Path(root).absolute()
        self.artifacts = Path(artifact_root or root).absolute()
        if (self.root / 'cohort.json').exists():
            stored = Path(self._load()['config']['artifact_root'])
            if artifact_root is not None and stored != self.artifacts:
                raise ClosureError('cohort artifact root changed')
            self.artifacts = stored
        if (self.artifacts / 'cohort-state.json').exists():
            with locked(self.root):
                atomic_json(self.root / 'cohort.json', json.loads(read_file(self.artifacts, 'cohort-state.json')))
            if self._load()['config']['artifact_root'] != str(self.artifacts):
                raise ClosureError('durable cohort artifact root changed')

    @classmethod
    def register(cls, root, *, cohort_id, problem_id, problem, arm_ids,
                 withdrawal_authority, withdrawal_deadline, release_sha256, arm_manifest_sha256s,
                 registered_at, latest_start_at, artifact_root=None):
        arms = list(arm_ids)
        if not arms or len(set(arms)) != len(arms) or not withdrawal_authority:
            raise ClosureError('unique registered arms and withdrawal authority required')
        unix_ms(withdrawal_deadline)
        unix_ms(registered_at)
        if set(latest_start_at) != set(arms):
            raise ClosureError('latest start deadline required for every registered arm')
        for value in latest_start_at.values():
            unix_ms(value)
            if not registered_at <= value <= withdrawal_deadline:
                raise ClosureError('latest starts must be within registration/withdrawal review bounds')
        digest(release_sha256)
        if set(arm_manifest_sha256s) != set(arms):
            raise ClosureError('immutable arm manifest hash required for every registration')
        for value in arm_manifest_sha256s.values():
            digest(value)
        for arm in arms:
            relative_name(arm)
            if '/' in arm:
                raise ClosureError('arm identifier must be a basename')
        config = {'schema': 1, 'cohort_id': cohort_id, 'problem_id': problem_id,
                  'problem_sha256': sha256(problem), 'arm_ids': arms,
                  'withdrawal_authority': withdrawal_authority, 'withdrawal_deadline': withdrawal_deadline,
                  'release_sha256': release_sha256, 'arm_manifest_sha256s': arm_manifest_sha256s,
                  'contracts_sha256': contracts_sha256(contract_directory()), 'artifact_root': str(Path(artifact_root or root).absolute())}
        config['registration_record'] = registration_record(config, registered_at=registered_at, latest_start_at=latest_start_at)
        self = cls(root, artifact_root=artifact_root)
        with locked(self.root):
            if (self.root / 'cohort.json').exists():
                if self._load()['config'] != config:
                    raise ClosureError('registered cohort cannot be changed')
                return self
            publish_tree(self.artifacts, 'problem', {'PROBLEM.md': problem})
            publish_tree(self.artifacts, 'registration', {'cohort-registration.json': canonical(config['registration_record'])})
            self._save({'config': config,
                        'arms': {arm: {'state': 'registered'} for arm in arms}, 'grading': None})
        return self

    def _load(self):
        value = json.loads(read_file(self.root, 'cohort.json'))
        if value['config']['contracts_sha256'] != contracts_sha256(contract_directory()):
            raise ClosureError('cohort contract identity changed')
        return value

    def _save(self, state):
        atomic_json(self.artifacts / 'cohort-state.json', state)
        atomic_json(self.root / 'cohort.json', state)

    def mark_started(self, arm_id, *, started_at):
        unix_ms(started_at)
        with locked(self.root):
            state = self._load()
            arm = state['arms'].get(arm_id)
            if arm is None or arm['state'] != 'registered' or state['grading'] is not None:
                raise ClosureError('arm cannot start or resume')
            registration = state['config']['registration_record']
            latest = next(row['latest_start_unix_ms'] for row in registration['arms'] if row['arm_id'] == arm_id)
            if not registration['registered_at_unix_ms'] <= unix_ms(started_at) <= latest:
                raise ClosureError('arm start is outside its registered original window')
            arm.update(state='active', started_at=started_at)
            self._save(state)

    def freeze(self, arm_id, *, sections, fence, frozen_at, closure_manifest,
               withdrawal=None, board_versions=(), ledger_export_id=None):
        """Snapshot each declared scientific section after fencing all writers.

        Sections must include reports, drafts, board and supporting (empty maps
        explicitly represent none). Billing state never belongs in these maps.
        A started withdrawn arm also needs its terminal closure artifact.
        """
        unix_ms(frozen_at)
        if set(sections) != {'reports', 'drafts', 'board', 'supporting'}:
            raise ClosureError('all scientific inventory sections must be declared')
        with locked(self.root):
            state = self._load()
            config = state['config']
            if arm_id not in state['arms']:
                raise ClosureError('unregistered arm')
            if state['grading'] is not None:
                raise ClosureError('no scientific changes after judging admission')
            arm = state['arms'][arm_id]
            if arm['state'] in {'frozen', 'withdrawn'}:
                raise ClosureError('arm already scientifically frozen')
            fence.validate(arm_id, config['problem_id'])
            earliest = unix_ms(arm['started_at']) if 'started_at' in arm else config['registration_record']['registered_at_unix_ms']
            if unix_ms(frozen_at) < earliest:
                raise ClosureError('freeze cannot precede registration or arm start')
            if withdrawal is not None:
                if (withdrawal.get('authority') != config['withdrawal_authority'] or
                        not withdrawal.get('reason') or withdrawal.get('at') != frozen_at):
                    raise ClosureError('explicit authorized withdrawal record required')
            elif arm['state'] != 'active':
                raise ClosureError('unstarted arm must be explicitly withdrawn, never called frozen')
            if arm['state'] == 'active' or withdrawal is None:
                if not closure_manifest or not sections['reports']:
                    raise ClosureError('started arm requires a terminal report artifact')
            if closure_manifest:
                if (closure_manifest.get('arm_id'), closure_manifest.get('problem_id')) != (arm_id, config['problem_id']):
                    raise ClosureError('terminal closure identity mismatch')
            files = {}
            for section, artifacts in sections.items():
                for name, data in artifacts.items():
                    relative_name(name)
                    if not isinstance(data, bytes):
                        raise ClosureError('snapshot payload must be bytes')
                    files[section + '/' + name] = data
            if closure_manifest:
                files['closure.json'] = canonical(closure_manifest)
                # A terminal label without the actual selected source/PDF is not closure.
                selected = closure_manifest['selected']
                unverified=closure_manifest.get('model_delivery')=='unverified-model-writeup'
                if unverified:
                    if (selected.get('authorship')!='model' or closure_manifest.get('correctness')!='unverified'
                            or not any(k in selected.get('files',{}) for k in ('FINAL.tex','FINAL.md'))
                            or not closure_manifest.get('check_status_notice')):
                        raise ClosureError('unverified terminal requires model source and notice')
                elif (selected.get('authorship') not in {'model','administrative-fallback'} or
                      selected.get('compilation',{}).get('outcome')!='pass' or
                      not {'FINAL.tex','FINAL.pdf'}<=set(selected.get('files',{}))):
                    raise ClosureError('terminal selection is not a compiling artifact')
                if unix_ms(closure_manifest['stopped_at']) > unix_ms(frozen_at):
                    raise ClosureError('scientific freeze cannot precede terminal stop')
                for name, expected in selected['files'].items():
                    key = 'reports/'+selected.get('storage','bundles')+'/' + selected['bundle_id'] + '/' + name
                    if key not in files or sha256(files[key]) != expected:
                        raise ClosureError('selected terminal artifact missing from scientific snapshot')
                frozen_record = freeze_record(config, arm_id, frozen_at=frozen_at, files=files,
                                              fence=fence, terminal=closure_manifest,
                                              board_versions=board_versions, ledger_export_id=ledger_export_id)
                files['freeze-record.json'] = canonical(frozen_record)
            else:
                frozen_record = None
            wire_record = withdrawal_record(config, arm_id, withdrawal, frozen_record) if withdrawal else frozen_record
            if withdrawal:
                files['withdrawal-record.json'] = canonical(wire_record)
            manifest = {'schema': 1, 'cohort_id': config['cohort_id'], 'problem_id': config['problem_id'],
                        'arm_id': arm_id, 'problem_sha256': config['problem_sha256'], 'frozen_at': frozen_at,
                        'state': 'withdrawn' if withdrawal is not None else 'frozen',
                        'withdrawal': withdrawal, 'fence': asdict(fence), 'wire_record': wire_record,
                        'inventory_sections': sorted(sections),
                        'files': {name: sha256(data) for name, data in sorted(files.items())},
                        'billing_reconciliation': 'separate-ledger-only'}
            snapshot_id = sha256(canonical(manifest))
            publish_tree(self.artifacts / 'snapshots', snapshot_id, {**files, 'freeze.json': canonical(manifest)})
            arm.update(state=manifest['state'], snapshot_id=snapshot_id, manifest=manifest)
            self._save(state)
            return manifest

    def _verify_arm(self, arm):
        manifest = arm['manifest']
        if sha256(canonical(manifest)) != arm['snapshot_id']:
            raise ClosureError('freeze manifest identity changed')
        hashes = {**manifest['files'], 'freeze.json': sha256(canonical(manifest))}
        verify_tree(self.artifacts / 'snapshots' / arm['snapshot_id'], hashes)
        return hashes

    def _eligible(self, state):
        if any(arm['state'] not in {'frozen', 'withdrawn'} for arm in state['arms'].values()):
            raise ClosureError('cohort embargo: every registered arm must freeze or be explicitly withdrawn')
        verify_tree(self.artifacts / 'problem', {'PROBLEM.md': state['config']['problem_sha256']})
        verify_tree(self.artifacts / 'registration', {'cohort-registration.json': sha256(canonical(state['config']['registration_record']))})
        for arm in state['arms'].values():
            self._verify_arm(arm)

    def assert_eligible(self):
        with locked(self.root):
            state = self._load()
            self._eligible(state)
            return True

    def embargo_status(self, now):
        """Content-free monitor result. Time alone never releases the embargo."""
        with locked(self.root):
            state = self._load()
            outstanding = [arm for arm, record in state['arms'].items() if record['state'] not in {'frozen', 'withdrawn'}]
            return {'outstanding': outstanding, 'eligible': not outstanding,
                    'withdrawal_review_due': bool(outstanding) and now >= state['config']['withdrawal_deadline'],
                    'withdrawal_authority': state['config']['withdrawal_authority']}
