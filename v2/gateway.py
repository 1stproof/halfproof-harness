"""Buffer provider attempts: partial generations never enter Qwen history.
Admission reserves the maximum; settlement uses billed cost or a byte-token
upper estimate, with persisted, bounded generation-cost reconciliation.
"""
import socket
import contextlib, fcntl, hashlib, email.utils, http.client, json, re, tempfile, threading, time, uuid
import urllib.error, urllib.parse, urllib.request
from dataclasses import replace
from decimal import Decimal, ROUND_CEILING, InvalidOperation
from .policy import backoff, error_class
from .provider_refusal import account_refusal
from .runtime_guards import Recovery
from .accounting import AdmissionDenied, Reconciler, bounded_call, LOOKUP_DELAYS, OSS_ROUTES
from .request_policy import normalize
from .server_search import SEARCH_BOUND, SEARCH_FEE, configure, search_count, host_matches, replay_chunk, scoped_tool_id


def normalize_text_messages(request):
    messages=request.get('messages',[])
    if not isinstance(messages,list):raise ValueError('messages must be a list')
    values=[]
    for message in messages:
        if not isinstance(message,dict):raise ValueError('message must be an object')
        content=message.get('content','')
        if isinstance(content,list):
            if any(not isinstance(part,dict) or set(part)!={'type','text'}
                   or part['type']!='text' or not isinstance(part['text'],str) for part in content):
                raise ValueError('only literal text content is admitted')
            content=''.join(part['text'] for part in content)
        elif content is not None and not isinstance(content,str):
            raise ValueError('unsupported message content')
        values.append(content)
    for message,content in zip(messages,values):message['content']=content


# Per-model stream limits (operator 2026-10-06: model dependent). roles/timing.json:
# First-token and inter-chunk limits count reasoning/text/tool deltas, not keepalives.
# Defaults: 5 min to the first token, 2 min between tokens; 90 min per incident.
NO_PROGRESS_DEFAULT = 120
REQUEST_DEADLINE_DEFAULT = 5400
NO_PROGRESS_SECONDS = {}


def _timing(model):
    import functools, json as _j
    from pathlib import Path as _P
    @functools.lru_cache(maxsize=1)
    def table():
        try:return _j.loads((_P(__file__).resolve().parent.parent/'roles'/'timing.json').read_text())
        except (OSError, ValueError):return {}
    t = table() if not hasattr(_timing, 'cache') else _timing.cache
    _timing.cache = t
    m = str(model)
    return t.get(m) or t.get(m.lower()) or t.get(m.split('/')[-1]) or {}


def request_deadline_seconds(model):
    return int(_timing(model).get('request_deadline_seconds', REQUEST_DEADLINE_DEFAULT))


def no_progress_seconds(model):
    return max(1, int(_timing(model).get('inter_chunk_seconds', _timing(model).get('no_progress_seconds', NO_PROGRESS_SECONDS.get(model, NO_PROGRESS_DEFAULT)))))


def first_token_seconds(model):
    return max(1, int(_timing(model).get('first_token_seconds', 300)))

class ProviderError(Exception):
    def __init__(self, status, outcome, retry_after=None):
        self.status=status; self.outcome=outcome; self.retry_after=retry_after


def retry_after(value):
    try: return max(0, float(value))
    except (TypeError, ValueError):
        try: return max(0, email.utils.parsedate_to_datetime(value).timestamp()-time.time())
        except (TypeError, ValueError, OverflowError): return 0

class StreamResponse:
    """SSE comments keep Node's HTTP socket alive; only validated data is replayed."""
    def __init__(self, handler, interval):
        self.handler=handler; self.status=200; self.wfile=self
        self.lock=threading.Lock(); self.stopped=threading.Event()
        handler.send_response(200); handler.send_header('Content-Type','text/event-stream'); handler.end_headers()
        handler.wfile.write(b': gateway waiting\n\n'); handler.wfile.flush()
        def heartbeat():
            while not self.stopped.wait(interval):
                with self.lock:
                    if self.stopped.is_set():return
                    try:handler.wfile.write(b': gateway waiting\n\n'); handler.wfile.flush()
                    except OSError:self.stopped.set();return
        self.thread=threading.Thread(target=heartbeat,daemon=True);self.thread.start()
    def close(self):
        self.stopped.set();self.thread.join(timeout=1)
    def send_response(self,status):
        # Stop comments BEFORE replay: a spool block can split an SSE data line.
        self.close();self.status=status
    def send_header(self,*args):pass
    def end_headers(self):pass
    def write(self,data):
        with self.lock:
            if self.status>=400:data=b'data: '+data+b'\n\ndata: [DONE]\n\n'
            return self.handler.wfile.write(data)
    def flush(self):self.handler.wfile.flush()
    def send_error(self,status):
        self.send_response(status)
        self.write(json.dumps({'error':{'code':status,'message':'Provider request failed; retry policy exhausted or admission unavailable.'}}).encode())
        self.flush()

class Gateway:
    def __init__(self, watchdog, *, model, provider, input_price, output_price, key=None, fake=None, accounting=None, admission=None, durable_ack=None, prices_verified=False, usage_cost_authoritative=False, publisher_ack=None, admission_resolver=None, metadata_lookup_free=False):
        self.watchdog=watchdog; self.model=model; self.provider=provider
        self.ip=Decimal(str(input_price)); self.op=Decimal(str(output_price))
        self.key=key; self.fake=fake; self.lock=threading.Lock()
        self.accounting=accounting; self.admission=admission; self.durable_ack=durable_ack; self.publisher_ack=publisher_ack;self.admission_resolver=admission_resolver
        self.metadata_lookup_free=bool(fake is not None or metadata_lookup_free)
        if accounting is not None:
            watchdog.accounting=accounting;watchdog.accounting_context=admission
        if (accounting is None) != (admission is None):raise ValueError('accounting and admission are a pair')
        if fake is None and (accounting is None or publisher_ack is None or not prices_verified):
            raise AdmissionDenied('paid-admission-not-configured')
        if accounting is not None:
            known={r['id'] for r in accounting.export()['attempts'] if r['arm']==admission.arm}
            if any(value and rid not in known for rid,value in watchdog.ledger.settled.items()):
                accounting.anomaly('unmigrated-ledger-liability')
                raise AdmissionDenied('unmigrated-ledger-liability')
        if admission and (admission.model!=model or admission.provider!=provider):
            raise AdmissionDenied('gateway-identity-mismatch')
        if (model not in OSS_ROUTES and not (fake is not None and accounting is None and model in ('fixture','synthetic'))) or (model in OSS_ROUTES and OSS_ROUTES[model]!=provider):
            raise AdmissionDenied('model-route-not-allowed')
        if not self.ip.is_finite() or not self.op.is_finite() or min(self.ip,self.op)<0:
            raise ValueError('invalid price')
        if fake is None:
            # Web search enabled for research workers (operator request; coordinator 18:3x PDT).
            checkpoint=accounting.checkpoint()
            if not {'cohort','profile-configured','author-reserves-preflight'} <= {r['key'] for r in checkpoint['tables']['settings']}:
                raise AdmissionDenied('cohort-preflight-incomplete')
            if admission.worker=='unregistered':raise AdmissionDenied('worker-identity-required')
        self.reconciliation_thread=None; self.last_reconciliation=-float("inf")
        self.usage_cost_authoritative=usage_cost_authoritative
        self.slot_directory=None; self.model_concurrency=4; self.heartbeat_interval=15
        self.serial=max([int(k.rsplit("-r",1)[1]) for k in watchdog.ledger.settled if "-r" in k and k.rsplit("-r",1)[1].isdigit()]+[0]); self.max_attempts=3; self.max_request_attempts=20; self.sleep=time.sleep
        self.lookup_delays=LOOKUP_DELAYS; self.reconciling=set()
    def closing_request(self):
        """Only controller-assigned closing roles get refusal recovery."""
        w = self.watchdog
        if self.admission:
            return self.admission.role in ('report', 'verify')
        return bool(w.state.get('report_lock') or w.run.writeup)

    def incident_deadline(self, incident):
        w = self.watchdog
        wall = w.run.start + w.run.wall
        if self.closing_request():
            # Stage holds can extend this boundary, but never the arm wall.
            return min(wall, getattr(w, 'stage_deadline', wall))
        return min(wall, incident['start'] + request_deadline_seconds(self.model))

    def failure_class(self, failure, *, search=False):
        from .closing_retry import transient_status
        if self.closing_request() and transient_status(failure.status) and failure.outcome != 'context':
            return 'rate' if failure.status == 429 else 'provider'
        return ('provider' if failure.status == 409 or search and failure.status == 404
                else error_class(failure.status, getattr(failure, 'detail', None)))

    def micros(self, value):
        try:value=Decimal(str(value))
        except InvalidOperation as exc:raise ValueError('invalid cost') from exc
        if not value.is_finite() or value<0: raise ValueError('invalid cost')
        return int((value*1000000).to_integral_value(rounding=ROUND_CEILING))
    def _wait_provider_account(self, worker):
        """Retain the buffered logical request/context while the controller probes.

        This wait neither sends nor renews any active-time deadline. Teardown and
        operator fences still own the worker; provider recovery needs no command.
        """
        circuit = getattr(self.accounting, 'provider_account', None)
        if not circuit:return True
        w = self.watchdog
        while True:
            with self.accounting.transaction() as db:
                local = db.execute("SELECT 1 FROM settings WHERE key='provider-refusal'").fetchone()
                operator = db.execute("SELECT 1 FROM settings WHERE key IN ('operator-paused','blocked')").fetchone()
            if operator or w.worker is not worker or w.state.get('terminal'):return False
            if not local and not circuit.snapshot()['paused']:return True
            self.sleep(.1)

    def resume_billing(self, control_id):
        """Controller-only credit-return control; never replenishes an incident.

        This method is not exposed on the worker HTTP endpoint. Authorization for
        an operational resume belongs to the controller; terminal runs stay shut.
        """
        w=self.watchdog
        with w.mutex:
            if w.state.get('terminal') or w.clock()>=w.run.start+w.run.wall:
                raise AdmissionDenied('terminal-or-deadline')
            prior=w.state.setdefault('billing_resume_controls',[])
            if control_id in prior:return
            incident=w.state.get('gateway_incident')
            if not incident:raise AdmissionDenied('no-billing-incident')
            recovery=Recovery(w.state.setdefault('recovery',{}))
            record=recovery.state['incidents'].get(incident['id'])
            if not record or record['terminal']!='billing':raise AdmissionDenied('no-billing-incident')
            if w.clock()>=record['deadline'] or record['attempts']>=3:
                raise AdmissionDenied('incident-deadline')
            if self.accounting:self.accounting.control(control_id,self.admission.arm,pause=None)
            record['terminal']=None
            if w.run.outage_start is not None:w.run.healthy(w.clock())
            elif w.run.phase=='paused-alert':
                w.run.phase='write-up' if w.run.writeup else 'research'
            prior.append(control_id);w.save()

    def lookup(self, generation):
        if self.fake:
            return self.fake.lookup(generation) if hasattr(self.fake,'lookup') else None
        url='https://openrouter.ai/api/v1/generation?'+urllib.parse.urlencode({'id':generation})
        with urllib.request.urlopen(urllib.request.Request(url,headers={'Authorization':'Bearer '+self.key}),timeout=15) as response:
            data=json.load(response)['data']
            return {k:data.get(k) for k in ('total_cost','provider_name')}
    def _project_accounting(self):
        """Compatibility projection for lifecycle; the cohort store is authority."""
        if self.accounting is None:return
        exported=self.accounting.export()
        w=self.watchdog
        with w.mutex:
            for row in exported['attempts']:
                if row['arm']==self.admission.arm:
                    w.ledger.settled[row['id']]=row['settled']  # spend-then-assess: only settled money counts
            w.state['accounting_totals']=self.accounting.totals(self.admission.arm)
            if w.state.get('terminal'):w.state['terminal']['spent']=w.ledger.spent
            w.save()
    def reconcile(self):
        if not self.metadata_lookup_free:return
        if self.accounting is not None:
            if time.monotonic()-self.last_reconciliation<1:return
            if self.reconciliation_thread and self.reconciliation_thread.is_alive():return
            self.last_reconciliation=time.monotonic()
            def drain():
                try:Reconciler(self.accounting,self.lookup,credential=self.admission.credential).drain()
                finally:self._project_accounting()
            self.reconciliation_thread=threading.Thread(target=drain,daemon=True)
            self.reconciliation_thread.start()
            return
        # Compatibility fixtures retain the same bounded lifetime queue in runtime
        # state. Production never enters this branch.
        w=self.watchdog
        with w.mutex:
            now=w.clock()
            for rid,item in w.state.get('unmetered',{}).items():
                count=item.get('lookups',0)
                ended=item.get('ended',now)
                if rid in self.reconciling or item.get('exhausted'):continue
                if count>=7 or now>=ended+86400:
                    item['exhausted']=True;continue
                if now<ended+self.lookup_delays[count]:continue
                if self.reconciling:break
                self.reconciling.add(rid)
                threading.Thread(target=self._reconcile,args=(rid,),daemon=True).start()
    def wait_reconciliation(self, seconds=330):
        self.reconcile()
        until=time.monotonic()+min(330,max(0,seconds))
        while time.monotonic()<until:
            active=self.reconciliation_thread.is_alive() if self.reconciliation_thread else bool(self.reconciling)
            if not active:break
            time.sleep(.05)
    def _reconcile(self,rid):
        w=self.watchdog
        try:
            with w.mutex:
                item=w.state.get('unmetered',{}).get(rid)
                if item is None:return
                item['lookups']=item.get('lookups',0)+1;w.save()
                generation=item['generation']
            try:
                record=bounded_call(self.lookup,(generation,),timeout=15)
                provider=record.get('provider_name') if isinstance(record,dict) else None
                cost=record.get('total_cost') if isinstance(record,dict) else record
                if cost is None:return
                amount=self.micros(cost)
            except (OSError,ValueError,KeyError,TypeError,EOFError):return
            with w.mutex:
                # Search outer records may omit attributable tool costs.
                if item.get('search_enabled') and (not isinstance(record,dict) or not record.get('complete',False)):return
                old=w.ledger.settled[rid]
                w.ledger.settled[rid]=amount
                w.state.setdefault('charge_corrections',[]).append({'request':rid,'before':old,'after':amount})
                for row in w.state.get('request_metrics',[]):
                    if row.get('request')==rid:row.update(cost=amount,host=provider,charge_source='generation')
                w.state['unmetered'].pop(rid,None)
                if w.state.get('terminal'):w.state['terminal']['spent']=w.ledger.spent
                w.save()
        finally:
            with w.mutex:self.reconciling.discard(rid)
    def _reject_route(self, rid, generation, metadata, error):
        w=self.watchdog
        rejected=dict(request=rid,generation=generation,metadata=metadata,error=str(error))
        w.state['off_route_rejections']=list(w.state.get('off_route_rejections',[]))+[rejected]
        w.save()
        if self.accounting:
            with self.accounting.transaction() as db:self.accounting.event(db,'off-route-rejected',rejected)
        return ProviderError(502,'off-route-rejected')

    def _open(self,request,worker,rid,raw):
        if self.fake and hasattr(self.fake,'stream'):return self.fake.stream(request,worker,rid)
        if self.fake:
            result=self.fake(request,worker)
            if isinstance(result,int):raise urllib.error.HTTPError('local',result,'fixture',{},None)
            msg,finish,usage=result
            for i,call in enumerate(msg.get('tool_calls',[])):call['index']=i
            chunks=[{'id':rid,'object':'chat.completion.chunk','model':self.model,'provider':self.provider,'choices':[{'index':0,'delta':msg,'finish_reason':None}]},
                    {'id':rid,'choices':[{'index':0,'delta':{},'finish_reason':finish}],'usage':usage}]
            return [('data: '+json.dumps(c)+'\n\n').encode() for c in chunks]+[b'data: [DONE]\n\n']
        from .owned_transport import OwnedStream
        remaining=min(self.watchdog.run.start+self.watchdog.run.wall,
                      self.watchdog.state['request_deadline'])-self.watchdog.clock()
        return OwnedStream('https://openrouter.ai/api/v1/chat/completions',raw,
            {'Authorization':'Bearer '+self.key,'Content-Type':'application/json'},remaining,
            first_token_timeout=first_token_seconds(self.model))
    @contextlib.contextmanager
    def model_slot(self,worker):
        if self.accounting is not None or self.slot_directory is None:
            yield True; return
        raise ValueError('directory-local provider locks retired; cohort admission required')
    @contextlib.contextmanager
    def _watchdog_step(self, name):
        started=time.monotonic()
        with self.watchdog.mutex:
            acquired=time.monotonic()
            try:yield
            finally:
                row=self.turn_timing.setdefault('watchdog_steps',{}).setdefault(name,dict(wait_s=0.,work_s=0.,count=0))
                row['wait_s']+=acquired-started
                row['work_s']+=time.monotonic()-acquired
                row['count']+=1

    def handle(self,h,request,worker):
        # One in-flight generation per run/model, including retries.
        response=StreamResponse(h,self.heartbeat_interval) if request.get('stream') else h
        received=time.time(); started=time.monotonic()
        try:
            with self.lock:
                self.turn_timing=dict(received_at=received, gateway_lock_s=time.monotonic()-started)
                import copy
                w=self.watchdog
                waiting=time.monotonic()
                with w.mutex:
                    self.turn_timing['initial_mutex_s']=time.monotonic()-waiting
                    refresh=copy.deepcopy(w.state.get('board_refresh'))
                self._request_sent=False
                try:
                    with self.model_slot(worker) as acquired:
                        if acquired:self._handle(response,request,worker)
                finally:
                    # A prepared digest is not a delivery if validation or
                    # admission stopped this request before any possible send.
                    if not self._request_sent and refresh is not None:
                        with w.mutex:
                            w.state['board_refresh']=refresh
                            w.save()
        finally:
            if response is not h:response.close()
    def _handle(self,h,request,worker):
        w=self.watchdog; client_stream=request.get('stream',False)
        if self.admission_resolver is not None:
            context=self.admission_resolver(worker)
            if context.arm!=self.admission.arm or context.model!=self.model or context.provider!=self.provider:
                h.send_error(403);return
            self.admission=context;w.accounting_context=context
        if self.admission and hasattr(worker,'contract_role'):
            role={'review':'reviewer','verify':'verifier','recovery':'report'}.get(self.admission.role,self.admission.role)
            if worker.contract_role!=role:h.send_error(403);return
        # Reject an explicit ancillary/secondary route before normalization. Clients
        # may omit a model, but may never choose a different or hidden model.
        if request.get('model',self.model)!=self.model or any(k in request for k in ('models','plugins','transforms','web_search_options','audio','modalities','prediction','route')):
            h.send_error(403);return
        if any(t.get('type')!='function' for t in request.get('tools',[])):
            h.send_error(403);return
        # The pinned client emits text-part arrays for K3/Qwen. Accept only
        # literal text parts; media and auxiliary inference remain forbidden.
        try:normalize_text_messages(request)
        except ValueError:
            h.send_error(400);return
        if hasattr(w, 'prepare_request'):
            waiting=time.monotonic()
            with self._watchdog_step('board-refresh'):
                self.turn_timing['board_mutex_s']=time.monotonic()-waiting
                preparing=time.monotonic()
                w.prepare_request(request, worker)
                self.turn_timing['board_refresh_s']=time.monotonic()-preparing
        self.turn_timing['board_refreshed_at']=time.time()
        report_locked=bool(w.state.get('report_lock') or w.run.writeup)
        if self.admission:
            # The restricted namespace independently enforces paths and actual
            # commands. Gateway rejects research-capable tool advertisements.
            role=self.admission.role
            if report_locked and role in ('research','specialist'):
                h.send_error(403);return
            if role=='specialist' or report_locked or role=='report':
                allowed={'mcp__role_files__read_evidence'}
                if role in ('report','recovery'):
                    allowed.update({'mcp__role_files__read_report','mcp__role_files__write_report'})
                if role in ('verify','review'):
                    allowed.update({'mcp__role_files__submit_review','mcp__role_files__retrieve_source','mcp__role_files__bounded_check'})
                if any(t.get('function',{}).get('name') not in allowed for t in request.get('tools',[])):
                    h.send_error(403);return
        if self.admission and self.admission.role == 'orchestrator':
            if request.get('tools') or request.get('functions'):
                h.send_error(403);return
            with self._watchdog_step('orchestrator-claim'):
                if w.state.get('orchestrator_request_claimed'):
                    h.send_error(409);return
                w.state['orchestrator_request_claimed']=True;w.save()
        normalize(request,self.model)
        output_limit=self.admission.max_output_tokens if self.admission else __import__('v2.output_limits',fromlist=['x']).provider_max(self.model)
        from . import provider_routes as _routes
        if getattr(self,'route_index',None) is None:
            from .parallel_ops import route_offset
            self.route_index=_routes.assign(self.model)+route_offset(self.admission.cohort if self.admission else None)
        route_order,route_up=_routes.order(self.model,self.provider,self.route_index)
        self.route_first=route_order[0]
        request.update(model=self.model,provider=_routes.request_provider(self.model,route_order),max_tokens=output_limit,stream=True,stream_options={'include_usage':True})
        for tool in request.get('tools',[]):
            if tool.get('type')=='function':tool['function'].setdefault('parameters',{'type':'object','properties':{}})
        client_names={t['function']['name'] for t in request.get('tools',[]) if t.get('type')=='function'}
        search_enabled=not w.state.get('server_search_disabled') and not report_locked
        if self.admission:
            search_enabled=search_enabled and self.admission.role=='research' and self.admission.search_provenance=='extractive-source-only'
        configure(request,search_enabled)
        request_digest=hashlib.sha256(json.dumps({k:v for k,v in request.items() if k!='provider'},sort_keys=True).encode()).hexdigest()  # provider order may change with health
        while request_digest in w.state.get('exhausted_request_shapes',[]):
            # The client can resend the capped shape after a gateway error. Keep
            # its entire history and change the request, never end the session.
            request.setdefault('messages',[]).append({'role':'user','content':
                'The previous reply exhausted its output budget. Keep the existing context; '
                'continue with a shorter next step. Do not repeat incomplete tool calls.'})
            request_digest=hashlib.sha256(json.dumps({k:v for k,v in request.items() if k!='provider'},sort_keys=True).encode()).hexdigest()
            w.state.setdefault('exhausted_request_recoveries',[]).append(dict(session=worker.session,at=w.clock()))
        with self._watchdog_step('incident'):
            incident_record=w.state.get('gateway_incident')
            if incident_record and self.admission and incident_record.get('role')!=self.admission.role:
                incident_record['complete']=True
            # diag-1231: a new client session never retries the previous session's incident.
            if incident_record and incident_record.get('session',worker.session)!=worker.session:
                incident_record['complete']=True
            if incident_record and not incident_record.get('complete'):
                prior=w.state.get('recovery',{}).get('incidents',{}).get(incident_record['id'],{})
                if prior.get('terminal') in ('recovery-exhausted','output-truncated','reasoning-exhausted'):
                    incident_record['id']=uuid.uuid4().hex
                    incident_record['fault_retries']=max(incident_record.get('fault_retries',0),
                        incident_record['attempts']-incident_record.get('rate_retries',0))
            if incident_record and not incident_record.get('complete'):
                incident_id=incident_record['id']
                if incident_record.get('shape')!=request_digest:
                    incident_record['complete']=True
                    incident_record=None
            if not incident_record or incident_record.get('complete'):
                incident_id=uuid.uuid4().hex
                incident_record={'id':incident_id,'start':w.clock(),'attempts':0,'complete':False,'shape':request_digest,'role':self.admission.role if self.admission else None,'session':worker.session}
                w.state['gateway_incident']=incident_record;w.save()
        if incident_record['attempts']>=self.max_request_attempts or incident_record['attempts']-incident_record.get('rate_retries',0)-incident_record.get('fault_retries',0)>=self.max_attempts:h.send_error(503);return
        # Provider 429s retry with backoff until the incident/run deadline and do
        # not use up the per-request attempts. Transient provider faults use fresh
        # requests; the incident deadline and total attempt ceiling still apply.
        rate_retries=incident_record.setdefault('rate_retries',0)+incident_record.get('fault_retries',0)
        for attempt in range(incident_record['attempts'],self.max_attempts+100000):
            if not self._wait_provider_account(worker):h.send_error(503);return
            if attempt>=self.max_request_attempts or attempt-rate_retries>=self.max_attempts:h.send_error(503);return
            if w.clock()>=self.incident_deadline(incident_record):h.send_error(503);return
            report_locked=bool(w.state.get('report_lock') or w.run.writeup)
            enabled=not w.state.get('server_search_disabled') and not report_locked
            if self.admission:
                enabled=enabled and self.admission.role=='research' and self.admission.search_provenance=='extractive-source-only' 
            configure(request,enabled)
            # Review 2a62c74 #4: route order recomputed on every attempt (health may have changed).
            route_order,route_up=_routes.order(self.model,self.provider,self.route_index)
            self.route_first=route_order[0]
            request['provider']=_routes.request_provider(self.model,route_order)
            raw=json.dumps(request).encode()
            from .request_bound import fits, fit_bytes
            if self.admission and not fits(fit_bytes(request),self.admission.max_input_tokens):
                with self._watchdog_step('oversize-refusal'):incident_record['complete']=True;w.save()
                h.send_error(413);return
            if self.admission and self.admission.role == 'orchestrator':
                with self._watchdog_step('orchestrator-request'):
                    w.state['orchestrator_request_sha256']=hashlib.sha256(raw).hexdigest()
                    w.state['orchestrator_request_bytes']=len(raw)
                    w.state['orchestrator_request']=request
                    w.save()
            input_bound=self.admission.max_input_tokens if self.admission else len(raw)
            bound=self.micros(input_bound*self.ip+output_limit*self.op)+(SEARCH_BOUND if enabled else 0)
            if self.admission:bound+=self.admission.tool_bound
            self.serial+=1; rid=uuid.uuid4().hex if self.accounting else f's{worker.session}-r{self.serial}'
            waiting=time.monotonic()
            with self._watchdog_step('pre-reservation-admission'):
                self.turn_timing['admission_mutex_s']=time.monotonic()-waiting
                admitting=time.monotonic()
                if hasattr(w,'role_admission') and not w.role_admission(worker):h.send_error(403);return
                if hasattr(worker,'research_events'):worker.research_events()
                if w.worker is not worker or w.state['terminal'] or w.run.phase in ('done','paused-alert') or w.clock()>=w.run.start+w.run.wall:
                    h.send_error(503); return
                if self.accounting is None and w.ledger.spent+max(bound,w.margin)>w.ledger.cap:
                    w.state['admission_stop']='budget'; w.save(); h.send_error(429); return
                try:
                    Recovery(w.state.setdefault('recovery',{})).admit(incident_id,self.provider,w.clock(),self.incident_deadline(incident_record))
                except ValueError as exc:
                    w.state['admission_stop']=str(exc);w.save();h.send_error(503);return
                incident_record['attempts']=attempt+1;w.save()
                self.turn_timing['role_admission_s']=time.monotonic()-admitting
                self.turn_timing['admitted_at']=time.time()
            if self.accounting:
                try:
                    if report_locked:self.accounting.control('report:'+self.admission.arm,self.admission.arm,mode='report')
                    reserving=time.monotonic()
                    while True:
                        try:
                            reservation=self.accounting.reserve(replace(self.admission,logical_request=incident_id),max(1,bound),attempt_id=rid,
                                # diag-1431: closing roles are bounded by the wall; stage gating with holds owns the boundary.
                                deadline=min(self.incident_deadline(incident_record), (float('inf') if self.admission and self.admission.role in ('report','verify') else getattr(w,'stage_deadline',float('inf')))))
                            break
                        except AdmissionDenied as busy:
                            if busy.reason == 'provider-refusal' and getattr(self.accounting,'provider_account',None):
                                if not self._wait_provider_account(worker):raise
                                continue
                            if busy.reason not in ('route-busy','worker-slot-busy'):raise
                            if (w.worker is not worker or w.state.get('terminal') or
                                w.clock()>=self.incident_deadline(incident_record)):
                                raise AdmissionDenied('request-deadline')
                            self.sleep(.1)
                    self.turn_timing['reservation_s']=time.monotonic()-reserving
                    self.turn_timing['reserved_at']=time.time()
                    publishing=time.monotonic()
                    # Publisher work never holds the watchdog/database lock.
                    if self.publisher_ack:
                        publication=self.publisher_ack.begin(reservation)
                        self.turn_timing['publication_submit_s']=time.monotonic()-publishing
                        # The publisher's own bound; a shorter wait here would pause the arm first.
                        from .durability import ACK_SECONDS
                        until=time.monotonic()+ACK_SECONDS
                        while not self.publisher_ack.acknowledged(publication):
                            if time.monotonic()>=until:raise TimeoutError('durability-unacknowledged')
                            self.sleep(.01)
                        ack=reservation['receipt']
                    else:
                        ack=(bounded_call(self.durable_ack,(reservation,),timeout=60) if self.durable_ack
                             else reservation['receipt'] if self.fake is not None else None)
                    self.turn_timing['durability_s']=time.monotonic()-publishing
                    self.turn_timing['durable_at']=time.time()
                    waiting=time.monotonic()
                    with self._watchdog_step('send-admission'):
                        self.turn_timing['send_mutex_s']=time.monotonic()-waiting
                        if hasattr(w,'role_admission') and not w.role_admission(worker):raise AdmissionDenied('role-admission')
                        if w.worker is not worker or w.state.get('terminal') or w.run.phase in ('done','paused-alert'):
                            raise AdmissionDenied('sender-no-longer-owned')
                        if w.state.get('report_lock') or w.run.writeup:
                            self.accounting.control('report:'+self.admission.arm,self.admission.arm,mode='report')
                        self.accounting.mark_sent(rid,durable_receipt=ack)
                        self._request_sent=True
                except (AdmissionDenied,OSError,ValueError,RuntimeError,EOFError) as exc:
                    self.accounting.cancel_waiter(rid)
                    if getattr(exc,'reason','')=='operator-paused':
                        self.accounting.cancel_unsent(rid)
                    if not isinstance(exc,AdmissionDenied) or getattr(exc,'reason','')=='durability-unacknowledged':
                        self.accounting.control('durability:'+rid,self.admission.arm,pause='durability-unacknowledged')
                    with self._watchdog_step('admission-refusal'):
                        # Operator pause is temporary, not a terminal slot/budget
                        # latch. The controller seals/restarts this session.
                        if getattr(exc,'reason','')!='operator-paused':
                            w.state['admission_stop']=getattr(exc,'reason','durability-unacknowledged')
                        if (getattr(exc,'reason','') in ('budget','liability-limit','deadline') and
                            self.admission.role in ('research','verify') and self.accounting.arm_config(self.admission.arm)['solo'] and
                            not w.state.get('report_lock') and not w.run.writeup and
                            w.clock()<w.run.start+w.run.wall):
                            incident_record['complete']=True
                            if hasattr(w,'enter_report_mode'):w.enter_report_mode('research-pool-limit')
                        w.save()
                    h.send_error(429);return
            with self._watchdog_step('pending-state'):
                w.state.setdefault('pending',{})[rid]=bound; w.state['inflight_since']=w.clock()
                w.state['request_deadline']=reservation['deadline'] if self.accounting else self.incident_deadline(incident_record)
                # The cohort store is the money authority; the local ledger is its
                # settled-only projection (spend-then-assess), so in-flight bounds
                # are not recorded locally (live solo check 5: conflicting settlement).
                if self.accounting:w.ledger.settled.setdefault(rid,0)
                else:w.settle(rid,bound)
                w.save()
            self.turn_timing['provider_start_at']=time.time()
            begin=time.monotonic(); usage=None; generation=None; output_bytes=0; host=None
            self._request_sent=True
            calls={}; provider_ids={}; response_text=''; finish=None; done=False; failure=None; lines=None; accepted=False; repaired=set()
            served_metadata={};first_provider_frame=None; last_progress=begin; stall_limit=first_token_seconds(self.model)
            # Spool to disk above 1 MiB. Only keepalive comments escape before validation.
            with tempfile.SpooledTemporaryFile(max_size=1048576) as spool:
                try:
                    if __import__('v2.provider_routes',fromlist=['x']).injected_fault(self.model,getattr(self,'route_first',self.provider)):
                        _f=ProviderError(404,'http-error');_f.injected=True;raise _f  # live fault injection (checks only)
                    lines=self._open(request,worker,rid,raw)
                    if hasattr(lines,'silent_seconds'):lines.silent_seconds=stall_limit;lines.last_line=last_progress
                    accepted=True
                    # Header/keepalive traffic is not token progress.
                    longest_gap=0.0; first_content=None; runaway=_timing(self.model).get('runaway_output_bytes')
                    for line in lines:
                        if time.monotonic()-last_progress>stall_limit:
                            # diag-0658: keepalives reset the socket timeout, so a reply stuck
                            # after its first frame held its slot until the 90-min bound.
                            if self.accounting:
                                with self.accounting.transaction() as db:
                                    self.accounting.event(db,'no-progress-kill',dict(id=rid,model=self.model,
                                        silent_seconds=round(time.monotonic()-last_progress,1),limit=stall_limit))
                            w.state['no_progress_kills']=list(w.state.get('no_progress_kills',[]))+[dict(request=rid,at=w.clock(),limit=stall_limit)]
                            raise ProviderError('timeout','transport-loss')
                        if first_provider_frame is None:
                            first_provider_frame=w.clock()
                            if self.accounting:
                                with self.accounting.transaction() as db:
                                    self.accounting.event(db,'provider-frame',dict(id=rid,at=first_provider_frame,
                                        max_tokens=request.get('max_tokens'),model=self.model,provider=self.provider))
                        if w.clock()>=w.state['request_deadline']:raise ProviderError('timeout','transport-loss')
                        spool.write(line)
                        if not line.startswith(b'data:'):continue
                        payload=line[5:].strip()
                        if payload==b'[DONE]':done=True; continue
                        d=json.loads(payload)
                        served_metadata.update({k:d[k] for k in ('provider','provider_name','model','quantization') if d.get(k)})
                        generation=d.get('id') or generation
                        host=d.get('provider_name') or d.get('provider') or host
                        if (d.get('provider') or d.get('provider_name')) and self.model in OSS_ROUTES:
                            from contracts.route import check, OffRoute
                            try: check(request,d)
                            except OffRoute as exc:raise self._reject_route(rid,generation,served_metadata,exc)
                        if d.get('model') and self.model in OSS_ROUTES:
                            from contracts.route import check_model, OffRoute
                            try:check_model(self.model,d['model'])
                            except OffRoute as exc:raise self._reject_route(rid,generation,served_metadata,exc)
                        # Keep metadata separate from content until route enforcement succeeds.
                        if d.get('usage'):usage=d['usage']
                        error=d.get('error'); has_error='error' in d and error is not None
                        for choice in d.get('choices',[]):
                            delta=choice.get('delta') or {}
                            for key in ('content','reasoning','reasoning_content','tool_calls','reasoning_details'):
                                value=delta.get(key)
                                if value:
                                    now_m=time.monotonic();longest_gap=max(longest_gap,now_m-last_progress);last_progress=now_m
                                    if first_content is None:first_content=now_m-begin
                                    stall_limit=no_progress_seconds(self.model)
                                    if hasattr(lines,'silent_seconds'):
                                        lines.silent_seconds=stall_limit;lines.last_line=last_progress
                                if value:output_bytes+=len((value if isinstance(value,str) else json.dumps(value,ensure_ascii=False)).encode())
                            response_text+=delta.get('content') or ''
                            finish=choice.get('finish_reason') or finish
                            if choice.get('error') is not None:error=choice['error']; has_error=True
                            for call in delta.get('tool_calls',[]):
                                if call.get('id'):provider_ids.setdefault(call.get('index',0),call['id'])
                                fn=call.get('function',{}); item=calls.setdefault(call.get('index',0),{'name':'','arguments':''})
                                item['name']+=fn.get('name',''); item['arguments']+=fn.get('arguments','')
                                if call.get('type') not in (None,'function'):item['server']=True
                        if runaway and output_bytes>runaway:
                            # K3 runaways (3.5 and 6.2 MB of degenerate text): end as runaway, not a model failure.
                            w.state['runaway_kills']=list(w.state.get('runaway_kills',[]))+[dict(request=rid,at=w.clock(),bytes=output_bytes)]
                            if self.accounting:
                                with self.accounting.transaction() as db:
                                    self.accounting.event(db,'runaway-kill',dict(id=rid,model=self.model,output_bytes=output_bytes))
                            raise ProviderError('timeout','transport-loss')
                        if has_error or finish=='error':
                            code=error.get('code',502) if isinstance(error,dict) else 502
                            try:code=int(code)
                            except (TypeError,ValueError):code='other'
                            header_delay=(getattr(lines,'headers',None) or {}).get('Retry-After')
                            raise ProviderError(code,'in-stream-error',(error.get('retry_after') if isinstance(error,dict) else None) or header_delay)
                    with (getattr(w,'mutex',None) or contextlib.nullcontext()):
                        timing=list(w.state.get('reply_timing',[]))[-199:]
                        timing.append(dict(request=rid,model=self.model,provider=host or getattr(self,'route_first',self.provider),first_content_s=None if first_content is None else round(first_content,1),
                            longest_gap_s=round(longest_gap,1),total_s=round(time.monotonic()-begin,1),at=w.clock(),output_bytes=output_bytes,done=bool(done and finish)))
                        w.state['reply_timing']=timing  # replace, never mutate a list a snapshot may hold
                        w.state['streaming_total']=w.state.get('streaming_total',0)+round(time.monotonic()-begin,1)
                    if not done or finish is None:raise ProviderError('timeout','transport-loss')
                    if self.model in OSS_ROUTES:
                        from contracts.route import check, OffRoute
                        try: check(request,served_metadata)
                        except OffRoute as exc:raise self._reject_route(rid,generation,served_metadata,exc)
                    # rdcheck3 fix 8: repairable calls (arguments sent as JSON text, missing
                    # name inferable from argument keys, bridge call to a visible tool) are
                    # repaired here and replayed whole, never refused.
                    try:
                        from .tool_repair import repair_bridge_calls
                        repaired=repair_bridge_calls(calls,request.get('tools') or [])
                    except Exception:repaired=set()
                    if finish=='tool_calls' and not any(v['name'] in client_names and not v.get('server') for v in calls.values()):
                        raise ProviderError('timeout','server-tool-boundary')
                except urllib.error.HTTPError as exc:
                    accepted=False
                    body=exc.read(1048576) if exc.fp else b''
                    context=exc.code==413 or (exc.code==400 and any(x in body.lower() for x in (b'context_length_exceeded',b'context length',b'maximum context')))
                    failure=ProviderError(exc.code,'context' if context else 'http-error',exc.headers.get('Retry-After'))
                    # Upstream error text names the refused parameter; it carries no model output.
                    failure.detail=body[:4096].decode('utf-8','replace')
                except ProviderError as exc:failure=exc
                except (OSError,ValueError,http.client.HTTPException) as exc:
                    failure=ProviderError('timeout','transport-loss')
                    # A wholly silent connection ends in the socket timeout (= the silent limit),
                    # never reaching the in-loop check: count it as a silent kill too.
                    if isinstance(exc,(TimeoutError,socket.timeout)) and last_progress is not None and time.monotonic()-last_progress>=stall_limit*0.9:
                        w.state['no_progress_kills']=list(w.state.get('no_progress_kills',[]))+[dict(request=rid,at=w.clock(),limit=stall_limit,socket_timeout=True)]
                        if self.accounting:
                            try:
                                with self.accounting.transaction() as db:
                                    self.accounting.event(db,'no-progress-kill',dict(id=rid,model=self.model,silent_seconds=round(time.monotonic()-last_progress,1),limit=stall_limit,socket_timeout=True))
                            except Exception:pass
                finally:
                    if hasattr(lines,'close'):lines.close()
                    self.turn_timing['provider_end_at']=time.time()
                # Per-(model, provider) health. Review 2a62c74 #3: a result is charged to the
                # provider that served it; when that is unknown it never counts toward quarantine.
                try:
                    from . import provider_routes as _routes
                    from .server_search import host_matches as _hm
                    served=host
                    if served is None and failure is not None and getattr(failure,'detail',None):
                        try:served=(json.loads(failure.detail).get('error',{}).get('metadata') or {}).get('provider_name')
                        except (ValueError,AttributeError):served=None
                    # Infer an unidentified failure only from the route actually sent.
                    # Router mode's local rotation does not identify the serving host.
                    sent_provider=request['provider']
                    explicit_route=sent_provider.get('only') if len(sent_provider.get('only',[]))==1 else []
                    if served is None and failure is not None and (getattr(failure,'injected',False) or getattr(failure,'outcome',None)=='transport-loss'):
                        served=explicit_route[0] if explicit_route else None
                    tag=next((t for t in _routes.eligible(self.model,self.provider) if served and _hm(t,served)),None)
                    if tag is not None:
                        bad=failure is not None and getattr(failure,'status',None) in (404,408,500,502,503,504,'timeout')
                        if failure is None:_routes.record(self.model,tag,True)
                        elif bad:_routes.record(self.model,tag,False)
                except Exception:pass
                allowed_indices={i:n for n,(i,v) in enumerate((i,v) for i,v in calls.items() if v['name'] in client_names and not v.get('server'))}
                calls={i:v for i,v in calls.items() if i in allowed_indices}
                from .reply_outcomes import CAP_FINISH_REASONS
                outcome=failure.outcome if failure else ('reasoning-exhausted' if not response_text and not calls else 'output-truncated') if finish in CAP_FINISH_REASONS else 'normal'
                # Closing gateways without a shared account monitor retain their
                # bounded retry path; otherwise no controller can clear the pause.
                refusal = bool(failure and account_refusal(failure.status,getattr(failure,'detail',None))
                               and (not self.closing_request() or
                                    getattr(self.accounting,'provider_account',None)))
                # Account health belongs to all peers even if this worker lost
                # ownership during the rejected request. Freeze before lifecycle
                # handling, not after the reply-ownership early return.
                if refusal and self.accounting:
                    self.accounting.provider_refused(failure.status,getattr(failure,'detail',None),self.model,self.provider)
                cost=(usage or {}).get('cost')
                # UTF-8 bytes are deliberately conservative token upper estimates.
                amount=0 if not output_bytes else self.micros(len(raw)*self.ip+output_bytes*self.op)
                source='zero-output' if not output_bytes else 'conservative'
                searches=search_count(usage)
                search_fee=searches*SEARCH_FEE if searches is not None else None
                # A timeout before response headers also leaves upstream acceptance
                # unknown: the server may already have executed a paid search.
                search_may_bill=enabled and (accepted or failure is not None and failure.outcome=='transport-loss')
                if search_may_bill:
                    amount+=SEARCH_BOUND
                    source='conservative'  # A search can be billed even without output.
                if cost is not None:
                    try:amount=self.micros(cost)+(search_fee or 0); source='usage'
                    except ValueError:cost=None
                estimate=amount
                # Missing usage (including zero-output) retains the whole admitted
                # upper liability. Usage is a settlement only when all tool costs
                # are covered; otherwise metadata must reconcile it later.
                authoritative=self.usage_cost_authoritative and cost is not None and not enabled and not (self.admission and self.admission.tool_bound)
                # Safe upper bound from the provider's reported token counts (completion
                # tokens include reasoning) at the verified prices, plus the unreported
                # search and tool fee bounds. None when the provider reported no usage.
                known_bound=None
                try:
                    if usage and failure is None and done:
                        pt=int(usage.get('prompt_tokens') or 0); ct=int(usage.get('completion_tokens') or 0)
                        if pt>0 or ct>0:
                            known_bound=self.micros(pt*self.ip+ct*self.op)+(SEARCH_BOUND if search_may_bill else 0)+(self.admission.tool_bound if self.admission else 0)
                            if cost is not None:known_bound=max(known_bound,amount+(SEARCH_BOUND if search_may_bill else 0))
                except (TypeError,ValueError):known_bound=None
                if self.accounting:
                    settling=time.monotonic()
                    saved=self.accounting.finish(rid,generation=generation,estimate=estimate if cost is not None else min(estimate,bound),outcome=outcome,known_bound=known_bound,
                                           authoritative_cost=amount if authoritative else None,
                                           upstream_done=False if failure is not None and failure.outcome=='transport-loss' and getattr(self,'cancelled',False) else None,
                                           usage_reported=cost is not None and not search_may_bill and not (self.admission and self.admission.tool_bound),
                                           provider_end=dict(id=rid,at=w.clock(),outcome=outcome,status=failure.status if failure else None))
                    self.turn_timing['accounting_finish_s']=time.monotonic()-settling
                    amount=saved['settled']+saved['liability']+saved['reservation']
                elif not authoritative:amount=bound
                if not authoritative:source='retained-liability'
                with self._watchdog_step('settlement-state'):
                    # Spend-then-assess: with the cohort store, the local ledger holds
                    # settled money only (projection); an unsettled bound never counts
                    # (live solo check 10: a $14.35 K3 bound ended k3-solo as 'budget').
                    w.ledger.settled[rid]=amount if (self.accounting is None or authoritative) else w.ledger.settled.get(rid,0)
                    w.state['pending'].pop(rid,None)
                    w.state['last_request_end']=w.clock(); w.state.pop('inflight_since',None);w.state.pop('request_deadline',None)
                    w.state['requests_since_notice']=w.state.get('requests_since_notice',0)+1
                    row={'request':rid,'session':worker.session,'attempt':attempt+1,'seconds':time.monotonic()-begin,'prompt_tokens':(usage or {}).get('prompt_tokens',0),'completion_tokens':(usage or {}).get('completion_tokens',0),'output_bytes':output_bytes,'input_token_upper':len(raw),'output_token_upper':output_bytes,'cost':amount,'charge_source':source,'outcome':outcome,'status':failure.status if failure else None,'host':host,'route_first':getattr(self,'route_first',self.provider),'generation':generation,'error_detail':getattr(failure,'detail',None),'server_search_enabled':enabled,'web_search_requests':searches,'search_fee_microdollars':search_fee,'search_fee_source':'configured-rate' if search_fee is not None else None,'search_fee_bound':SEARCH_BOUND if enabled else 0}
                    w.state.setdefault('request_metrics',[]).append(row)
                    if w.state.get('terminal'):w.state['terminal']['spent']=w.ledger.spent
                    if generation and not authoritative and self.accounting is None:
                        w.state.setdefault('unmetered',{})[rid]={'generation':generation,'lookups':0,'ended':w.clock(),'search_enabled':search_may_bill}
                    w.save()
                self.reconcile()
                with self._watchdog_step('recovery-state'):
                    recovery_outcome=('provider-refusal' if refusal else self.failure_class(failure, search=enabled) if failure else outcome)
                    recovery=Recovery(w.state.setdefault('recovery',{}))
                    if failure and failure.outcome=='context':
                        # Context reduction is a changed request, not a host/route
                        # configuration defect. The old incident remains terminal.
                        recovery.state['incidents'][incident_id]['terminal']='context'
                        incident_record['complete']=True
                    else:
                        recovery.outcome(incident_id,recovery_outcome,w.clock(),
                            retry_after=retry_after(failure.retry_after) if failure else 0,
                            host=host or getattr(self,'route_first',self.provider),duration=time.monotonic()-begin,
                            identified_cutoff=w.state.get('identified_host_cutoffs',{}).get(self.provider))
                    if outcome in ('output-truncated','reasoning-exhausted'):
                        w.state.setdefault('exhausted_request_shapes',[]).append(request_digest)
                    w.save()
                with self._watchdog_step('reply-ownership'):
                    if w.worker is not worker or w.state['terminal']:return
                    if not failure or failure.outcome=='context':
                        if w.run.outage_start is not None:w.run.healthy(w.clock())
                        w.state['retry_at']=None
                if failure:
                    # Only repeated compatibility failures disable search for this run; dropped
                    # streams are common on every host and keep search. A cut attempt stays charged.
                    if enabled and (failure.status==404 or failure.outcome=='server-tool-boundary'):
                        with self._watchdog_step('search-failure'):
                            w.state['search_compat_failures']=w.state.get('search_compat_failures',0)+1
                            w.save()
                    if enabled and w.state.get('search_compat_failures',0)>=3:
                        with self._watchdog_step('disable-search'):
                            w.state['server_search_disabled']={'reason':failure.outcome,'status':failure.status}
                            w.event('server-search-disabled');w.state['events'][-1].update(reason=failure.outcome,status=failure.status)
                            w.save()
                    kind='provider-refusal' if refusal else self.failure_class(failure, search=enabled)
                    # HTTP 400 is a rejected request, not an arm-wide credential/configuration failure.
                    # End that worker so its owner can retry with a fresh context; keep other work admissible.
                    if kind=='configuration' and failure.status==400:
                        with self._watchdog_step('configuration-failure'):
                            errors=list(w.state.get('request_configuration_failures',[]))[-49:]
                            errors.append(dict(request=rid,status=400,at=w.clock(),detail=getattr(failure,'detail',None)))
                            w.state['request_configuration_failures']=errors;w.save()
                    if self.accounting and kind == 'provider-refusal':
                        if getattr(self.accounting,'provider_account',None):
                            if not self._wait_provider_account(worker):h.send_error(503);return
                            rate_retries += 1
                            incident_record['fault_retries']=incident_record.get('fault_retries',0)+1
                            old_incident=incident_id
                            incident_id=uuid.uuid4().hex
                            incident_record['id']=incident_id
                            incident_record.setdefault('previous_requests',[]).append(old_incident)
                            continue
                    if self.accounting and kind in ('configuration','billing') and failure.outcome!='context' and failure.status!=400:
                        self.accounting.control('failure:'+rid,self.admission.arm,pause=kind)
                    from .parallel_ops import retry_delay
                    delay=retry_delay(backoff(attempt),retry_after(failure.retry_after))
                    remaining=self.incident_deadline(incident_record)-w.clock()
                    # Writer/author/verifier provider failures retry with backoff until
                    # the arm's deadline (coordinator, after live solo check 8).
                    # Transient failures are new provider requests with fresh logical
                    # identities and reservations, within the ORIGINAL incident wall.
                    transient=kind in ('timeout','provider') or self.closing_request() and kind=='rate'
                    if transient and attempt+1<self.max_request_attempts and delay<remaining:
                        if kind!='rate':rate_retries+=1
                        if kind!='rate':incident_record['fault_retries']=incident_record.get('fault_retries',0)+1
                        old_incident=incident_id
                        incident_id=uuid.uuid4().hex
                        incident_record['id']=incident_id
                        incident_record.setdefault('previous_requests',[]).append(old_incident)
                        self.route_index+=1
                        if kind!='rate':kind='retry-fault'
                    if kind=='rate':
                        w.state.setdefault('rate_limited',{}).setdefault(self.model,0);w.state['rate_limited'][self.model]+=1
                        if delay<remaining:
                            rate_retries+=1;incident_record['rate_retries']=incident_record.get('rate_retries',0)+1
                    if attempt+1<self.max_request_attempts and (kind in ('rate','retry-fault') or kind in ('timeout','provider') and attempt-rate_retries+1<self.max_attempts) and delay<remaining:
                        with self._watchdog_step('retry-state'):
                            row['retry_delay']=delay
                            # Freeze inactivity clocks using the existing outage policy.
                            # retry_at is used by the watchdog only if a hard session wall
                            # or process loss has removed this worker; inline retries own it otherwise.
                            w.run.billing(w.clock(),kind);w.state['retry_at']=w.clock()+delay;w.save()
                        # Poll ownership/deadline during backoff so teardown never waits minutes.
                        left=delay
                        while left>0:
                            step=min(1,left); self.sleep(step); left-=step
                            if w.worker is not worker or w.state['terminal']:return
                        continue
                    if failure.outcome=='context':
                        worker.last_exit={'kind':'context','info':{'compressionStatus':None,'source':'provider-context'}}
                        h.send_response(failure.status); h.send_header('Content-Type','application/json'); h.end_headers()
                        h.wfile.write(b'{"error":{"message":"context length exceeded","code":"context_length_exceeded"}}'); return
                    worker.last_exit={'kind':'provider','status':failure.status}
                    with self._watchdog_step('failed-incident'):
                        if self.closing_request() and kind in ('rate','timeout','provider'):
                            w.state['closing_retry_exhausted'] = dict(status=failure.status, at=w.clock(), attempts=attempt+1)
                            worker.events.put(worker.last_exit);w.save()
                        elif kind in ('rate','timeout','provider','billing'):
                            w.run.phase='paused-alert'; w.event('provider-retries-exhausted'); w.save()
                        else:worker.events.put(worker.last_exit)
                    h.send_error(failure.status if isinstance(failure.status,int) and failure.status!=409 else 502); return
                with self._watchdog_step('successful-incident'):
                    w.state['retry_attempt']=0;incident_record['complete']=True
                    w.state.pop('closing_retry_exhausted',None);w.save()
                from .reply_outcomes import CAPPED_OUTCOMES
                if outcome in CAPPED_OUTCOMES:
                    # Incomplete calls may contain syntactically valid writes,
                    # publications or verdicts. Never execute them in the client.
                    calls={};allowed_indices={}
                repaired={i for i in repaired if i in allowed_indices}
                with self._watchdog_step('reply-registration'):
                    # Hard fencing may occur after settlement. Ownership and
                    # registration must be one transaction with that fence.
                    if w.worker is not worker or w.state['terminal'] or w.clock()>=w.run.start+w.run.wall:
                        w.state.setdefault('bounded_cancellations',[]).append(
                            dict(request=rid,session=worker.session,at=w.clock(),reason='reply-fenced-before-registration'))
                        w.save();return
                    reply_id=worker.prepare_reply(rid,outcome) if hasattr(worker,'prepare_reply') else rid
                call_ids={i:provider_ids.get(i) or scoped_tool_id(reply_id,n) for i,n in allowed_indices.items()}
                self.turn_timing['delivery_start_at']=time.time()
                if client_stream:
                    h.send_response(200); h.send_header('Content-Type','text/event-stream'); h.end_headers()
                    spool.seek(0)
                    emitted_ids=set()
                    for line in spool:
                        if not line.startswith(b'data:'):continue
                        payload=line[5:].strip()
                        if payload==b'[DONE]':continue
                        clean=replay_chunk(json.loads(payload),{i:n for i,n in allowed_indices.items() if i not in repaired}, call_ids=call_ids, emitted_ids=emitted_ids)
                        if clean is not None:
                            clean['id']=reply_id
                            for choice in clean['choices']:choice['finish_reason']=None
                            h.wfile.write(b'data: '+json.dumps(clean).encode()+b'\n\n')
                    for i in sorted(repaired):
                        whole={'id':reply_id,'object':'chat.completion.chunk','model':self.model,'choices':[{'index':0,'delta':{'tool_calls':[
                            {'index':allowed_indices[i],'id':call_ids[i],'type':'function','function':{'name':calls[i]['name'],'arguments':calls[i]['arguments']}}]},'finish_reason':None}]}
                        h.wfile.write(b'data: '+json.dumps(whole).encode()+b'\n\n')
                    final={'id':reply_id,'object':'chat.completion.chunk','model':self.model,'choices':[{'index':0,'delta':{},'finish_reason':finish}]}
                    h.wfile.write(b'data: '+json.dumps(final).encode()+b'\n\n')
                    h.wfile.write(b'data: [DONE]\n\n')
                    h.wfile.flush()
                else:
                    h.send_response(200); h.send_header('Content-Type','application/json'); h.end_headers()
                    message={'role':'assistant','content':response_text}
                    if calls:message['tool_calls']=[{'id':call_ids[i],'type':'function','function':v} for i,v in calls.items()]
                    h.wfile.write(json.dumps({'id':reply_id,'object':'chat.completion','model':self.model,'choices':[{'index':0,'message':message,'finish_reason':finish}],'usage':usage or {}}).encode())
                h.wfile.flush()
                self.turn_timing['reply_flushed_at']=time.time()
                timing=dict(self.turn_timing,id=rid,model=self.model)
                if self.accounting:
                    with self.accounting.transaction() as db:
                        self.accounting.event(db,'gateway-timing',timing)
                with self._watchdog_step('timing-state'):
                    w.state['gateway_timing']=timing
                for item in calls.values():
                    try:worker.last_operation=(item['name'],json.loads(item['arguments']))
                    except ValueError:pass
                notice=worker.current_notice
                if notice and re.search(r'(?i)generation\s*'+str(notice['generation'])+r'\b',response_text):worker.events.put({'kind':'ack','generation':notice['generation']})
                return
