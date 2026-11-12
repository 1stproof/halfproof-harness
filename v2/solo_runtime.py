"""Solo service composition for the v3 controller; no credential discovery.

The cohort controller owns one P3 store and one P7 publisher and passes both in.
Start/step/close use only the declared open-weight model. This module never loads
provider keys, launches a judge, or registers a new cap on restart.
"""
from pathlib import Path
import time
from .accounting import AdmissionContext
from .contracts_v3 import load_contracts
from .control import atomic_json
from .owned_transport import RuntimeGateway
from .lifecycle import Watchdog
from .worker import QwenWorker


class SoloRuntime:
    def __init__(self, root, *, strategy, profile, cohort, problem, start,
                 contracts_sha256, release, accounting, publisher, durable_root,
                 input_price, output_price, prices_verified=False, key=None,
                 credential_id='fake', fake=None, clock=time.time, continuous_goal=True, deployment=None,
                 metadata_lookup_free=False):
        policy=load_contracts(expected_sha256=contracts_sha256)
        if strategy not in policy[profile] or not strategy.endswith('-solo'):
            raise ValueError('declared solo strategy required')
        config=policy[profile][strategy]
        lane=config['models'][0];route=policy['models'][lane]
        root=Path(root);root.mkdir(parents=True,exist_ok=True)
        from .deployment import local_root
        root=local_root(root)
        release=Path(release)
        self.root,self.strategy,self.accounting,self.publisher=root,strategy,accounting,publisher
        self.gateways=[];self.closed=False;self.clock=clock
        from .fencing import WriterScope
        self.writer_scope=WriterScope(strategy)
        # Root authorization is local control metadata, not a paid-admission flag.
        manifest=root/'runtime-root.json'
        declaration=dict(version=1,authorization='operator-approved-campaign-root',runs_root=str(root.absolute()))
        if manifest.exists():
            import json
            if json.loads(manifest.read_text())!=declaration:raise ValueError('runtime root identity changed')
        else:atomic_json(manifest,declaration)
        def factory(work,session,mode,notice):
            w=self.watchdog
            from .runtime_closure import RuntimeAcknowledgment
            acknowledgment=RuntimeAcknowledgment(accounting,publisher,w)
            if mode=='verification':
                atomic_json(work/'CLAIM.json',dict(status=w.state.get('claimed_status'),
                    candidate_hash=w.state['verification_identity']['candidate_hash'],model=route['model_id'],session=session))
            reporting=bool(w.state.get('report_lock'))
            role='verify' if mode=='verification' else 'report' if reporting else 'research'
            pool=('final-verification' if role=='verify' else 'report') if reporting else 'research_and_verification'
            from .solo_limits import INPUT_TOKENS, OUTPUT_TOKENS
            shape=dict(max_input_tokens=INPUT_TOKENS, max_output_tokens=__import__('v2.output_limits',fromlist=['x']).provider_max(route['model_id'])) if reporting else {}
            # A fresh gateway object binds its context for the entire session;
            # old settlement threads cannot observe a replaced role/account.
            context=AdmissionContext(arm=strategy,account=strategy+'/'+pool,
                model=route['model_id'],provider=route['provider'],credential=credential_id,
                role=role,request_class='internal-verification' if role=='verify' else 'report' if role=='report' else 'primary',
                cohort=cohort,problem=problem,worker=strategy,phase='report' if reporting else 'research',
                search_provenance='extractive-source-only' if role=='research' else None,**shape)
            gateway=RuntimeGateway(w,model=route['model_id'],provider=route['provider'],
                input_price=input_price,output_price=output_price,key=key,fake=fake,
                accounting=accounting,admission=context,publisher_ack=acknowledgment,
                prices_verified=prices_verified,metadata_lookup_free=metadata_lookup_free)
            if deployment is not None:
                import copy
                gateway.deployment=copy.deepcopy(deployment)
            self.gateways.append(gateway)
            worker=QwenWorker(work,session,mode,notice,lane=lane,model=route['model_id'],
                release=release,gateway=gateway,seconds=max(1,min(7500,int(start+config['wall_seconds']-clock()))),
                research_live=fake is None,continuous_goal=continuous_goal)
            self.writer_scope.register(worker)
            return worker
        # The first persisted allocation uses the ORIGINAL start, even on late construction.
        self.watchdog=Watchdog(root,strategy,lambda:start,factory,lane=lane,
            wall=config['wall_seconds'],cap=config['cap_microusd'],manifest=manifest)
        w=self.watchdog;w.clock=clock
        w.state['solo_final_check_required']=True
        w.save()
        if (w.run.start,w.run.wall,w.run.cap)!=(start,config['wall_seconds'],config['cap_microusd']):
            raise ValueError('original solo allocation mismatch')
        # Production solos: write-up from 85% of the wall (was 90%), ~3.5 h (coordinator ~16:35).
        self.research_stop=start+config.get('research_stop_seconds',.85*config['wall_seconds'])
        w.research_stop=self.research_stop
        w.attach_runtime_publisher(publisher)
        w.attach_closure(publisher,catalog=dict(root=str(w.path/'closure-state'),arm_id=strategy,
            problem_id=problem,strategy=strategy,profile=profile,
            artifact_root=str(Path(durable_root)/strategy/'closure')),scratch=str(root))

    def start(self):
        with self.watchdog.mutex:self.watchdog.start()

    def step(self):
        w=self.watchdog
        for gateway in self.gateways:gateway.reconcile()
        with w.mutex:
            if not w.state['terminal'] and self.clock()>=self.research_stop and not w.state.get('report_lock'):
                w.enter_report_mode('original-research-stop')
            if w.state.get('report_lock'):
                self.accounting.control('report:'+self.strategy,self.strategy,mode='report')
            w.closure_poll()
            if (not w.worker and not w.state.get('final_verification') and
                    w.run.phase not in ('done','paused-alert') and w.state.get('closure_ready')):
                w.start()
            return w.tick(boundary=w.worker is None or w.worker.boundary)

    def close(self, reason='controller-stop'):
        # Selection is asynchronous; controller continues step() until terminal.
        with self.watchdog.mutex:
            self.watchdog.finish(reason)

    def scientific_sections(self):
        """Separate administrative findings never alter reviewed report bytes."""
        w=self.watchdog;root=w.path/'final-check-attachment';root.mkdir(exist_ok=True)
        atomic_json(root/'ADMINISTRATIVE-STATUS.json',dict(administrative=True,
            reviewed_document_unchanged=True,checks=w.state.get('final_checks',[]),
            final_verification=w.state.get('final_verification'),
            verification_incomplete=w.state.get('closure_manifest',{}).get('final_verification_status',
                w.state.get('final_verification',{}).get('status'))!='internal-supported'))
        return dict(supporting=[dict(root=str(root),prefix='final-check-attachment/')])

    def fence_receipt(self):
        """Return P7-owned evidence for P6 freeze after bounded terminal cleanup."""
        w=self.watchdog
        with w.mutex:
            if (not w.state.get('closure_manifest') or w.worker is not None or w.state.get('pending')
                    or any(g.active_handlers for g in self.gateways)):
                raise ValueError('closure or request cleanup incomplete')
            if w.state.get('receipt_failure'):
                raise ValueError('reply consumption invariant failed')
            if not w.runtime_durable():
                from .fencing import FencePending
                raise FencePending('runtime durability pending or failed')
            self.accounting.control('terminal:'+self.strategy,self.strategy,mode='terminal')
            evidence=self.writer_scope.fence(admission_closed=True,active_requests=0,
                cancelled_request_ids=list(dict.fromkeys(row['request'] for row in w.state.get('bounded_cancellations',[]))))
            w.state['writer_fence']=evidence;w.save()
            return evidence


def restore_runtime(checkpoint, *, root, strategy, start, wall, cap, fence_proof):
    """Restore an acknowledged service checkpoint only after P7 fences old owners.

    Artifact restoration and P3 ledger checkpoint restoration are separate
    controller operations; this helper never invents either or resumes a worker.
    """
    import json
    from .policy import Ledger
    from .deployment import local_root
    if not fence_proof:
        raise ValueError('old runtime ownership fence required')
    state=json.loads(json.dumps(checkpoint.get('runtime',checkpoint)))
    run=state['run']
    if (run['start'],run['wall'],run['cap'])!=(start,wall,cap):
        raise ValueError('restored original allocation mismatch')
    if Ledger.restore(state['ledger']).cap!=cap:
        raise ValueError('restored ledger cap mismatch')
    if state.get('scientific_frozen') or state.get('closure_manifest') or state.get('terminal'):
        raise ValueError('scientifically terminal runtime cannot resume')
    if state.get('receipt_failure') or any(row['outcome']=='normal' and row['consumed_at'] is None
                                         for row in state.get('reply_receipts',{}).values()):
        raise ValueError('unacknowledged normal reply blocks runtime resume')
    root=local_root(root)
    if not strategy.endswith('-solo') or '/' in strategy or strategy.startswith('.'):
        raise ValueError('invalid solo strategy')
    path=root/strategy;path.mkdir(exist_ok=True)
    if path.is_symlink():raise ValueError('runtime restore symlink refused')
    state['restored_with_fence']=str(fence_proof)
    # Any persisted ambiguity stays recorded; P3 restores the corresponding
    # reservation as full unresolved liability rather than authorizing replay.
    cancelled=state.setdefault('bounded_cancellations',[])
    for request in state.get('pending',{}):
        if not any(row['request']==request for row in cancelled):
            cancelled.append(dict(request=request,session=state['session'],at=time.time(),reason='checkpoint-owner-fenced'))
    state['pending']={};state.pop('inflight_since',None);state.pop('request_deadline',None)
    atomic_json(path/'runtime.json',state)
    return path/'runtime.json'
