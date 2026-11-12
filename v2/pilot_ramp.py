"""Deterministic admission ramp measured on ordinary live cohort work only.

No additional prompts, synthetic task, paid probes or claims of sustained
capacity. A missing rung remains unqualified. State lives in the P3 checkpoint.
"""
import json
from .accounting import AdmissionDenied,canonical

RUNGS={'dsf':[1,2,4,8,12],'dsp':[1,2],'qwen24t':[1,2],'k3':[1,2,3],'glm':[1]}


def configure(store,policy,start):
    value=dict(original_start=start,stop=start+5400,
        qualification_scope='observed role and request-shape mix only; no sustained-capacity claim',routes={})
    for key,rungs in RUNGS.items():
        model=policy['models'][key]['model_id']
        # Operator ruling 2026-10-05 ('too conservative'): admit at the top rung
        # from the start (capped at route capacity in admission); waves are still
        # recorded as evidence. Provider 429s remain handled by gateway retries.
        value['routes'][model]=dict(key=key,rungs=rungs,index=len(rungs)-1,admission_capacity=rungs[-1],
            qualified=[],waves=[],used=[],incidents=[],stopped=False)
    with store.transaction() as db:
        cohort=json.loads(db.execute("SELECT value FROM settings WHERE key='cohort'").fetchone()[0])
        if cohort['profile'] not in ('pilot','production','swarm-check','solo-check','closing-check','prod-check','open-check','open-production','prod-continue','open-continue'):raise AdmissionDenied('live-cohort-ramp-only')
        old=db.execute("SELECT value FROM settings WHERE key='pilot-route-ramp'").fetchone()
        if old:
            if json.loads(old[0])['original_start']!=start:raise AdmissionDenied('immutable-ramp-clock')
            return
        db.execute("INSERT INTO settings VALUES ('pilot-route-ramp',?)",(canonical(value),))


def advance(store):
    now=store.clock()
    with store.transaction() as db:
        row=db.execute("SELECT value FROM settings WHERE key='pilot-route-ramp'").fetchone()
        if row is None:return None
        state=json.loads(row[0]);before=canonical(state)
        events={}
        for event in db.execute("SELECT kind,payload FROM events WHERE kind IN ('provider-frame','provider-end','reply-consumed') ORDER BY seq"):
            payload=json.loads(event['payload'])
            events.setdefault(payload['id'],{})[event['kind']]=payload
        attempts=[dict(r) for r in db.execute('SELECT * FROM attempts')]
        for model,route in state['routes'].items():
            if route['stopped']:continue
            rung=route['rungs'][route['index']]
            stop=state['stop']
            candidates=[]
            for attempt in attempts:
                context=json.loads(attempt['context'])
                if context['model']!=model:continue
                rid=attempt['id'];e=events.get(rid,{})
                end=e.get('provider-end',{})
                code=end.get('status')
                if (code==429 or isinstance(code,int) and 500<=code<600) and rid not in route['incidents']:
                    route['incidents'].append(rid)
                if rid in route['used']:continue
                valid=attempt['outcome']=='normal' or (route['key']=='glm' and attempt['outcome'] in ('reasoning-exhausted','output-truncated'))
                if (not valid or attempt['state']!='settled' or 'provider-frame' not in e or 'provider-end' not in e
                        or (attempt['outcome']=='normal' and 'reply-consumed' not in e)):
                    continue
                start=e['provider-frame']['at'];finish=end['at']
                # Later lower-concurrency work cannot certify the breadth rung.
                if finish>stop or finish<start:continue
                # Never count earlier-rung requests again, or pre-ascent starts.
                if start<route.get('rung_started_at',state['original_start']):continue
                candidates.append((start,finish,rid,context['role'],e['provider-frame'].get('max_tokens'),
                    {key:context[key] for key in ('role','request_class','max_input_tokens','max_output_tokens')}))
            if len(route['incidents'])>1:
                route['stopped']=True;route['stop_reason']='repeated-route-failure'
                route['admission_capacity']=max(route['qualified'],default=1)
                continue
            candidates.sort()
            if rung==1:
                waves=[[row] for row in candidates[:2-len(route['waves'])]]
            else:
                waves=[]
                # Any common 30s interval proves this exact simultaneous subset.
                for pivot in candidates:
                    members=[row for row in candidates if row[0]<=pivot[0] and row[1]>=pivot[0]+30]
                    if len(members)>=rung:
                        chosen=members[:rung];waves.append(chosen)
                        consumed={row[2] for row in chosen}
                        candidates=[row for row in candidates if row[2] not in consumed]
                        if len(waves)+len(route['waves'])==2:break
            for wave in waves:
                ids=[row[2] for row in wave]
                route['waves'].append(dict(attempts=ids,overlap_seconds=min(row[1] for row in wave)-max(row[0] for row in wave),
                    roles=[row[3] for row in wave],completion_limits=[row[4] for row in wave],
                    admitted_shapes=[row[5] for row in wave]))
                route['used'].extend(ids)
            if len(route['waves'])==2:
                route['qualified'].append(rung)
                route.setdefault('evidence',{})[str(rung)]=route['waves'];route['waves']=[]
                if now<stop and route['index']+1<len(route['rungs']):
                    route['index']+=1;route['admission_capacity']=route['rungs'][route['index']]
                    route['rung_started_at']=now
                else:
                    route['stopped']=True;route['stop_reason']='completed-or-original-stop'
            if now>=stop:
                route['stopped']=True;route['stop_reason']='original-ramp-stop'
                route['admission_capacity']=max(route['qualified'],default=1)
        if canonical(state)!=before:
            db.execute("UPDATE settings SET value=? WHERE key='pilot-route-ramp'",(canonical(state),))
            store.event(db,'pilot-ramp',state)
        return state
