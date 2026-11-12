"""P9 whole-swarm handoff adapter; no scheduling choices or model calls.

Private service capabilities are retained in the durable handoff. Provider keys
are excluded. Every owned slot is physically fenced before this state is sealed.
"""
from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import threading

from .accounting import AdmissionContext
from .allocation import Allocation
from .allocation_services import BoardBridge
from .board import Board
from .board_rpc import BoardServer,_wire,_unwire
from .cohort_allocation import CohortAccountingBridge
from .fencing import WriterScope

# Object/transport ownership is rebuilt; it must never be serialized as truth.
EPHEMERAL={'accounting','allocation','board','bridge','funding','watchdog','mutex',
    'server','server_thread','publisher','gateways','slots','writer_scope','clock','fake',
    'backup','key','transport_key','provider_key','live_transport','reconciliation_gateway','policy','config','executor'}


def _encode(value):
    if isinstance(value,Path):return {'$path':str(value)}
    if isinstance(value,bytes):return _wire(value)
    if isinstance(value,dict):return {key:_encode(row) for key,row in value.items()}
    if isinstance(value,(list,tuple)):return [_encode(row) for row in value]
    if isinstance(value,set):return {'$set':sorted(_encode(row) for row in value)}
    if value is None or type(value) in (str,int,float,bool):return value
    raise ValueError('unhandled swarm service state type: '+type(value).__name__)


def _decode(value):
    if isinstance(value,dict):
        if set(value)=={'$path'}:return Path(value['$path'])
        if set(value)=={'$set'}:return set(_decode(row) for row in value['$set'])
        if set(value)=={'$bytes'}:return _unwire(value)
        return {key:_decode(row) for key,row in value.items()}
    if isinstance(value,list):return [_decode(row) for row in value]
    return value


def capture(runtime):
    if any(w.worker is not None for w in runtime.owned_watchdogs()):
        raise ValueError('all swarm slots must be fenced before capture')
    if runtime.server_thread.is_alive() or any(g.active_handlers for g in runtime.gateways):
        raise ValueError('swarm services still active at handoff')
    properties={key:_encode(value) for key,value in vars(runtime).items() if key not in EPHEMERAL}
    if hasattr(runtime,'executor'):
        return dict(kind='stateless-v1',properties=properties,executor=runtime.executor.snapshot(private=True),
            author=role_state(runtime.watchdog),slots={key:role_state(w) for key,w in runtime.slots.items()},
            backup=role_state(runtime.backup) if getattr(runtime,"backup",None) is not None else None)
    slots={key:dict(name=w.path.name,intent=w.intent,stage=w.stage,token=w.token,lease=w.lease,
        packet=_encode(w.packet),context=asdict(w.context)) for key,w in runtime.slots.items()}
    return dict(properties=properties,slots=slots)


def restore(controller,arm,saved):
    if saved.get('kind')=='stateless-v1':return restore_stateless(controller,arm,saved)
    from .swarm_runtime import SwarmRuntime,SlotWatchdog,AuthorWatchdog
    runtime=SwarmRuntime.__new__(SwarmRuntime)
    for key,value in saved['properties'].items():setattr(runtime,key,_decode(value))
    runtime.policy=controller.policy;runtime.config=controller.policy[controller.profile][arm]
    if runtime.strategy!=arm or runtime.start_time!=controller.start or runtime.deadline!=controller.start+runtime.config['wall_seconds']:
        raise ValueError('swarm original allocation changed')
    expected_root=controller.root/'arms'
    if runtime.root!=expected_root or runtime.path!=expected_root/arm:
        raise ValueError('swarm retained service path changed')
    __import__('v2.provider_routes',fromlist=['x']).restore(_decode(saved['properties'].get('provider_routes_saved')) if saved['properties'].get('provider_routes_saved') is not None else None)
    runtime.release=controller.release;runtime.clock=controller.clock
    runtime.accounting=controller.store;runtime.publisher=controller.publisher
    runtime.fake=controller.streams[arm];runtime.key=controller.transport_key
    runtime.credential_id=controller.credential_id
    runtime.prices_verified=controller.live;runtime.metadata_lookup_free=controller.live
    runtime.mutex=threading.RLock();runtime.gateways=[];runtime.slots={}
    runtime.writer_scope=WriterScope(arm,epoch=controller._handoff_writer_fences[arm]['publication_epoch'])
    runtime.board=Board(runtime.path/'board.sqlite',arm_id=arm,problem_id=runtime.problem,
        cohort_id=runtime.cohort,controller_secret=runtime.secret,mixed=arm=='mixed-swarm',clock=runtime.clock,
        expected_contracts_sha256=controller.contracts_sha256)
    runtime.allocation=Allocation(runtime.path/'allocation.sqlite',arm_id=arm,problem_id=runtime.problem,
        strategy=arm,profile=runtime.profile,seed='v3-preregistered-rotation:'+runtime.cohort+':'+arm,
        started_at_ms=int(runtime.start_time*1000),workers=runtime.workers,
        clock=lambda:int(runtime.clock()*1000),expected_contracts_sha256=controller.contracts_sha256)
    runtime.funding=CohortAccountingBridge(runtime.allocation,controller.store);runtime.funding.register_pools()
    runtime.bridge=BoardBridge(runtime.allocation,runtime.board,controller_secret=runtime.secret,funding=runtime.funding)
    runtime.closed=False
    runtime.watchdog=AuthorWatchdog(runtime.root,arm,lambda:runtime.start_time,runtime._author_factory,
        lane='dsf',wall=runtime.config['wall_seconds'],cap=runtime.config['cap_microusd'],manifest=runtime.manifest)
    w=runtime.watchdog;w.owner=runtime;w.clock=runtime.clock;w.mutex=runtime.mutex;w.author_id=runtime.assembler
    w.attach_runtime_publisher(controller.publisher)
    w.attach_closure(controller.publisher,catalog=dict(root=str(runtime.path/'closure-state'),arm_id=arm,
        problem_id=runtime.problem,strategy=arm,profile=runtime.profile,assembler_id=runtime.assembler,
        artifact_root=str(controller.durable/arm/'closure')),scratch=str(runtime.root))
    for key,row in saved['slots'].items():
        slot_root=runtime.path/'workers'
        w=SlotWatchdog(slot_root,row['name'],lambda:runtime.start_time,lambda *args:None,
            lane=row['intent']['model'],wall=row['intent']['deadline_ms']/1000-runtime.start_time,
            cap=row['intent']['cap_microusd'],manifest=slot_root/'runtime-root.json')
        w.owner=runtime;w.clock=runtime.clock;w.mutex=runtime.mutex
        for field in ('intent','stage','token','lease'):setattr(w,field,row[field])
        w.slot_id=key;w.packet=_decode(row['packet']);w.context=AdmissionContext(**row['context'])
        if w.state.get('pending') or w.awaiting_reply() or w.state.get('receipt_failure'):
            raise ValueError('ambiguous/unconsumed swarm reply cannot resume')
        w.attach_runtime_publisher(controller.publisher)
        runtime.slots[key]=w
    runtime.reconciliation_gateway=runtime._gateway(runtime.watchdog,runtime._report_context())
    runtime.reconciliation_gateway.metadata_lookup_free=True
    # Completed turns stay completed. An interrupted unfinished slot gets fresh
    # client state under its existing intent/cap/deadline/packet/session lineage.
    runtime.pending=[]
    publication=runtime.checkpoint()
    for key,w in runtime.slots.items():
        if runtime.slot_admitted(w):runtime.pending.append(dict(worker_id=key,publication=publication))
    runtime.socket_dir=Path(tempfile.mkdtemp(prefix='v3-board-',dir='/tmp'))
    runtime.server=BoardServer(runtime.socket_dir/'rpc',runtime.board,after_mutation=runtime._fence_board_pressure)
    runtime.server_thread=threading.Thread(target=runtime.server.serve_forever,daemon=True)
    if arm in controller.frozen:
        runtime.server.server_close();runtime.socket_dir.rmdir();runtime.closed=True
    else:runtime.server_thread.start()
    return runtime


def role_state(w):
    fields=('slot_id','stage','stage_deadline','packet','brief','publication','author_id')
    return dict(name=w.path.name,lane=w.run.lane,properties={k:_encode(getattr(w,k)) for k in fields if hasattr(w,k)},
        context=asdict(w.context) if w.context is not None else None)


def restore_stateless(controller,arm,saved):
    from .swarm_runtime import SwarmRuntime,SlotWatchdog
    from .swarm_executor import EventExecutor
    runtime=SwarmRuntime.__new__(SwarmRuntime)
    for key,value in saved['properties'].items():setattr(runtime,key,_decode(value))
    runtime.policy=controller.policy;runtime.config=controller.policy[controller.profile][arm]
    if (runtime.strategy!=arm or runtime.start_time!=controller.start or
            runtime.deadline!=controller.start+runtime.config['wall_seconds'] or
            runtime.root!=controller.root/'arms' or runtime.path!=controller.root/'arms'/arm):
        raise ValueError('stateless original allocation/storage changed')
    __import__('v2.provider_routes',fromlist=['x']).restore(_decode(saved['properties'].get('provider_routes_saved')) if saved['properties'].get('provider_routes_saved') is not None else None)
    runtime.release=controller.release;runtime.clock=controller.clock
    runtime.accounting=controller.store;runtime.publisher=controller.publisher
    runtime.fake=controller.streams[arm];runtime.key=controller.transport_key
    runtime.prices_verified=controller.live;runtime.metadata_lookup_free=True
    runtime.mutex=threading.RLock();runtime.gateways=[];runtime.slots={};runtime.closed=False
    runtime.writer_scope=WriterScope(arm,epoch=controller._handoff_writer_fences[arm]['publication_epoch'])
    runtime.executor=EventExecutor(runtime.path/'executor.sqlite',arm=arm,profile=runtime.profile,
        start=runtime.start_time,problem=runtime.problem,contracts_sha256=controller.contracts_sha256,clock=controller.clock)
    if runtime.executor.snapshot(private=True)!=saved['executor']:
        raise ValueError('retained events/decisions/actions/closing state changed')
    runtime.board=Board(runtime.path/'board.sqlite',arm_id=arm,problem_id=runtime.problem,
        cohort_id=runtime.cohort,controller_secret=runtime.secret,mixed=arm=='mixed-swarm',clock=runtime.clock,
        expected_contracts_sha256=controller.contracts_sha256, planning_state_path=runtime.executor.path)
    runtime.board.source_cache_root=runtime.path/'source-cache'
    runtime.board.require_evidence=runtime.fake is None
    def watchdog(row,root,manifest):
        w=SlotWatchdog(root,row['name'],lambda:runtime.start_time,lambda *a:None,lane=row['lane'],
            wall=runtime.config['wall_seconds'],cap=runtime.config['cap_microusd'],manifest=manifest)
        w.owner=runtime;w.clock=runtime.clock;w.mutex=runtime.mutex
        for key,value in row['properties'].items():setattr(w,key,_decode(value))
        w.context=AdmissionContext(**row['context']) if row['context'] is not None else None
        if w.state.get('pending') or w.awaiting_reply() or w.state.get('receipt_failure'):
            raise ValueError('unacknowledged role response forbids restored admission')
        w.attach_runtime_publisher(controller.publisher)
        return w
    w=runtime.watchdog=watchdog(saved['author'],runtime.root,runtime.manifest)
    w.attach_closure(controller.publisher,catalog=dict(root=str(runtime.path/'closure-state'),arm_id=arm,
        problem_id=runtime.problem,strategy=arm,profile=runtime.profile,assembler_id=runtime.assembler,
        artifact_root=str(controller.durable/arm/'closure')),scratch=str(runtime.root))
    for key,row in saved['slots'].items():
        slot_root=runtime.path/'workers';slot=watchdog(row,slot_root,slot_root/'runtime-root.json')
        if slot.slot_id!=key:raise ValueError('retained role identity changed')
        if slot.stage=='orchestrator':
            decision=next((d for d in saved['executor']['decisions'] if d['id']==key),None)
            if decision is None:raise ValueError('orchestration slot has no durable decision')
            if slot.state.get('response_text') is not None:
                # A one-response context with saved text is consumed before any
                # new model admission; never clear/replay its paid response.
                slot.state['turn_complete']=True
            elif decision['status']!='prepared':
                slot.state['slot_failure']='fenced-orchestrator-restart; liability retained'
            slot.save()
        runtime.slots[key]=slot
    backup=saved.get('backup')
    if backup is not None:
        runtime.backup=watchdog(backup,runtime.path/'workers',runtime.path/'workers'/'runtime-root.json')
        b=runtime.backup
        if b.slot_id!=runtime.assembler+':backup' or b.stage!='backup-writer':
            raise ValueError('retained backup role identity changed')
        runtime.backup_resume_required=not any(b.state.get(k) for k in
            ('turn_complete','slot_closed','admission_stop','slot_failure'))
    closing=saved['executor'].get('closing')
    if closing and saved['executor']['mode']=='closing':
        stage=closing['stage']
        if (stage=='draft' or stage.startswith('revision')) and stage in runtime.stage_started:
            if not w.state.get('turn_complete') and not w.state.get('admission_stop') and not w.state.get('closing'):
                runtime.stage_started[stage]['resume_required']=True
    runtime.reconciliation_gateway=runtime._gateway(w,runtime._context(runtime.writer_model(),'report',runtime.assembler,'writer'))
    runtime.reconciliation_gateway.metadata_lookup_free=True
    # Operator pause restores exactly the sealed board, with no continuation
    # imports even if a seed directory changed while the job was paused.
    if arm not in controller.frozen and not getattr(controller,'state',{}).get('operator_clock') and hasattr(runtime,'import_seed_claims'):
        runtime.import_seed_claims()  # retries any import a transient failure left out (idempotent)
    publication=runtime.checkpoint()
    for slot in runtime.slots.values():slot.publication=publication
    runtime.socket_dir=Path(tempfile.mkdtemp(prefix='v3-board-',dir='/tmp'))
    runtime.server=BoardServer(runtime.socket_dir/'rpc',runtime.board,after_mutation=runtime._fence_board_pressure)
    runtime.server_thread=threading.Thread(target=runtime.server.serve_forever,daemon=True)
    if arm in controller.frozen:
        runtime.server.server_close();runtime.socket_dir.rmdir();runtime.closed=True
    else:runtime.server_thread.start()
    return runtime
