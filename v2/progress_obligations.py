"""Operational progress, stage ownership and cost; no mathematical judgments."""
from .global_planner import current, hashed
from .claim_ledger import writer_verified


def progress(claims, assignments, policy, now, latency=None):
    rows=[];supported=[];latency=latency or {}
    for c in claims.values():
        if c.get('post_type')=='literature':continue
        rid=current(c);ident=hashed([c['key'],rid,c.get('revision',0),c.get('text'),c.get('dependency_invalid')])
        live=[a for a in assignments.values() if a.get('state') in ('queued','running','stopping') and
              (a.get('bound_record')==rid or c['key'] in (a.get('preassign') or []) or c.get('route_key') in (a.get('preassign') or []))]
        if writer_verified(c,policy):stage='supported-reasoning';cause='current review policy satisfied';supported.append(ident)
        elif c['state']=='needs-repair':stage='repair';cause='unresolved objection or changed premise'
        elif c['state']=='accepted':
            all_pair_accepted=(policy.get('certification') or {}).get('all_pair_accepted', False)
            stage='certification' if live or all_pair_accepted else 'selection'
            cause='certification running/queued' if live else ('eligible reasoning waits for a certifier slot' if all_pair_accepted else 'eligible reasoning waits for a published plan')
        elif c['state']=='tiebreak':stage='tie-break';cause='pair disagreement'
        elif c['state']=='refuted':stage='refuted';cause='rejected current revision'
        else:stage='pair-review';cause='initial independent reviews incomplete'
        owner=[a['id'] for a in live] or ([c['proposer']] if stage=='repair' else [])
        rows.append(dict(claim_id=ident,current_record=rid,stage=stage,owners=owner,cause=cause,
                         age_seconds=max(0,now-(c.get('at') or now)),latency=latency.get(c.get('route_key',c['key']),{})))
    return dict(current_supported_reasoning=sorted(supported),obligations=rows,
                notes_credit=0,literature_completion_credit=0,stalled_stages=sorted({r['stage'] for r in rows if not r['owners'] and r['stage'] not in ('supported-reasoning','refuted')}))


def stage_costs(attempts,assignments):
    result={}
    for attempt in attempts:
        context=__import__('json').loads(attempt['context']);worker=context.get('worker')
        row=assignments.get(worker,{})
        stage='repair' if row.get('stance')=='repair' else row.get('standing') or ('write-up' if context.get('role')=='report' else 'final-audit' if context.get('role') in ('verify','internal-verifier') else context.get('role','unknown'))
        group=result.setdefault(stage,dict(settled_microusd=0,unresolved_liability_microusd=0,active_reservation_microusd=0,attempts=0,assignments={}))
        for out,key in [('settled_microusd','settled'),('unresolved_liability_microusd','liability'),('active_reservation_microusd','reservation')]:group[out]+=attempt.get(key,0)
        group['attempts']+=1
        group['assignments'][worker]=dict(current_records=[row['bound_record']] if row.get('bound_record') else [],
                                          shared_cost=not bool(row.get('bound_record')),state=row.get('state'))
    return result
