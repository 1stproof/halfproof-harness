"""Controller-owned current claim identity shared by planning and publication."""
import json
from .global_planner import current, hashed
from .claim_ledger import writer_verified


def manifest(claims, policy, namespace):
    rows=[]
    for c in claims.values():
        text=str(c.get('text') or c.get('statement') or '')
        rid=current(c)
        state=('source-checked' if c['state']=='accepted' else c['state']) if c.get('post_type')=='literature' else ('accepted' if writer_verified(c,policy) else 'awaiting-audit' if c['state']=='accepted' else c['state'])
        origin=(c.get('imported') or {}).get('origin') or '%s#%s'%(namespace,c['records']['proposal'])
        row=dict(c, state=state, from_markdown=True, namespace=namespace, lineage=origin,
                 current_record=rid, records=dict(c['records'], proposal=rid), text=text,
                 statement_sha256=hashed(c.get('statement')), text_sha256=hashed(text),
                 evidence_sha256=hashed([text,c.get('premise_refs'),c.get('premise_bindings'),c.get('source_bindings'),c.get('literature_refs'),c.get('verdicts'),c.get('verdict_records')]),
                 policy_sha256=hashed(policy))
        row['claim_id']=hashed([namespace,origin,rid,c.get('revision',0),row['evidence_sha256'],row['policy_sha256']])
        rows.append(row)
    return dict(schema=1,namespace=namespace,policy_sha256=hashed(policy),claims=rows)


def read(directory):
    from pathlib import Path
    data=json.loads((Path(directory)/'CLAIMS-MANIFEST.json').read_text())
    if data['schema']!=1:raise ValueError('unknown claim manifest')
    for c in data['claims']:
        if (c['namespace']!=data['namespace'] or c['policy_sha256']!=data['policy_sha256'] or
                hashed(c['text'])!=c['text_sha256'] or hashed(c['statement'])!=c['statement_sha256'] or
                hashed([c['text'],c.get('premise_refs'),c.get('premise_bindings'),c.get('source_bindings'),c.get('literature_refs'),c.get('verdicts'),c.get('verdict_records')])!=c['evidence_sha256'] or
                hashed([c['namespace'],c['lineage'],c['current_record'],c.get('revision',0),c['evidence_sha256'],c['policy_sha256']])!=c['claim_id']):
            raise ValueError('claim manifest integrity failure')
    return data['claims']
