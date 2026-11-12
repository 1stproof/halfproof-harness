"""Completion evidence shared by solo and swarm acceptance gates."""
CAPPED_OUTCOMES = frozenset({'output-truncated', 'reasoning-exhausted'})
CAP_FINISH_REASONS = frozenset({'length', 'max_tokens', 'max_output_tokens',
                               'output_cap', 'output-truncated',
                               'reasoning_exhausted', 'reasoning-exhausted'})


def capped_session(state, session=None):
    """Return an unrecovered cap in this session, retaining all paid-attempt evidence.

    Swarm clients may make a guided retry in the same session. Solo lifecycle
    gates use any_capped_session and retain the any-cap policy. A later normal
    completion clears an earlier cap; HTTP errors and other failed attempts do
    not. Gateway strips capped tool calls, and receipt fencing still applies to
    the normal retry before its output can be accepted.
    """
    session = state.get('session') if session is None else session
    for row in reversed(state.get('request_metrics', [])):
        if row.get('session') != session:
            continue
        outcome = row.get('outcome')
        if outcome == 'normal':
            return None
        if outcome in CAPPED_OUTCOMES:
            return outcome
    return None


def any_capped_session(state, session=None):
    """Return any cap in the owned solo session, even after a normal retry.

    Solo verifiers and report authors retain the original policy: a capped
    attempt poisons its context; a fresh bounded session may retry.
    """
    session = state.get('session') if session is None else session
    for row in reversed(state.get('request_metrics', [])):
        if row.get('session') == session and row.get('outcome') in CAPPED_OUTCOMES:
            return row['outcome']
    return None
