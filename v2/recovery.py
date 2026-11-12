"""Content-free adapters and persisted model-specific recovery decisions."""
import hashlib
import json
import math


def fingerprint(tool, arguments):
    # Canonical JSON normalizes key order, NOT shell whitespace (which can be meaningful).
    if not isinstance(tool, str) or not isinstance(arguments, dict): raise ValueError('operation schema')
    data = json.dumps([tool, arguments], sort_keys=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(data.encode()).hexdigest()


def numeric_compaction(record):
    """Qwen recording payload; allowlist fields, never export compressedHistory."""
    if record.get('type') != 'system' or record.get('subtype') != 'chat_compression': return None
    info = record.get('systemPayload', {}).get('info', {})
    out = {}
    for key in ('originalTokenCount', 'newTokenCount', 'compressionStatus'):
        value = info.get(key)
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0: return None
        out[key] = value
    if out['compressionStatus'] not in range(1, 8): return None
    for key in ('originalTokenCountIsEstimated', 'newTokenCountIsEstimated'):
        if type(info.get(key)) is bool: out[key] = info[key]
    return out


def goal_record(record):
    if record.get('type') == 'system' and record.get('subtype') == 'goal_state':
        payload = record.get('systemPayload', {})
        snapshot = payload.get('snapshot', {})
        cause = payload.get('cause')
    else:
        event = record.get('event', {})
        if event.get('type') != 'goal_state': return None
        snapshot = event.get('goal_state', {}); cause = None
    goal = snapshot.get('goal') or {}
    status = goal.get('status')
    if status not in ('active','complete','blocked','paused','usage_limited'): return None
    result = {'status': status}
    if type(goal.get('revision')) is int: result['revision'] = goal['revision']
    if cause in ('verifier_accept','verifier_reject'): result['cause'] = cause
    return result


def compaction_failure(state, session, lane):
    sessions = state.setdefault('failed_sessions', [])
    if session in sessions: return 'duplicate'
    sessions.append(session)
    if state.get('fallback'):
        return 'pause-alert'
    if len([x for x in sessions if session - x < 3]) >= 2:
        if lane in ('glm','qwen24t'):
            state['fallback'] = True
            return 1800 if lane == 'glm' else 3600
    return 'recover'


class DepthScore:
    """Per notice TYPE and lane. Publication alone never passes obedience."""
    def __init__(self, state): self.state = state
    def observe(self, kind, *, depth, compacted, attached, acknowledged, written, seconds):
        if kind=='checkpoint-final': kind='checkpoint'
        if kind not in ('checkpoint','session-wall','write-up'): raise ValueError('notice kind')
        eligible = depth >= 90000 and compacted
        passed = eligible and attached and acknowledged and written and seconds <= 300
        entry = self.state.setdefault(kind, {'attempts':0,'misses':0,'passed':0,'restart_delivery':False})
        if eligible:
            entry['attempts'] += 1
            entry['passed'] += int(passed)
            entry['misses'] += int(not passed)
            if entry['misses'] >= 2: entry['restart_delivery'] = True
        return 'pass' if passed else 'miss' if eligible else 'incomplete-depth'
