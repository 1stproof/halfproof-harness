"""Persistent runtime guards. All clocks and identities come from the controller.

State dictionaries are stored in the watchdog's acknowledged runtime journal.
No method makes provider calls, interprets scientific content, or extends caps.
"""
import math

MAX_ATTEMPTS = 3
INCIDENT_SECONDS = 90 * 60
AUTHOR_RESTRICT_SECONDS = 10 * 60
AUTHOR_STOP_SECONDS = 20 * 60


class Recovery:
    def __init__(self, state):
        self.state = state
        state.setdefault('incidents', {})
        state.setdefault('routes', {})

    def admit(self, logical_id, route, now, parent_deadline):
        if self.state['routes'].get(route, {}).get('quarantined'):
            raise ValueError('route-quarantined')
        incident = self.state['incidents'].setdefault(logical_id, {
            'route': route, 'start': now, 'deadline': min(now + INCIDENT_SECONDS, parent_deadline),
            'attempts': 0, 'retry_at': now, 'terminal': None,
        })
        if incident['route'] != route:
            raise ValueError('route-swap-forbidden')
        if (incident['terminal'] or incident['attempts'] >= MAX_ATTEMPTS or
                now >= min(incident['deadline'], parent_deadline)):
            raise ValueError(incident['terminal'] or 'recovery-exhausted')
        if now < incident['retry_at']:
            raise ValueError('retry-not-due')
        incident['attempts'] += 1
        return incident['attempts']

    def outcome(self, logical_id, outcome, now, *, retry_after=0, host=None,
                duration=None, identified_cutoff=None):
        incident = self.state['incidents'][logical_id]
        route = self.state['routes'].setdefault(incident['route'], {})
        if outcome in ('normal', 'configuration', 'output-truncated', 'reasoning-exhausted'):
            incident['terminal'] = outcome
            if outcome == 'configuration':
                route['quarantined'] = 'configuration'
        elif outcome == 'provider-refusal':
            # A measured account outage is not a route/configuration quarantine.
            incident['terminal'] = 'provider-refusal'
        elif outcome == 'billing':
            # An operator-authorized resume can only reuse this incident/deadline.
            incident['terminal'] = 'billing'
        elif outcome in ('transport-loss', 'rate', 'provider', 'timeout', 'in-stream-error'):
            if identified_cutoff is not None and duration is not None:
                if (not host or not math.isfinite(identified_cutoff) or identified_cutoff <= 0
                        or not math.isfinite(duration) or duration < 0):
                    raise ValueError('invalid-cutoff-evidence')
                if abs(duration - identified_cutoff) <= .1 * identified_cutoff:
                    key = f'{host}:{identified_cutoff}'
                    counts = route.setdefault('cutoff_failures', {})
                    counts[key] = counts.get(key, 0) + 1
                    if counts[key] >= 2:
                        route['quarantined'] = 'recurring-host-cutoff'
                        incident['terminal'] = 'recurring-host-cutoff'
            delay = max(min(600, 60 * 2 ** (incident['attempts'] - 1)), float(retry_after))
            if not math.isfinite(delay) or delay < 0:
                raise ValueError('invalid-retry-after')
            incident['retry_at'] = now + delay
            if incident['attempts'] >= MAX_ATTEMPTS or incident['retry_at'] >= incident['deadline']:
                incident['terminal'] = incident['terminal'] or 'recovery-exhausted'
        else:
            raise ValueError('unknown-recovery-outcome')
        return incident


class ReportClock:
    """One-way lock and author-ready inactivity, never renewed by session changes."""
    def __init__(self, state):
        self.state = state

    def lock(self, now, reason, deadline):
        if not self.state:
            self.state.update(locked_at=now, reason=reason, deadline=deadline,
                              ready_at=None, last_tick=now, idle_seconds=0,
                              revision=None, restriction=False, stopped=False,
                              verifier_until=None)
        elif self.state['deadline'] != deadline:
            raise ValueError('report-deadline-changed')
        return self.state

    def ready(self, now):
        if self.state['ready_at'] is None:
            self.state['ready_at'] = now
            self.state['last_tick'] = now

    def advance(self, now, *, revision=None, request_active=False, writes_pending=False):
        s = self.state
        if not s or s['ready_at'] is None:
            return 'handover'
        if now < s['last_tick']:
            raise ValueError('clock-moved-backwards')
        # Only a controller-authenticated verifier can suspend author-ready time.
        pause_until = s.get('verifier_until')
        effective_start = max(s['last_tick'], pause_until or s['last_tick'])
        s['idle_seconds'] += max(0, now - effective_start)
        s['last_tick'] = now
        if revision is not None and revision != s['revision']:
            s['revision'] = revision
            s['idle_seconds'] = 0
        if now >= s['deadline']:
            s['stopped'] = True
        if not request_active and not writes_pending:
            if s['idle_seconds'] >= AUTHOR_RESTRICT_SECONDS:
                s['restriction'] = True
            if s['idle_seconds'] >= AUTHOR_STOP_SECONDS:
                s['stopped'] = True
        return 'stop' if s['stopped'] else 'restrict' if s['restriction'] else 'pending' if s['idle_seconds'] >= AUTHOR_RESTRICT_SECONDS else 'author'

    def verifier(self, now, deadline):
        self.advance(now)
        self.state['verifier_until'] = min(deadline, self.state['deadline'])

    def author(self, now):
        self.advance(now)
        self.state['verifier_until'] = None
