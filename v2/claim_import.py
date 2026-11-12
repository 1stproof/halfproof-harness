"""Unsettled claims carried into a continuation (operator 2026-10-07, p01 cont1).

A continuation's ledger and routing see only its own board, so a claim an earlier session left
unsettled (open, disputed or needs-repair) could never be accepted. At the start of a
continuation the controller re-posts each such claim on the new board (one record holding its
full text) together with its standing verdicts; the ledger (claim_ledger.ledger, import_claim)
then treats it as a claim of this session: routed to critics who have not judged it, settled by
the same rule (one more accept from a distinct critic can settle a claim with one accept), and
restatements of it attach to it. Settled claims (accepted, refuted) are not imported: they stay
as the earlier session's ledger has them.

collect() is pure: it reads earlier sessions' board records (oldest session first) and returns
the import list; a claim imported into a later session and still unsettled there carries its
original origin, and the latest session's state of each origin wins.
"""
import json

SETTLED = ('accepted', 'refuted')
MAX_TEXT = 40 * 1024  # per record (the board's record bound is 64 KiB of escaped JSON); longer texts go in parts
FILE = 'IMPORTED-CLAIMS.json'


def session_ledger(records, policy, models, bound=None):
    """Ledger of one earlier session as its own controller computed it: when its records carry
    member roles, critics are the registered critic members (as Board._ledger_of); otherwise
    critics are identified by model (sessions before per-member roles). bound: {certifier/tie-break
    session: the claim record it was launched for} from that session's executor rows (review ev5:
    such a verdict counts for that revision only), merged into policy['bound_reviews']."""
    from .claim_ledger import ledger, CRITIC_ROLES
    if bound:
        policy = dict(policy, bound_reviews=dict(policy.get('bound_reviews') or {}, **bound))
    roled = {r.get('author'): (r.get('body') or {}).get('role') for r in records
             if isinstance(r.get('body'), dict) and (r.get('body') or {}).get('role')}
    if roled:
        critics = sorted({w for w, role in roled.items() if str(role).lower() in CRITIC_ROLES})
        recs = []
        for r in records:
            b = r.get('body')
            if isinstance(b, dict) and r.get('author') not in roled:
                r = dict(r, body=dict(b, role='none'))
            recs.append(r)
        policy = dict(policy, critic_models=[], critic_workers=list(dict.fromkeys(
            list(policy.get('critic_workers') or []) + critics + ['(no critic registered)'])))
        return ledger(recs, policy, models)
    return ledger(records, policy, models)


def collect(sessions, policy, *, include_settled=False):
    """sessions: [(session id, records, {worker: model}[, {assignment id: bound record}])], oldest
    first; a session id is unique per run (the run directory's path); the optional fourth element
    binds certifier/tie-break verdicts to the revision their session was launched for. Returns the claims to import (dicts; see item()),
    unsettled in the latest session that has them. A lineage is an origin plus the aliases it
    absorbed: identical claims from several sessions (same statement and the same full text)
    are imported once, every critic slot's latest verdict counted once, and a later session's
    state of the lineage (settled or not) replaces every member's earlier state."""
    from .claim_ledger import _voter, same_title, same_content, needs_audit
    latest, canon = {}, {}
    for n, (name, records, models, *rest) in enumerate(sessions):
        wmodel = dict(models or {})
        from .global_planner import current, hashed
        session_claims=session_ledger(records, policy, models, rest[0] if rest else None)
        all_claims={current(c):c for c in session_claims.values()}
        sources={i:c for i,c in all_claims.items() if c.get('post_type')=='literature'}
        for c in session_claims.values():
            c=dict(c)
            if c.get('premise_refs'):
                c['premise_bindings']=[dict(origin=(all_claims[i].get('imported') or {}).get('origin') or '%s#%s'%(name,all_claims[i]['records']['proposal']),text_hash=hashed(all_claims[i]['text'])) if i in all_claims else dict(origin='missing:%s#%s'%(name,i),text_hash='missing') for i in c['premise_refs']]
            if c.get('literature_refs'):
                c['source_bindings']=[dict(origin=(sources[i].get('imported') or {}).get('origin') or '%s#%s'%(name,sources[i]['records']['proposal']), text_hash=hashed(sources[i]['text'])) if i in sources else dict(origin='missing:%s#%s'%(name,i),text_hash='missing') for i in c['literature_refs']]
            elif (c.get('imported') or {}).get('source_bindings'):
                c['source_bindings']=c['imported']['source_bindings']
            m = c.get('imported') or {}
            origin = m.get('origin') if isinstance(m.get('origin'), str) and m.get('origin') else '%s#%s' % (name, c['records']['proposal'])
            aliases = [a for a in (m.get('aliases') if isinstance(m.get('aliases'), list) else []) if isinstance(a, str)]
            voter_models = dict(m.get('voter_models')) if isinstance(m.get('voter_models'), dict) else {}
            vmods = c.get('verdict_models') or {}
            voter_models = {w: (vmods.get(w) or (voter_models.get(w) if m else None) or wmodel.get(w)) for w in c['verdicts']}
            rid = (c['revisions'][-1]['record'] if c.get('revisions') else None) or c['records']['proposal']
            latest[origin] = dict(c=c, name=name, n=n, origin=origin, voter_models=voter_models, record=rid,
                                  first=m.get('first_session') or name, first_record=m.get('first_record', c['records']['proposal']),
                                  aliases=aliases)
            for a in aliases:
                if a != origin:canon[a] = origin
    groups = []  # identical claims (same statement, same full text), oldest first; settled ones included
    for origin, x in sorted(latest.items(), key=lambda kv: kv[1]['n']):
        if origin in canon:
            continue  # absorbed into another lineage, whose latest state stands for it
        c = x['c']
        i = item(c, origin=origin, session=x['name'], record=x['record'], first_session=x['first'], first_record=x['first_record'],
                 voter_models=x['voter_models'], text=c.get('text') or '', aliases=x['aliases'])
        # An accepted claim still awaiting audit (its accepts from too few models for the writer) is
        # not settled: it is carried over so an auditor of another model can confirm or reopen it.
        i['_settled'] = c['state'] if c['state'] in SETTLED and not needs_audit(c, policy) else None
        twin = next((o for o in groups if same_title(o['title'], i['title']) and same_content(o['text'], i['text'])), None)
        if twin is None:
            groups.append(i)
            continue
        twin['aliases'] = list(dict.fromkeys(twin['aliases'] + [i['origin']] + i['aliases']))
        twin['authors'] = list(dict.fromkeys(twin['authors'] + i['authors']))
        twin['_settled'] = twin['_settled'] or i['_settled']
        for w in i.get('reviewed') or ():  # a later review by a slot (incl. a retraction) replaces its earlier verdict
            if w not in i['verdicts']:
                for old in [o for o in twin['verdicts'] if _voter(o) == _voter(w)]:
                    twin['verdicts'].pop(old)
        for w, v in i['verdicts'].items():  # a later session's verdict of the same critic slot replaces the earlier one
            for old in [o for o in twin['verdicts'] if _voter(o) == _voter(w)]:
                twin['verdicts'].pop(old)
            twin['verdicts'][w] = v
            twin['voter_models'][w] = i['voter_models'].get(w)
    out = []
    for i in groups:
        own = {_voter(a) for a in i['authors']}
        from .claim_ledger import is_cert, MODEL_FAMILY
        from .certifier_repair import required_families, reviewer_family
        i['verdicts'] = {w: v for w, v in i['verdicts'].items()
                         if (reviewer_family(w, i['voter_models'].get(w)) in required_families(i)
                             if is_cert(w) else _voter(w) not in own)}
        settled = i.pop('_settled')
        cw = {str(k).lower(): str(v).lower() for k, v in (policy.get('critic_weights') or {}).items()}
        wt = lambda w: cw.get(str(i['voter_models'].get(w) or '').lower(), 'full')
        i['verdicts'] = {w: v for w, v in i['verdicts'].items() if wt(w) != 'none'}
        rej = [w for w, v in i['verdicts'].items() if v == 'reject']
        k_ref = int((policy.get('acceptance') or {}).get('k_refute') or 1)
        full_rej = sum(wt(w) == 'full' for w in rej)
        if ((policy.get('acceptance') or {}).get('mode') == 'pair'):
            # Pair rule (review ev3): settled only when the pair (or a tie-break) resolved it.
            from .claim_ledger import pair_outcome
            ps = pair_outcome(i['verdicts'], i['voter_models'], policy)
            if not include_settled and (settled or ps == 'refuted'):
                continue  # certified (writer-verified) accepts and refutations are settled
            # An accepted claim not yet certified, or sent back by a certifier, is carried over (review ev4).
            i['state'] = 'needs-repair' if i.get('state') == 'needs-repair' else ps if ps in ('accepted', 'needs-repair', 'tiebreak') else 'open'
            i['accepts'] = sum(v == 'accept' for v in i['verdicts'].values())
            i['voter_models'] = {w: m for w, m in i['voter_models'].items() if m and w in i['verdicts']}
            out.append(i)
            continue
        if not include_settled and (settled or full_rej >= k_ref or (len(rej) > full_rej and len(rej) >= max(2, k_ref))):  # as the ledger's state_of
            continue  # settled in some session, or refuted by standing rejections: stays settled
        i['state'] = 'needs-repair' if 'object' in i['verdicts'].values() else i['state'] if i['state'] == 'accepted' else 'open'
        i['accepts'] = sum(v == 'accept' and wt(w) == 'full' for w, v in i['verdicts'].items())  # counting accepts
        i['voter_models'] = {w: m for w, m in i['voter_models'].items() if m and w in i['verdicts']}
        out.append(i)
    order = {x[0]: n for n, x in enumerate(sessions)}
    out.sort(key=lambda i: (order.get(i['first_session'], -1), i['first_record'] if isinstance(i['first_record'], int) else 0))
    return out


def item(c, *, origin, session, record, first_session, first_record, voter_models, text, aliases=()):
    return dict(repair_depth=c.get('repair_depth', 0), repair_author_families=c.get('repair_author_families', []), revision_author=c.get('revision_author'), revision_author_model=c.get('revision_author_model'),
                repair_of=c.get('repair_of'), repair_verdict=c.get('repair_verdict'), repair_complete=c.get('repair_complete'),
                literature=c.get('literature'),premise_bindings=c.get('premise_bindings',[]),premise_refs=c.get('premise_refs',[]),literature_evidence_required=c.get('literature_evidence_required',False),literature_evidence_verified=c.get('literature_evidence_verified',False),source_bindings=c.get('source_bindings', []), literature_refs=c.get('literature_refs', []), post_type=c.get('post_type', 'reasoning'), origin=origin, key=c['key'], statement=c.get('statement') or '', title=c.get('title') or [],
                text=str(text or c.get('statement') or ''), proposer=c['proposer'], proposer_model=c.get('proposer_model'),
                authors=list(c.get('authors') or [c['proposer']]),
                verdicts=dict(c['verdicts']), voter_models={w: m for w, m in voter_models.items() if w in c['verdicts']},
                state=c['state'], accepts=c.get('accepts', 0), kind=c.get('kind') or 'finding',
                has_finding=bool(c.get('has_finding', True)), self_checked=bool(c.get('self_checked')),
                session=session, record=record, first_session=first_session, first_record=first_record, aliases=list(aliases),
                reviewed=list(c.get('reviewed_by') or []))


def _display(session):
    return str(session).rstrip('/').rsplit('/', 1)[-1]


def bodies(i, fits):
    """Board record bodies for one import: the claim record (with the metadata) and, when the text
    does not fit one record, continuation parts (comments pointing to it). fits(body) -> bool."""
    v = i.get('verdicts') or {}
    counts = ', '.join('%d %s' % (sum(x == k for x in v.values()), k) for k in ('accept', 'reject', 'object')
                       if any(x == k for x in v.values())) or 'no verdicts'
    meta = {k: i.get(k) for k in ('repair_depth', 'repair_author_families', 'revision_author', 'revision_author_model', 'repair_of', 'repair_verdict', 'repair_complete', 'literature', 'premise_bindings', 'premise_refs', 'literature_evidence_required', 'literature_evidence_verified', 'source_bindings', 'literature_refs', 'post_type', 'origin', 'key', 'statement', 'title', 'proposer', 'proposer_model', 'authors', 'verdicts',
                                  'voter_models', 'state', 'kind', 'has_finding', 'self_checked', 'first_session', 'first_record',
                                  'aliases')}
    text = str(i.get('text') or '')
    note = ('Carried over from an earlier session of this team (%s, record #%s), where it was left %s (%s). '
            'Its earlier verdicts stand; it is reviewed and settled here like any claim. '
            'Review it by this record\'s id.' % (_display(i.get('first_session')), i.get('first_record'),
                                                  'accepted by critics of one model, awaiting audit' if i.get('state') == 'accepted' else i.get('state'), counts))
    head = {'post_type': i.get('post_type') or 'reasoning', 'task': str(i.get('statement') or '')[:400], 'text': text, 'note': note, 'imported': meta}
    if fits(head):
        return head, []
    # Too long for one record: the text goes in parts, each a record of its own (the head first).
    def take(rest, make):
        n = min(len(rest), MAX_TEXT)
        while n > 64 and not fits(make(rest[:n])):
            n = n * 3 // 4
        if not fits(make(rest[:n])):
            raise ValueError('import metadata does not fit one board record')
        return n
    n = take(text, lambda t: dict(head, text=t, parts=999999))
    chunks, rest = [text[:n]], text[n:]
    while rest:
        n = take(rest, lambda t: part_body(t, 10 ** 12, 999999, 999999))
        chunks.append(rest[:n]); rest = rest[n:]
    head = dict(head, text=chunks[0], parts=len(chunks),
                note=note + ' The text continues in %d continuation records that point back to this one.' % (len(chunks) - 1))
    return head, chunks[1:]


def part_body(text, head_id, k, total):
    return {'text': text, 'about': str(head_id), 're': head_id, 'part': k, 'of': total}


def body(i):
    return bodies(i, lambda b: True)[0]


def load(seed_dir):
    from pathlib import Path
    p = Path(seed_dir) / FILE
    if not p.is_file():
        return []
    data = json.loads(p.read_text())
    return data.get('claims', []) if isinstance(data, dict) else []
