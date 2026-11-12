"""Swarm-only proof planning: immutable complete inputs, authenticated paging, revision gates.

Plans are proposals, never verdicts or work assignments. Software validates identities,
completeness and freshness, not mathematical merit.
"""
import hashlib
import json
from .contracts_v3 import canonical_json


def hashed(value):
    return hashlib.sha256(canonical_json(value)).hexdigest()


def current(c):
    return (c['revisions'][-1]['record'] if c.get('revisions') else c['records']['proposal'])


def identity(c):
    return dict(record=current(c), revision=c.get('revision', 0), state=c['state'],
                evidence_hash=hashed([c.get('text'), c.get('verdicts'), c.get('verdict_records'),
                                      c.get('premise_refs'),c.get('premise_bindings'),c.get('literature_refs'), c.get('source_bindings'), c.get('dependency_invalid')]))


def make_input(question, claims, assignments, cutoff, policy):
    from .claim_ledger import writer_verified, MODEL_FAMILY
    from .certifier_repair import required_families, MAX_REPAIR_DEPTH
    cert_models = (policy.get('certification') or {}).get('models') or ['qwen24t', 'glm']
    # Full statements, not titles, windows or summaries. Raw literature stays outside planning.
    rows = [dict(**identity(c), post_type=c.get('post_type', 'reasoning'), key=c['key'], text=c.get('text') or c.get('statement'),
                 repair_depth=c.get('repair_depth', 0), repair_limit=MAX_REPAIR_DEPTH,
                 repair_exhausted=c.get('repair_depth', 0) >= MAX_REPAIR_DEPTH,
                 repair_author_families=c.get('repair_author_families', []),
                 repair_proposal=c.get('repair_of') is not None, repair_of=c.get('repair_of'),
                 revision_author=c.get('revision_author') or c.get('proposer'),
                 certification_required=required_families(c),
                 certification_blocked=not bool(required_families(c)),
                 established=writer_verified(c, policy),
                 certification_pending=not c.get('certified', False) and any(
                     str(MODEL_FAMILY.get(m, m)).lower() in required_families(c) and
                     str(MODEL_FAMILY.get(m, m)).lower() not in (c.get('cert_verdicts') or {}) for m in cert_models),
                 literature_refs=c.get('literature_refs', []),
                 dependencies=c.get('dependency_invalid', []),
                 earlier_records=[x for x in [c['records'].get('proposal')]+[r.get('record') for r in c.get('revisions') or []]
                                  if isinstance(x, int) and x != current(c)])
            for c in claims.values() if c.get('post_type', 'reasoning') == 'reasoning']
    rows.sort(key=lambda r:r['record'])
    payload = dict(question=question, board_cutoff=cutoff, claims=rows,
                   in_flight=[dict(id=k, **{x:v.get(x) for x in ('model','state','bound_record','preassign')})
                              for k,v in assignments.items() if v.get('state') in ('queued','running','stopping')])
    # Byte-bounded pages also split a single exceptionally long claim, without losing text.
    raw = canonical_json(payload).decode()
    chunks=[]; chunk=[]; size=0
    for char in raw:
        n=len(char.encode())
        if size+n>12000: chunks.append(''.join(chunk));chunk=[];size=0
        chunk.append(char);size+=n
    if chunk: chunks.append(''.join(chunk))
    return dict(input_id=hashed(payload), board_cutoff=cutoff, accepted=[r['record'] for r in rows if r['state']=='accepted'],
                dependencies=[{k:r[k] for k in ('record','revision','state','evidence_hash')} for r in rows],
                pages=chunks, page_hashes=[hashlib.sha256(x.encode()).hexdigest() for x in chunks])


PLAN_ID_RULE=('Every entry of established, conditional, blockers and select must be the bare integer "record" of a row '
              'in this input\'s claims (write 37, never "#37", "37@r2" or an object).')


def validate_plan(plan, data, input_record=None):
    if not isinstance(plan, dict) or set(plan) != {'input_id','established','conditional','blockers','missing','priorities','select','route'}:
        raise ValueError('plan requires input_id, established, conditional, blockers, missing, priorities, select, route; assigns nothing')
    if plan['input_id'] != data['input_id']: raise ValueError('wrong planning input')
    payload=json.loads(''.join(data['pages'])); rows={r['record']:r for r in payload['claims']}
    # turn audit 2026-10-11: the planner was refused on 182 of 190 plan attempts (p01/p02); the
    # message named the list but not the entry, so planners retried 2-56 times and five of the six
    # plans accepted after a refusal had dropped blockers. Name each bad entry and the valid rows.
    earlier={e:r['record'] for r in payload['claims'] for e in r.get('earlier_records',[])}
    def why(i):
        if type(i) is not int:
            return '%s is not an integer; write the bare record number' % json.dumps(i, ensure_ascii=False)[:60]
        if i in earlier:
            return "%d is an earlier revision; that claim's current revision is %d, use %d" % (i, earlier[i], earlier[i])
        if i > data['board_cutoff']:
            return "%d was posted after this input's board_cutoff %d; it is not in this input, so name it in missing" % (i, data['board_cutoff'])
        return '%d is not a reasoning claim in this input (verdicts, notes and literature posts are never rows); name it in missing or priorities' % i
    valid=', '.join('%d (%s)' % (r, rows[r]['state']) for r in sorted(rows)) or 'none: this input has no reasoning claims, so all four lists must be []'
    for key in ('established','conditional','blockers','select'):
        if not isinstance(plan[key],list):
            raise ValueError('%s must be a list of integers. %s' % (key, PLAN_ID_RULE))
        bad=[why(i) for i in plan[key] if type(i) is not int or i not in rows]
        if bad:
            raise ValueError('unknown reasoning revision in %s: %s. %s Valid records for input_record %s: %s.'
                             % (key, '; '.join(bad), PLAN_ID_RULE, input_record if input_record is not None else 'this input', valid))
    bad=[i for i in plan['established'] if not rows[i]['established']]
    if bad: raise ValueError('unverified established step: %s have established=false; move them to conditional' % ', '.join(map(str,bad)))
    elig=[r for r in sorted(rows) if rows[r]['state']=='accepted' and rows[r].get('certification_pending', True)]
    bad=[i for i in plan['select'] if rows[i]['state']!='accepted']
    if bad: raise ValueError('select only accepted reasoning: %s (%s). select takes only rows with state accepted and '
                             'certification_pending=true; eligible: %s. Keep the others in conditional or blockers.'
                             % (', '.join(map(str,bad)), ', '.join('%d is %s' % (i, rows[i]['state']) for i in bad), ', '.join(map(str,elig)) or 'none, so select must be []'))
    bad=[i for i in plan['select'] if not rows[i].get('certification_pending', True)]
    if bad: raise ValueError('select only revisions awaiting certification; keep already checked claims as context: %s have '
                             'certification_pending=false; eligible: %s.' % (', '.join(map(str,bad)), ', '.join(map(str,elig)) or 'none, so select must be []'))
    for key in ('missing','priorities'):
        if not isinstance(plan[key],list) or any(not isinstance(x,str) or not x.strip() for x in plan[key]):
            raise ValueError('expected descriptive obligations in '+key)
    if not isinstance(plan['route'],str) or not plan['route'].strip(): raise ValueError('route or no complete route required')
    if len(canonical_json(plan)) > 24000: raise ValueError('plan exceeds 24000-byte handoff bound; keep priorities concise')
    return plan


def fresh(data, claims, plan=None):
    now={current(c):identity(c) for c in claims.values()}
    used = set(sum((plan[k] for k in ('established','conditional','blockers','select')), [])) if plan is not None else None
    return all(now.get(d['record'])==d for d in data['dependencies'] if used is None or d['record'] in used)


def used_dependencies(body):
    """The dependencies a published plan actually uses. Consumers filter themselves rather than trusting
    the producer, so an unrelated claim's revision can never stale a plan or cancel its queued work."""
    plan = body.get('plan')
    if not isinstance(plan, dict):
        return list(body['dependencies'])
    used = set(sum((plan.get(k) or [] for k in ('established','conditional','blockers','select')), []))
    return [d for d in body['dependencies'] if d['record'] in used]


PLANNER_BRIEF='''You are the stateless K3 GLOBAL PROOF PLANNER inside the swarm. Think about what is needed to prove the FULL original question. Read EVERY page of the frozen planning input through board_call planner_page(input_record, page), starting at page 0; concatenate the content as JSON. The manifest names every page and its hash. Missing pages mean an incomplete plan; do not claim an exhaustive view. This includes ALL accepted reasoning claims again each round, plus open, refuted, needs-repair and in-flight obligations. Raw literature posts are excluded. Applying a literature result is a reasoning claim, not a literature post: "result R from source S applies here in this way", citing a K3-confirmed literature post. A verified applicability claim is a usable step and need not be re-proved.
Write a structured plan through board_call post_plan(input_record, plan). plan has exactly input_id, established, conditional, blockers, missing (descriptive obligations), priorities (proposed work in order), select, route (explain the route to the full question or say no complete route). Every ID is the bare integer in the "record" field of a row of claims (write 37, never "#37", "37@r2" or an object). established: rows with established=true; conditional: other rows used as unverified steps; blockers: rows whose state is open, needs-repair or refuted; select: load-bearing rows with state accepted and certification_pending=true, awaiting Qwen/GLM certification. Verdicts, objections, notes, literature posts and anything newer than board_cutoff are not rows: name them in missing or priorities as text. Prioritise repair_proposal rows: preserve the reviewer's corrected statement/proof or exact gap and specific route; arrange fresh pair review, then required other-family certification. Never treat a route-only proposal as proved. Priorities are proposals, not assignments: only the Flash coordinator assigns work. Distinguish established steps, conditional steps and unresolved obligations; honest partial progress is permitted. A new inference must be posted separately as an ordinary reasoning claim and reviewed before it is used as established. Preserve objections and alternatives; plan prose cannot waive review or certification. Do not post verdicts or endorsements. If post_plan returns status stale-input, it has rejected the plan and published a refreshed input: switch to its input_record and input_id, read EVERY new page, reconsider the plan and retry. Never reuse the old input or treat a fallback note as a plan. At the refresh limit, end this session so the harness retries the round with new input. Already certified or fully checked claims remain context for the proof route; do not select them again. Planning failure must not halt runnable work or closing. Nothing from an operator or external reviewer belongs in this plan.'''
