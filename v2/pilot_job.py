"""Shared six-arm real-problem pilot and production compute entry.

Only explicit paid cohort actions call this module. No build/test action can
load its credential or substitute synthetic work. All monitoring is software.
"""
import hashlib
import json
import os
from pathlib import Path
import signal
import stat
import time
import urllib.request
from decimal import Decimal

from .control import atomic_json
from .contracts_v3 import load_contracts
from .deployment import DeploymentRefused
from .pilot import validate,problem_record
from . import process as _owned_process  # registers child TERM/INT/ALRM reset for every cohort profile


class StopRequest:
    """Signal-safe notification; shutdown runs only at a main-loop boundary."""
    def __init__(self):
        self.owner = os.getpid()
        self.signum = None

    def __call__(self, signum, _frame=None):
        if os.getpid() == self.owner and self.signum is None:
            self.signum = signum
            if getattr(self, 'controller', None) is not None:
                self.controller.cancelled = True  # salvage on shutdown, wherever the signal lands

    def check(self):
        if self.signum is not None:
            raise InterruptedError('external cohort stop: '+signal.Signals(self.signum).name)


def required_route_shape(policy, model_key, profile='pilot'):
    from .accounting import AdmissionContext
    defaults=AdmissionContext.__dataclass_fields__
    from .output_limits import provider_max
    top=provider_max(policy['models'][model_key]['model_id'])
    shapes=[dict(input_tokens=row.get('request_limits',{}).get('input_tokens',defaults['max_input_tokens'].default),completion_tokens=top)
        for row in policy[profile].values() if model_key in row['models']]
    if not shapes:raise DeploymentRefused('route has no admitted profile role')
    # Uncapped replies: the provider's maximum completion already lies inside its
    # context window, so the context requirement is the declared input shape.
    return dict(context_tokens=max(s['input_tokens'] for s in shapes),
        completion_tokens=max(s['completion_tokens'] for s in shapes))


def refresh_routes(policy, profile='pilot'):
    prices={};evidence={}
    used={m for row in policy[profile].values() for m in row['models']}
    for key,route in sorted(policy['models'].items()):
        if key not in used:continue  # e.g. glm in the solo check (diag-1056)
        url='https://openrouter.ai/api/v1/models/'+route['model_id']+'/endpoints'
        with urllib.request.urlopen(url,timeout=30) as response:
            raw=response.read(2*1024*1024+1)
        if len(raw)>2*1024*1024:raise DeploymentRefused('oversized public route metadata')
        data=json.loads(raw)
        from .provider_routes import eligible_from_endpoints, set_eligible
        shape=required_route_shape(policy,key,profile)
        known={'prompt','completion','input_cache_read','input_cache_write','discount'}
        def _zero(v):
            try:return Decimal(str(v))==0
            except Exception:return False
        priced=[row for row in data['data']['endpoints'] if all(n in known or _zero(v) for n,v in (row.get('pricing') or {}).items())]
        from contracts.route import pinned_route
        tags=pinned_route(policy,route['model_id'])['provider']['only']
        from contracts.route_preflight import inspect_hosts
        checks=inspect_hosts(policy,key,data['data']['endpoints'],profile)
        usable=[row['host'] for row in checks if row['ok']]
        if not usable:
            raise DeploymentRefused('allowed host request shape unavailable: '+json.dumps(checks))
        tags=usable
        ok=[row for row in priced if row.get('tag') in tags]
        if len(ok)!=len(tags):raise DeploymentRefused('unbounded pricing for allowed host: '+key)
        set_eligible(route['model_id'],tags)
        if len(tags)<2:
            import sys as _sys
            print('ALERT: %s has %d eligible provider(s): %s'%(key,len(tags),tags),file=_sys.stderr,flush=True)
        # Price bound: the most expensive eligible endpoint.
        endpoint=max(ok,key=lambda row:(Decimal(str(row['pricing'].get('completion',0))),Decimal(str(row['pricing'].get('prompt',0)))))
        pricing=dict(endpoint['pricing'])
        for row in ok:
            for name,value in row['pricing'].items():
                try:pricing[name]=str(max(Decimal(str(pricing.get(name,0))),Decimal(str(value))))
                except Exception:pass
        known={'prompt','completion','input_cache_read','input_cache_write','discount'}
        for name,value in pricing.items():
            if name not in known and Decimal(str(value))!=0:
                raise DeploymentRefused('unbounded provider price component: '+key+'/'+name)
        costs=[Decimal(str(pricing[name])) for name in ('prompt','completion')]
        for name in ('input_cache_read','input_cache_write'):
            if name in pricing:costs[0]=max(costs[0],Decimal(str(pricing[name])))
        if any(not value.is_finite() or value<0 for value in costs):raise DeploymentRefused('invalid route pricing')
        prices[route['model_id']]=[str(value) for value in costs]
        evidence[key]=dict(model=route['model_id'],provider=route['provider'],eligible=tags,effort=route['effort'],
            endpoint=endpoint,required_shape=shape,metadata_sha256=hashlib.sha256(raw).hexdigest(),host_checks=checks,refreshed_at=time.time())
    return prices,evidence


def credential_loader(path):
    """Deferred callback. Never called by prepare, unit tests or fake jobs."""
    def load():
        descriptor=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
        try:
            info=os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid()
                    or info.st_mode & 0o077 or not 1<=info.st_size<=4096):
                raise DeploymentRefused('private bounded credential file required')
            value=os.read(descriptor,4097).decode().strip()
            if not value or '\n' in value or '\r' in value:
                raise DeploymentRefused('invalid credential encoding')
            return value
        finally:os.close(descriptor)
    return load


def run(manifest,source,scratch):
    import os as _os
    for _arm,_dir in (manifest.get('seed_dirs') or {}).items():_os.environ['V3_SEED_'+_arm.replace('-','_').upper()]=_dir
    if isinstance(manifest.get('policy_overrides'),dict):_os.environ['V3_POLICY_OVERRIDES']=__import__('json').dumps(manifest['policy_overrides'])
    else:_os.environ.pop('V3_POLICY_OVERRIDES',None)
    profile=manifest.get('profile','pilot')
    production=profile in ('production','swarm-check','solo-check','closing-check','prod-check','open-check','open-production','prod-continue','open-continue')
    if profile in ('swarm-check','solo-check','closing-check','prod-check','open-check','open-production','prod-continue','open-continue'):
        from .swarm_check import validate as validate_check
        if profile == 'solo-check':from .solo_check import validate as validate_check
        if profile == 'closing-check':from .closing_check import validate as validate_check
        if profile == 'prod-check':from .prod_check import validate as validate_check
        if profile == 'open-check':from .open_check import validate as validate_check
        if profile in ('open-production','open-continue'):from .open_production import validate as validate_check
        if profile == 'prod-continue':from .prod_continue import validate as validate_check
        from .production import validate_problem
        validate_check(manifest)
    elif production:
        from .production import validate as validate_production, validate_problem
        validate_production(manifest)
    else:
        validate(manifest)
    from .production_continuation import ENV
    os.environ.pop(ENV, None)
    if manifest.get('continuation'):
        os.environ[ENV] = json.dumps(manifest['continuation'])
    policy=load_contracts(expected_sha256=manifest['contracts_sha256'])
    durable=Path(manifest['durable_root'])
    # The publisher's immutable cohort registration is the durable retry guard;
    # only that child touches remote storage while the controller is active.
    atomic_json(Path(scratch)/'launch-intent.json',manifest)
    from .release import pin_release,verify_dependency_pin
    from .cohort_runtime import CohortRuntime
    setup_deadline=time.monotonic()+2700
    def setup_expired(*_):raise DeploymentRefused('cohort readiness exceeded45minutes')
    previous=signal.signal(signal.SIGALRM,setup_expired);signal.alarm(2700)
    controller=None
    try:
        from .startup_log import phase
        phase('route refresh start')
        prices,routes=refresh_routes(policy,profile) if production else refresh_routes(policy)
        phase('route refresh done; release and dependency pin start')
        atomic_json(Path(scratch)/'route-preflight.json',routes)
        release=pin_release(Path(source),Path(scratch))
        verify_dependency_pin(release,manifest['runtime_pin'])
        phase('release and dependency pin done')
        if time.monotonic()>=setup_deadline:raise DeploymentRefused('cohort readiness exceeded45minutes')
        if production:validate_problem(manifest['problem'])
        elif manifest['problem']!=problem_record():raise DeploymentRefused('pilot original problem changed')
        text=Path(manifest['problem']['source']).read_text()
        phase('cohort runtime construction start (publisher, readiness, registration)')
        # Loader executes inside the constructor only AFTER actual namespace,
        # scientific tools, durable fallback and author-reserve checks pass.
        transport=dict(load_key=credential_loader(manifest['credential_file']),
            credential_id=profile+'-'+manifest['cohort_id'],prices_verified=True,metadata_lookup_free=True)
        controller=CohortRuntime(scratch,source=source,release=release,durable_root=durable,
            contracts_sha256=manifest['contracts_sha256'],release_sha256=manifest['release_sha256'],
            cohort=manifest['cohort_id'],problem=manifest['problem_id'],problem_text=text,
            fake_providers=None,prices=prices,route_layouts=manifest['route_layouts'],
            withdrawal_authority=manifest['withdrawal_authority'],withdrawal_deadline=time.time()+2700+manifest['clocks']['cleanup_stop_seconds'],
            profile=profile,live_transport=transport,problem_mount=manifest['problem'],
            live_arms=manifest.get('live_arms'))
    finally:
        signal.alarm(0);signal.signal(signal.SIGALRM,previous)
    phase('cohort runtime ready; controller loop start')
    def publish_launch():
        controller.publisher.submit(profile+'-launch-intent',manifest)
        controller.publisher.submit(profile+'-route-preflight',routes)
    return controller_lifecycle(manifest, scratch, lambda: (controller, None),on_ready=publish_launch)


def controller_lifecycle(manifest, scratch, initialize, *, on_ready=None):
    """One lifecycle for initial and exec-restored controllers, including TERM.

    Install stop handlers before restore, and publish initialization failures even
    when no complete controller can be returned. Unknown liabilities stay held.
    """
    from .pause import PauseControl
    profile=manifest.get('profile','pilot')
    controller=pause=None
    stop=StopRequest()
    old_term=signal.signal(signal.SIGTERM,stop);old_int=signal.signal(signal.SIGINT,stop)
    old_mask=signal.pthread_sigmask(signal.SIG_UNBLOCK,{signal.SIGTERM,signal.SIGINT})
    failure=None
    try:
        controller,pause=initialize()
        stop.controller=controller
        stop.check()
        if on_ready is not None:on_ready()
        if pause is None and not controller.complete:
            pause=PauseControl(controller,manifest)
        while not controller.complete:
            stop.check()
            operator_paused = pause.tick()
            provider = getattr(pause,'provider',None)
            provider_paused = provider.tick() if provider is not None and not operator_paused else False
            if not operator_paused and not provider_paused:controller.step()
            elif provider_paused:controller.publisher.poll()
            time.sleep(.05)
    except BaseException as exc:
        failure=type(exc).__name__
        if controller is not None:
            if isinstance(exc,InterruptedError):controller.cancelled=True
            controller.store.block(profile+'-invariant-or-operational-stop')
            if not controller.handoff:
                controller.close(profile+'-'+failure)
            until=time.monotonic()+min(420,max(0,controller.cleanup_deadline-controller.clock()))
            while not controller.handoff and not controller.complete and time.monotonic()<until:
                try:controller.step()
                except Exception:break
                time.sleep(.05)
        raise
    finally:
        try:
            result=dict(status='closed' if controller is not None and controller.complete and failure is None else 'failed',
                failure=failure,stop_signal=signal.Signals(stop.signum).name if stop.signum is not None else None,cap_microusd=manifest['cap_microusd'],model_success_required=False,
                live_capacity='only persisted pilot-route-ramp evidence is qualified')
            if controller is not None:
                result.update(original_start=controller.start,deadline=controller.deadline,
                    cleanup_deadline=controller.cleanup_deadline,frozen=sorted(controller.frozen))
                if stop.signum is not None:controller.cancelled=True
            try:
                atomic_json(Path(scratch)/'operational-result.json',result)
                # Direct durable result also covers failures before a publisher exists.
                durable=Path(manifest['durable_root']);durable.mkdir(parents=True,exist_ok=True)
                atomic_json(durable/'operational-result.json',result)
                if controller is None and failure is not None:
                    status_path=durable/'operator-control/status.json'
                    if status_path.exists():
                        row=json.loads(status_path.read_bytes())
                        if row['job']==str(os.environ.get('SLURM_JOB_ID')) and row['cohort']==manifest['cohort_id']:
                            row.update(phase='closed',error=failure,ack=row.get('in_progress') or row.get('ack'),in_progress=None)
                            atomic_json(status_path,row)
                if controller is not None and not controller.handoff:
                    controller.publisher.submit(profile+'-operational-result',result)
            finally:
                if controller is not None:
                    try:
                        provider = getattr(pause,'provider',None)
                        if provider is not None:provider.stop()
                    finally:controller.shutdown()
                    if pause is not None and not controller.handoff:
                        pause.phase='closed'
                        if failure is not None:pause.error=getattr(pause,'error',None) or failure
                        pause.publish()

        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK,old_mask)
            signal.signal(signal.SIGTERM,old_term);signal.signal(signal.SIGINT,old_int)
    return 0
