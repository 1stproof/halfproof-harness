"""Redesign part A: critic routing, the STATE.md page and the no-content-share metric.

Pure functions over the claim ledger (v2/claim_ledger.py); the runtime wiring lives in
SwarmRuntime.refresh_board / _flush_harness_events. Every behavior is a parameter in
roles/<swarm>/policy.json (absent = the default below):

  routing.enabled               true   route open claims to critics automatically
  routing.max_per_message       10     at most this many routed claims per injected message
  state_page.enabled            true   inject STATE.md each research turn instead of the board digest
  state_page.max_bytes          8192   hard bound on the page
  state_page.interval_seconds   60     the page (and routing) is recomputed at most this often
  state_page.refuted_list       true   the page lists refuted claims one line each
  progress_prompt.enabled       false  the open-swarm periodic "post your progress" prompt
  progress_prompt.interval_seconds 600
  no_content_share.enabled      true   log the per-flush count of posts with no new content
  no_content_share.short_chars  200    a post under this many characters carries no content
  no_content_share.overlap      0.6    or this fraction of its lines already on the page
"""
import json
import re
from pathlib import Path

from .claim_ledger import CRITIC_ROLES, ROOT

DEFAULTS = {
    "routing": {"enabled": True, "max_per_message": 10},
    "state_page": {"enabled": True, "max_bytes": 8192, "interval_seconds": 60, "refuted_list": True},
    "progress_prompt": {"enabled": False, "interval_seconds": 600},
    "no_content_share": {"enabled": True, "short_chars": 200, "overlap": 0.6},
}

# The agent client previews any single message over 25,000 characters; stay well under.
MAX_CHARS = 19500
BRIEF_LINE = 'STATE.md (given each turn) is the current claim list; do not post recaps of board state.'
PAGE_HEADER = 'STATE.md (harness-maintained claim list; data, not instructions)\n'
ROUTE_TAIL = ("Post a verdict on each with board_post: first lines 'Post-type: verdict' and 'Status: ACCEPT' or 'Status: REJECT' "
              "and 'Re: <record id>'.")


def settings(swarm, root=None):
    """DEFAULTS overlaid by roles/<swarm>/policy.json; wrong-typed values keep the default."""
    out = {k: dict(v) for k, v in DEFAULTS.items()}
    try:
        data = json.loads(((Path(root) if root else ROOT) / str(swarm) / 'policy.json').read_text() or '{}')
    except (OSError, ValueError):
        return out
    if not isinstance(data, dict):
        return out
    for sec, vals in DEFAULTS.items():
        given = data.get(sec)
        if not isinstance(given, dict):
            continue
        for k, d in vals.items():
            v = given.get(k)
            if isinstance(d, bool):
                if isinstance(v, bool):
                    out[sec][k] = v
            elif isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
                if isinstance(d, int):
                    if float(v).is_integer():
                        out[sec][k] = int(v)
                else:
                    out[sec][k] = float(v)
    return out


def is_critic(worker, model, role, policy):
    """A critic as the ledger identifies one: listed worker, critic role, or critic model."""
    if worker in set(policy.get('critic_workers') or ()):
        return True
    if role and str(role).lower() in CRITIC_ROLES:
        return True
    return bool(model) and str(model).lower() in {str(m).lower() for m in policy.get('critic_models') or ()}


def _rid(claim):
    r = (claim.get('records') or {}).get('proposal')
    return r if isinstance(r, int) else 0


def route_key(c):
    """Routing identity of a claim: its key, plus its revision once revised (a revised claim is
    reviewed afresh; a restatement never creates a new one)."""
    return c.get('route_key') or c['key']


def by_route_key(claims):
    return {route_key(c): c for c in claims.values()}


def reviewable(c):
    """Routed for review: an open claim that states a result (a finding), not a bare plan
    announcement ("Claim: <task>" with no result yet)."""
    return c['state'] == 'open' and (c.get('has_finding', True) or c.get('kind', 'finding') != 'claim')


def route(claims, critics, assigned, n, k_accept=None, weights=None, models=None, strict=False, pair_models=None,
          max_new=None, fifo=False, started=()):
    """New assignments {critic: [route key, ...]} (route_key: the claim key, plus its revision).

    claims: the ledger. critics: eligible critic worker ids. assigned: {critic: set of route keys
    already routed to it}. Each open claim with fewer than n verdicts reaches n distinct
    critics (verdicts given count), never its proposer or a reviser; the least-loaded critic goes first.
    models: {critic: model family}; when given, a critic of a model not yet among the claim's verdicts or
    pending reviewers goes first (operator 2026-10-09: the two reviews of a claim run in parallel on two
    models, so no serial second-model step follows).
    max_new bounds new reservations per voter. fifo orders immutable revisions by
    their current post record, including missing-family reviews and failed retries.
    started is retained for caller compatibility; start evidence never changes queue age.
    """
    from .claim_ledger import _voter  # sessions of one standing critic slot count once
    rk = by_route_key(claims)
    weights = weights or {}  # critic id -> 'full' | 'advisory' | 'none' (absent = full)
    critics = sorted(x for x in set(critics) if weights.get(x, 'full') != 'none')
    advisory = {x for x in set(critics) | set(assigned) if weights.get(x, 'full') != 'full'}
    assigned = {c: set(keys or ()) for c, keys in assigned.items()}
    for c in critics: assigned.setdefault(c, set())
    load = {c: sum(1 for k in assigned[c] if k in rk and rk[k]['state'] == 'open'
                   and _voter(c) not in {_voter(v) for v in rk[k]['verdicts']}) for c in critics}
    new = {}
    blockers = [x for x in critics if weights.get(x) == 'block'] if models else []
    def room(x):
        return max_new is None or len(new.get(x, ())) < max_new
    def order(c):
        revs = c.get('revisions') or []
        return (revs[-1]['record'] if revs else _rid(c), route_key(c)) if fifo else (c.get('repair_of') is None, _rid(c))
    for c in sorted(claims.values(), key=order):
        if blockers and c['state'] == 'accepted':
            # A claim accepted before its blocking vote landed (e.g. that session failed) still gets one.
            k = route_key(c)
            seen_b = {_voter(v) for v in c['verdicts']} | {_voter(v) for v in c.get('reviewed_by') or ()} \
                | {_voter(x) for x in blockers if k in assigned[x]}
            if not any(_voter(x) in seen_b for x in blockers):
                own = {_voter(a) for a in (c.get('authors') or [c['proposer']])}
                bp = sorted((x for x in blockers if _voter(x) not in own and room(x)), key=lambda x: (load[x], x))
                if bp:
                    assigned[bp[0]].add(k); load[bp[0]] += 1; new.setdefault(bp[0], []).append(k)
            continue
        if c.get('post_type') == 'literature' and models:
            if reviewable(c): _route_pair(c, critics, assigned, load, new, models, ['k3'], max_new)
            continue
        if strict and pair_models and models:
            if reviewable(c):
                _route_pair(c, critics, assigned, load, new, models, pair_models, max_new)
            continue
        if not reviewable(c) or len([w for w in c['verdicts'] if w not in set(c.get('advisory') or ()) and w not in advisory]) >= n:
            continue
        # Every open claim is routed at once; proposer.max_unreviewed_claims only adds the
        # proposer reminder (proposer_controls.nudge), never delays review.
        k = route_key(c)
        voted = {_voter(v) for v in c['verdicts']}
        pending = {_voter(x) for x in assigned if k in assigned[x]} - voted - {_voter(v) for v in c.get('reviewed_by') or ()}
        have = voted | pending
        need = n - len(have - {_voter(a) for a in advisory} - {_voter(w) for w in c.get('advisory') or ()})
        if k_accept:
            # Operator 2026-10-07: no reviews after settlement. Route only as many critics as the
            # accepts still missing (one more if same-model accepts left it open), never past n.
            accepts = c['accepts'] if isinstance(c.get('accepts'), int) else sum(v == 'accept' for v in c['verdicts'].values())
            counting = {v for v in pending if v not in {_voter(a) for a in advisory}}  # advisory accepts do not count
            need = min(need, max(k_accept - accepts, 0 if counting else 1) - len(counting))
        own = {_voter(a) for a in (c.get('authors') or [c['proposer']])}
        done = {_voter(v) for v in c.get('reviewed_by') or ()}
        pool = sorted((x for x in critics if _voter(x) not in have | done and _voter(x) not in own and room(x)),
                      key=lambda x: (x in advisory if k_accept else False, load[x], x))  # counting critics first
        if models:
            pool = [x for x in pool if x not in advisory]  # model spread among counting critics; blocking votes below
            mv = {_voter(x): models.get(x) for x in assigned}
            def fam_of(v):  # a voter no longer among the critics (an ended session) is known by its slot id
                if mv.get(v):
                    return mv[v]
                slot = str(v).split(':', 1)[-1]
                return next((f for f in set(models.values()) if f and (slot == f or slot.startswith(str(f) + '-'))), None)
            seen = {fam_of(v) for v in have if fam_of(v)} | {str(m).lower() for m in c.get('accept_models') or ()}
            picked = []
            for _ in range(max(0, need)):
                fresh = [x for x in pool if x not in picked and models.get(x) not in seen]
                rest = [x for x in pool if x not in picked]
                if not rest:
                    break
                if strict and not fresh:
                    break  # pair rule: one critic of each pair model, never two of one
                x = (fresh or rest)[0]
                picked.append(x); seen.add(models.get(x))
        else:
            picked = pool[:max(0, need)]
        if models:  # operator 2026-10-09: one 'block' critic (accepts ignored, reject/objection -> repair) in parallel
            if blockers and not any(_voter(x) in have | done for x in blockers):
                bp = sorted((x for x in blockers if _voter(x) not in own and room(x)), key=lambda x: (load[x], x))
                picked += bp[:1]
        for x in picked:
            assigned[x].add(k); load[x] += 1
            new.setdefault(x, []).append(k)
    return new


def _family(m):
    from .claim_ledger import MODEL_FAMILY
    m = str(m or '').lower()
    return str(MODEL_FAMILY.get(m, m)).lower()


def _route_pair(c, critics, assigned, load, new, models, pair_models, max_new=None):
    """Pair rule (operator 2026-10-09): one critic of each pair model per claim. A pair family counts as
    covered by a verdict of that family (its own model, verdict_models) or a pending assignment to a critic
    of that family; off-pair verdicts and a second verdict of one family never cover the other."""
    from .claim_ledger import _voter
    k = route_key(c)
    vm = c.get('verdict_models') or {}
    fam_of_voter = {_voter(x): _family(models.get(x)) for x in critics}
    covered = set()
    for w in c['verdicts']:
        f = _family(vm.get(w)) if vm.get(w) else fam_of_voter.get(_voter(w))
        if not f:
            slot = str(_voter(w)).split(':', 1)[-1]
            f = next((_family(p) for p in pair_models if slot == str(p) or slot.startswith(str(p) + '-')), None)
        if f:
            covered.add(f)
    voted = {_voter(v) for v in c['verdicts']} | {_voter(v) for v in c.get('reviewed_by') or ()}
    covered |= {_family(models.get(x)) for x in assigned if k in assigned[x] and _voter(x) not in voted}
    own = {_voter(a) for a in (c.get('authors') or [c['proposer']])}
    for p in (('k3',) if c.get('post_type') == 'literature' else pair_models):
        f = _family(p)
        if f in covered:
            continue
        pool = sorted((x for x in critics if _family(models.get(x)) == f and _voter(x) not in voted | own
                       and (max_new is None or len(new.get(x, ())) < max_new)),
                      key=lambda x: (load[x], x))
        if pool:
            x = pool[0]; assigned[x].add(k); load[x] += 1; new.setdefault(x, []).append(k); covered.add(f)


def route_audit(claims, auditors, assigned, policy, models, pool=None, available=None, max_new=None):
    """New audit assignments {auditor: [route key, ...]} (operator 2026-10-08).

    A claim accepted by critics of fewer models than writer.min_accept_models goes to ONE auditor
    whose model is not yet among its accept models, never to one of its authors or to a voter that
    already judged it; the least-loaded auditor first. auditors: auditor worker ids; assigned:
    {auditor: set of route keys already routed}; models: {worker: model family}."""
    from .claim_ledger import _voter, needs_audit
    assigned = {a: set(assigned.get(a) or ()) for a in auditors}
    rk = by_route_key(claims)
    load = {a: sum(1 for k in assigned[a] if k in rk and needs_audit(rk[k], policy)) for a in auditors}
    new = {}
    for c in sorted(claims.values(), key=lambda c: (c.get('repair_of') is None, _rid(c))):
        if not needs_audit(c, policy):
            continue
        k = route_key(c)
        judged = {_voter(v) for v in list(c.get('verdicts') or ()) + list(c.get('reviewed_by') or ())}
        if any(k in assigned[a] and _voter(a) not in judged for a in auditors):
            continue  # an audit is already pending (an auditor that judged or retracted is no longer pending)
        have = {str(m).lower() for m in c.get('accept_models') or ()}
        own = {_voter(x) for x in (c.get('authors') or [c.get('proposer')])}
        done = {_voter(v) for v in list(c.get('verdicts') or ()) + list(c.get('reviewed_by') or ())}
        pool = sorted((a for a in auditors if str(models.get(a) or '').lower() not in have
                       and _voter(a) not in own | done and k not in assigned[a]
                       and (available is None or a in available)
                       and (max_new is None or len(new.get(a, ())) < max_new)), key=lambda a: (load[a], a))
        if pool:
            a = pool[0]
            assigned[a].add(k); load[a] += 1
            new.setdefault(a, []).append(k)
    # Operator 2026-10-08: an idle pool member also reviews an open claim one counting accept short
    # (its verdict counts in full), one member per claim; the critics' routing never waits on it.
    held = set().union(*assigned.values()) if assigned else set()
    for c in sorted(claims.values(), key=lambda c: (c.get('repair_of') is None, _rid(c))):
        if not (reviewable(c) and c.get('accepts', 0) == 1):
            continue
        k = route_key(c)
        if k in held:
            continue
        own = {_voter(x) for x in (c.get('authors') or [c.get('proposer')])}
        done = {_voter(v) for v in list(c.get('verdicts') or ()) + list(c.get('reviewed_by') or ())}
        members = auditors if pool is None else [a for a in auditors if a in set(pool)]  # pool: who may take open claims
        members = [a for a in members if (available is None or a in available)
                   and (max_new is None or len(new.get(a, ())) < max_new)]
        if not any(load[a] == 0 for a in members):
            break  # no idle member left
        idle = sorted(a for a in members if load[a] == 0 and _voter(a) not in own | done)
        if not idle:
            continue  # this claim is not for the idle members; a later one may be
        a = idle[0]; assigned[a].add(k); load[a] += 1; held.add(k)
        new.setdefault(a, []).append(k)
    return new


def _line(c, extra=''):
    rid = (c.get('revisions') or [{}])[-1].get('record') or _rid(c)
    label = c.get('post_type', 'reasoning').upper() + (' REPAIR PROPOSAL' if c.get('repair_of') is not None else '')
    return '- #%d [%s] %s%s' % (rid, label, ' '.join(str(c.get('statement') or '').split())[:160], extra)


def _section(title, items, budget, fmt=_line):
    """Newest first in full while the budget lasts; older ones as a count and id range."""
    if not items:
        return ''
    items = sorted(items, key=_rid, reverse=True)
    head = '\n## %s (%d)\n' % (title, len(items))
    out, size = [head], len(head.encode())
    for i, c in enumerate(items):
        line = fmt(c) + '\n'
        if size + len(line.encode()) > budget - 60:
            rest = items[i:]
            out.append('- ... %d older: ids #%d-#%d\n' % (len(rest), min(map(_rid, rest)), max(map(_rid, rest))))
            break
        out.append(line); size += len(line.encode())
    return ''.join(out)


def own(c, slot):
    """True when slot (any session of it) authored claim c."""
    from .claim_ledger import _voter
    return _voter(slot) in {_voter(a) for a in (c.get('authors') or [c.get('proposer')])}


def render(claims, *, n_critics=3, max_bytes=8192, refuted_list=True, hide_own=None, policy=None):
    """The STATE.md text, at most max_bytes bytes (UTF-8). hide_own: a critic slot id; its own
    claims are left out of the lists awaiting review (it never reviews them). policy: the ledger
    policy; with writer.min_accept_models > 1, accepted claims still awaiting audit are listed apart."""
    from .claim_ledger import needs_audit
    by = {s: [c for c in claims.values() if c.get('state') == s] for s in ('open', 'accepted', 'disputed', 'refuted', 'needs-repair')}
    audit = [c for c in by['accepted'] if policy is not None and needs_audit(c, policy)]
    by['accepted'] = [c for c in by['accepted'] if c not in audit]
    if hide_own:
        audit = [c for c in audit if not own(c, hide_own)]
    if hide_own:
        for s in ('open', 'disputed'):
            by[s] = [c for c in by[s] if not own(c, hide_own)]
    head = (PAGE_HEADER + 'Claims: %d total; %d accepted, %s%d refuted, %d disputed, %d needs repair, %d open.\n'
            % (len(claims), len(by['accepted']), ('%d awaiting audit, ' % len(audit)) if audit else '', len(by['refuted']),
               len(by['disputed']), len(by['needs-repair']), len(by['open'])))
    room = max(0, max_bytes - len(head.encode()))

    def verdicts(c):
        v = list(c['verdicts'].values())
        return ' [%d accept, %d reject, %d objection of %d critics]' % (v.count('accept'), v.count('reject'), v.count('object'), n_critics)
    shares = [('Open claims awaiting critics', by['open'], .3, lambda c: _line(c, verdicts(c))),
              ('Accepted', by['accepted'], .25, _line),
              ('Accepted by one model, awaiting audit (not yet usable as verified in the final report)', audit, .15, _line),
              ('Disputed', by['disputed'], .2, lambda c: _line(c, verdicts(c))),
              ('Needs repair (a critic objected; proposer to repair or retract)', by['needs-repair'], .15, lambda c: _line(c, verdicts(c)))]
    if refuted_list:
        shares.append(('Refuted ideas (do not retry)', by['refuted'], .25, _line))
    used = sum(s[2] for s in shares if s[1]) or 1
    body = ''.join(_section(t, items, int(room * w / used), f) for t, items, w, f in shares)
    text = head + body
    raw = text.encode()
    text = text if len(raw) <= max_bytes else raw[:max_bytes].decode(errors='ignore')
    return text[:MAX_CHARS]


def _route_line(rid, first):
    return '\n- %s %s' % (rid, ' '.join(str(first).split())[:160])


def route_fits(items):
    """Keys of the leading items whose lines fit in one message under MAX_CHARS."""
    out, size = [], len('Claims routed to you for review:\n' + ROUTE_TAIL) + 50
    for key, first in items:
        n = len(_route_line(10 ** 9, first))
        if size + n > MAX_CHARS:
            break
        out.append(key); size += n
    return out


REPAIR_HEAD = 'Claims of yours needing repair:'
REPAIR_TAIL = ' \u2014 address the objection and repost or retract'


def repair_fits(items):
    """Keys of the leading (key, first line) items that fit one repair message under MAX_CHARS."""
    out, size = [], len(REPAIR_HEAD) + 50
    for key, first in items:
        n = len(_route_line(10 ** 9, first) + REPAIR_TAIL)
        if size + n > MAX_CHARS:
            break
        out.append(key); size += n
    return out


def repair_message(items):
    """items: [(record id, first line)] of the proposer's needs-repair claims."""
    return (REPAIR_HEAD + ''.join(_route_line(rid, first) + REPAIR_TAIL for rid, first in items))[:MAX_CHARS]


def route_message(items):
    """items: [(record id, first line)]."""
    lines = ''.join(_route_line(rid, first) for rid, first in items)
    budget = MAX_CHARS - len('Claims routed to you for review:\n' + ROUTE_TAIL) - 50  # the route_fits budget
    return 'Claims routed to you for review:' + lines[:budget] + '\n' + ROUTE_TAIL


_LABEL = re.compile(r"\s*[*#>_-]*\s*[*_]*\s*(status|claim|re|task)\s*[*_]*\s*[:=]", re.I)


def no_content(text, page, *, short_chars=200, overlap=0.6):
    """A post with no new claim/critique/proof content: no Status/Claim/Re/Task first lines,
    and either under short_chars characters or mostly lines already on the state page."""
    text = str(text or '')
    if any(_LABEL.match(l) for l in text.splitlines()[:8]):
        return False
    if len(text.strip()) < short_chars:
        return True
    norm = lambda s: ' '.join(re.sub(r'[^a-z0-9 ]', ' ', s.lower()).split())
    lines = [norm(l) for l in text.splitlines() if len(norm(l)) >= 12]
    if not lines:
        return True
    pagetext = norm(page or '')
    return sum(l in pagetext for l in lines) >= overlap * len(lines)
