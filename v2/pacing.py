"""Original-clock pilot events, committed together with controller transitions.

No inference and no external side effects: event handlers are pure checkpoint
transforms. The checkpoint containing the acknowledged cursor must be published
and acknowledged by the cohort publisher before admission resumes.
"""
import hashlib
import copy
import json
from .deployment import DeploymentRefused


def fixture_rows(release, hashes):
    """Convert P8's pinned file inventory to actual read-only worker mounts."""
    rows = []
    for name, digest in sorted(hashes.items()):
        if name == 'tests/PILOT-PROBLEM.md':
            target = '/work/PILOT-PROBLEM.md'
        elif name.startswith('tests/p8/fixture/'):
            target = '/work/pilot-fixture/' + name[len('tests/p8/fixture/'):]
        else:
            raise DeploymentRefused('undeclared P8 fixture member')
        rows.append(dict(source=str(release/name), target=target, sha256=digest))
    required = {'/work/PILOT-PROBLEM.md', '/work/pilot-fixture/events.json', '/work/pilot-fixture/graph.json'}
    if {x['target'] for x in rows} != required:
        raise DeploymentRefused('incomplete P8 fixture inventory')
    return rows


def advance(state, events, now, transition, *, mode):
    if mode not in ('test', 'pilot'):
        raise DeploymentRefused('production pacing refused')
    original_state = state
    state = copy.deepcopy(state)
    original_start = state.get('pilot_started_at')
    schedule_hash = hashlib.sha256(json.dumps(events, sort_keys=True).encode()).hexdigest()
    if 'pilot_started_at' not in state:
        raise DeploymentRefused('original pilot start required')
    if len(events) > 1000 or len({e['id'] for e in events}) != len(events):
        raise DeploymentRefused('invalid bounded event schedule')
    times = [e['release_after_seconds'] for e in events]
    if any(type(t) is not int or t < 0 for t in times) or times != sorted(times):
        raise DeploymentRefused('invalid event clocks')
    if state.get('pacing_schedule_sha256', schedule_hash) != schedule_hash:
        raise DeploymentRefused('pacing schedule changed on resume')
    state['pacing_schedule_sha256'] = schedule_hash
    cursor = state.get('pacing_cursor', 0)
    if type(cursor) is not int or not 0 <= cursor <= len(events):
        raise DeploymentRefused('invalid persisted cursor')
    due = []
    while cursor < len(events) and now >= state['pilot_started_at'] + times[cursor]:
        event = events[cursor]
        transition(state, event)
        due.append(event['id'])
        cursor += 1
        state['pacing_cursor'] = cursor
    if state['pilot_started_at'] != original_start or state['pacing_schedule_sha256'] != schedule_hash:
        raise DeploymentRefused('transition changed immutable pacing clock/schedule')
    original_state.clear()
    original_state.update(state)
    return due
