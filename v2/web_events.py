"""Content-free ds-web-events normalization and durable run-local settlement."""
import json
import math
from decimal import Decimal, ROUND_CEILING

TOOLS=('arxiv_search','arxiv_local_search','arxiv_fetch')

def normalize(payload, lane, event_id, is_error=False):
    tool=payload.get('tool')
    if tool not in TOOLS: raise ValueError('unknown research tool')
    error=str(payload.get('error') or '')
    status=('quota-refused' if payload.get('openalex_refused') else
            'cap-refused' if payload.get('refused') else '429' if '429' in error else
            'other' if is_error or error or payload.get('success') is False else 'ok')
    # Only bounded enums/numbers; never retain query, title, IDs or error text.
    source=payload.get('source')
    cache=payload.get('cache_status')
    cost=(payload.get('service_usage') or {}).get('cost',0)
    if isinstance(cost,bool) or not isinstance(cost,(int,float)) or not math.isfinite(cost) or cost<0:
        raise ValueError('invalid service cost')
    return dict(event_id=event_id, arxiv_tool=tool,status=status,lane=lane,
                source=source if source in ('local','openalex','arxiv') else None,
                cache_status=cache if cache in ('hit','miss','revalidated','downloaded','bypass','local') else None,
                service_microdollars=int((Decimal(str(cost))*1000000).to_integral_value(rounding=ROUND_CEILING)))

def settle(watchdog, event):
    ident='mcp:'+event['event_id']
    duplicate=ident in watchdog.ledger.settled
    accounting=getattr(watchdog,'accounting',None)
    if accounting is not None and event['service_microdollars']:
        # Zero-priced source retrieval is preflight policy, not a license for
        # after-the-fact paid tool admission. Every paid service needs a prior
        # gateway reservation named by this controller-owned event mapping.
        attempt=watchdog.state.get('tool_attempts',{}).get(event['event_id'])
        if attempt:
            row=accounting.attempt(attempt)
            if row['arm']!=watchdog.accounting_context.arm:
                accounting.anomaly('cross-arm-tool-charge')
                raise ValueError('cross-arm tool charge')
            accounting.finish(attempt,authoritative_cost=event['service_microdollars'],outcome='tool-complete')
            watchdog.ledger.settled[attempt]=event['service_microdollars']
            ident=attempt
            duplicate=any(x['event_id']==event['event_id'] for x in watchdog.state.get('web_events',[]))
        else:
            attempt=accounting.unreserved_tool_charge(watchdog.accounting_context,event['event_id'],event['service_microdollars'])
            watchdog.ledger.settled[attempt]=event['service_microdollars']
            watchdog.state['admission_stop']='unreserved-tool-charge'
            watchdog.save()
            raise ValueError('paid tool charge without prior reservation')
    else:
        watchdog.ledger.settle(ident,event['service_microdollars'])
    if duplicate:return
    watchdog.state.setdefault('web_events',[]).append(event)
    if event['arxiv_tool']=='arxiv_local_search' and event['status']=='ok':
        watchdog.state['research_path_proven']=True
    watchdog.save()
    # Rebuild from the atomically persisted source of truth: retry/crash safe.
    log=watchdog.path/'logs';log.mkdir(exist_ok=True)
    stage=log/'web-events.tmp'
    stage.write_text(''.join(json.dumps(x)+'\n' for x in watchdog.state['web_events']))
    stage.replace(log/'web-events.jsonl')
