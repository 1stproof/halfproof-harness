"""Actual deployment entry for bounded, zero-spend cohort integration checks.

This action accepts only fake providers and synthetic fixtures. It is not a
paid pilot entry; undeclared arms and paid modes are rejected before import
of any test provider. The real pinned Qwen client still executes every role.
"""
import importlib.util
import json
from pathlib import Path
import time
import sys

from .cohort_runtime import CohortRuntime
from .contracts_v3 import load_contracts, contracts_sha256
from .deployment import DeploymentRefused
from .release import pin_release
from .control import atomic_json


def run(manifest, source, scratch):
    if (manifest.get('mode')!='test' or manifest.get('fake_provider') is not True
            or manifest.get('synthetic') is not True or manifest.get('paid_api_microusd')!=0):
        raise DeploymentRefused('fake cohort check requires explicit synthetic zero-spend configuration')
    policy=load_contracts(expected_sha256=manifest['contracts_sha256'])
    scope=manifest['arms']
    if (not isinstance(scope,list) or not scope or len(set(scope))!=len(scope)
            or any(arm not in policy['pilot'] for arm in scope)):
        raise DeploymentRefused('only the declared fake-check scope is integrated')
    limit=manifest.get('test_wall_seconds',600)
    if type(limit) is not int or not 1<=limit<=600:
        raise DeploymentRefused('fake cohort check must be bounded at ten minutes')
    # A fixed, inventory-verified synthetic fixture; never a caller-selected module.
    fixture=Path(source)/'tests/p8/integrated_solo.py'
    spec=importlib.util.spec_from_file_location('cohort_synthetic_fixture',fixture)
    module=importlib.util.module_from_spec(spec)
    prior_path=list(sys.path)
    try:
        sys.path.insert(0,str(Path(source)/'tests/p8'))
        spec.loader.exec_module(module)
    finally:sys.path[:]=prior_path
    swarm_fixture=Path(source)/'tests/p10/stateless_cohort.py'
    spec=importlib.util.spec_from_file_location('cohort_swarm_fixture',swarm_fixture)
    swarm_module=importlib.util.module_from_spec(spec);spec.loader.exec_module(swarm_module)
    # The fake provider supplies model responses; the production runtime alone
    # selects and executes swarm actions. Solos retain their existing goal cycle.
    class SoloProvider(module.SyntheticStream):
        @__import__('v2.fixture',fromlist=['routed_fake']).routed_fake
        def stream(self,request,worker,rid):
            if worker.role!='internal-verifier':
                yield from super().stream(request,worker,rid);return
            names=[t['function']['name'] for t in request.get('tools',[])]
            prior=sum(c['role']=='internal-verifier' for c in self.calls)
            self.calls.append(dict(role=worker.role,session=worker.session,rid=rid,tools=names))
            if prior==0:
                suffix,args='read_evidence',dict(path='FINAL-INVENTORY.json')
            elif prior==1:
                suffix,args='read_evidence',dict(path='FINAL.tex')
            else:
                inv=worker.gateway.watchdog.state['verification_identity']['final_inventory']
                report={k:inv[k] for k in ('candidate_sha256','source_sha256','rendered_sha256')}
                report.update(outcome='supported-completion',reason_category='supported',
                    citation_ledger=[],claim_ledger=[],uncatalogued_claims=[],inventory_discrepancies=[])
                suffix,args='submit_review',dict(text=json.dumps(report))
            name=next(n for n in names if n.endswith(suffix))
            frame=dict(id='provider-'+rid,object='chat.completion.chunk',model=worker.model,
                choices=[dict(index=0,delta=dict(role='assistant',tool_calls=[dict(index=0,id='tool-'+rid,
                    type='function',function=dict(name=name,arguments=json.dumps(args)))]),finish_reason=None)])
            yield b'data: '+json.dumps(frame).encode()+b'\n\n'
            frame['choices']=[dict(index=0,delta={},finish_reason='tool_calls')]
            frame['usage']=dict(cost=.001,prompt_tokens=10,completion_tokens=10)
            yield b'data: '+json.dumps(frame).encode()+b'\n\n';yield b'data: [DONE]\n\n'
    streams={arm:swarm_module.Provider() if arm.endswith('-swarm') else
             SoloProvider('supported',policy['models'][policy['pilot'][arm]['models'][0]]['model_id'])
             for arm in scope}
    prices={row['model_id']:(.3e-6,1.2e-6) for row in policy['models'].values()}
    from .pilot import route_layouts
    layouts=route_layouts(policy)
    release=pin_release(Path(source),Path(scratch))
    local=Path(scratch)/'cohort';local.mkdir()
    controller=CohortRuntime(local,source=source,release=release,durable_root=manifest['durable_root'],
        contracts_sha256=manifest['contracts_sha256'],release_sha256=manifest['release_sha256'],
        cohort=manifest['cohort_id'],problem=manifest['problem_id'],
        problem_text='Synthetic operational fixture. No scientific correctness claim.',
        fake_providers=streams,prices=prices,route_layouts=layouts,
        withdrawal_authority=manifest['withdrawal_authority'],withdrawal_deadline=manifest['withdrawal_deadline'],
        fixture_hashes=manifest['fixture_hashes'])
    deadline=time.monotonic()+limit
    requested=set()
    result=None
    try:
        while not controller.complete:
            if time.monotonic()>=deadline:raise DeploymentRefused('bounded fake cohort check timed out')
            controller.step()
            for arm,runtime in controller.runtimes.items():
                if arm not in controller.started:continue
                if arm.endswith('-swarm'):
                    continue
                w=runtime.watchdog
                with w.mutex:
                    if not w.state.get('report_lock') and w.state.get('reply_receipts') and not w.awaiting_reply():
                        w.enter_report_mode('synthetic-launcher-report-check')
                    if w.state.get('closure_bundles') and not w.awaiting_reply() and arm not in requested:
                        w.goal({'status':'complete'});requested.add(arm)
            time.sleep(.05)
        controller.store.assert_conservation()
        result=dict(status='fake-cohort-check-passed',release_sha256=manifest['release_sha256'],
            scope=sorted(scope),registered=controller.state['registered_arms'],frozen=sorted(controller.frozen),
            unstarted=sorted(set(controller.state['registered_arms'])-set(scope)),
            judging_eligible=set(controller.frozen)==set(controller.state['registered_arms']),paid_api_microusd=0,
            receipts={arm:len(r.watchdog.state.get('reply_receipts',{}))+sum(x['receipts'] for x in getattr(r,'finished',{}).values()) for arm,r in controller.runtimes.items()},
            internal_status={arm:r.watchdog.state['closure_manifest']['internal_status'] for arm,r in controller.runtimes.items()})
        result['swarm_dispatch']={arm:dict(board_sha256=r.board_frozen['sha256'],
            completed_slots=len(r.finished),review_pairs=sum(bool(g.get('released')) for g in r.executor.snapshot()['checks'].values()),
            stages=sorted(set(c['stage'] for c in streams[arm].calls)),
            solo_verifier_cycles=r.watchdog.run.cycles,
            decisions=len(r.executor.snapshot()['decisions']),
            assignments=len(r.executor.snapshot()['assignments']))
            for arm,r in controller.runtimes.items() if arm.endswith('-swarm')}
        if any(result['receipts'][arm]!=len(streams[arm].calls) for arm in scope):
            raise DeploymentRefused('fake cohort reply consumption count mismatch')
        if any(value!='internal-supported' for value in result['internal_status'].values()):
            raise DeploymentRefused('fake supported fixture did not close internally supported')
        atomic_json(Path(scratch)/'cohort-result.json',result)
    finally:
        controller.shutdown()
    print(json.dumps(result,sort_keys=True),flush=True)
    return result
