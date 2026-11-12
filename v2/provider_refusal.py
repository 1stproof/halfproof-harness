"""Account refusal circuit and warning-only daily spend pacing.

The shared directory is beside the campaign's durable run directories. No secrets,
provider mutations, operator commands or historical accounting rewrites live here.
All coordination/probe deadlines use real time, never the frozen science clock.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
from pathlib import Path
import threading
import time
import urllib.error
import urllib.request
import uuid
from .control import atomic_json
from .accounting import bounded_call, AdmissionDenied, OSS_ROUTES, micros

INTERVAL = 600
BUDGET_SOURCES = {'openrouter_guardrail_budget', 'openrouter_workspace_budget', 'openrouter_key_limit'}


def metadata(detail):
    try:
        error = json.loads(detail).get('error', {})
        value = error.get('metadata', {})
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError, AttributeError):return {}


def account_refusal(status, detail=None):
    """Do not turn authentication, content-policy or request-shape 403s into outages."""
    from .policy import error_class
    if status == 402:return error_class(status, detail) != 'rate'
    return status == 403 and metadata(detail).get('limit_source') in BUDGET_SOURCES


def scalar(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def budget_rows(data):
    rows = data.get('budgets')
    if not isinstance(rows, list):rows = []
    effective = data.get('effective_budget')
    if isinstance(effective, dict) and effective not in rows:rows = rows + [effective]
    return [dict(scope=str(r.get('scope', 'unknown'))[:64], limit_usd=r['limit_usd'],
                 spend_usd=r['spend_usd'], reset_interval=r.get('reset_interval'),
                 resets_at=r.get('resets_at')) for r in rows if isinstance(r, dict)
            and scalar(r.get('limit_usd')) and scalar(r.get('spend_usd'))]


def pace(data, runs, now):
    """Measured live-run slopes, bounded by each run's remaining active life.

    Key usage is the daily baseline; scope-specific guardrail usage stays separate
    because a member/workspace can include other keys. Never sum either baseline
    with already-billed run totals. Forecasts only warn and never close admission.
    """
    end = (int(now // 86400) + 1) * 86400
    live = [r for r in runs.values() if r.get('live') and now-r.get('at', 0) <= 120]
    forecast = sum(r.get('usd_per_second', 0) * min(end-now, r.get('remaining_seconds', 0)) for r in live)
    rows = budget_rows(data)
    daily = [r for r in rows if r['reset_interval'] == 'daily']
    if data.get('limit_reset') == 'daily' and scalar(data.get('limit')) and scalar(data.get('usage_daily')):
        daily.append(dict(scope='key_limit',limit_usd=data['limit'],spend_usd=data['usage_daily']))
    warning = [dict(**r, projected_usd=r['spend_usd']+forecast)
               for r in daily if r['spend_usd']+forecast > r['limit_usd']]
    return dict(at=now, live_runs=len(live), registered_runs=len(runs),
                stale_live_runs=sum(bool(r.get('live')) and now-r.get('at', 0)>120 for r in runs.values()),
                key_usage_daily=data.get('usage_daily') if scalar(data.get('usage_daily')) else None,
                projected_additional_usd=forecast, budgets=rows, warnings=warning,
                metadata_available=bool(rows), action='warn-only')


def client_timing(config):
    """Native supervised deadlines own active time, including provider outages.

    Qwen's independent Date.now()/setTimeout wall budget and HTTP lifetime would
    otherwise abort a retained turn during a daily guardrail pause. External
    provider sockets still use the gateway's unchanged bounded real-time I/O.
    """
    store = getattr(getattr(config,'gateway',None),'accounting',None)
    aware = bool(getattr(config,'supervised',False) and getattr(store,'provider_account',None))
    return (None, 2000000000) if aware else (str(config.seconds),22000000)


class AccountCircuit:
    def __init__(self, directory, identity, *, wall=time.time):
        self.directory = Path(directory)/hashlib.sha256(identity.encode()).hexdigest()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory/'state.json'
        self.wall = wall

    @contextmanager
    def edit(self):
        # Shared filesystem flock + atomic rename, not network-filesystem SQLite.
        with (self.directory/'lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = self.snapshot()
            yield state
            atomic_json(self.path, state)

    def snapshot(self):
        try:return json.loads(self.path.read_bytes())
        except FileNotFoundError:return dict(paused=False, generation=0, next_probe=0, runs={})

    def refuse(self, status, detail, model, provider):
        with self.edit() as state:
            if not state['paused']:
                state.update(paused=True, since=self.wall(), next_probe=self.wall()+INTERVAL)
            state['generation'] += 1  # a late refusal invalidates an in-flight success
            m = metadata(detail)
            state['cause'] = dict(status=status, model=model, provider=provider,
                **{k:m[k] for k in ('limit_source','scope','limit_usd','spend_usd','resets_at') if k in m})

    def claim_probe(self, owner):
        now = self.wall()
        with self.edit() as state:
            if not state['paused'] or now < state['next_probe']:return None
            token = uuid.uuid4().hex
            state.update(next_probe=now+INTERVAL, probe=dict(token=token,owner=owner,
                         generation=state['generation'],at=now))
            return dict(state['probe'], cause=state['cause'])

    def start_probe(self, claim):
        """Anchor cadence at dispatch, after potentially slow reservation ACK."""
        with self.edit() as state:
            current = state.get('probe') or {}
            if (not state['paused'] or current.get('token') != claim['token'] or
                    state['generation'] != claim['generation'] or current.get('sent_at') is not None):return False
            if self.wall() < state.get('last_probe_sent',float('-inf'))+INTERVAL:return False
            state['probe']['sent_at'] = self.wall()
            state['last_probe_sent'] = self.wall()
            state['next_probe'] = self.wall()+INTERVAL
            return True

    def complete_probe(self, claim, success):
        with self.edit() as state:
            if (state.get('probe') or {}).get('token') != claim['token'] or state['probe'].get('completed'):return False
            state['probe']['completed'] = True
            state['last_probe'] = dict(at=self.wall(), success=bool(success), owner=claim['owner'])
            state['next_probe'] = max(state['next_probe'],self.wall()+INTERVAL)
            if success and state['generation'] == claim['generation']:
                state.update(paused=False, resumed_at=self.wall())
                return True
        return False

    def heartbeat(self, owner, row):
        with self.edit() as state:state.setdefault('runs', {})[owner] = dict(row, at=self.wall())

    def claim_metadata(self):
        with self.edit() as state:
            if self.wall() < state.get('next_metadata', 0):return False
            state['next_metadata'] = self.wall()+60
            return True

    def publish_metadata(self, data):
        with self.edit() as state:
            state['pacer'] = pace(data, state.get('runs', {}), self.wall())
            state.pop('metadata_error',None)
            atomic_json(self.directory/'daily-spend-pacer.json',state['pacer'])


def get_key_metadata(key):
    req = urllib.request.Request('https://openrouter.ai/api/v1/key',
                                 headers={'Authorization':'Bearer '+key})
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.loads(response.read(1024*1024))['data']


def minimal_probe(key, model, provider):
    """Inference is the health proof; metadata success alone cannot release a pause.

    Unknown charges remain liabilities. No auth/body data leaves this transport.
    """
    body = dict(model=model, messages=[dict(role='user',content='OK')], max_tokens=1,
                usage=dict(include=True))
    from contracts.route import build, check, OffRoute
    body=build(body,model,host=provider)
    request = urllib.request.Request('https://openrouter.ai/api/v1/chat/completions',
        data=json.dumps(body).encode(), headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read(1024*1024))
            cost = (data.get('usage') or {}).get('cost')
            try:check(body,data)
            except OffRoute:
                return dict(ok=False,cost=cost if scalar(cost) else None,generation=data.get('id'),done=True,
                    off_route=True,served_provider=data.get('provider'))
            ok = response.status == 200 and isinstance(data.get('choices'),list) and bool(data['choices'])
            return dict(ok=ok, cost=cost if scalar(cost) else None, generation=data.get('id'), done=True)
    except urllib.error.HTTPError as exc:
        exc.close()
        return dict(ok=False,cost=None,generation=None,done=True)
    except Exception:return dict(ok=False,cost=None,generation=None,done=False)


class ProviderControl:
    def __init__(self, pause, *, circuit=None, probe=minimal_probe, metadata_call=get_key_metadata):
        self.pause, self.c, self.clock = pause, pause.c, pause.clock
        identity = self.c.transport_key or self.c.credential_id
        self.circuit = circuit or AccountCircuit(self.c.durable.parent/'.provider-account', identity)
        self.store = self.c.store
        self.store.provider_account = self.circuit
        self.probe_call, self.metadata_call = probe, metadata_call
        self.probe_thread = self.metadata_thread = None
        self.next_heartbeat = 0
        self.probe_ready = False
        self.samples = list(self.c.state.get('provider_spend_samples', []))
        self.owner = self.c.cohort
        self.stopping = threading.Event()

    def _choose_probe(self):
        with self.store.transaction() as db:
            arms = [dict(r) for r in db.execute('SELECT * FROM arms')
                    if r['id'] in self.c.streams and r['mode']!='terminal']
        candidates = sorted((sum(float(p) for p in self.c.prices[m]), arm['id'], m)
            for arm in arms for m in json.loads(arm['config'])['models'] if m in self.c.prices)
        for _, arm, model in candidates:
            ip, op = map(float,self.c.prices[model])
            bound = max(1,micros(128*ip+op))
            provider = OSS_ROUTES[model]
            try:self.store.reserve_health_probe(arm,model,provider,bound,credential=self.c.credential_id,dry_run=True)
            except AdmissionDenied:continue
            return arm,model,provider,bound
        return None

    def _probe(self, claim):
        rid = None
        try:
            route = self._choose_probe()
            if route is None:
                self.circuit.complete_probe(claim,False)
                return
            arm,model,provider,bound = route
            from . import provider_routes
            from contracts.route import pinned_route
            target = pinned_route(model=model)['provider']['only'][0]
            reservation = self.store.reserve_health_probe(arm, model, provider, bound, credential=self.c.credential_id)
            rid = reservation['id']
            publication = 'provider-probe-'+rid
            self.c.publisher.submit(publication, self.store.checkpoint())
            end = time.monotonic()+60
            while not self.c.publisher.require_ack(publication):
                if self.stopping.is_set():raise InterruptedError('probe stopping')
                if time.monotonic() >= end:raise TimeoutError('probe publication')
                time.sleep(.05)
            if not self.circuit.start_probe(claim):raise InterruptedError('probe claim changed')
            self.store.send_health_probe(rid, reservation['receipt'])
            with self.store.transaction() as db:
                self.store.event(db,'health-probe-route',dict(id=rid,model=model,provider=target))
            result = bounded_call(self.probe_call,(self.c.transport_key, model, target),timeout=35)
            if result.get('off_route'):
                with self.store.transaction() as db:
                    self.store.event(db,'off-route-rejected',dict(id=rid,provider=result.get('served_provider'),model=model))
            self.store.finish(rid, generation=result.get('generation'), outcome='normal' if result['ok'] else 'http-error',
                authoritative_cost=micros(result['cost']) if result.get('cost') is not None else None,
                upstream_done=result.get('done',False))
            self.circuit.complete_probe(claim, result['ok'])
        except Exception as exc:
            if rid:self.store.cancel_unsent(rid)
            self.circuit.complete_probe(claim, False)
            self.pause.log('provider-probe-error', error=type(exc).__name__)

    def _metadata(self):
        try:self.circuit.publish_metadata(bounded_call(self.metadata_call,(self.c.transport_key,),timeout=25))
        except Exception as exc:
            with self.circuit.edit() as state:
                state['metadata_error'] = dict(at=self.circuit.wall(),kind=type(exc).__name__)

    def busy(self):
        return any(t and t.is_alive() for t in (self.probe_thread,self.metadata_thread))

    def stop(self, seconds=45):
        self.stopping.set()
        end = time.monotonic()+seconds
        for thread in (self.probe_thread,self.metadata_thread):
            if thread:thread.join(max(0,end-time.monotonic()))
        if self.busy():raise RuntimeError('provider monitor survived shutdown')
        self.circuit.heartbeat(self.owner,dict(live=False,usd_per_second=0,remaining_seconds=0))

    def tick(self, *, monitor=True):
        state = self.circuit.snapshot()
        with self.store.transaction() as db:
            old = db.execute("SELECT value FROM settings WHERE key='provider-refusal'").fetchone()
        # A missing shared record is never a successful health proof. Rebuild the
        # refusal from its native durable cause instead of silently clearing it.
        if old and not state['paused'] and not state.get('resumed_at'):
            cause = json.loads(old[0])
            self.circuit.refuse(cause['status'],json.dumps({'error':{'metadata':cause.get('metadata',{})}}),
                                cause['model'],cause['provider'])
            state = self.circuit.snapshot()
        paused = state['paused']
        with self.store.transaction() as db:
            if paused and not old:
                cause = state['cause']
                payload = dict(status=cause['status'],model=cause['model'],provider=cause['provider'],
                               metadata={k:v for k,v in cause.items() if k not in ('status','model','provider')})
                db.execute("INSERT OR REPLACE INTO settings VALUES ('provider-refusal',?)", (json.dumps(payload),))
                self.store.event(db,'provider-account-paused',dict(generation=state['generation']))
            elif not paused and old:
                db.execute("DELETE FROM settings WHERE key='provider-refusal'")
                self.store.event(db,'provider-account-resumed',dict(generation=state['generation']))
        if paused:self.clock.pause('provider')
        else:self.clock.resume('provider')
        self.c.state['provider_refusal'] = dict(paused=paused, generation=state['generation'],
            cause=state.get('cause'), next_probe=state.get('next_probe'), last_probe=state.get('last_probe'),
            pacer=state.get('pacer'),metadata_error=state.get('metadata_error'))
        pacer = state.get('pacer')
        if pacer:
            scopes = sorted({r['scope'] for r in pacer['warnings']})
            previous = self.c.state.get('provider_spend_warning_scopes', [])
            if scopes and scopes != previous:self.pause.log('daily-spend-warning', pacer=pacer)
            if previous and not scopes:self.pause.log('daily-spend-warning-cleared', measured_at=pacer['at'])
            self.c.state['provider_spend_warning_scopes'] = scopes
        if not monitor or self.stopping.is_set():return paused
        if paused:
            for runtime in getattr(self.c,'runtimes',{}).values():
                gateway = getattr(runtime,'reconciliation_gateway',None)
                if gateway is None:
                    gateway = next(iter(getattr(runtime,'gateways',[])),None)
                if gateway is not None:gateway.reconcile()
        now = self.circuit.wall()
        if now >= self.next_heartbeat:
            spent = self.store.totals()['settled']/1e6
            active = self.clock()
            self.samples.append((active, spent));self.samples = self.samples[-61:]
            self.c.state['provider_spend_samples'] = self.samples
            first = self.samples[0]
            rate = max(0,spent-first[1])/max(1,active-first[0])
            remaining = max(0,self.c.deadline-active)
            self.probe_ready = self._choose_probe() is not None if paused else False
            self.circuit.heartbeat(self.owner, dict(live=not self.c.complete, usd_per_second=rate,
                                   remaining_seconds=remaining,paused=paused,can_probe=self.probe_ready))
            self.next_heartbeat = now+30
            self.c.checkpoint()
        if paused and self.probe_ready and self.pause.phase == 'running' and (not self.probe_thread or not self.probe_thread.is_alive()):
            claim = self.circuit.claim_probe(self.owner)
            if claim:
                self.probe_thread = threading.Thread(target=self._probe,args=(claim,),daemon=True)
                self.probe_thread.start()
        if (not self.metadata_thread or not self.metadata_thread.is_alive()) and self.circuit.claim_metadata():
            self.metadata_thread = threading.Thread(target=self._metadata,daemon=True)
            self.metadata_thread.start()
        return paused
