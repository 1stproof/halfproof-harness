"""Standing critic slots of a coordinated (roster) swarm.

The roster's critic slots are staffed by the harness, not by the coordinator: during
research each critic slot keeps one live assignment, relaunched when it ends (like the
open-swarm members in open_swarm.seed). They draw on the research budget like any worker.
"""

BRIEF = ('You are a critic of the team. This session exists because claims wait for your review: the harness routes '
         'them to you (messages headed "Claims routed to you for review"); each routed claim\'s full record is in '
         '/work/routed/<record id>.md, so read that file rather than paging the board. Review each one; check every step, special '
         'To replace an objection by acceptance on the same revision, include Resolves-objection: #<verdict id> and Explanation: <resolution>. Check cases and every cited source. Post each verdict as soon as it is ready with board_post {text}: first lines "Post-type: verdict", '
         '"Status: ACCEPT" or "Status: REJECT" (or "Status: objection" naming the gap to repair) and "Re: <record id>". '
         'When every routed claim has your verdict, end your turn; the harness starts a new session when another claim '
         'waits for you. For a Post-type: literature report, check the source bytes yourself, attribution, version, '
         'exact quotes and locations or bounded search receipts. Check that it contains no mathematical inference; '
         'To replace your own objection/rejection by acceptance on the same revision, include "Resolves-objection: #<verdict id>" and "Explanation: <why the objection is resolved>". Missing source evidence means not ready/unverified. Check qualifications and the limited scope of negative searches: no match does not establish no result exists, open, or inapplicable. If mixed, post Post-type: verdict, Status: objection and Re: <record id> and return it to the author to split. This is the only literature review; '
         'no tie-break or certification follows. Review only the reserved revision; do not pick other claims.')
AUDIT_BRIEF = ('You are the AUDITOR of the team. Critics of a single model have accepted the claims the harness routes to you '
               '(messages headed "Claims routed to you for review"); the final report may use a claim as verified only after '
               'a critic of a second model confirms it. Check every step, special cases and every cited source independently; '
               'take your time, accuracy matters more than speed. Post each verdict with board_post {text}: first lines "Post-type: verdict", '
               '"Status: ACCEPT" or "Status: REJECT" (or "Status: objection" naming the gap to repair) and "Re: <record id>". '
               'A REJECT refutes the claim and an objection sends it back for repair, so name the exact failing step. '
               'Audit only the claims routed to you (each goes to one auditor); when none are left, end your turn.')
EXPLORE_BRIEF = ('You are an EXPLORER of the team: you post extra exploratory ideas, partial results and leads for '
                 'PROBLEM.md. Nobody waits for your work, so pick directions the rest of the team is not taking. Post any '
                 'result as a claim (first lines "Post-type: reasoning", "Status: ..." and "Task: ...") so critics review it; never post verdicts '
                 'on other members\' claims.')
TIEBREAK_BRIEF = ('You are the TIE-BREAKER for one claim. Two independent critics reviewed it and disagreed. Decide it '
                  'yourself: check every step, special cases and every cited source; the critiques below are arguments, '
                  'not authorities. Post exactly one verdict with board_post {text}: first lines "Post-type: verdict", "Status: ACCEPT" or '
                  '"Status: REJECT" (or "Status: objection" naming the gap to repair) and "Re: <record id>". Your verdict '
                  'settles the claim. On REJECT or objection include the detailed repair below unless the lineage is exhausted. Then end your turn.')
BLIND_LINE = ('Judge each claim independently: do not read other critics\' verdicts on it before posting yours, and '
              'ignore who proposed it.')
LIVE = ('queued', 'running', 'stopping')

from contextlib import contextmanager
from .certification_checking import brief as checking_brief


def _audit_claims(runtime, now, key=None):
    """Claims awaiting audit (claim ledger, cached for 60 s while key -- the auditor assignments and
    their states -- is unchanged); None when the ledger cannot be read."""
    cache = runtime.__dict__.setdefault('audit_wait_cache', {})
    if cache and now - cache.get('at', -1e18) < 60 and cache.get('key') == key:
        return cache['value']
    try:
        from .claim_ledger import load_policy, needs_audit
        pol = load_policy(runtime.strategy)
        from .state_page import reviewable
        value = [c for c in runtime.claim_ledger(pol).values()
                 if needs_audit(c, pol) or (reviewable(c) and c.get('accepts', 0) == 1)]  # audits, or open claims one accept short
    except Exception:
        value = None
    cache.update(at=now, value=value, key=key)
    return value


def _audit_for(runtime, now, voter, state):
    """True when some claim awaiting audit is not held by a live auditor session and this auditor
    slot (voter) may audit it (not an author, has not judged it). A live auditor session not yet
    routed anything is counted as holding one such claim. Unknown ledger = True."""
    key = tuple(sorted((aid, a.get('state')) for aid, a in (state.get('assignments') or {}).items()
                       if str(a.get('branch', '')).startswith('critic-audit-')))
    claims = _audit_claims(runtime, now, key)  # an auditor session ending refreshes the ledger
    if claims is None:
        return True
    from .claim_ledger import _voter
    from .state_page import route_key
    held, unrouted = set(), 0
    slots = getattr(runtime, 'slots', None) or {}
    for aid, a in (state.get('assignments') or {}).items():
        if not (str(a.get('branch', '')).startswith('critic-audit-') and a.get('state') in LIVE):
            continue
        w = slots.get(aid)
        got = set(((w.state.get('routing') or {}).get('assigned') or ())) if w is not None else set()
        held |= got
        unrouted += not got
    free = [c for c in claims if route_key(c) not in held]
    mine = [c for c in free if voter not in {_voter(v) for v in list(c.get('verdicts') or ()) + list(c.get('reviewed_by') or ())}
            | {_voter(x) for x in (c.get('authors') or [c.get('proposer')])}]
    return len(mine) > unrouted


def standing_slots(r):
    """[(slot_id, model, role)] of the roster's harness-staffed slots (critic, auditor, explorer)
    when standing critics are on, else []."""
    from .roles import roster_slots, STANDING_ROLES
    if not r or not r.get('standing_critics', bool(r.get('critics'))):
        return []
    return [(sid, m, role) for sid, m, role in roster_slots(r) if role in STANDING_ROLES]


def critic_slots(r):
    """[(slot_id, model)] of the roster's critic slots when standing critics are on, else []."""
    return [(sid, m) for sid, m, role in standing_slots(r) if role == 'critic']


def is_audit_slot(worker):
    """A session of a roster auditor slot ('critic:audit-<model>...:<n>')."""
    return str(worker).startswith('critic:audit-')


def is_explorer_slot(worker):
    return str(worker).startswith('critic:explore-')


def _slot_voter(sid):
    return 'critic:' + sid


def critic_demand(claims, policy, slots, held, fam, weights=None, started=()):
    """Event-driven critic staffing (operator 2026-10-09: critics run only when a claim waits for them).

    claims: the ledger; slots: critic slot voter ids ('critic:<slot>'); held: {voter: set of route keys}
    for every slot with a live (queued or running) session -- what that session is reviewing or was
    routed; fam: {voter: model family, lower case}. Returns {voter: [route key, ...]} for slots WITHOUT
    a live session that the router would give a waiting claim to: exactly the sessions to launch now.
    started: retained for caller compatibility, without scheduling effect. Every
    still-needed review uses current-revision post order, including failed retries.
    Pure function (no clock): identical ledger and live set give identical launches."""
    from .state_page import route, route_audit
    n = policy['acceptance']['n_critics']
    assigned = {v: set(held.get(v) or ()) for v in slots}
    strict = policy['acceptance'].get('mode') == 'pair'
    critics = [v for v in slots if not v.startswith('critic:audit-')]  # auditors only take route_audit claims
    # A live session is bound to one immutable revision. Keep its reservation
    # for coverage, but never route additional work to it and then discard that
    # work below. Only idle slots can accept new reservations, one each.
    new = route(claims, [v for v in critics if v not in held], {v: assigned[v] for v in critics}, n,
                policy['acceptance'].get('k_accept'), weights or {},
                models={v: fam.get(v) for v in critics}, strict=strict,
                pair_models=policy['acceptance'].get('pair_models'), max_new=1, fifo=True, started=started)
    for v, keys in new.items():
        assigned[v] |= set(keys)
    audit_models = {str(m).lower() for m in (policy.get('audit') or {}).get('models') or ()}
    if audit_models:
        extra = route_audit(claims, [v for v in slots if (v.startswith('critic:audit-') or fam.get(v) in audit_models) and (weights or {}).get(v, 'full') == 'full'],
                            assigned, policy, fam, pool=[],
                            available={v for v in slots if v not in held and not new.get(v)}, max_new=1)
        for v, keys in extra.items():
            new.setdefault(v, []).extend(k for k in keys if k not in new.get(v, ()))
    return {v: keys for v, keys in new.items() if v not in held and keys}


def _held(runtime, s, voters):
    """{voter: route keys} for critic slots with a live session (queued rows: their pre-assignment)."""
    from .claim_ledger import _voter
    slots = getattr(runtime, 'slots', None) or {}
    out = {}
    for aid, a in (s.get('assignments') or {}).items():
        if a.get('state') not in LIVE or a.get('standing') not in ('critic', 'auditor'):
            continue
        v = _voter(aid)
        if v not in voters:
            continue
        w = slots.get(aid)
        got = set(((w.state.get('routing') or {}).get('assigned') or ())) if w is not None else set()
        out.setdefault(v, set()).update(got | set(a.get('preassign') or ()))
    return out


def _log_launch(runtime, slot, trigger, keys=(), *, db=None):
    from .launch_journal import append, migrate_tail
    event = dict(at=runtime.executor.clock(), slot=str(slot)[:200], trigger=trigger, claims=[str(k)[:120] for k in keys][:10])
    db = db if db is not None else runtime.__dict__.get('_staff_db')
    if db is None:
        with runtime.executor.tx() as (db, _):
            migrate_tail(db, runtime)
            event = append(db, event)
    else:
        migrate_tail(db, runtime)
        event = append(db, event)
    log = runtime.__dict__.setdefault('launch_triggers', [])
    log.append(event)
    del log[:-500]


@contextmanager
def _staff_tx(runtime):
    """Use the transaction's assignment rows for bindings; never nest executor writes."""
    with runtime.executor.tx() as (db, state):
        runtime.__dict__['_staff_assignments'] = state['assignments']
        runtime.__dict__['_staff_db'] = db
        try:
            yield db, state
        finally:
            runtime.__dict__.pop('_staff_assignments', None)
            runtime.__dict__.pop('_staff_db', None)


def staff(runtime, headroom):
    """Standing slots, launched on state changes only (operator 2026-10-09: nothing on a clock).

    critic: a session starts when the router would give it a waiting claim (critic_demand) and its
    slot has no live session; the claims are pre-assigned to that session. auditor: the same, for
    claims awaiting a second model. explorer: proposers nobody waits for; a slot whose session ended
    (completed or failed) is replaced at once. No launch throttle: a failed session is replaced as
    soon as it ends; a provider outage is the gateway's provider fallback, not a launch delay."""
    if getattr(runtime, 'config', {}).get('open_swarm'):
        return
    if hasattr(runtime, 'closing_only') and runtime.closing_only():
        return
    from .roles import roster
    slots = standing_slots(roster(runtime.strategy))
    if not slots or headroom <= 0:
        return
    e = runtime.executor
    with _staff_tx(runtime) as (db, s):
        if s['mode'] != 'research':
            return
        live_branches = {a['branch'] for a in s['assignments'].values() if a['state'] in LIVE and a.get('standing')}
        ended = [(slot_id, model, srole) for slot_id, model, srole in slots if model in e.config.get('models', ())
                 and 'critic-' + slot_id.replace('#', '-') not in live_branches]
        if not ended:
            return
        _tiebreaks(runtime, s, slots, [x for x in ended if x[2] == 'tiebreak'], headroom)
        _certification(runtime, s, [x for x in ended if x[2] in ('select', 'certify')], headroom)
        ended = [x for x in ended if x[2] not in ('tiebreak', 'select', 'certify')]
        reviewers = [(sid, m, r) for sid, m, r in ended if r in ('critic', 'auditor')]
        demand = {}
        if reviewers:
            demand = _demand(runtime, s, slots)
            if demand is None:
                reviewers = []  # unreadable ledger: launch no reviewer (the next state change retries)
        for slot_id, model, srole in ended:
            sid = slot_id.replace('#', '-')  # '#' is not legal in an event identifier
            branch = 'critic-' + sid
            n = sum(a['branch'] == branch for a in s['assignments'].values()) + 1
            while 'critic:%s:%d' % (sid, n) in s['assignments'] or 'critic:%s:%d:ideas' % (sid, n) in s['assignments']:
                n += 1
            prev = [a for a in s['assignments'].values() if a['branch'] == branch]
            if srole == 'explorer':
                trigger = 'start' if not prev else ('failed-replace' if prev[-1]['state'] == 'failed' else 'session-ended')
                spec = dict(standing='explorer', model=model, role='research', roster_role='critic', mix_role='ideas', brief=EXPLORE_BRIEF, stance='ideas',
                            input_refs=[], branch=branch if branch in s['branches'] else dict(
                                id=branch, approach='standing explorer ' + slot_id, target='explore', assumptions=[]))
                aid = 'critic:%s:%d:ideas' % (sid, n)
                try:
                    e._launch(s, aid, spec, {}, headroom)
                except ValueError:
                    continue
                _log_launch(runtime, aid, trigger)
                continue
            keys = (demand or {}).get(_slot_voter(sid))
            if not keys:
                continue  # no claim waits for this critic: no session
            keys = keys[:1]  # one immutable revision per ordinary review session
            audit = srole == 'auditor'
            from .state_page import by_route_key
            reserved = by_route_key(runtime.claim_ledger())[keys[0]]
            rid = (reserved['revisions'][-1]['record'] if reserved.get('revisions') else None) or reserved['records']['proposal']
            typed_brief = '\nReserved post type: %s; exact revision record: %s.\n' % (reserved.get('post_type', 'reasoning'), rid)
            typed_brief += 'Finish this reserved check even if another reviewer settles the claim; return any objection you find.\n'
            typed_brief += 'Read the full exact reserved record from the board, including every continuation, before deciding; the excerpt below may be truncated. Do not substitute a later revision.\n'
            typed_brief += '\nRESERVED REVISION:\n' + str(reserved.get('text') or reserved.get('statement') or '')[:20000]
            if reserved.get('post_type') == 'literature':
                typed_brief += 'This is a Flash source report. K3 must check the source itself. Reject mixed reasoning back to the author to split; no tie-break or certifier follows.\n'
            spec = dict(standing=srole, model=model, role='research', roster_role='critic', brief=checking_brief(AUDIT_BRIEF if audit else BRIEF + ' ' + BLIND_LINE) + typed_brief, stance='critic',
                        input_refs=[], branch=branch if branch in s['branches'] else dict(
                            id=branch, approach='standing %s %s' % (srole, slot_id), target='review', assumptions=[]))
            if audit:
                spec['mix_role'] = 'critic'
            aid = 'critic:%s:%d' % (sid, n)
            try:
                e._launch(s, aid, spec, {}, headroom)
            except ValueError:
                continue
            s['assignments'][aid]['preassign'] = list(keys)  # persisted with the executor state (restart-safe)
            s['assignments'][aid]['bound_record'] = rid
            _log_launch(runtime, aid, 'repair-pair-review' if reserved.get('repair_of') is not None else
                        'claim-waiting' if not prev or prev[-1]['state'] != 'failed' else 'failed-replace+claim-waiting', keys)


def _demand(runtime, s, slots):
    """critic_demand over the runtime's ledger, or None when the ledger cannot be read."""
    try:
        from .claim_ledger import load_policy
        from .board import MODEL_KEYS
        pol = load_policy(runtime.strategy)
        claims = runtime.claim_ledger(pol)
    except Exception:
        return None
    cw = {str(k).lower(): str(v).lower() for k, v in (pol.get('critic_weights') or {}).items()}
    voters, fam, weights = [], {}, {}
    for slot_id, model, srole in slots:
        if srole not in ('critic', 'auditor'):
            continue
        v = _slot_voter(slot_id.replace('#', '-'))
        f = str(MODEL_KEYS.get(model, model) or model).lower()
        voters.append(v); fam[v] = f; weights[v] = cw.get(f, 'full')
    return critic_demand(claims, pol, voters, _held(runtime, s, set(voters)), fam, weights)


def _record_text(r):
    b = r.get('body') if isinstance(r.get('body'), dict) else {}
    return str(b.get('text') or b.get('statement') or '')


_LABEL = __import__('re').compile(r'^\s*[*_#>]*\s*(status|verdict)\s*[*_]*\s*[:=].*$', __import__('re').I | __import__('re').M)


def tiebreak_brief(claim, records, show_critiques=True, pair_models=('dsf', 'k3')):
    """Blinded tie-break brief: the claim's current text (latest revision, assembled) and, when
    show_critiques, exactly the two active pair critiques (each pair model's standing verdict) as
    anonymous arguments with no author, model or verdict label."""
    from .claim_ledger import is_tiebreak
    from .state_page import _family
    by = {r.get('id'): r for r in records or ()}
    revs = claim.get('revisions') or []
    rid = (revs[-1].get('record') if revs else None) or claim['records']['proposal']
    text = str(claim.get('text') or '') or _record_text(by.get(rid, {})) or str(claim.get('statement') or '')
    out = [checking_brief(TIEBREAK_BRIEF), lineage_brief(claim), '', 'CLAIM (record %s):' % rid, text]
    if show_critiques:
        vm = claim.get('verdict_models') or {}
        vr = claim.get('verdict_records') or {}
        chosen = {}
        for w in claim.get('verdicts') or {}:
            if is_tiebreak(w) or w not in vr:
                continue
            f = _family(vm.get(w)) if vm.get(w) else None
            if f is None:
                slot = str(w).split(':')[1] if ':' in str(w) else ''
                f = next((_family(p) for p in pair_models if slot == p or slot.startswith(p + '-')), None)
            if f in {_family(p) for p in pair_models}:
                chosen[f] = vr[w]  # the latest verdict of that family stands
        for label, f in zip('AB', sorted(chosen)):
            r = by.get(chosen[f])
            if r:
                out += ['', 'CRITIQUE %s:' % label, _LABEL.sub('', _record_text(r)).strip()]
    return '\n'.join(out)


def _tiebreaks(runtime, s, slots, free, headroom):
    """One fresh tie-break session per claim in state 'tiebreak' that has no tie-break verdict and no live
    tie-break session holding it (state change: the second pair verdict disagreed with the first)."""
    if not free:
        return
    try:
        from .claim_ledger import load_policy
        pol = load_policy(runtime.strategy)
        claims = runtime.claim_ledger(pol)
    except Exception:
        return
    tb = (pol.get('acceptance') or {}).get('tiebreak') or {}
    held = {k for a in s['assignments'].values() if a['state'] in LIVE for k in a.get('preassign') or ()
            if a.get('standing') == 'tiebreak'}
    from .state_page import route_key
    waiting = sorted((c for c in claims.values() if c['state'] == 'tiebreak' and route_key(c) not in held),
                     key=lambda c: c['records']['proposal'] or 0)
    if not waiting:
        return
    try:
        records = runtime._harness_view(runtime.clock(), runtime.swarm_settings())['records']
    except Exception:
        records = []
    e = runtime.executor
    for slot_id, model, _ in free:
        if not waiting or (tb.get('model') and model != tb['model']):
            continue
        c = waiting.pop(0)
        sid = slot_id.replace('#', '-'); branch = 'critic-' + sid
        n = sum(a['branch'] == branch for a in s['assignments'].values()) + 1
        tb_revs = c.get('revisions') or []
        tb_rid = (tb_revs[-1].get('record') if tb_revs else None) or c['records']['proposal']
        spec = dict(standing='tiebreak', model=model, role='research', roster_role='critic', mix_role='critic', stance='critic', input_refs=[],
                    brief=checking_brief(TIEBREAK_BRIEF) + '\n' + lineage_brief(c) + '\n\nCLAIM (record %s): its full text and evidence bundle follow. Your verdict must say Re: %s.' % (tb_rid, tb_rid),
                    branch=branch if branch in s['branches'] else dict(id=branch, approach='tie-break ' + slot_id, target='review', assumptions=[]))
        aid = 'critic:%s:%d' % (sid, n)
        try:
            e._launch(s, aid, spec, {}, headroom)
        except ValueError:
            continue
        s['assignments'][aid]['preassign'] = [route_key(c)]
        revs = c.get('revisions') or []
        s['assignments'][aid]['bound_record'] = (revs[-1].get('record') if revs else None) or c['records']['proposal']
        _log_launch(runtime, aid, 'tiebreak', [route_key(c)])


from .global_planner import PLANNER_BRIEF as SELECT_BRIEF
CERT_BRIEF = ('You are a CERTIFIER. One claim below was accepted by two independent critics and is load-bearing for the '
              'final write-up. Check it independently: every step, special cases and every cited source. Post exactly '
              'one verdict with board_post {text} as soon as you have decided: first lines "Post-type: verdict", "Status: ACCEPT" or '
              '"Status: REJECT" (or "Status: objection" naming the gap to repair) and "Re: <record id>"; for a REJECT or '
              'objection name the exact failing step. Keep the check focused on this one claim and post the atomic verdict promptly within the existing deadline; '
              'include a detailed repair on REJECT or objection before ending your turn unless the lineage is exhausted. '
              'Everything you need is in the evidence bundle below: the claim, its earlier revisions, every review posted on it, '
              'and the accepted claims and checked literature it depends on. A typical certification is: read the bundle, '
              'check the argument (run a computation if a step needs one), post the verdict, end your turn.')
from .certifier_repair import REPAIR_BRIEF, required_families, lineage_brief
TIEBREAK_BRIEF += '\n' + REPAIR_BRIEF
CERT_BRIEF += '\n' + REPAIR_BRIEF
# The same posting contract applies to every standing research/review role.
from .post_types import POST_CONTRACT
BRIEF += '\n' + POST_CONTRACT
AUDIT_BRIEF += '\n' + POST_CONTRACT
EXPLORE_BRIEF += '\n' + POST_CONTRACT
TIEBREAK_BRIEF += '\n' + POST_CONTRACT
CERT_BRIEF += '\n' + POST_CONTRACT

_SELECT_LINE = __import__('re').compile(r'^\s*[*_-]*\s*select(?:ed)?\s*[*_]*\s*[:=]?\s*#\s*(\d+)', __import__('re').I | __import__('re').M)
_SELECT_NONE = __import__('re').compile(r'^\s*[*_-]*\s*select(?:ed)?\s*[*_]*\s*[:=]\s*none\b', __import__('re').I | __import__('re').M)


def selections(records, claims):
    """{selector session: [route key, ...]} from every selector post ('Select: #<id>' lines; 'Select: none' is an
    explicit empty selection). Ids are a claim's current proposal or revision record."""
    from .state_page import route_key
    by_rec = {}
    for c in claims.values():
        revs = c.get('revisions') or []
        cur = (revs[-1].get('record') if revs else None) or c['records']['proposal']
        if c.get('post_type') != 'literature':
            by_rec[cur] = c
    out = {}
    for r in records or ():
        a = str(r.get('author') or '')
        if not a.startswith('critic:select-'):
            continue
        body = r.get('body') or {}
        if r.get('kind') == 'global-plan' and isinstance(body, dict):
            keys = [route_key(by_rec[i]) for i in body['plan']['select'] if i in by_rec and by_rec[i]['state'] == 'accepted']
            out[a] = keys
            continue
        text = _record_text(r)
        found = [int(x) for x in _SELECT_LINE.findall(text)]
        keys = [route_key(by_rec[i]) for i in found if i in by_rec]
        if keys or _SELECT_NONE.search(text):  # unknown or superseded ids alone are not a selection (review ev5)
            out.setdefault(a, [])
            out[a] += [k for k in keys if k not in out[a]]
    return out


def selected_claims(records, claims):
    """Route keys selected by any selector session (the union over all selections; a later round never
    erases an earlier one, as the runtime's persisted obligations)."""
    out = []
    for keys in selections(records, claims).values():
        out += [k for k in keys if k not in out]
    return out


def _certification(runtime, s, free, headroom):
    """With all_pair_accepted, dispatch every accepted reasoning revision without a selector.

    Legacy selector rounds (operator 2026-10-09: every certification.interval_seconds of research and once
    final_before_stop_seconds before research stops -- the one clocked trigger, by operator choice) and one
    certifier session per (selected claim revision, certifier model) until that model's verdict lands.

    Obligations persist in executor state (certification.obligations: {route key: [models]}): every selector
    post adds to them, a later round never erases them, and a revision supersedes them (new route key). A
    round counts as done only when its selector session completes having posted a selection (or 'Select:
    none'); a failed selector is relaunched on the next state change."""
    try:
        from .claim_ledger import load_policy, MODEL_FAMILY
        pol = load_policy(runtime.strategy)
    except Exception:
        return
    cert = pol.get('certification') or {}
    if not cert.get('enabled'):
        return
    e = runtime.executor
    now = e.clock()
    st = s.setdefault('certification', dict(rounds=0, final=False))
    st.setdefault('obligations', {})
    start = s['identity']['start']
    stop = start + e.config.get('research_stop_seconds', 10 ** 9)
    try:
        claims = runtime.claim_ledger(pol)
        records = runtime._harness_view(now, runtime.swarm_settings())['records']
    except Exception:
        return
    from .state_page import route_key
    latency = st.setdefault('latency', {})
    for c in claims.values():
        if c.get('post_type') != 'literature' and c['state'] == 'accepted' and not c.get('certified'):
            latency.setdefault(route_key(c), {}).setdefault('eligible_at', now)
    all_pair_accepted = cert.get('all_pair_accepted', False)
    sel = {} if all_pair_accepted else selections(records, claims)
    planned_sessions={r.get('author') for r in records if r.get('kind')=='global-plan'}
    sel={a:keys for a,keys in sel.items() if not (s['assignments'].get(a) or {}).get('planning_input') or a in planned_sessions}
    pend = st.get('pending')
    if pend:
        row = s['assignments'].get(pend['session']) or {}
        if row.get('state') == 'completed' and pend['session'] in sel:
            st['initial'] = True
            if pend['final']:
                st['final'] = True  # the final round and the interval rounds are tracked independently
            else:
                st['rounds'] = max(st['rounds'], pend['round'])
            st.setdefault('log', []).append(dict(at=now, session=pend['session'], selected=len(sel[pend['session']]), final=pend['final']))
            st.pop('pending')
            st.pop('retry_failures', None)
        elif row.get('state') in ('completed', 'failed', 'cancelled'):
            st.setdefault('log', []).append(dict(at=now, session=pend['session'], failed=row.get('state'), final=pend['final'], reason='no-published-plan', round=pend['round']))
            st['retry_failures'] = st.get('retry_failures', 0) + 1
            delay = min(cert.get('selector_retry_max_seconds', 900),
                        cert.get('selector_retry_initial_seconds', 60) * 2**min(st['retry_failures']-1, 10))
            st['retry'] = dict(final=pend['final'], round=pend['round'], not_before=now+delay)
            st.pop('pending')  # no selection posted: the round is retried
    certifier_models = sorted({m for sid, m, r in standing_slots(__import__('v2.roles', fromlist=['x']).roster(runtime.strategy)) if r == 'certify'})
    for a, keys in sel.items():
        for k in keys:
            st['obligations'].setdefault(k, list(certifier_models))
            timing=latency.setdefault(k, {})
            timing.setdefault('selected_at', now)
            timing['eligible_to_selection_seconds']=timing['selected_at']-timing.get('eligible_at',timing['selected_at'])
    rk = {route_key(c): c for c in claims.values()}
    # Repair proposals already inherit a review obligation from the rejected claim.
    # Dispatch certification on pair acceptance even if no selector round is due.
    for k, c in rk.items():
        if (all_pair_accepted or c.get('repair_of') is not None) and c.get('post_type') != 'literature' and c['state'] == 'accepted' and not c.get('certified'):
            st['obligations'].setdefault(k, list(certifier_models))
    for k in list(st['obligations']):
        if k not in rk:
            st['obligations'].pop(k)  # superseded by a revision (a new route key) or gone
            continue
        done = rk[k].get('cert_verdicts') or {}
        st['obligations'][k] = [m for m in st['obligations'][k] if str(MODEL_FAMILY.get(m, m)).lower() not in done
                                and str(MODEL_FAMILY.get(m, m)).lower() in required_families(rk[k])]
        if not st['obligations'][k]:
            st['obligations'].pop(k)
    if not free:
        return
    due = now >= start + cert['interval_seconds'] * (st['rounds'] + 1)
    initial_delay = cert.get('initial_round_after_seconds')
    initial_due = initial_delay is not None and not st.get('initial') and now >= start + initial_delay
    due = due or initial_due
    final_due = not st['final'] and now >= stop - cert['final_before_stop_seconds']
    sel_slots = [] if all_pair_accepted else [x for x in free if x[2] == 'select']
    retry = st.get('retry')
    if now < stop - 1 and (due or final_due or retry) and sel_slots and not st.get('pending') and (not retry or now >= retry.get('not_before', now)):
        slot_id, model, _ = sel_slots[0]
        sid = slot_id.replace('#', '-'); branch = 'critic-' + sid
        n = sum(a['branch'] == branch for a in s['assignments'].values()) + 1
        while 'critic:%s:%d' % (sid, n) in s['assignments']:
            n += 1
        aid = 'critic:%s:%d' % (sid, n)
        from .global_planner import make_input
        if hasattr(runtime, 'board'):
            question = (runtime.watchdog.work/'PROBLEM.md').read_text()
            bound_policy=runtime._bound_policy(pol) if hasattr(runtime,'_bound_policy') else pol
            data = runtime.board.planning_snapshot(runtime.secret, question=question, assignments=s['assignments'], policy=bound_policy)
            data['owner'] = aid
            input_record = runtime.board.publish_control_record(runtime.secret, identity=aid+':input:'+data['input_id'], kind='planner-input', body=data)
            brief = SELECT_BRIEF + '\n\ninput_record: %s; input_id: %s; pages: %d' % (input_record, data['input_id'], len(data['pages']))
        else:
            # Minimal scheduler fixtures have no board transport; production always uses paging.
            data = make_input('fixture question', claims, s['assignments'], 0, pol)
            brief = SELECT_BRIEF + '\n' + ''.join(data['pages'])
        spec = dict(standing='select', model=model, role='research', roster_role='critic', mix_role='critic', stance='critic', input_refs=[],
                    brief=brief,
                    branch=branch if branch in s['branches'] else dict(id=branch, approach='global proof planning', target='plan', assumptions=[]))
        # When both are due the interval round runs first; the final round stays due and runs as its own
        # session afterwards (an interval round never consumes it).
        final = retry['final'] if retry else bool(final_due)
        try:
            e._launch(s, aid, spec, {}, headroom)
            if hasattr(runtime,'board'):s['assignments'][aid]['planning_input']=input_record
            s['assignments'][aid]['selector_session_seconds'] = cert.get('selector_session_seconds', 3600)
            st['pending'] = dict(session=aid, final=final, round=retry['round'] if retry else int((now - start) // cert['interval_seconds']))
            st.pop('retry', None)
            _log_launch(runtime, aid, 'selector-final' if final else 'selector-interval')
        except ValueError:
            pass
    held = set()
    for a in s['assignments'].values():
        if a['state'] in LIVE and a.get('standing') == 'certify':
            for k in a.get('preassign') or ():
                held.add((k, a['model']))
    for slot_id, model, srole in free:
        if srole != 'certify':
            continue
        todo = [k for k in sorted(st['obligations'], key=lambda k: (rk[k].get('repair_of') is None, k)) if model in st['obligations'][k] and (k, model) not in held]
        # (while the revision is current: the other certifier's objection does not cancel this check, review ev5)
        if not todo:
            continue
        k = todo[0]; held.add((k, model)); c = rk[k]
        sid = slot_id.replace('#', '-'); branch = 'critic-' + sid
        n = sum(a['branch'] == branch for a in s['assignments'].values()) + 1
        while 'critic:%s:%d' % (sid, n) in s['assignments']:
            n += 1
        revs = c.get('revisions') or []
        rid = (revs[-1].get('record') if revs else None) or c['records']['proposal']
        spec = dict(standing='certify', model=model, role='research', roster_role='critic', mix_role='critic', stance='critic', input_refs=[],
                    brief=checking_brief(CERT_BRIEF) + '\n' + lineage_brief(c) + '\n\nCLAIM (record %s): its full text and evidence bundle follow. Your verdict must say Re: %s.' % (rid, rid),
                    branch=branch if branch in s['branches'] else dict(id=branch, approach='certifier ' + slot_id, target='review', assumptions=[]))
        aid = 'critic:%s:%d' % (sid, n)
        try:
            e._launch(s, aid, spec, {}, headroom)
        except ValueError:
            continue
        s['assignments'][aid]['preassign'] = [k]
        s['assignments'][aid]['bound_record'] = rid  # its verdict counts only for this revision
        timing=latency.setdefault(k, {})
        timing.setdefault('certifier_started_at', now)
        timing['selection_to_start_seconds']=timing['certifier_started_at']-timing.get('selected_at',now)
        _log_launch(runtime, aid, 'certify', [k])
