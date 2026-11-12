"""Dynamic role mix (operator 2026-10-07: idle critics are wasted time).

Each research slot has a default role (its roster list) and allowed roles. At every new
assignment of a slot the harness picks the role mechanically from the review queue:
  review   when routable unsettled claims waiting > review_queue_per_critic per live critic,
           or the oldest has waited > max_wait_seconds (and the slot's model may review);
  propose  when the review queue is empty (and the slot may propose); a critic-default slot
           switches only while the live critics keep at least k_accept distinct models;
  default  otherwise.
A slot never reviews its own claims (the ledger and routing exclude a slot's own sessions).
Policy (roles/<swarm>/policy.json "mix", all optional):
  enabled true; review_queue_per_critic 1; max_wait_seconds 600;
  roles {"ideas": ["ideas", "critic"], "critic": ["critic", "ideas"], "literature": ["literature"]};
  proposer_only_models []  (model keys that never review, e.g. a weak critic).
Models allowed to review: critic_models of the policy (family names or model keys).
"""
import json
from pathlib import Path

DEFAULTS = {"enabled": True, "review_queue_per_critic": 1, "max_wait_seconds": 600,
            "roles": {"ideas": ["ideas", "critic"], "critic": ["critic", "ideas"], "literature": ["literature"]},
            "proposer_only_models": []}
ROLES = ('ideas', 'critic', 'literature')


def settings(swarm, root=None):
    from .claim_ledger import ROOT, policy_overrides, _deep_merge
    out = json.loads(json.dumps(DEFAULTS))
    try:
        data = json.loads(((Path(root) if root else ROOT) / str(swarm) / 'policy.json').read_text() or '{}')
    except (OSError, ValueError):
        data = {}
    data = _deep_merge(data if isinstance(data, dict) else {}, policy_overrides())
    mix = data.get('mix') if isinstance(data.get('mix'), dict) else {}
    if isinstance(mix.get('enabled'), bool):out['enabled'] = mix['enabled']
    for k in ('review_queue_per_critic', 'max_wait_seconds'):
        if isinstance(mix.get(k), (int, float)) and not isinstance(mix.get(k), bool) and mix[k] >= 0:out[k] = mix[k]
    if isinstance(mix.get('roles'), dict):
        for k, v in mix['roles'].items():
            if k in ROLES and isinstance(v, list):
                out['roles'][k] = [r for r in v if r in ROLES] or [k]
    if isinstance(mix.get('proposer_only_models'), list):
        out['proposer_only_models'] = [str(m).lower() for m in mix['proposer_only_models']]
    out['critic_models'] = [str(m).lower() for m in (data.get('critic_models') or [])]
    acc = data.get('acceptance') if isinstance(data.get('acceptance'), dict) else {}
    out['k_accept'] = acc.get('k_accept') if isinstance(acc.get('k_accept'), int) and acc['k_accept'] > 0 else 2
    return out


def may_review(model, opts):
    from .board import MODEL_KEYS
    m = str(model).lower()
    if m in opts['proposer_only_models']:
        return False
    fam = str(MODEL_KEYS.get(m, m)).lower()
    return not opts['critic_models'] or m in opts['critic_models'] or fam in opts['critic_models']


def queue(claims, now):
    """(number of routable unsettled claims, seconds the oldest has waited)."""
    from .state_page import reviewable
    waiting = [c for c in claims.values() if reviewable(c)]
    ats = [c.get('at') for c in waiting if isinstance(c.get('at'), (int, float))]
    return len(waiting), (now - min(ats)) if ats else 0.0


def choose(default, model, opts, *, queued, oldest_wait, live_critics, other_critic_models):
    """Role for a slot's next assignment. live_critics: live sessions in the critic role (this
    slot excluded); other_critic_models: their distinct models."""
    if not opts['enabled'] or default not in ROLES:
        return default
    allowed = opts['roles'].get(default) or [default]
    if 'critic' in allowed and may_review(model, opts) and (
            queued > opts['review_queue_per_critic'] * max(1, live_critics) or
            (queued and oldest_wait > opts['max_wait_seconds']) or
            (queued and len(other_critic_models) < opts['k_accept'])):  # keep k_accept critic models while claims wait
        return 'critic'
    if default == 'critic' and not may_review(model, opts):
        return 'ideas'  # a proposer-only model never reviews, whatever its slot allows
    if queued == 0 and 'ideas' in allowed:
        if default != 'critic' or len(other_critic_models) >= opts['k_accept']:
            return 'ideas'
    out = default if default in allowed or default == 'literature' else allowed[0]
    return 'ideas' if out == 'critic' and not may_review(model, opts) else out


def log_switch(runtime, slot, default, chosen, *, queued, oldest_wait, live_critics):
    """One entry per decision; published as 'role-mix' harness events (never trimmed)."""
    runtime.__dict__.setdefault('role_mix_log', []).append(dict(
        at=runtime.clock(), slot=str(slot)[:200], default=default, role=chosen, switched=chosen != default,
        queue=queued, oldest_wait=round(oldest_wait, 1), live_critics=live_critics))


LIVE = ('queued', 'running', 'stopping')


def _family(m):
    from .state_page import _family as fam
    return fam(m)


def reservations(runtime):
    """{route key: {voter: model family}} held by live critic sessions: a live standing critic or auditor
    assignment's pre-assignment and its slot's routed keys, and the routed keys of any other live slot."""
    from .claim_ledger import _voter
    slots = getattr(runtime, 'slots', None) or {}
    out = {}
    def hold(aid, model, keys):
        for k in keys:
            out.setdefault(k, {})[_voter(aid)] = _family(model) if model else None
    seen = set()
    for aid, a in (runtime.executor.snapshot().get('assignments') or {}).items():
        if a.get('standing') not in ('critic', 'auditor'):
            continue
        seen.add(aid)
        if a.get('state') not in LIVE:
            continue
        w = slots.get(aid)
        routed = ((w.state.get('routing') or {}).get('assigned') or ()) if w is not None else ()
        hold(aid, a.get('model'), set(routed) | set(a.get('preassign') or ()))
    for aid, w in slots.items():
        if aid in seen or w.state.get('slot_closed'):
            continue
        hold(aid, getattr(getattr(w, 'run', None), 'lane', None), (w.state.get('routing') or {}).get('assigned') or ())
    return out


def split(claims, held, policy, now):
    """Reviewable claims split into (waiting, in review), each as (count, seconds since the oldest was posted).
    held: reservations(). A claim waits when some model it still needs has no live reserved critic that has
    not yet judged it: under the pair rule each pair model without a verdict (K3 alone for literature, as
    the router), otherwise any one critic."""
    from .claim_ledger import _voter
    from .state_page import reviewable, route_key
    acc = (policy or {}).get('acceptance') or {}
    pair = [_family(m) for m in acc.get('pair_models') or ()] if acc.get('mode') == 'pair' else []
    waiting, busy = [], []
    for c in claims.values():
        if not reviewable(c):
            continue
        judged = {_voter(v) for v in list(c.get('verdicts') or ()) + list(c.get('reviewed_by') or ())}
        live = {v: f for v, f in (held.get(route_key(c)) or {}).items() if v not in judged}
        need = ['k3'] if c.get('post_type') == 'literature' else pair
        if need:
            vm = c.get('verdict_models') or {}
            covered = {f for v, f in live.items() if f}
            for w in c.get('verdicts') or ():
                if vm.get(w):
                    covered.add(_family(vm[w])); continue
                slot = str(_voter(w)).split(':', 1)[-1]  # an ended session is known by its slot id, as the router
                covered |= {f for f in need if slot == f or slot.startswith(f + '-')}
            short = any(f not in covered for f in need)
        else:
            short = not live
        (waiting if short else busy).append(c.get('at'))
    def summary(ats):
        known = [a for a in ats if isinstance(a, (int, float))]
        return len(ats), round((now - min(known)) if known else 0.0, 1)
    return summary(waiting), summary(busy)


def sample_queue(runtime, claims, now, policy=None):
    """Queue length over time (at most one sample a minute), published as 'review-queue' events.
    queue/oldest_wait count every reviewable claim, including those a live critic session already holds;
    waiting/oldest_unreserved_wait and in_review/oldest_in_review split them by live reservation."""
    log = runtime.__dict__.setdefault('review_queue_log', [])
    if log and now - log[-1]['at'] < 60:
        return
    n, oldest = queue(claims, now)
    row = dict(at=now, queue=n, oldest_wait=round(oldest, 1))
    log.append(row)
    try:
        (w, ow), (b, ob) = split(claims, reservations(runtime), policy, now)
        row.update(waiting=w, oldest_unreserved_wait=ow, in_review=b, oldest_in_review=ob)
    except Exception as exc:
        row['split_error'] = type(exc).__name__ + ': ' + str(exc)[:200]


def live_roles(runtime):
    """[(slot id, model, role)] of live research sessions."""
    from .open_swarm import member_role
    out = []
    for w in (getattr(runtime, 'slots', None) or {}).values():
        if getattr(w, 'stage', None) != 'research' or w.state.get('slot_closed'):
            continue
        role = w.state.get('member_role') or member_role(w.slot_id)
        out.append((w.slot_id, getattr(getattr(w, 'run', None), 'lane', None), role))
    return out


def decide(runtime, slot_key, default, model):
    """Role for a new assignment of slot slot_key (default role, model key); logs the decision."""
    try:
        opts = settings(runtime.strategy)
        if default == 'critic' and not may_review(model, opts):
            return 'ideas'  # a model that may not review never takes a critic role, mix or no mix
        if not opts['enabled']:
            return default
        claims = runtime.claim_ledger()
        now = runtime.clock()
        queued, oldest = queue(claims, now)
        from .board import MODEL_KEYS
        from .claim_ledger import load_policy
        cw = {str(k).lower(): str(v).lower() for k, v in (load_policy(runtime.strategy).get('critic_weights') or {}).items()}
        full = lambda m: cw.get(str(MODEL_KEYS.get(m, m) or '').lower(), 'full') == 'full'
        others = [(s, m) for s, m, r in live_roles(runtime) if r == 'critic' and str(s) != str(slot_key)
                  and not str(s).startswith(str(slot_key) + ':') and full(m)]  # counting critics keep the reserve
        role = choose(default, model, opts, queued=queued, oldest_wait=oldest, live_critics=len(others),
                      other_critic_models={m for _, m in others})
        log_switch(runtime, slot_key, default, role, queued=queued, oldest_wait=oldest, live_critics=len(others))
        return role
    except Exception as exc:
        import time
        clock = getattr(runtime, 'clock', time.time)
        runtime.__dict__.setdefault('slot_errors', []).append(dict(slot=str(slot_key)[:200], what='role-mix',
                                                                  error=type(exc).__name__ + ': ' + str(exc)[:300], at=clock()))
        return 'ideas' if default == 'critic' and str(model).lower() == 'glm' else default
