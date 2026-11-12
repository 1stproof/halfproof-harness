"""Public endpoint metadata only: no key read and no completion requests."""
import argparse
import json
from pathlib import Path
import urllib.request
from .route import pinned_route


def inspect_hosts(policy,key,endpoints,profile='production'):
    from v2.pilot_job import required_route_shape
    shape=required_route_shape(policy,key,profile)
    # Union of request parameters used by research/tool and JSON writer envelopes.
    required={'max_tokens','tools','tool_choice','reasoning','include_reasoning','response_format'}
    result=[]
    for tag in pinned_route(policy,key)['provider']['only']:
        row=next((e for e in endpoints if e.get('tag')==tag),None)
        missing=sorted(required-set(row.get('supported_parameters',[]))) if row else sorted(required)
        ok=bool(row and row.get('context_length',0)>=shape['context_tokens'] and
                (row.get('max_completion_tokens') or 0)>=shape['completion_tokens'] and not missing)
        result.append(dict(host=tag,ok=ok,required_shape=shape,missing_parameters=missing,
            context_tokens=row.get('context_length') if row else None,
            completion_tokens=row.get('max_completion_tokens') if row else None))
    return result


def main():
    p=argparse.ArgumentParser();p.add_argument('--output',required=True);a=p.parse_args()
    from v2.deployment import require_compute
    from v2.contracts_v3 import load_contracts
    require_compute();policy=load_contracts();result={}
    for key,row in policy['models'].items():
        url='https://openrouter.ai/api/v1/models/'+row['model_id']+'/endpoints'
        try:
            with urllib.request.urlopen(url,timeout=30) as r:raw=r.read(2*1024*1024+1)
            if len(raw)>2*1024*1024:raise ValueError('metadata too large')
            result[key]=inspect_hosts(policy,key,json.loads(raw)['data']['endpoints'])
        except Exception as e:result[key]=[dict(ok=False,error=str(e))]
    Path(a.output).write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result,indent=2))
    return int(not all(any(r['ok'] for r in rows) for rows in result.values()))

if __name__=='__main__':raise SystemExit(main())
