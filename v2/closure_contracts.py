"""Explicit translation between closure internals and P1 version-1 wire records."""
import math
from pathlib import Path
import re

from .closure_io import ClosureError, sha256
from .contracts_v3 import CONTRACT_VERSION, contracts_sha256, record_sha256, validate_cohort_export


def contract_directory():
    """Support both source packages and P7's pinned flat release layout."""
    here = Path(__file__).resolve().parent
    return here / 'contracts' if (here / 'contracts').is_dir() else here.parent / 'contracts'


def digest(value):
    if not isinstance(value, str) or re.fullmatch('[0-9a-f]{64}', value) is None:
        raise ClosureError('lowercase SHA-256 digest required')
    return value


def unix_ms(seconds):
    if isinstance(seconds, bool) or not isinstance(seconds, (float, int)) or not math.isfinite(seconds) or seconds < 0:
        raise ClosureError('finite nonnegative absolute time required')
    return int(seconds * 1000)


def registration_record(config, *, registered_at, latest_start_at):
    return {'contract_version': CONTRACT_VERSION, 'cohort_id': config['cohort_id'],
            'problem_id': config['problem_id'], 'registered_at_unix_ms': unix_ms(registered_at),
            'operator_id': config['withdrawal_authority'], 'closed_to_new_arms': True,
            'arms': [{'arm_id': arm, 'latest_start_unix_ms': unix_ms(latest_start_at[arm]),
                      'withdrawal_decision_deadline_unix_ms': unix_ms(config['withdrawal_deadline'])}
                     for arm in config['arm_ids']]}


def freeze_record(config, arm_id, *, frozen_at, files, fence, terminal, board_versions, ledger_export_id):
    selected = terminal['selected']
    status = terminal['internal_status']
    if status == 'not-applicable':
        status = 'completion-unconfirmed'
    if not isinstance(ledger_export_id, str) or not ledger_export_id:
        raise ClosureError('separate ledger export identifier required')
    versions = list(board_versions)
    for version in versions:
        if (set(version) != {'object_id', 'version', 'sha256'} or not version['object_id'] or
                type(version['version']) is not int or version['version'] < 1):
            raise ClosureError('invalid board dependency version')
        digest(version['sha256'])
    if len({(v['object_id'], v['version']) for v in versions}) != len(versions):
        raise ClosureError('duplicate board version')
    return {'contract_version': CONTRACT_VERSION, 'cohort_id': config['cohort_id'],
            'problem_id': config['problem_id'], 'arm_id': arm_id,
            'release_sha256': config['release_sha256'], 'contracts_sha256': config['contracts_sha256'],
            'arm_manifest_sha256': config['arm_manifest_sha256s'][arm_id],
            'frozen_at_unix_ms': unix_ms(frozen_at), 'admission_closed': True,
            'writers_fenced': True, 'pending_calls_recorded': True,
            'scientific_artifacts': [{'path': name, 'sha256': sha256(data), 'bytes': len(data)}
                                     for name, data in sorted(files.items())],
            'board_versions': versions, 'cancellations': list(fence.cancellation_records),
            'ledger_export_id': ledger_export_id, 'compilation': 'passed',
            'authorship': selected['authorship'], 'internal_status': status,
            'structural_completeness': 'incomplete' if selected['structure']['label'] == 'skeleton' else 'unknown',
            'substantive_grade': 'ungraded', 'stop_reason': terminal['stop_reason']}


def withdrawal_record(config, arm_id, withdrawal, freeze=None):
    return {'contract_version': CONTRACT_VERSION, 'cohort_id': config['cohort_id'],
            'problem_id': config['problem_id'], 'arm_id': arm_id,
            'withdrawn_at_unix_ms': unix_ms(withdrawal['at']), 'operator_id': withdrawal['authority'],
            'reason': withdrawal['reason'], 'admission_closed': True, 'writers_fenced': True,
            'resume_prohibited': True, 'freeze_sha256': record_sha256(freeze) if freeze else None}


def cohort_record(state, *, now, rubric_sha256):
    config = state['config']
    record = {'contract_version': CONTRACT_VERSION, 'cohort_id': config['cohort_id'],
              'problem_id': config['problem_id'], 'registered_arm_ids': config['arm_ids'],
              'terminal_records': [{'arm_id': arm_id, 'kind': arm['state'],
                                    'record_sha256': record_sha256(arm['manifest']['wire_record'])}
                                   for arm_id, arm in state['arms'].items()],
              'judging_started_at_unix_ms': unix_ms(now), 'resume_prohibited': True,
              'judge': {'product': 'Codex', 'model': 'gpt-6-astra', 'effort': 'high',
                        'rubric_sha256': rubric_sha256, 'max_jobs_per_arm': 1,
                        'cap_per_arm_microusd': 10_000_000, 'seconds_per_arm': 3600,
                        'max_concurrent_jobs': 3, 'stage_seconds': 14400}}
    validate_cohort_export(record)
    return record
