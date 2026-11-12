"""Reduced production envelope. Absolute deadlines include restart/setup downtime."""
import json
import math
import os

ENV = 'V3_PRODUCTION_CONTINUATION'


def envelope(original_start, prepared_at, committed, *, source_cohort, evidence):
    start = math.ceil(prepared_at)
    deadline = math.floor(original_start + 86400)
    return dict(original_start=original_start, prepared_at=start, deadline=deadline,
                committed_microusd=committed, source_cohort=source_cohort, evidence=evidence)


def profiles(base, row):
    """Only reduce allowances and budget pools; retain the full closing time."""
    if row.get('kind') == 'operator-fresh-p01-20261011':
        required = {'kind','original_start','prepared_at','deadline','committed_microusd','source_cohort','evidence'}
        if (set(row) != required or row['source_cohort'] != 'p01-coordinated-20261010'
                or type(row['prepared_at']) is not int
                or row['deadline'] != row['prepared_at'] + 21600 + 10140
                or type(row['committed_microusd']) is not int or row['committed_microusd'] < 0
                or not row['evidence']):
            raise ValueError('invalid operator fresh continuation')
        result = json.loads(json.dumps(base))
        for config in result.values():
            config['wall_seconds'] = 31740
            if 'research_stop_seconds' in config:
                config['research_stop_seconds'] = 31740-config['closing_max_seconds']
        mixed = result['mixed-swarm']
        mixed['cap_microusd'] = 80000000
        if sum(mixed['closing_reserves_microusd'].values()) != 42000000:
            raise ValueError('operator requires normal protected closing pools')
        return result
    if set(row) != {'original_start','prepared_at','deadline','committed_microusd','source_cohort','evidence'}:
        raise ValueError('invalid continuation envelope')
    if (not all(type(row[k]) in (int,float) and math.isfinite(row[k]) for k in ('original_start','prepared_at','deadline'))
            or row['prepared_at'] < row['original_start']
            or row['deadline'] != math.floor(row['original_start']+86400)
            or type(row['committed_microusd']) is not int or row['committed_microusd'] < 0):
        raise ValueError('invalid original continuation allowances')
    result = json.loads(json.dumps(base))
    wall = row['deadline'] - row['prepared_at']
    mixed = result['mixed-swarm']
    cap = mixed['cap_microusd'] - row['committed_microusd']
    if wall <= mixed['closing_max_seconds'] or cap <= 0:
        raise ValueError('remaining allowance cannot fund research and mandatory closing')
    # Narrow each protected closing pool in the same proportion as the arm cap.
    # Runtime admission still enforces the remaining budget on every request.
    mixed['closing_reserves_microusd'] = {k:v*cap//mixed['cap_microusd']
        for k,v in mixed['closing_reserves_microusd'].items()}
    if not all(mixed['closing_reserves_microusd'].values()):
        raise ValueError('remaining closing pools exhausted')
    mixed['report_reserve_microusd'] = sum(mixed['closing_reserves_microusd'].values())
    # Inactive arms keep their registered caps but share the reduced clock.
    for arm, config in result.items():
        config['wall_seconds'] = wall
        if 'research_stop_seconds' in config:
            config['research_stop_seconds'] = wall-config['closing_max_seconds']
    mixed['cap_microusd'] = cap
    return result


def fresh_envelope(original_start, prepared_at, committed, *, source_cohort, evidence):
    """Operator 2026-10-11: old spend is evidence, never a debit against the new cap."""
    start = math.ceil(prepared_at)
    return dict(kind='operator-fresh-p01-20261011', original_start=original_start,
                prepared_at=start, deadline=start+31740, committed_microusd=committed,
                source_cohort=source_cohort, evidence=evidence)


def active():
    raw = os.environ.get(ENV)
    return json.loads(raw) if raw else None


def apply(data):
    row = active()
    if row:
        data['production'] = profiles(data['production'], row)
        data['production_total_microusd'] = sum(r['cap_microusd'] for r in data['production'].values())
